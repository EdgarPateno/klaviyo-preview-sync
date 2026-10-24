#!/usr/bin/env python3
"""Sync Klaviyo campaign Preview text FIELDS to the editable hidden preview block in hybrid templates.

Hybrid (USER_DRAGGABLE) Magnum templates ignore the campaign "Preview text" field; Gmail shows the
hidden <div class="mg-preheader"> block instead. This tool makes the field match the block so the
Klaviyo UI / reports show what inboxes actually show.

Usage:
    python3 sync_preview_text.py <campaign_id>                       # dry run (default): report only
    python3 sync_preview_text.py <campaign_id> --apply [--force]     # PATCH each DIFF message's preview_text
    python3 sync_preview_text.py --all-drafts [--apply] [--quiet]    # every Draft email campaign (launchd poller)

Safety (both modes):
  * only Draft campaigns are modified (status re-checked right before each PATCH);
  * block text that is empty, contains "[REPLACE]" or starts with "Preview text" is refused unless --force
    (--all-drafts never forces);
  * each message is re-read right before its PATCH and skipped if its definition changed since the first read;
  * after the PATCH the whole definition (minus content.preview_text) is compared with the pre-PATCH read;
  * template html is never written.

--all-drafts runs on a schedule (GitHub Actions, .github/workflows/sync.yml, every 5 minutes):
  * one filtered campaigns call (Draft + email, messages included) per page;
  * per message a sparse `updated`-only template read, and the full html only when `updated` changed;
    messages known to have the block are checked every run, others on a slow rotation, new ones seeded
    SYNC_SEED_PER_RUN (default 15) per run; state lives in .sync_state.json next to this script
    (persisted between Actions runs with actions/cache);
  * HTTP 429 -> back off (Retry-After, min 120 s) and exit 0; a lock file makes overlapping runs exit at once;
  * a template that Klaviyo itself cannot load (HTTP 4xx) is logged as WARN and not counted as a failure;
  * --quiet logs only changes and errors (timestamped, one line each) to sync_preview_text.log (capped ~1 MB).
Exit status is non-zero if any API call or apply failed (except a clean 429 back-off and 4xx template reads).

Stdlib only, Python 3.9+. Reads the private key from the KLAVIYO_API_KEY environment variable (GitHub secret),
falling back to ~/.claude.json (mcpServers.klaviyo.env.KLAVIYO_API_KEY) for local runs; never prints it.
"""
import copy
import datetime
import fcntl
import json
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser

REVISION = "2026-04-15"
BASE = "https://a.klaviyo.com"
HERE = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(HERE, ".sync_state.json")
LOG_PATH = os.path.join(HERE, "sync_preview_text.log")
LOCK_PATH = os.path.join(HERE, ".sync_preview_text.lock")
LOG_CAP = 1_000_000
ROTATE_PER_RUN = 5     # non-block messages re-checked per run
SEED_PER_RUN = int(os.environ.get("SYNC_SEED_PER_RUN", "15"))  # uncached messages fetched per run
# invisible characters used in the preheader padding / that may trail the text
INVISIBLE = "‌ ​͏ ﻿"
PLACEHOLDER_RE = re.compile(r"\[REPLACE\]|^Preview text\b", re.I)
QUIET = False


def _key():
    env_key = os.environ.get("KLAVIYO_API_KEY", "").strip()
    if env_key:
        return env_key
    try:
        with open(os.path.expanduser("~/.claude.json")) as fh:
            cfg = json.load(fh)
        return cfg["mcpServers"]["klaviyo"]["env"]["KLAVIYO_API_KEY"]
    except (OSError, ValueError, KeyError):
        sys.exit("ERROR: no Klaviyo key. Set the KLAVIYO_API_KEY environment variable (GitHub: repo secret).")


KEY = _key()
CALLS = 0


class ApiError(Exception):
    pass


class RateLimited(Exception):
    def __init__(self, retry_after):
        super().__init__(f"rate limited, retry after {retry_after}s")
        self.retry_after = retry_after


def now():
    return datetime.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S%z")


def log(msg, always=False):
    """Quiet mode: write to the capped log file. Normal mode: print."""
    line = f"{now()} {msg}"
    if not QUIET:
        print(msg)
        return
    try:
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > LOG_CAP:
            with open(LOG_PATH, "rb") as fh:
                fh.seek(-LOG_CAP // 2, os.SEEK_END)
                tail = fh.read()
            with open(LOG_PATH, "wb") as fh:
                fh.write(b"[log truncated]\n" + tail[tail.find(b"\n") + 1:])
        with open(LOG_PATH, "a") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def api(method, path, body=None):
    """Return (status, json). Raises ApiError on network errors/timeouts and RateLimited on 429."""
    global CALLS
    CALLS += 1
    data = json.dumps(body).encode() if body is not None else None
    url = path if path.startswith("http") else BASE + path
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": "Klaviyo-API-Key " + KEY, "revision": REVISION,
        "accept": "application/vnd.api+json", "content-type": "application/vnd.api+json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            txt = resp.read().decode()
            return resp.status, (json.loads(txt) if txt else None)
    except urllib.error.HTTPError as e:
        if e.code == 429:
            try:
                ra = int(float(e.headers.get("Retry-After") or 0))
            except ValueError:
                ra = 0
            raise RateLimited(max(ra, 120))
        txt = e.read().decode()
        try:
            return e.code, json.loads(txt)
        except ValueError:
            return e.code, txt
    except (urllib.error.URLError, socket.timeout, TimeoutError) as e:
        raise ApiError(f"{method} {path.split('?')[0]}: network error/timeout: {e}")


def describe(status, payload):
    if status >= 500:
        return f"HTTP {status} server error"
    return f"HTTP {status} {str(payload)[:200]}"


class PreheaderText(HTMLParser):
    """Text of the first div.mg-preheader, stopping at span.mg-pad (or, if the block has no mg-pad span, at its first span)."""

    def __init__(self, stop_at_any_span):
        super().__init__(convert_charrefs=True)
        self.stop_at_any_span = stop_at_any_span
        self.found = self.collecting = self.done = False
        self.depth = 0
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if self.done:
            return
        classes = (dict(attrs).get("class") or "").split()
        if self.collecting:
            if tag == "span" and ("mg-pad" in classes or self.stop_at_any_span):
                self.collecting, self.done = False, True
            elif tag == "br":
                self.parts.append(" ")
            elif tag == "div":
                self.depth += 1
            return
        if tag == "div" and "mg-preheader" in classes:
            self.found = self.collecting = True
            self.depth = 1

    def handle_startendtag(self, tag, attrs):
        if self.collecting and tag == "br":
            self.parts.append(" ")
        else:
            self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if self.collecting and tag == "div":
            self.depth -= 1
            if self.depth == 0:
                self.collecting, self.done = False, True

    def handle_data(self, data):
        if self.collecting:
            self.parts.append(data)


def block_text(html):
    has_pad = re.search(r'<span[^>]*class="[^"]*\bmg-pad\b', html) is not None
    p = PreheaderText(stop_at_any_span=not has_pad)
    p.feed(html)
    if not p.found:
        return None
    text = "".join(p.parts)
    for ch in INVISIBLE:
        text = text.replace(ch, " ")
    return " ".join(text.split())


def is_placeholder(text):
    return not text or PLACEHOLDER_RE.search(text) is not None


def get_message(mid):
    st, d = api("GET", f"/api/campaign-messages/{mid}/")
    if st != 200:
        raise ApiError(f"read message {mid}: {describe(st, d)}")
    return d["data"]["attributes"]["definition"]


def campaign_status(cid):
    st, d = api("GET", f"/api/campaigns/{cid}/?fields[campaign]=status")
    if st != 200:
        raise ApiError(f"read campaign {cid}: {describe(st, d)}")
    return d["data"]["attributes"]["status"]


def patch_preview(mid, first_definition, new_text, cid=None):
    """Re-check Draft + re-read the message, abort if changed, PATCH the full definition with only
    preview_text changed, then re-read and compare everything else."""
    if cid is not None:
        stt = campaign_status(cid)
        if stt != "Draft":
            return False, f"campaign {cid} is {stt!r}, not Draft - skipped"
    cur = get_message(mid)
    if cur != first_definition:
        return False, "definition changed since the first read (someone editing?) - skipped"
    new_def = copy.deepcopy(cur)
    new_def["content"]["preview_text"] = new_text
    if new_def.get("label") is None:
        new_def.pop("label", None)          # never send label: null
    body = {"data": {"type": "campaign-message", "id": mid, "attributes": {"definition": new_def}}}
    st, resp = api("PATCH", f"/api/campaign-messages/{mid}/", body)
    if st >= 300:
        return False, f"PATCH {describe(st, resp)}"
    after = None
    for _ in range(6):                       # the GET can lag the PATCH by a few seconds
        time.sleep(3)
        try:
            after = get_message(mid)
        except ApiError:
            continue
        if after["content"].get("preview_text") == new_text:
            break
    if after is None:
        return False, "re-read failed"
    if after["content"].get("preview_text") != new_text:
        return False, f"re-read after ~18s still shows preview_text={after['content'].get('preview_text')!r}"
    a, b = copy.deepcopy(cur), copy.deepcopy(after)
    a["content"].pop("preview_text", None)
    b["content"].pop("preview_text", None)
    if a != b:
        diff = sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))
        return False, f"preview_text OK but other definition fields changed: {diff}"
    return True, "re-read confirms preview_text; rest of the definition unchanged"


# ---------------------------------------------------------------- single campaign (manual) mode
def run_campaign(cid, apply, force):
    failures = 0
    st, camp = api("GET", f"/api/campaigns/{cid}/")
    if st != 200:
        sys.exit(f"ERROR reading campaign: {describe(st, camp)}")
    ca = camp["data"]["attributes"]
    status = ca.get("status")
    print(f"Campaign {cid} '{ca.get('name')}' status={status} mode={'APPLY' if apply else 'DRY RUN'}{' +force' if force else ''}")
    if apply and status != "Draft":
        sys.exit(f"REFUSED: campaign status is {status!r}; only Draft campaigns are modified.")
    st, d = api("GET", f"/api/campaigns/{cid}/campaign-messages/")
    if st != 200:
        sys.exit(f"ERROR reading campaign messages: {describe(st, d)}")
    todo = []
    for m in d["data"]:
        mid = m["id"]
        definition = m["attributes"]["definition"]
        label = definition.get("label") or ""
        field = (definition.get("content") or {}).get("preview_text") or ""
        st, t = api("GET", f"/api/campaign-messages/{mid}/template/")
        if st != 200:
            print(f"- {label} / {mid}: ERROR reading template: {describe(st, t)}")
            failures += 1
            continue
        if not t.get("data"):
            print(f"- {label} / {mid}: NO TEMPLATE assigned")
            continue
        tid = t["data"]["id"]
        block = block_text(t["data"]["attributes"].get("html") or "")
        if block is None:
            print(f"- {label} / {mid} [tpl {tid}]: NO BLOCK | field={field!r} -> unchanged")
            continue
        state = "MATCH" if block == field else "DIFF"
        refuse = " (block looks empty/placeholder: will NOT apply without --force)" if state == "DIFF" and is_placeholder(block) else ""
        print(f"- {label} / {mid} [tpl {tid}]: block={block!r} | field={field!r} | {state}{refuse}")
        if state == "DIFF":
            todo.append((mid, label, block, definition, bool(refuse)))
    if apply:
        for mid, label, block, definition, placeholder in todo:
            if placeholder and not force:
                print(f"  SKIP  {label} / {mid}: placeholder/empty block text refused (use --force to override)")
                continue
            try:
                ok, msg = patch_preview(mid, definition, block, cid)
            except ApiError as e:
                ok, msg = False, str(e)
            print(f"  APPLY {label} / {mid}: {'OK' if ok else 'FAILED'} - {msg}")
            failures += 0 if ok else 1
    elif todo:
        print("Dry run: nothing changed. Re-run with --apply to set each DIFF field to its block text.")
    return 1 if failures else 0


# ---------------------------------------------------------------- all-drafts (poller) mode
def load_state():
    try:
        with open(STATE_PATH) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {"messages": {}, "rotation": 0, "backoff_until": 0}


def save_state(state):
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(state, fh, indent=1, sort_keys=True)
    os.replace(tmp, STATE_PATH)


def list_draft_messages():
    """[(campaign_id, campaign_name, message_id, definition, template_id)] for Draft email campaigns."""
    flt = urllib.parse.quote("and(equals(messages.channel,'email'),equals(status,'Draft'))")
    path = (f"/api/campaigns/?filter={flt}&include=campaign-messages"
            f"&fields[campaign]=name,status&fields[campaign-message]=definition")
    out = []
    while path:
        st, d = api("GET", path)
        if st != 200:
            raise ApiError(f"list Draft campaigns: {describe(st, d)}")
        msgs = {x["id"]: x for x in d.get("included", []) if x["type"] == "campaign-message"}
        for c in d["data"]:
            if c["attributes"].get("status") != "Draft":
                continue
            for ref in c["relationships"]["campaign-messages"]["data"]:
                m = msgs.get(ref["id"])
                if not m:
                    continue
                tpl = ((m.get("relationships") or {}).get("template") or {}).get("data") or {}
                out.append((c["id"], c["attributes"].get("name"), m["id"], m["attributes"]["definition"], tpl.get("id")))
        path = (d.get("links") or {}).get("next")
    return out


def run_all_drafts(apply):
    state = load_state()
    if time.time() < state.get("backoff_until", 0):
        return 0
    cache = state.setdefault("messages", {})
    failures = 0
    try:
        msgs = list_draft_messages()
        live = {m[2] for m in msgs}
        for mid in list(cache):              # forget messages that are no longer in a Draft campaign
            if mid not in live:
                del cache[mid]
        block_ids = [m for m in msgs if cache.get(m[2], {}).get("block") is not None]
        new_ids = [m for m in msgs if m[2] not in cache][:SEED_PER_RUN]
        noblock = sorted((m for m in msgs if m[2] in cache and cache[m[2]].get("block") is None), key=lambda m: m[2])
        rot = state.get("rotation", 0) % max(len(noblock), 1)
        rotating = (noblock[rot:] + noblock[:rot])[:ROTATE_PER_RUN]
        state["rotation"] = rot + ROTATE_PER_RUN
        for cid, cname, mid, definition, tid in block_ids + new_ids + rotating:
            entry = cache.get(mid)
            st, t = api("GET", f"/api/campaign-messages/{mid}/template/?fields[template]=updated")
            if st != 200 or not t.get("data"):
                if 400 <= st < 500:
                    # Klaviyo itself can't load this message's template (e.g. 422 "Failed to load
                    # definition"); permanent for that message, so park it on the slow rotation.
                    if (entry or {}).get("error") != st:
                        log(f"WARN {cname!r} msg {mid}: template read {describe(st, t)}", True)
                    cache[mid] = {"template": None, "updated": None, "block": None, "error": st}
                elif st != 200:
                    log(f"ERROR {cname!r} msg {mid}: template read {describe(st, t)}", True)
                    failures += 1
                else:
                    cache[mid] = {"template": None, "updated": None, "block": None}
                continue
            tid, upd = t["data"]["id"], t["data"]["attributes"].get("updated")
            if not entry or entry.get("template") != tid or entry.get("updated") != upd:
                st, full = api("GET", f"/api/templates/{tid}/")
                if 400 <= st < 500:
                    if (entry or {}).get("error") != st:
                        log(f"WARN {cname!r} msg {mid}: template {tid} html read {describe(st, full)}", True)
                    cache[mid] = {"template": tid, "updated": upd, "block": None, "error": st}
                    continue
                if st != 200:
                    log(f"ERROR {cname!r} msg {mid}: template {tid} html read {describe(st, full)}", True)
                    failures += 1
                    continue
                block = block_text(full["data"]["attributes"].get("html") or "")
                cache[mid] = {"template": tid, "updated": upd, "block": block}
            block = cache[mid]["block"]
            if block is None or is_placeholder(block):
                continue                      # no block, or placeholder text: never synced automatically
            field = (definition.get("content") or {}).get("preview_text") or ""
            if block == field:
                continue
            label = definition.get("label") or ""
            if not apply:
                log(f"DIFF (dry run) {cname!r} / {label} msg {mid}: field={field!r} block={block!r}", True)
                continue
            try:
                ok, msg = patch_preview(mid, definition, block, cid)
            except ApiError as e:
                ok, msg = False, str(e)
            if ok:
                log(f"SYNCED {cname!r} / {label} msg {mid} tpl {tid}: {field!r} -> {block!r}", True)
            else:
                log(f"FAILED {cname!r} / {label} msg {mid} tpl {tid}: {msg}", True)
                failures += 1
    except RateLimited as e:
        state["backoff_until"] = time.time() + e.retry_after
        log(f"RATE LIMITED (429): backing off {e.retry_after}s", True)
        save_state(state)
        return 0
    except ApiError as e:
        log(f"ERROR {e}", True)
        failures += 1
    save_state(state)
    if not QUIET:
        print(f"api calls this run: {CALLS}")
    return 1 if failures else 0


def main():
    global QUIET
    flags = {a for a in sys.argv[1:] if a.startswith("--")}
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if flags - {"--apply", "--force", "--all-drafts", "--quiet"}:
        sys.exit(__doc__)
    QUIET = "--quiet" in flags
    if "--all-drafts" in flags:
        if args or "--force" in flags:
            sys.exit("--all-drafts takes no campaign id and never uses --force")
        lock = open(LOCK_PATH, "w")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return 0                          # another run is in progress
        return run_all_drafts("--apply" in flags)
    if len(args) != 1:
        sys.exit(__doc__)
    return run_campaign(args[0], "--apply" in flags, "--force" in flags)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RateLimited as e:
        log(f"RATE LIMITED (429): {e}", True)
        sys.exit(0)
    except ApiError as e:
        log(f"ERROR: {e}", True)
        sys.exit(1)

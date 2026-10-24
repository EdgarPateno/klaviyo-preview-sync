# klaviyo-preview-sync

Keeps the Klaviyo campaign **Preview text** field in step with the hidden yellow preview block in
hybrid (drag-and-drop + custom HTML) email templates.

Klaviyo ignores the Preview text field for these templates; inboxes show the hidden
`<div class="mg-preheader">` block at the top of the email. Editors change that block in the email
editor, and this job copies its text into the field so the campaign screen shows what inboxes show.

## How it runs

- `.github/workflows/sync.yml` is started every 2 minutes by an external cron-job.org job that calls the
  `workflow_dispatch` API with a fine-grained token limited to Actions on this repo. GitHub's own 5-minute
  schedule is kept as a backup (it is best-effort and can be late), and **Actions > Sync preview text >
  Run workflow** starts it by hand.
- Only **Draft** email campaigns are touched, only the Preview text field is written, and template html is
  never changed.
- Empty block text and the base-template placeholder `[REPLACE]` are never copied.
- Each message is re-read right before the update and skipped if someone changed it in the meantime.

## Setup

Add a repo secret `KLAVIYO_API_KEY` (Settings > Secrets and variables > Actions) holding a Klaviyo private
key limited to **Campaigns: read/write** and **Templates: read**.

## Manual use

```
KLAVIYO_API_KEY=... python3 sync_preview_text.py <campaign_id>            # dry run
KLAVIYO_API_KEY=... python3 sync_preview_text.py <campaign_id> --apply
```

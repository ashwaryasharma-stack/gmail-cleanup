# Gmail Cleanup

Automated Gmail cleanup using Claude AI. Scans your inbox for junk emails, sends you a weekly digest, and archives them with a 'gmail-cleanup' label on your approval.

## How It Works

1. **Scan** — Fetches emails from the last 7 days and uses Claude to identify junk (newsletters, promotions, notifications, etc.)
2. **Digest** — Emails you an HTML summary of what it found
3. **Approve** — You review the list in your terminal and confirm what to archive

Approved emails are **labeled 'gmail-cleanup' and archived** (removed from Inbox, never deleted). They remain in All Mail and are fully recoverable — just remove the label to restore them.

### Safety Features

The tool has built-in protections to prevent accidental deletion of important emails:

- **Labeled emails are never deleted** — Any email with a custom label is excluded from deletion, ensuring your organized messages are preserved
- **Replied-to emails are never deleted** — If you've replied to an email, it's marked as important and excluded from deletion

## Setup

### 1. Prerequisites

- Python 3.10+
- An [Anthropic API key](https://console.anthropic.com/)
- A Google Cloud project with Gmail API enabled

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Google Cloud setup

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a new project (or use an existing one)
3. Enable the **Gmail API**: APIs & Services → Library → search "Gmail API" → Enable
4. Create OAuth 2.0 credentials: APIs & Services → Credentials → Create Credentials → OAuth 2.0 Client ID
   - Application type: **Desktop app**
   - Name: anything (e.g. "Gmail Cleanup")
   - Click Create, then **Download JSON**
5. Save the downloaded file as `credentials/credentials.json`

### 4. Configure environment

```bash
cp .env.example .env
```

Edit `.env` with your values:

```
ANTHROPIC_API_KEY=your_anthropic_api_key_here
DIGEST_RECIPIENT=your@email.com
```

## Usage

```bash
# Full weekly run: scan + classify + send digest email
python main.py run

# Or step by step:
python main.py scan     # Fetch and classify emails, save to pending_deletions.json
python main.py digest   # Send the digest email
python main.py approve  # Interactive review and deletion
```

### First run

The first time you run, a browser window opens for Google OAuth. Sign in with the Gmail account you want to clean up and grant permission. The token is saved to `credentials/token.json` for future runs.

### Approve flow

Emails are grouped by category. You decide per-group:

```
$ python main.py approve

42 junk emails pending, grouped by category.

── NEWSLETTERS (12) ──
   Amazon Newsletter — Your weekly picks
   Medium — Top stories this week
   ... and 10 more
Archive all 12? [y/n/show] y
   ✓ queued for deletion

── PROMOTIONS & SALE ALERTS (18) ──
   deals@amazon.com — 48-hour flash sale
   ... and 17 more
Archive all 18? [y/n/show] show
      1. deals@amazon.com  —  48-hour flash sale
      2. gap.com           —  Extra 40% off this weekend
      ...
   Select to archive (e.g. 1,3,5-7 | all | none): 1,2

── SOCIAL NOTIFICATIONS (7) ──
   ...
Archive all 7? [y/n/show] n
   ✗ skipped

Archiving 14 emails...
Done. 14 emails archived with label 'gmail-cleanup'.
```

- `y` — archive the whole category
- `n` — skip, keep all in this category
- `show` — expand the list, then pick by number/range (`1,3,5-7`), `all`, or `none`

## Configuration

| Variable | Default | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | required | Your Anthropic API key |
| `DIGEST_RECIPIENT` | Gmail account | Where to send the digest email |
| `DAYS_TO_SCAN` | `7` | How many days back to scan |
| `CREDENTIALS_FILE` | `credentials/credentials.json` | Google OAuth credentials path |
| `TOKEN_FILE` | `credentials/token.json` | Saved OAuth token path |

## Automation (weekly cron)

```bash
crontab -e
```

Add this line to run every Monday at 9 AM:

```
0 9 * * 1  cd /path/to/gmail-cleanup && python main.py run
```

## File Structure

```
gmail-cleanup/
├── main.py                  # CLI entry point (scan / digest / approve / run)
├── gmail_client.py          # Gmail API wrapper (fetch, send, archive)
├── classifier.py            # Claude AI email classification with token logging
├── digest.py                # HTML digest email formatter with stats tracking
├── config.py                # Environment config
├── requirements.txt
├── .env.example
├── .gitignore
├── README.md
├── ARCHITECTURE.md
├── credentials/
│   └── credentials.json     # (you provide this — gitignored)
└── data/
    ├── stats.json           # Cumulative stats (runtime, gitignored)
    └── whitelist.json       # Whitelisted sender emails (version-controlled)
```

## Observability

**Cost & Token Tracking:**
- **Per-batch logging** — Each batch of 50 emails logs: input tokens, output tokens, cost (`$X.XXXX`)
- **Summary per run** — Total tokens used and total cost printed to stderr after classification completes
- **Cumulative stats** — `data/stats.json` tracks last 10 scans with junk count breakdown by category

**Progress & Logging:**
- **Progress indicator** — Shows `"Classifying emails X-Y of Z..."` during classification to stderr
- **All logging goes to stderr** — Console output remains clean for scripting; stats/costs logged separately
- **Digest email includes** — Shows summary count: total emails shown vs. hidden per category

**Cost Estimate (200 emails/week):**
- 4 batches of 50 emails ≈ ~4,400 tokens ≈ **$0.0013/run** (with caching: ~$0.001/run)
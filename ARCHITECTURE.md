# Architecture

## Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                          main.py (CLI)                          │
│              scan | digest | approve | run                      │
└────────┬────────────────┬──────────────────────┬───────────────┘
         │                │                      │
         ▼                ▼                      ▼
  gmail_client.py    classifier.py          digest.py
  (Gmail API)        (Claude API)           (HTML formatter)
         │                │
         ▼                ▼
   Google Gmail      Anthropic API
   (OAuth 2.0)       (claude-sonnet-4-6)
```

## Data Flow

```
Gmail API                    classifier.py             pending_deletions.json
─────────                    ─────────────             ──────────────────────
fetch_recent_emails()  ───►  classify_emails()  ───►  { scan_date, junk_emails[] }
                                                                │
                                                       ┌────────┴────────┐
                                                       ▼                 ▼
                                               digest.py          main.py approve
                                               format HTML         interactive CLI
                                                    │                    │
                                                    ▼                    ▼
                                             send_digest_email()   trash_emails()
                                             (via Gmail API)       (via Gmail API)
```

## Modules

### `config.py`
Loads environment variables from `.env`. Single source of truth for all constants:
- `ANTHROPIC_API_KEY`
- `DIGEST_RECIPIENT` — defaults to the authenticated Gmail account
- `GMAIL_SCOPES` — readonly + modify + send
- `DAYS_TO_SCAN` — default 7
- `CLASSIFIER_BATCH_SIZE` — default 50 emails per Claude API call
- `PENDING_FILE` — path to `pending_deletions.json`
- `STATS_FILE` — path to `data/stats.json` for cumulative tracking

### `gmail_client.py` — `GmailClient` class
Wraps the Google Gmail API v1. Handles OAuth 2.0 token flow on first run.

| Method | Description |
|---|---|
| `_authenticate()` | OAuth flow; loads/refreshes/saves token |
| `user_email` (property) | Fetches authenticated user's email address |
| `fetch_recent_emails(days)` | Lists + gets metadata for emails in the past N days |
| `trash_emails(ids)` | Moves emails to Trash (recoverable for 30 days) |
| `send_digest_email(to, subject, html)` | Sends the digest via Gmail API |

**Email metadata fetched per message:** From, Subject, Date headers + snippet (first ~100 chars of body). Full body is never fetched — keeps it fast and cheap.

**Pagination:** `fetch_recent_emails` handles `nextPageToken` to retrieve all matching messages beyond the 500-result page limit.

### `classifier.py`
Uses the Anthropic SDK to classify emails as junk or keep.

**Key design decisions:**
- **Batching:** processes `CLASSIFIER_BATCH_SIZE` (50) emails per API call to balance cost vs. latency
- **Progress tracking:** shows progress indicator "Classifying emails X-Y of Z" to stderr for each batch
- **Token logging:** logs input/output tokens and cost after each batch; shows total usage and cost at end
- **Pricing:** uses Claude Sonnet 4.6 rates ($3/$15 per 1M tokens input/output)
- **Prompt caching:** the system prompt is marked `cache_control: ephemeral` so repeated runs within 5 minutes reuse the cached prompt, reducing cost
- **Model:** `claude-sonnet-4-6` — good balance of accuracy and cost for this classification task
- **Protected emails:** automatically marks emails with custom labels or replied-to status as keep (never deleted)
- **Fallback:** if Claude returns malformed JSON or an API error occurs for a batch, all emails in that batch default to `is_junk: false` (safe — never accidentally deletes)
- **Input:** sender, subject, date, snippet (~100 chars), label_ids, has_been_replied_to — enough signal without sending full bodies

**Classification prompt strategy:**
- System prompt defines junk vs. keep clearly, biased toward aggressive junk detection
- User prompt asks for a JSON array: `[{id, is_junk, category, reason}]`
  - `category`: one of `newsletters`, `promotions`, `social`, `automated`, `spam`, `other` — used for grouping in `approve`
  - `reason`: ≤8 word explanation shown in the digest table
- Claude returns structured JSON; code strips markdown code fences if present

### `digest.py`
Formats junk emails into an HTML email digest.

`format_digest_html(junk_emails, scan_date)` → HTML string

Produces a self-contained HTML email with:
- Summary header (count, date)
- **Category grouping:** emails grouped by classification (Promotions, Newsletters, Social, Automated, Spam, Other)
- **Top 20 per category:** shows first 20 emails per category with "+ X more" indicator for remaining
- Table: From | Subject | Date | Reason
- Instructions to run `python main.py approve`
- Summary section: total shown vs hidden emails
- **Cumulative stats:** saves scan data to `data/stats.json` for tracking trends (keeps last 10 scans)

### `main.py` — CLI
Uses `argparse` with subcommands. Each subcommand is a thin `cmd_*` function.

| Command | What it does |
|---|---|
| `scan` | `GmailClient.fetch_recent_emails()` → `classify_emails()` → saves `pending_deletions.json` |
| `digest` | Loads `pending_deletions.json` → `format_digest_html()` → `send_digest_email()` |
| `approve` | Loads `pending_deletions.json` → category-based interactive prompt → `trash_emails()` → updates file |
| `run` | `scan` then `digest` in sequence |

**`approve` flow — category-based:**

Claude assigns a `category` to each junk email (e.g. `newsletters`, `promotions`, `social`, `automated`). The approve command groups by category and prompts once per group:

```
NEWSLETTERS (12 emails)
  amazon@newsletter.com — Your weekly picks
  medium.com — Top stories this week
  ... 10 more
  Trash all 12? [y/n/show]: y  ✓

PROMOTIONAL / SALE ALERTS (18 emails)
  Trash all 18? [y/n/show]: n  ✗ skipped

SOCIAL NOTIFICATIONS (7 emails)
  Trash all 7? [y/n/show]: show
    1. Twitter — @someone mentioned you
    2. LinkedIn — 3 new connections
    ...
  Select to trash (e.g. 1,3,5-7 or all/none): 1,2
```

- `y` — trash the whole category
- `n` — skip this category, keep all
- `show` — expand the list, then pick by number/range or `all`/`none`

## State File: `pending_deletions.json`

```json
{
  "scan_date": "2026-05-24T09:00:00+00:00",
  "junk_emails": [
    {
      "id": "18f3a2b...",
      "subject": "Your weekend deals",
      "sender": "deals@amazon.com",
      "date": "Sat, 23 May 2026 08:12:00 +0000",
      "snippet": "Shop now and save up to 40% on...",
      "is_junk": true,
      "reason": "promotional email, sale alert"
    }
  ]
}
```

- Written by `scan`, read by `digest` and `approve`
- `approve` rewrites the file after deletion, removing trashed IDs
- Gitignored (contains email metadata)

## Data Directory Files

### `data/stats.json` (runtime-generated, gitignored)
Cumulative statistics tracking across scans.

```json
{
  "scans": [
    {
      "scan_date": "2026-05-24T09:00:00+00:00",
      "total_junk": 42,
      "by_category": {
        "promotions": 18,
        "newsletters": 12,
        "social": 7,
        "spam": 5
      }
    }
  ],
  "total_junk": 150,
  "total_by_category": {
    "promotions": 60,
    "newsletters": 45,
    "social": 30,
    "spam": 15
  }
}
```

- Written by `digest.py` after each scan
- Keeps last 10 scans to prevent large file size
- Useful for trend analysis and reporting

### `data/whitelist.json` (version-controlled)
List of whitelisted sender email addresses that should never be classified as junk.

```json
[
  "boss@company.com",
  "important-alerts@service.com"
]
```

- Kept in git for team coordination
- Can be extended to filter emails during classification

## Gmail API Scopes

| Scope | Used for |
|---|---|
| `gmail.readonly` | `fetch_recent_emails` — listing and reading message metadata |
| `gmail.modify` | `trash_emails` — moving messages to Trash |
| `gmail.send` | `send_digest_email` — sending the digest |

`gmail.modify` does **not** permanently delete — it moves to Trash, which Gmail auto-purges after 30 days. Permanent deletion would require `https://mail.google.com/` (full access scope), which we deliberately avoid.

## Cost Estimates

Rough numbers for a typical inbox (200 emails/week):
- 4 batches of 50 emails × ~800 tokens input + ~300 tokens output ≈ **4,400 tokens/run**
- Claude Sonnet 4.6 pricing: ~$0.0013 per run
- With prompt caching on repeated same-day runs: first batch ~50% cache hit → ~$0.001/run
- **Token/cost logging:** each batch logs input tokens, output tokens, and cost to stderr for visibility

## Web Dashboard
- Built with Flask (Python) — keeps everything in one language
- Hosted on Railway (free tier) — zero cost
- Dashboard served at a unique URL with approval token for security
- Token expires after 48 hours
- Mobile first design optimized for iPhone
- Large tap targets (min 44px) for buttons

### Dashboard features
- Shows emails grouped by category (Promotions, Newsletters, Spam, Social)
- Top 20 emails per category with "+ X more" count
- "Never delete" button next to each sender — updates whitelist.json
- Live count updates as you whitelist senders
- "Approve X deletions" button at top and bottom
- Triggers Gmail API deletion on approval

## Infrastructure
- GitHub — code storage (free)
- GitHub Actions — weekly scheduler, runs every Monday 8am (free)
- Railway — hosts the Flask web dashboard (free tier)
- Anthropic API — email classification (pennies per week)
- Gmail API — fetch and delete emails (free)

## Deployment
- Push code to GitHub → Railway auto deploys
- API keys stored in Railway environment variables (never in code)
- Gmail OAuth credentials stored as environment variables in Railway
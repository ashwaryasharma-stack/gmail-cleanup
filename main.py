from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone

from config import DAYS_TO_SCAN, DIGEST_RECIPIENT, PENDING_FILE
from classifier import classify_emails
from digest import format_digest_html
from gmail_client import GmailClient

_CATEGORY_LABELS = {
    "newsletters": "NEWSLETTERS",
    "promotions": "PROMOTIONS & SALE ALERTS",
    "social": "SOCIAL NOTIFICATIONS",
    "automated": "AUTOMATED / DIGESTS",
    "spam": "SPAM",
    "other": "OTHER",
}


# ── Commands ──────────────────────────────────────────────────────────────────

def cmd_scan(args) -> tuple[list[dict], str]:
    print(f"Fetching emails from the last {DAYS_TO_SCAN} days...")
    client = GmailClient()
    emails = client.fetch_recent_emails(days=DAYS_TO_SCAN)
    print(f"Found {len(emails)} emails. Classifying with Claude...", file=sys.stderr)

    classified = classify_emails(emails)
    junk = [e for e in classified if e.get("is_junk")]

    scan_date = datetime.now(timezone.utc).isoformat()
    PENDING_FILE.write_text(json.dumps({"scan_date": scan_date, "junk_emails": junk}, indent=2))

    keep_count = len(emails) - len(junk)
    print(f"\nResults: {len(junk)} junk / {keep_count} to keep")
    print(f"Saved to {PENDING_FILE}", file=sys.stderr)
    return junk, scan_date


def cmd_digest(args) -> None:
    if not PENDING_FILE.exists():
        print(f"No pending scan found. Run: python main.py scan")
        sys.exit(1)

    data = json.loads(PENDING_FILE.read_text())
    junk = data["junk_emails"]
    scan_date = data["scan_date"]

    client = GmailClient()
    recipient = DIGEST_RECIPIENT or client.user_email
    html = format_digest_html(junk, scan_date)
    subject = f"Gmail Cleanup: {len(junk)} junk emails found ({scan_date[:10]})"

    print(f"Sending digest to {recipient}...")
    client.send_digest_email(recipient, subject, html)
    print("Digest sent!")


def cmd_approve(args) -> None:
    if not PENDING_FILE.exists():
        print(f"No pending scan found. Run: python main.py scan")
        sys.exit(1)

    data = json.loads(PENDING_FILE.read_text())
    junk = data["junk_emails"]

    if not junk:
        print("Nothing pending — inbox is already clean.")
        return

    # Group by category
    groups: dict[str, list[dict]] = defaultdict(list)
    for email in junk:
        groups[email.get("category", "other")].append(email)

    print(f"\n{len(junk)} junk emails pending, grouped by category.\n")

    to_trash: list[dict] = []

    for category, emails in groups.items():
        label = _CATEGORY_LABELS.get(category, category.upper())
        print(f"── {label} ({len(emails)}) ──")

        # Show up to 3 as preview
        for email in emails[:3]:
            sender = email["sender"][:50]
            subject = email["subject"][:60]
            print(f"   {sender}  —  {subject}")
        if len(emails) > 3:
            print(f"   ... and {len(emails) - 3} more")

        while True:
            choice = input(f"Trash all {len(emails)}? [y/n/show] ").strip().lower()
            if choice == "y":
                to_trash.extend(emails)
                print("   ✓ queued for deletion\n")
                break
            elif choice == "n" or choice == "":
                print("   ✗ skipped\n")
                break
            elif choice == "show":
                selected = _show_and_select(emails)
                to_trash.extend(selected)
                print(f"   ✓ {len(selected)} queued for deletion\n")
                break
            else:
                print("   Please enter y, n, or show.")

    if not to_trash:
        print("Nothing selected — no emails deleted.")
        return

    # Final safeguard: filter out protected emails
    client = GmailClient()
    protected_ids = {e["id"] for e in to_trash if client.is_email_protected(e)}
    if protected_ids:
        print(f"Warning: Skipping {len(protected_ids)} protected email(s) (labeled or replied-to)")
        to_trash = [e for e in to_trash if e["id"] not in protected_ids]

    if not to_trash:
        print("No unprotected emails to trash.")
        return

    print(f"Trashing {len(to_trash)} emails...")
    count = client.trash_emails([e["id"] for e in to_trash])
    print(f"Done. {count} emails moved to Trash.")

    trashed_ids = {e["id"] for e in to_trash}
    data["junk_emails"] = [e for e in junk if e["id"] not in trashed_ids]
    PENDING_FILE.write_text(json.dumps(data, indent=2))


def cmd_run(args) -> None:
    cmd_scan(args)
    cmd_digest(args)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _show_and_select(emails: list[dict]) -> list[dict]:
    print()
    for i, e in enumerate(emails, 1):
        print(f"   {i:3}. {e['sender'][:45]}  —  {e['subject'][:55]}")
    print()
    raw = input("   Select to trash (e.g. 1,3,5-7 | all | none): ").strip()
    return _parse_selection(raw, emails)


def _parse_selection(raw: str, emails: list[dict]) -> list[dict]:
    raw = raw.strip().lower()
    if raw == "all":
        return emails
    if raw == "none" or raw == "":
        return []

    indices: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if "-" in part:
            try:
                a, b = part.split("-", 1)
                indices.update(range(int(a), int(b) + 1))
            except ValueError:
                pass
        else:
            try:
                indices.add(int(part))
            except ValueError:
                pass

    return [e for i, e in enumerate(emails, 1) if i in indices]


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Gmail cleanup automation powered by Claude AI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  python main.py run       # scan + send digest (weekly workflow)\n"
            "  python main.py scan      # classify emails, save results\n"
            "  python main.py digest    # email yourself the digest\n"
            "  python main.py approve   # review and trash by category\n"
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("scan",    help="Fetch and classify emails; save to pending_deletions.json")
    sub.add_parser("digest",  help="Send weekly digest email of pending junk")
    sub.add_parser("approve", help="Review junk by category and move approved emails to Trash")
    sub.add_parser("run",     help="scan + digest in one step (weekly automation entry point)")

    args = parser.parse_args()
    {"scan": cmd_scan, "digest": cmd_digest, "approve": cmd_approve, "run": cmd_run}[args.command](args)


if __name__ == "__main__":
    main()

from __future__ import annotations

import json
import os
import secrets
from collections import defaultdict
from pathlib import Path

from flask import Flask, abort, jsonify, render_template, request

from config import DIGEST_RECIPIENT, PENDING_FILE, STATS_FILE
from gmail_client import GmailClient

app = Flask(__name__)

_WHITELIST_FILE = Path("data/whitelist.json")
_TOKEN_FILE = Path("data/token.txt")

_CATEGORY_LABELS = {
    "newsletters": "Newsletters",
    "promotions": "Promotions & Sale Alerts",
    "social": "Social Notifications",
    "automated": "Automated / Digests",
    "spam": "Spam",
    "other": "Other",
}


def _load_token() -> str:
    if tok := os.environ.get("DASHBOARD_TOKEN"):
        return tok
    if _TOKEN_FILE.exists():
        return _TOKEN_FILE.read_text().strip()
    tok = secrets.token_urlsafe(32)
    _TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    _TOKEN_FILE.write_text(tok)
    return tok


DASHBOARD_TOKEN = _load_token()


def _verify_token() -> None:
    token = request.args.get("token", "")
    if request.is_json and not token:
        token = (request.get_json() or {}).get("token", "")
    if not secrets.compare_digest(token, DASHBOARD_TOKEN):
        abort(403)


def _load_whitelist() -> set[str]:
    whitelist = set(json.loads(_WHITELIST_FILE.read_text())) if _WHITELIST_FILE.exists() else set()
    if DIGEST_RECIPIENT:
        whitelist.add(_extract_email_addr(DIGEST_RECIPIENT))
    return whitelist


def _extract_email_addr(sender: str) -> str:
    if "<" in sender and ">" in sender:
        return sender.split("<")[1].split(">")[0].strip().lower()
    return sender.strip().lower()


@app.template_filter("fmt_int")
def fmt_int(value: int) -> str:
    return f"{value:,}"


@app.get("/")
def dashboard():
    _verify_token()

    if PENDING_FILE.exists():
        pending = json.loads(PENDING_FILE.read_text())
    else:
        pending = {"scan_date": None, "total_scanned": 0, "token_usage": None, "junk_emails": []}

    # Always start with all checkboxes checked; whitelist handles permanent exclusions
    for email in pending.get("junk_emails", []):
        email["approved"] = True

    whitelist = _load_whitelist()
    stats = json.loads(Path(STATS_FILE).read_text()) if Path(STATS_FILE).exists() else None

    junk = pending.get("junk_emails", [])
    groups: dict[str, list[dict]] = defaultdict(list)
    whitelisted_count = 0
    approved_count = 0

    for email in junk:
        sender_addr = _extract_email_addr(email["sender"])
        if sender_addr in whitelist:
            whitelisted_count += 1
        else:
            groups[email.get("category", "other")].append(email)
            if email.get("approved", False):
                approved_count += 1

    groups_total = {cat: len(emails) for cat, emails in groups.items()}
    groups_preview = {cat: emails[:20] for cat, emails in groups.items()}
    total_junk = sum(len(e) for e in groups.values())

    return render_template(
        "dashboard.html",
        scan_date=pending.get("scan_date"),
        total_scanned=pending.get("total_scanned", 0),
        total_junk=total_junk,
        whitelisted_count=whitelisted_count,
        approved_count=approved_count,
        groups=groups_preview,
        groups_total=groups_total,
        token_usage=pending.get("token_usage"),
        stats=stats,
        token=DASHBOARD_TOKEN,
        category_labels=_CATEGORY_LABELS,
        scope="Primary inbox only (Promotions & Spam excluded)",
        whitelist=sorted(whitelist),
    )


@app.post("/approve")
def approve():
    _verify_token()

    if not PENDING_FILE.exists():
        return jsonify({"error": "No pending scan found"}), 404

    pending = json.loads(PENDING_FILE.read_text())
    whitelist = _load_whitelist()

    # Only delete emails that are explicitly approved AND not whitelisted
    to_delete = [
        e for e in pending["junk_emails"]
        if e.get("approved", False) and _extract_email_addr(e["sender"]) not in whitelist
    ]

    if not to_delete:
        return jsonify({"archived": 0, "message": "No approved emails to archive"})

    client = GmailClient()
    count = client.archive_emails([e["id"] for e in to_delete])

    archived_ids = {e["id"] for e in to_delete}
    pending["junk_emails"] = [e for e in pending["junk_emails"] if e["id"] not in archived_ids]
    PENDING_FILE.write_text(json.dumps(pending, indent=2))

    return jsonify({"archived": count})


@app.post("/toggle-approval")
def toggle_approval():
    """Toggle approval status for a single email (safety: explicit per-email approval required)."""
    _verify_token()

    if not PENDING_FILE.exists():
        return jsonify({"error": "No pending scan found"}), 404

    data = request.get_json() or {}
    email_id = data.get("id", "").strip()
    if not email_id:
        return jsonify({"error": "id is required"}), 400

    pending = json.loads(PENDING_FILE.read_text())

    # Find and toggle the email's approval status
    for email in pending["junk_emails"]:
        if email["id"] == email_id:
            email["approved"] = not email.get("approved", False)
            PENDING_FILE.write_text(json.dumps(pending, indent=2))
            return jsonify({"id": email_id, "approved": email["approved"]})

    return jsonify({"error": "Email not found"}), 404


@app.post("/whitelist")
def add_to_whitelist():
    _verify_token()

    data = request.get_json() or {}
    sender = data.get("sender", "").strip()
    if not sender:
        return jsonify({"error": "sender is required"}), 400

    email_addr = _extract_email_addr(sender)
    whitelist = _load_whitelist()
    whitelist.add(email_addr)

    _WHITELIST_FILE.parent.mkdir(parents=True, exist_ok=True)
    _WHITELIST_FILE.write_text(json.dumps(sorted(whitelist), indent=2))

    return jsonify({"whitelisted": email_addr})


if __name__ == "__main__":
    print(f"\n  Dashboard URL: http://localhost:5000/?token={DASHBOARD_TOKEN}\n")
    app.run(debug=False, port=5000)

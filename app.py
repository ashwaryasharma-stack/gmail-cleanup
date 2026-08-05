from __future__ import annotations

import json
import os
import secrets
import threading
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

from flask import Flask, abort, jsonify, render_template, request

from config import DIGEST_RECIPIENT, PENDING_FILE, STATS_FILE
from gmail_client import GmailClient
from main import cmd_scan

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
print(f"\n  Dashboard token: {DASHBOARD_TOKEN}\n", flush=True)

_scan_lock = threading.Lock()
_scan_state = {"running": False, "error": None, "started_at": None, "finished_at": None}


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


def _nth_sunday_utc(year: int, month: int, n: int) -> datetime:
    first = datetime(year, month, 1, tzinfo=timezone.utc)
    first_sunday = first + timedelta(days=(6 - first.weekday()) % 7)
    return first_sunday + timedelta(weeks=n - 1)


def _is_us_eastern_dst(dt_utc: datetime) -> bool:
    """US DST runs from 2nd Sunday of March 07:00 UTC to 1st Sunday of November 06:00 UTC."""
    dst_start = _nth_sunday_utc(dt_utc.year, 3, 2).replace(hour=7)
    dst_end = _nth_sunday_utc(dt_utc.year, 11, 1).replace(hour=6)
    return dst_start <= dt_utc < dst_end


def _to_eastern(dt: datetime) -> datetime:
    dt_utc = dt.astimezone(timezone.utc)
    offset = timedelta(hours=-4) if _is_us_eastern_dst(dt_utc) else timedelta(hours=-5)
    return dt_utc.astimezone(timezone(offset))


@app.template_filter("fmt_email_date")
def fmt_email_date(value: str) -> str:
    if not value:
        return ""
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return ""
    dt = _to_eastern(dt)
    hour_12 = dt.strftime("%I:%M %p").lstrip("0")
    return f"{dt.strftime('%b')} {dt.day}, {hour_12}"


@app.get("/health")
def health():
    return "ok", 200


@app.errorhandler(Exception)
def handle_exception(e):
    import traceback
    return f"<pre>{traceback.format_exc()}</pre>", 500


@app.get("/")
def dashboard():
    print("dashboard: verifying token", flush=True)
    _verify_token()
    print("dashboard: token ok", flush=True)

    if PENDING_FILE.exists():
        pending = json.loads(PENDING_FILE.read_text())
    else:
        pending = {"scan_date": None, "total_scanned": 0, "token_usage": None, "junk_emails": []}
    print("dashboard: pending loaded", flush=True)

    # Always start with all checkboxes checked; whitelist handles permanent exclusions
    for email in pending.get("junk_emails", []):
        email["approved"] = True

    whitelist = _load_whitelist()
    print("dashboard: whitelist loaded", flush=True)
    stats = json.loads(Path(STATS_FILE).read_text()) if Path(STATS_FILE).exists() else None
    print("dashboard: stats loaded", flush=True)

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

    print("dashboard: rendering template", flush=True)
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

    data = request.get_json() or {}
    checked_ids = set(data.get("ids", []))

    pending = json.loads(PENDING_FILE.read_text())
    whitelist = _load_whitelist()

    # Only delete emails whose checkbox was actually checked in the browser AND not whitelisted
    to_delete = [
        e for e in pending["junk_emails"]
        if e["id"] in checked_ids and _extract_email_addr(e["sender"]) not in whitelist
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


@app.post("/scan")
def scan():
    _verify_token()

    with _scan_lock:
        if _scan_state["running"]:
            return jsonify({"error": "A scan is already in progress"}), 409
        _scan_state["running"] = True
        _scan_state["error"] = None
        _scan_state["started_at"] = datetime.now(timezone.utc).isoformat()
        _scan_state["finished_at"] = None

    def _run_scan():
        try:
            cmd_scan(None)
        except Exception as e:
            _scan_state["error"] = str(e)
        finally:
            _scan_state["running"] = False
            _scan_state["finished_at"] = datetime.now(timezone.utc).isoformat()

    threading.Thread(target=_run_scan, daemon=True).start()
    return jsonify({"status": "started"})


@app.get("/scan-status")
def scan_status():
    _verify_token()
    return jsonify(_scan_state)


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
    port = int(os.environ.get("PORT", 5000))
    print(f"\n  Dashboard URL: http://localhost:{port}/?token={DASHBOARD_TOKEN}\n")
    app.run(debug=False, host="0.0.0.0", port=port)

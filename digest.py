from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from pathlib import Path

from config import DASHBOARD_URL, STATS_FILE

_CATEGORY_LABELS = {
    "newsletters": "Newsletters",
    "promotions": "Promotions & Sale Alerts",
    "social": "Social Notifications",
    "automated": "Automated / Digests",
    "spam": "Spam",
    "other": "Other",
}

_GROUP_COLORS = ["#f9f9f9", "#ffffff"]


def format_digest_html(
    junk_emails: list[dict],
    scan_date: str,
    token_usage: dict | None = None,
    dashboard_token: str = "",
) -> str:
    """Formats junk emails into an HTML digest.

    Layout order:
      1. Dashboard approval link (big, clickable)
      2. Cost summary (tokens used, estimated cost, cumulative totals)
      3. Emails grouped by category
      4. 'Never delete' buttons next to each sender

    Groups by category, shows top 20 per category, saves stats to data/stats.json.
    """
    # Ensure data directory exists
    Path(STATS_FILE).parent.mkdir(parents=True, exist_ok=True)

    if token_usage is None:
        token_usage = {}

    # ── 1. Dashboard approval link ────────────────────────────────────────────
    base_url = DASHBOARD_URL.rstrip("/")
    dashboard_href = f"{base_url}/?token={dashboard_token}" if dashboard_token else base_url
    dashboard_section = f"""  <div style="margin-bottom:28px;text-align:center">
    <a href="{dashboard_href}"
       style="display:inline-block;padding:16px 36px;background:#cc3333;color:#fff;
              font-size:18px;font-weight:bold;text-decoration:none;border-radius:6px;
              letter-spacing:0.3px">
      Review and Approve Deletions &rarr;
    </a>
  </div>
"""

    # ── 2. Cost summary ───────────────────────────────────────────────────────
    input_tokens  = token_usage.get("input_tokens", 0)
    output_tokens = token_usage.get("output_tokens", 0)
    total_tokens  = input_tokens + output_tokens
    # Claude Sonnet 4.6 pricing: $3/$15 per 1M tokens input/output
    estimated_cost = (input_tokens * 3 + output_tokens * 15) / 1_000_000

    cumulative_junk, cumulative_cost = _cumulative_stats()

    cost_section = f"""  <div style="margin-bottom:24px;padding:14px 18px;background:#f0f4ff;
       border-left:4px solid #4a90d9;border-radius:3px;font-size:13px;color:#333">
    <strong style="font-size:14px">Classification Cost</strong><br>
    <table style="margin-top:8px;border-collapse:collapse;width:auto">
      <tr>
        <td style="padding:3px 20px 3px 0;color:#555">Input tokens</td>
        <td style="padding:3px 0;font-weight:bold">{input_tokens:,}</td>
      </tr>
      <tr>
        <td style="padding:3px 20px 3px 0;color:#555">Output tokens</td>
        <td style="padding:3px 0;font-weight:bold">{output_tokens:,}</td>
      </tr>
      <tr>
        <td style="padding:3px 20px 3px 0;color:#555">Total tokens</td>
        <td style="padding:3px 0;font-weight:bold">{total_tokens:,}</td>
      </tr>
      <tr>
        <td style="padding:3px 20px 3px 0;color:#555">Estimated cost</td>
        <td style="padding:3px 0;font-weight:bold">${estimated_cost:.4f}</td>
      </tr>
    </table>
    <div style="margin-top:10px;padding-top:8px;border-top:1px solid #c8d8f0;color:#666;font-size:12px">
      Cumulative (all scans): <strong>{cumulative_junk:,}</strong> junk emails found &nbsp;|&nbsp;
      ~<strong>${cumulative_cost:.3f}</strong> total spend
    </div>
  </div>
"""

    # ── Header ────────────────────────────────────────────────────────────────
    header = f"""<!DOCTYPE html>
<html>
<body style="font-family:Arial,sans-serif;max-width:960px;margin:40px auto;color:#333;font-size:14px">
  <h2 style="color:#cc3333;margin-bottom:4px">Gmail Cleanup — Weekly Digest</h2>
  <p style="color:#666;margin-top:0">Scan date: <strong>{scan_date[:10]}</strong>
     &nbsp;|&nbsp; Junk emails found: <strong>{len(junk_emails)}</strong></p>

{dashboard_section}{cost_section}"""

    if not junk_emails:
        html = header + """  <p style="color:#2e7d32;font-size:16px">Your inbox is clean — no junk found!</p>
</body></html>"""
        _save_stats(scan_date, junk_emails, {}, token_usage)
        return html

    # ── 3. Emails grouped by category, with 4. Never-delete buttons ──────────
    groups: dict[str, list[dict]] = defaultdict(list)
    for email in junk_emails:
        groups[email.get("category", "other")].append(email)

    table_rows = ""
    total_shown = 0
    total_hidden = 0

    for color_idx, (category, emails) in enumerate(groups.items()):
        label = _CATEGORY_LABELS.get(category, category.title())
        bg = _GROUP_COLORS[color_idx % 2]
        shown_count = min(20, len(emails))
        hidden_count = len(emails) - shown_count
        total_shown += shown_count
        total_hidden += hidden_count

        group_header = (
            f'<tr><td colspan="5" style="padding:10px 8px 4px;font-weight:bold;'
            f'background:{bg};border-top:2px solid #ddd;color:#555;font-size:12px;'
            f'text-transform:uppercase;letter-spacing:0.5px">'
            f'{label} ({len(emails)})</td></tr>'
        )
        table_rows += group_header

        for email in emails[:20]:
            sender  = email["sender"][:55]
            subject = email["subject"][:80]
            date    = email["date"][:16]
            reason  = email.get("reason", "")

            # ── 4. Never-delete button ────────────────────────────────────────
            # Extract the raw sender address for whitelist instructions
            raw_sender = email["sender"]
            addr_match = re.search(r'<([^>]+)>', raw_sender)
            sender_addr = addr_match.group(1) if addr_match else raw_sender.strip()

            never_delete_btn = (
                f'<a href="mailto:?subject=whitelist%3A%20{sender_addr}&body=Add%20{sender_addr}%20to%20whitelist.json"'
                f' title="Never delete from {sender_addr}"'
                f' style="display:inline-block;padding:2px 8px;background:#e8f5e9;color:#2e7d32;'
                f'font-size:11px;border:1px solid #a5d6a7;border-radius:3px;text-decoration:none;'
                f'white-space:nowrap">Never delete</a>'
            )

            table_rows += (
                f'<tr style="background:{bg}">'
                f'<td style="padding:6px 8px;border-bottom:1px solid #eee;max-width:180px;overflow:hidden">{sender}</td>'
                f'<td style="padding:6px 8px;border-bottom:1px solid #eee">{subject}</td>'
                f'<td style="padding:6px 8px;border-bottom:1px solid #eee;white-space:nowrap;color:#555">{date}</td>'
                f'<td style="padding:6px 8px;border-bottom:1px solid #eee;color:#888;font-size:12px">{reason}</td>'
                f'<td style="padding:6px 8px;border-bottom:1px solid #eee;white-space:nowrap">{never_delete_btn}</td>'
                f'</tr>'
            )

        if hidden_count > 0:
            table_rows += (
                f'<tr style="background:{bg}">'
                f'<td colspan="5" style="padding:6px 8px;border-bottom:1px solid #eee;color:#999;font-size:12px;font-style:italic">'
                f'+ {hidden_count} more in this category</td></tr>'
            )

    table = f"""  <table style="width:100%;border-collapse:collapse;font-size:13px;margin-top:8px">
    <thead>
      <tr style="background:#eee;text-align:left">
        <th style="padding:10px 8px">From</th>
        <th style="padding:10px 8px">Subject</th>
        <th style="padding:10px 8px">Date</th>
        <th style="padding:10px 8px">Reason</th>
        <th style="padding:10px 8px"></th>
      </tr>
    </thead>
    <tbody>{table_rows}</tbody>
  </table>"""

    footer = f"""
  <div style="margin-top:20px;padding:12px;background:#f5f5f5;border-radius:3px;font-size:12px;color:#666">
    <strong>Summary:</strong> {total_shown} emails shown, {total_hidden} more available
  </div>

  <p style="color:#bbb;font-size:11px;margin-top:32px">Generated by Gmail Cleanup &nbsp;|&nbsp; Emails are archived under the 'gmail-cleanup' label, not permanently deleted.</p>
</body></html>"""

    html = header + table + footer

    # Save cumulative stats
    _save_stats(scan_date, junk_emails, groups, token_usage)

    return html


def _cumulative_stats() -> tuple[int, float]:
    """Returns (total_junk_across_all_scans, estimated_total_cost) from stats.json."""
    stats_file = Path(STATS_FILE)
    if not stats_file.exists():
        return 0, 0.0
    try:
        stats = json.loads(stats_file.read_text())
        total_junk = stats.get("total_junk", 0)
        total_cost = stats.get("total_estimated_cost", 0.0)
        return total_junk, total_cost
    except (json.JSONDecodeError, KeyError):
        return 0, 0.0


def _save_stats(
    scan_date: str,
    junk_emails: list[dict],
    by_category: dict[str, list[dict]],
    token_usage: dict | None = None,
) -> None:
    """Saves cumulative stats to data/stats.json."""
    if token_usage is None:
        token_usage = {}

    stats_file = Path(STATS_FILE)
    stats_file.parent.mkdir(parents=True, exist_ok=True)

    # Load existing stats or start fresh
    if stats_file.exists():
        try:
            stats = json.loads(stats_file.read_text())
        except json.JSONDecodeError:
            stats = {"scans": [], "total_junk": 0, "total_by_category": {}, "total_estimated_cost": 0.0}
    else:
        stats = {"scans": [], "total_junk": 0, "total_by_category": {}, "total_estimated_cost": 0.0}

    # Compute this run's cost
    input_tokens  = token_usage.get("input_tokens", 0)
    output_tokens = token_usage.get("output_tokens", 0)
    run_cost = (input_tokens * 3 + output_tokens * 15) / 1_000_000

    # Add this scan's data
    scan_record = {
        "scan_date": scan_date,
        "total_junk": len(junk_emails),
        "by_category": {cat: len(emails) for cat, emails in by_category.items()},
        "token_usage": token_usage,
        "estimated_cost": round(run_cost, 6),
    }
    stats["scans"].append(scan_record)
    stats["total_junk"] += len(junk_emails)
    stats["total_estimated_cost"] = round(
        stats.get("total_estimated_cost", 0.0) + run_cost, 6
    )

    for category, emails in by_category.items():
        stats["total_by_category"][category] = (
            stats["total_by_category"].get(category, 0) + len(emails)
        )

    # Keep only last 10 scans to prevent huge file
    if len(stats["scans"]) > 10:
        stats["scans"] = stats["scans"][-10:]

    stats_file.write_text(json.dumps(stats, indent=2))
    print(f"Saved cumulative stats to {stats_file}", file=sys.stderr)

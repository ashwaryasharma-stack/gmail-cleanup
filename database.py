from __future__ import annotations

import json
import os
from contextlib import contextmanager

import psycopg2
from psycopg2.extras import RealDictCursor


@contextmanager
def _db():
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS pending_scan (
                    id SERIAL PRIMARY KEY,
                    scan_date TEXT NOT NULL,
                    total_scanned INTEGER NOT NULL DEFAULT 0,
                    token_usage JSONB NOT NULL DEFAULT '{}'
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS pending_emails (
                    id TEXT PRIMARY KEY,
                    subject TEXT NOT NULL DEFAULT '',
                    sender TEXT NOT NULL DEFAULT '',
                    date TEXT NOT NULL DEFAULT '',
                    snippet TEXT NOT NULL DEFAULT '',
                    label_ids JSONB NOT NULL DEFAULT '[]',
                    has_been_replied_to BOOLEAN NOT NULL DEFAULT FALSE,
                    is_junk BOOLEAN NOT NULL DEFAULT TRUE,
                    category TEXT NOT NULL DEFAULT 'other',
                    reason TEXT NOT NULL DEFAULT '',
                    approved BOOLEAN NOT NULL DEFAULT FALSE,
                    archived BOOLEAN NOT NULL DEFAULT FALSE,
                    archived_at TIMESTAMPTZ
                )
            """)
            # Migrate tables created before archived/archived_at existed
            cur.execute("ALTER TABLE pending_emails ADD COLUMN IF NOT EXISTS archived BOOLEAN NOT NULL DEFAULT FALSE")
            cur.execute("ALTER TABLE pending_emails ADD COLUMN IF NOT EXISTS archived_at TIMESTAMPTZ")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS whitelist (
                    email TEXT PRIMARY KEY,
                    added_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS stats (
                    id SERIAL PRIMARY KEY,
                    scan_date TEXT NOT NULL,
                    total_junk INTEGER NOT NULL DEFAULT 0,
                    by_category JSONB NOT NULL DEFAULT '{}',
                    token_usage JSONB NOT NULL DEFAULT '{}',
                    estimated_cost FLOAT NOT NULL DEFAULT 0.0,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)


# ── Pending scan ──────────────────────────────────────────────────────────────

def get_pending() -> dict | None:
    """Returns the current pending scan as a dict, or None if no scan exists."""
    with _db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT scan_date, total_scanned, token_usage FROM pending_scan LIMIT 1")
            scan = cur.fetchone()
            if not scan:
                return None
            cur.execute("""
                SELECT id, subject, sender, date, snippet, label_ids,
                       has_been_replied_to, is_junk, category, reason, approved
                FROM pending_emails
                WHERE archived = FALSE
            """)
            emails = [dict(row) for row in cur.fetchall()]
    return {
        "scan_date": scan["scan_date"],
        "total_scanned": scan["total_scanned"],
        "token_usage": scan["token_usage"],
        "junk_emails": emails,
    }


def save_pending(scan_date: str, total_scanned: int, token_usage: dict, junk_emails: list[dict]) -> None:
    """Replaces the current pending scan with new data.

    Only unarchived rows are cleared — archived emails are kept forever so
    all-time/weekly archive stats stay accurate across scans.
    """
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM pending_scan")
            cur.execute("DELETE FROM pending_emails WHERE archived = FALSE")
            cur.execute(
                "INSERT INTO pending_scan (scan_date, total_scanned, token_usage) VALUES (%s, %s, %s)",
                (scan_date, total_scanned, json.dumps(token_usage or {})),
            )
            for email in junk_emails:
                cur.execute(
                    """
                    INSERT INTO pending_emails
                        (id, subject, sender, date, snippet, label_ids,
                         has_been_replied_to, is_junk, category, reason, approved)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (id) DO UPDATE SET
                        subject = EXCLUDED.subject,
                        sender = EXCLUDED.sender,
                        date = EXCLUDED.date,
                        snippet = EXCLUDED.snippet,
                        label_ids = EXCLUDED.label_ids,
                        has_been_replied_to = EXCLUDED.has_been_replied_to,
                        is_junk = EXCLUDED.is_junk,
                        category = EXCLUDED.category,
                        reason = EXCLUDED.reason,
                        approved = EXCLUDED.approved
                    """,
                    (
                        email["id"],
                        email.get("subject", ""),
                        email.get("sender", ""),
                        email.get("date", ""),
                        email.get("snippet", ""),
                        json.dumps(email.get("label_ids", [])),
                        email.get("has_been_replied_to", False),
                        email.get("is_junk", True),
                        email.get("category", "other"),
                        email.get("reason", ""),
                        email.get("approved", False),
                    ),
                )


def has_pending() -> bool:
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pending_scan LIMIT 1")
            return cur.fetchone() is not None


def toggle_email_approval(email_id: str) -> bool | None:
    """Atomically toggles approval for one email. Returns new value, or None if not found."""
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE pending_emails SET approved = NOT approved WHERE id = %s RETURNING approved",
                (email_id,),
            )
            row = cur.fetchone()
    return row[0] if row else None


def archive_pending_emails(email_ids: list[str]) -> int:
    """Marks emails archived instead of deleting them, so they still count toward archive stats."""
    if not email_ids:
        return 0
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE pending_emails SET archived = TRUE, archived_at = NOW()"
                " WHERE id = ANY(%s) AND archived = FALSE",
                (list(email_ids),),
            )
            return cur.rowcount


# ── Whitelist ─────────────────────────────────────────────────────────────────

def get_whitelist() -> set[str]:
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT email FROM whitelist")
            return {row[0] for row in cur.fetchall()}


def add_to_whitelist(email: str) -> None:
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO whitelist (email) VALUES (%s) ON CONFLICT (email) DO NOTHING",
                (email,),
            )


# ── Stats ─────────────────────────────────────────────────────────────────────

def get_cumulative_stats() -> tuple[int, float]:
    """Returns (total_junk_across_all_scans, total_estimated_cost)."""
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COALESCE(SUM(total_junk), 0), COALESCE(SUM(estimated_cost), 0.0) FROM stats"
            )
            row = cur.fetchone()
    return int(row[0]), float(row[1])


def get_stats() -> dict | None:
    """Returns stats in the dashboard-compatible format, or None if no stats recorded yet."""
    with _db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT scan_date, total_junk, by_category, token_usage, estimated_cost"
                " FROM stats ORDER BY created_at"
            )
            all_scans = [dict(row) for row in cur.fetchall()]

    if not all_scans:
        return None

    total_junk = sum(s["total_junk"] for s in all_scans)
    total_cost = sum(s["estimated_cost"] for s in all_scans)
    total_by_category: dict[str, int] = {}
    for s in all_scans:
        for cat, count in (s["by_category"] or {}).items():
            total_by_category[cat] = total_by_category.get(cat, 0) + count

    return {
        "scans": all_scans[-10:],
        "total_junk": total_junk,
        "total_by_category": total_by_category,
        "total_estimated_cost": round(total_cost, 6),
    }


def get_archive_stats() -> dict:
    """Returns cumulative archive activity: all-time total, this week's total, and the most recent archive time."""
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM pending_emails WHERE archived = TRUE")
            total_all_time = cur.fetchone()[0]
            cur.execute(
                "SELECT COUNT(*) FROM pending_emails"
                " WHERE archived = TRUE AND archived_at >= NOW() - INTERVAL '7 days'"
            )
            total_this_week = cur.fetchone()[0]
            cur.execute("SELECT MAX(archived_at) FROM pending_emails WHERE archived = TRUE")
            last_archived_at = cur.fetchone()[0]
    return {
        "total_all_time": total_all_time,
        "total_this_week": total_this_week,
        "last_archived_at": last_archived_at,
    }


def save_stats_entry(
    scan_date: str,
    total_junk: int,
    by_category: dict[str, int],
    token_usage: dict,
    estimated_cost: float,
) -> None:
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO stats (scan_date, total_junk, by_category, token_usage, estimated_cost)
                   VALUES (%s, %s, %s, %s, %s)""",
                (
                    scan_date,
                    total_junk,
                    json.dumps(by_category),
                    json.dumps(token_usage or {}),
                    estimated_cost,
                ),
            )

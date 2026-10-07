from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone

import db
from src.http import BudgetExceeded
from src.logging_config import redact


class Store:
    def reserve_ai_request(self, limit: int):
        # Count every outbound model request, including HTTP retries and fallback.
        key = "ai_requests:" + datetime.now(timezone.utc).date().isoformat()
        with db.get_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
            used = int(row[0]) if row else 0
            if used >= limit:
                raise BudgetExceeded("daily AI request budget exhausted")
            conn.execute("INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                         (key, str(used + 1)))

    def save_event(self, event_id: str, fingerprint: str, event: dict) -> bool:
        with db.get_conn() as conn:
            cursor = conn.execute("INSERT OR IGNORE INTO ai_events(id,fingerprint,payload) VALUES(?,?,?)",
                                  (event_id, fingerprint, json.dumps(event, ensure_ascii=False)))
            return cursor.rowcount == 1

    def pending_events(self, limit: int, max_attempts: int) -> list[dict]:
        with db.get_conn() as conn:
            rows = conn.execute("SELECT * FROM ai_events WHERE status IN ('new','failed') AND attempts<? "
                                "AND next_attempt_at<=? ORDER BY created_at,id LIMIT ?",
                                (max_attempts, int(time.time()), limit)).fetchall()
        return [{**dict(row), "event": json.loads(row["payload"])} for row in rows]

    def claim_event(self, event_id: str) -> bool:
        with db.get_conn() as conn:
            cursor = conn.execute("UPDATE ai_events SET status='writing',attempts=attempts+1 "
                                  "WHERE id=? AND status IN ('new','failed')", (event_id,))
            return cursor.rowcount == 1

    def fail_event(self, event_id: str, error: str):
        with db.get_conn() as conn:
            row = conn.execute("SELECT attempts FROM ai_events WHERE id=?", (event_id,)).fetchone()
            delay = min(21600, 300 * 2 ** min(10, max(0, row["attempts"] - 1)))
            conn.execute("UPDATE ai_events SET status='failed',error=?,next_attempt_at=? WHERE id=?",
                         (redact(error)[:500], int(time.time()) + delay, event_id))

    def reject_event(self, event_id: str, reason: str):
        with db.get_conn() as conn:
            conn.execute("UPDATE ai_events SET status='rejected',error=? WHERE id=?", (reason, event_id))

    def enqueue(self, text: str, *, event_id: str | None = None, opening: str = "",
                image_paths: list[str] | None = None, publish_at: int | None = None) -> int | None:
        if not text.strip():
            raise ValueError("empty post")
        try:
            with db.get_conn() as conn:
                # BEGIN IMMEDIATE reserves the write lock; dedup and insert are one transaction.
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("INSERT INTO published_hashes(hash) VALUES(?)", (db.text_hash(text),))
                cursor = conn.execute("INSERT INTO binance_queue(text,image_paths,event_id,publish_at) VALUES(?,?,?,?)",
                                      (text, json.dumps(image_paths or []), event_id,
                                       int(time.time()) if publish_at is None else publish_at))
                if event_id:
                    conn.execute("UPDATE ai_events SET status='queued',opening=?,error=NULL WHERE id=?", (opening, event_id))
                return int(cursor.lastrowid)
        except sqlite3.IntegrityError:
            return None

    def recent_openings(self) -> list[str]:
        with db.get_conn() as conn:
            return [row[0] for row in conn.execute("SELECT opening FROM ai_events WHERE opening IS NOT NULL "
                                                   "AND opening!='' ORDER BY created_at DESC,id DESC LIMIT 30")]

    def drafts_today(self) -> int:
        midnight = int(datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
        with db.get_conn() as conn:
            return conn.execute("SELECT COUNT(*) FROM binance_queue WHERE event_id IS NOT NULL AND created_at>=?",
                                (midnight,)).fetchone()[0]

    def queue(self, limit: int = 100, offset: int = 0) -> list[dict]:
        with db.get_conn() as conn:
            rows = conn.execute("SELECT * FROM binance_queue ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset)).fetchall()
        return [dict(row) for row in rows]

    def events(self, limit: int = 100, offset: int = 0) -> list[dict]:
        with db.get_conn() as conn:
            rows = conn.execute("SELECT * FROM ai_events ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?",
                                (limit, offset)).fetchall()
        return [{**dict(row), "event": json.loads(row["payload"])} for row in rows]

    def review_post(self, post_id: int, reason: str):
        with db.get_conn() as conn:
            conn.execute("UPDATE binance_queue SET status='review',last_error=? WHERE id=?",
                         (redact(reason)[:500], post_id))

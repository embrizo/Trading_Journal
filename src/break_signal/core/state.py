"""SQLite persistence: alert dedupe and broken-line memory.

Keyed by (symbol, tf, line_id). Once a line fires it is recorded as broken and
never re-armed, so a service restart cannot double-send. WAL mode keeps writes
cheap and SD-card friendly on a Pi.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from .retest import PendingRetest


class State:
    def __init__(self, db_path: str):
        self.path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self._init_schema()

    def _init_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS broken_lines (
                symbol   TEXT NOT NULL,
                tf       TEXT NOT NULL,
                line_id  TEXT NOT NULL,
                broke_ts INTEGER NOT NULL,
                PRIMARY KEY (symbol, tf, line_id)
            );
            CREATE TABLE IF NOT EXISTS alerts (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol   TEXT NOT NULL,
                tf       TEXT NOT NULL,
                line_id  TEXT NOT NULL,
                event    TEXT NOT NULL,
                price    REAL,
                candle_ts INTEGER,
                sent_ts  INTEGER
            );
            CREATE TABLE IF NOT EXISTS progress (
                symbol       TEXT NOT NULL,
                tf           TEXT NOT NULL,
                last_candle_ts INTEGER,
                PRIMARY KEY (symbol, tf)
            );
            CREATE TABLE IF NOT EXISTS pending_retests (
                symbol    TEXT NOT NULL,
                tf        TEXT NOT NULL,
                line_id   TEXT NOT NULL,
                side      TEXT NOT NULL,
                ts_ref    INTEGER NOT NULL,
                value_ref REAL NOT NULL,
                slope     REAL NOT NULL,
                touches   INTEGER NOT NULL,
                broke_ts  INTEGER NOT NULL,
                PRIMARY KEY (symbol, tf, line_id)
            );
            """
        )
        self.conn.commit()

    # ── broken lines ────────────────────────────────────────────────────
    def broken_ids(self, symbol: str, tf: str) -> set[str]:
        cur = self.conn.execute(
            "SELECT line_id FROM broken_lines WHERE symbol=? AND tf=?", (symbol, tf)
        )
        return {row[0] for row in cur.fetchall()}

    def mark_broken(self, symbol: str, tf: str, line_id: str) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO broken_lines(symbol, tf, line_id, broke_ts) VALUES (?,?,?,?)",
            (symbol, tf, line_id, int(time.time() * 1000)),
        )
        self.conn.commit()

    def is_broken(self, symbol: str, tf: str, line_id: str) -> bool:
        cur = self.conn.execute(
            "SELECT 1 FROM broken_lines WHERE symbol=? AND tf=? AND line_id=?",
            (symbol, tf, line_id),
        )
        return cur.fetchone() is not None

    # ── pending retests ─────────────────────────────────────────────────
    def add_pending(self, symbol: str, tf: str, p: PendingRetest) -> None:
        """Record a broken line to watch for a retest. No-op if already present."""
        self.conn.execute(
            "INSERT OR IGNORE INTO pending_retests"
            "(symbol, tf, line_id, side, ts_ref, value_ref, slope, touches, broke_ts) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (symbol, tf, p.line_id, p.side, p.ts_ref, p.value_ref, p.slope,
             p.touches, p.broke_ts),
        )
        self.conn.commit()

    def pending_retests(self, symbol: str, tf: str) -> list[PendingRetest]:
        cur = self.conn.execute(
            "SELECT line_id, side, ts_ref, value_ref, slope, touches, broke_ts "
            "FROM pending_retests WHERE symbol=? AND tf=?",
            (symbol, tf),
        )
        return [
            PendingRetest(
                line_id=r[0], side=r[1], ts_ref=r[2], value_ref=r[3],
                slope=r[4], touches=r[5], broke_ts=r[6],
            )
            for r in cur.fetchall()
        ]

    def retire_pending(self, symbol: str, tf: str, line_id: str) -> None:
        self.conn.execute(
            "DELETE FROM pending_retests WHERE symbol=? AND tf=? AND line_id=?",
            (symbol, tf, line_id),
        )
        self.conn.commit()

    # ── alerts log ──────────────────────────────────────────────────────
    def log_alert(self, symbol: str, tf: str, line_id: str, event: str, price: float, candle_ts: int) -> None:
        self.conn.execute(
            "INSERT INTO alerts(symbol, tf, line_id, event, price, candle_ts, sent_ts) "
            "VALUES (?,?,?,?,?,?,?)",
            (symbol, tf, line_id, event, price, candle_ts, int(time.time() * 1000)),
        )
        self.conn.commit()

    # ── progress ────────────────────────────────────────────────────────
    def last_candle_ts(self, symbol: str, tf: str) -> int | None:
        cur = self.conn.execute(
            "SELECT last_candle_ts FROM progress WHERE symbol=? AND tf=?", (symbol, tf)
        )
        row = cur.fetchone()
        return row[0] if row else None

    def set_last_candle_ts(self, symbol: str, tf: str, ts: int) -> None:
        self.conn.execute(
            "INSERT INTO progress(symbol, tf, last_candle_ts) VALUES (?,?,?) "
            "ON CONFLICT(symbol, tf) DO UPDATE SET last_candle_ts=excluded.last_candle_ts",
            (symbol, tf, ts),
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

"""SQLite persistence for the trade journal.

Separate file from ``state.db`` so wiping alert state never touches trades.
Same WAL pragmas as ``core/state.py``. All timestamps are epoch ms UTC.

Numbers cached on the trade row (``r_multiple``, ``pnl_amount``) are computed
by ``analytics`` at close time — this module never does arithmetic on prices.
"""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import analytics
from .models import (
    DIRECTIONS,
    OUTCOMES,
    TAG_CATEGORIES,
    TAG_PHASES,
    Screenshot,
    SignalRow,
    Tag,
    Trade,
    TradeEvent,
)

SCHEMA_VERSION = 3

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (
    version    INTEGER NOT NULL,
    applied_ts INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS signals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT NOT NULL,
    symbol      TEXT NOT NULL,
    exchange    TEXT NOT NULL,
    tf          TEXT NOT NULL,
    event       TEXT NOT NULL,
    side        TEXT NOT NULL,
    line_id     TEXT NOT NULL,
    price       REAL NOT NULL,
    line_price  REAL NOT NULL,
    atr_dist    REAL, touches INTEGER, age_bars INTEGER, vol_ratio REAL, rsi REAL,
    candle_ts   INTEGER NOT NULL,
    created_ts  INTEGER NOT NULL,
    UNIQUE (symbol, tf, line_id, candle_ts)
);

CREATE TABLE IF NOT EXISTS trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id       INTEGER REFERENCES signals(id),
    symbol          TEXT NOT NULL,
    tf              TEXT,
    direction       TEXT NOT NULL CHECK (direction IN ('LONG','SHORT')),
    status          TEXT NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN','CLOSED','SKIPPED')),
    entry_price     REAL, sl_price REAL, tp_price REAL, exit_price REAL,
    position_size   REAL, leverage REAL, fees REAL,
    risk_amount     REAL, risk_pct REAL,
    opened_ts       INTEGER, closed_ts INTEGER,
    outcome         TEXT CHECK (outcome IN ('WIN','LOSS','BE')),
    pnl_amount      REAL, r_multiple REAL,
    entry_reason    TEXT, exit_reason TEXT,
    confidence      INTEGER CHECK (confidence BETWEEN 1 AND 5),
    emotion_before  TEXT, emotion_after TEXT,
    notes           TEXT,
    ctx_rsi REAL, ctx_atr REAL, ctx_atr_dist REAL, ctx_vol_ratio REAL,
    ctx_session     TEXT,
    created_ts      INTEGER NOT NULL, updated_ts INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_trades_symbol_tf ON trades(symbol, tf);
CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
CREATE INDEX IF NOT EXISTS idx_trades_closed_ts ON trades(closed_ts);

CREATE TABLE IF NOT EXISTS tags (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    name      TEXT NOT NULL,
    category  TEXT NOT NULL CHECK (category IN ('SETUP','PSYCH','EXIT','MISTAKE','OTHER')),
    UNIQUE (name COLLATE NOCASE)
);

CREATE TABLE IF NOT EXISTS trade_tags (
    trade_id  INTEGER NOT NULL REFERENCES trades(id) ON DELETE CASCADE,
    tag_id    INTEGER NOT NULL REFERENCES tags(id),
    phase     TEXT NOT NULL CHECK (phase IN ('ENTRY','EXIT')),
    PRIMARY KEY (trade_id, tag_id, phase)
);

CREATE TABLE IF NOT EXISTS trade_events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id  INTEGER NOT NULL REFERENCES trades(id) ON DELETE CASCADE,
    type      TEXT NOT NULL,
    data      TEXT,
    event_ts  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS screenshots (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id   INTEGER NOT NULL REFERENCES trades(id) ON DELETE CASCADE,
    phase      TEXT NOT NULL CHECK (phase IN ('PRE','POST')),
    path       TEXT NOT NULL,
    created_ts INTEGER NOT NULL
);

-- AI-side tables (used from phase J3/J4; created now so migrations don't churn)
CREATE TABLE IF NOT EXISTS rules (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    name      TEXT NOT NULL UNIQUE,
    condition TEXT NOT NULL,
    severity  TEXT NOT NULL DEFAULT 'high',
    enabled   INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS rule_violations (
    trade_id  INTEGER NOT NULL REFERENCES trades(id) ON DELETE CASCADE,
    rule_id   INTEGER NOT NULL REFERENCES rules(id),
    detail    TEXT,
    PRIMARY KEY (trade_id, rule_id)
);

CREATE TABLE IF NOT EXISTS ai_analysis (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id       INTEGER REFERENCES trades(id) ON DELETE CASCADE,
    kind           TEXT NOT NULL,
    model          TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    input_metrics  TEXT NOT NULL,
    output         TEXT NOT NULL,
    created_ts     INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS memories (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    type           TEXT NOT NULL,
    key            TEXT,                        -- stable id of the observation (upsert target), v2
    content        TEXT NOT NULL,
    evidence       TEXT NOT NULL,
    confirmed      INTEGER NOT NULL DEFAULT 0,
    first_seen_ts  INTEGER NOT NULL, last_seen_ts INTEGER NOT NULL
);
"""
# NB: idx_memories_key is created by _migrate_v2 (fresh DBs run it too), not here —
# on a v1 file the column does not exist yet when this script runs.


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(r["name"] == column for r in conn.execute(f"PRAGMA table_info({table})"))


def _migrate_v2(conn: sqlite3.Connection) -> None:
    """v1 → v2: memories.key for evidence-backed upserts (journal/memory.py)."""
    if not _has_column(conn, "memories", "key"):
        conn.execute("ALTER TABLE memories ADD COLUMN key TEXT")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_memories_key ON memories(key)")


def _migrate_v3(conn: sqlite3.Connection) -> None:
    """v2 → v3: 'Never widen the stop' judges the direction of the move.

    The seeded rule fired on *any* ``sl_moved`` event, so trailing a stop to
    break-even counted as widening it. Repoint the stored rule at the new
    ``sl_widened`` fact and drop the violations it recorded that are no longer
    violations. A user-edited rule (different condition) is left alone.
    """
    from .rules import sl_widened

    old = {"field": "has_event_sl_moved", "op": "is_false"}
    new = json.dumps({"field": "sl_widened", "op": "is_false"})
    for row in conn.execute("SELECT id, condition FROM rules").fetchall():
        try:
            if json.loads(row[1]) != old:
                continue
        except (TypeError, ValueError):
            continue
        rule_id = row[0]
        for (trade_id,) in conn.execute(
                "SELECT trade_id FROM rule_violations WHERE rule_id=?", (rule_id,)).fetchall():
            tr = conn.execute("SELECT direction FROM trades WHERE id=?", (trade_id,)).fetchone()
            events = [{"type": t, "data": _loads(data), "event_ts": ts} for t, data, ts in conn.execute(
                "SELECT type, data, event_ts FROM trade_events WHERE trade_id=?", (trade_id,))]
            if sl_widened(tr[0] if tr else None, events) is not True:
                conn.execute("DELETE FROM rule_violations WHERE rule_id=? AND trade_id=?",
                             (rule_id, trade_id))
        conn.execute("UPDATE rules SET condition=? WHERE id=?", (new, rule_id))


def _loads(raw: Any) -> dict:
    try:
        d = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


# (target version, upgrade function) in order. Append; never edit a shipped step.
_MIGRATIONS: list[tuple[int, Any]] = [
    (2, _migrate_v2),
    (3, _migrate_v3),
]

# Bilingual word bank. Users extend freely; names are unique case-insensitively.
SEED_TAGS: list[tuple[str, str]] = [
    # setup
    ("Breakout", "SETUP"), ("Retest", "SETUP"), ("Pullback", "SETUP"),
    ("Liquidity Sweep", "SETUP"), ("FVG", "SETUP"), ("Range", "SETUP"),
    ("Trend Continuation", "SETUP"), ("Reversal", "SETUP"),
    # psychology
    ("FOMO", "PSYCH"), ("Revenge", "PSYCH"), ("Calm", "PSYCH"), ("Hesitation", "PSYCH"),
    ("Overconfident", "PSYCH"), ("Boredom", "PSYCH"), ("ตามวินัย", "PSYCH"),
    # exit
    ("Hit TP", "EXIT"), ("Hit SL", "EXIT"), ("ชน SL", "EXIT"), ("Trailing Stop", "EXIT"),
    ("Manual Exit", "EXIT"), ("Time Stop", "EXIT"), ("Partial", "EXIT"),
    # mistake
    ("Moved SL", "MISTAKE"), ("แหกกฎเลื่อน SL", "MISTAKE"), ("Oversized", "MISTAKE"),
    ("Early Entry", "MISTAKE"), ("Late Entry", "MISTAKE"), ("No Plan", "MISTAKE"),
    ("Chased", "MISTAKE"),
]

_TRADE_COLUMNS = (
    "signal_id", "symbol", "tf", "direction", "status",
    "entry_price", "sl_price", "tp_price", "exit_price",
    "position_size", "leverage", "fees", "risk_amount", "risk_pct",
    "opened_ts", "closed_ts", "outcome", "pnl_amount", "r_multiple",
    "entry_reason", "exit_reason", "confidence", "emotion_before", "emotion_after",
    "notes", "ctx_rsi", "ctx_atr", "ctx_atr_dist", "ctx_vol_ratio", "ctx_session",
)


def now_ms() -> int:
    return int(time.time() * 1000)


def iso_to_ms(iso: str) -> int:
    """``2026-01-13T00:00:00Z`` → epoch ms (inverse of ``core.types.ms_to_iso``)."""
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


class JournalDB:
    def __init__(self, db_path: str | Path, account_size: float | None = None):
        self.path = str(db_path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.account_size = account_size  # optional; enables risk_pct from risk_amount
        self._init_schema()

    def _init_schema(self) -> None:
        self.conn.executescript(_SCHEMA)
        cur = self.conn.execute("SELECT MAX(version) FROM schema_version")
        current = cur.fetchone()[0] or 0
        fresh = current == 0
        # Incremental migrations: each step upgrades from version n to n+1 and is
        # recorded so it never runs twice. Fresh databases start at the current
        # schema (the CREATE statements above) and just record the version.
        for version, step in _MIGRATIONS:
            if current < version:
                step(self.conn)
                self.conn.execute("INSERT INTO schema_version(version, applied_ts) VALUES (?,?)",
                                  (version, now_ms()))
                current = version
        if current < SCHEMA_VERSION:
            self.conn.execute("INSERT INTO schema_version(version, applied_ts) VALUES (?,?)",
                              (SCHEMA_VERSION, now_ms()))
        self.seed_tags()
        if fresh:
            # Seed rules once, on creation only, so a trader who deletes one is
            # not nagged with it again on the next start. (Tags re-seed: they
            # are harmless vocabulary; rules are opinions.)
            from . import rules
            rules.ensure_seed(self)
        self.conn.commit()

    @property
    def schema_version(self) -> int:
        return int(self.conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] or 0)

    def close(self) -> None:
        self.conn.close()

    # ── signals ─────────────────────────────────────────────────────────
    def insert_signal(self, sig: Any, source: str = "live", candle_ts: int | None = None) -> int:
        """Store a ``core.types.Signal`` (or its ``to_dict()``); idempotent on
        (symbol, tf, line_id, candle_ts). ``candle_ts`` defaults to the parsed
        ISO ``time`` field. Returns the row id either way."""
        d = sig if isinstance(sig, dict) else sig.to_dict()
        line_id = getattr(sig, "line_id", None) or d.get("line_id") or ""
        if candle_ts is None:
            candle_ts = iso_to_ms(d["time"])
        self.conn.execute(
            "INSERT OR IGNORE INTO signals(source, symbol, exchange, tf, event, side, line_id, "
            "price, line_price, atr_dist, touches, age_bars, vol_ratio, rsi, candle_ts, created_ts) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (source, d["symbol"], d.get("exchange", ""), d["tf"], d["event"], d["side"], line_id,
             d["price"], d.get("line", d.get("line_price")), d.get("atr_dist"), d.get("touches"),
             d.get("age_bars"), d.get("vol_ratio"), d.get("rsi"), candle_ts, now_ms()),
        )
        self.conn.commit()
        row = self.conn.execute(
            "SELECT id FROM signals WHERE symbol=? AND tf=? AND line_id=? AND candle_ts=?",
            (d["symbol"], d["tf"], line_id, candle_ts),
        ).fetchone()
        return int(row["id"])

    def get_signal(self, signal_id: int) -> SignalRow | None:
        row = self.conn.execute("SELECT * FROM signals WHERE id=?", (signal_id,)).fetchone()
        return SignalRow(**dict(row)) if row else None

    def list_signals(self, symbol: str | None = None, tf: str | None = None,
                     source: str | None = None, since: int | None = None,
                     limit: int = 50) -> list[SignalRow]:
        where, args = self._where({"symbol": symbol, "tf": tf, "source": source})
        if since is not None:
            where.append("candle_ts >= ?")
            args.append(since)
        sql = "SELECT * FROM signals"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY candle_ts DESC, id DESC LIMIT ?"
        args.append(limit)
        return [SignalRow(**dict(r)) for r in self.conn.execute(sql, args)]

    def latest_signal(self, symbol: str, tf: str | None = None, event: str | None = None,
                      since: int | None = None) -> SignalRow | None:
        """Most recent signal matching the filters — used to auto-link trades."""
        where, args = self._where({"symbol": symbol, "tf": tf, "event": event})
        if since is not None:
            where.append("candle_ts >= ?")
            args.append(since)
        row = self.conn.execute(
            "SELECT * FROM signals WHERE " + " AND ".join(where) +
            " ORDER BY candle_ts DESC, id DESC LIMIT 1", args
        ).fetchone()
        return SignalRow(**dict(row)) if row else None

    # ── tags ────────────────────────────────────────────────────────────
    def seed_tags(self) -> None:
        self.conn.executemany(
            "INSERT OR IGNORE INTO tags(name, category) VALUES (?,?)", SEED_TAGS
        )

    def get_tag(self, name: str) -> Tag | None:
        row = self.conn.execute(
            "SELECT * FROM tags WHERE name = ? COLLATE NOCASE", (name.strip(),)
        ).fetchone()
        return Tag(**dict(row)) if row else None

    def get_or_create_tag(self, name: str, category: str = "OTHER") -> Tag:
        name = name.strip()
        if not name:
            raise ValueError("empty tag name")
        tag = self.get_tag(name)
        if tag:
            return tag
        if category not in TAG_CATEGORIES:
            raise ValueError(f"bad tag category {category!r}")
        cur = self.conn.execute("INSERT INTO tags(name, category) VALUES (?,?)", (name, category))
        self.conn.commit()
        return Tag(id=int(cur.lastrowid), name=name, category=category)

    def list_tags(self, category: str | None = None) -> list[Tag]:
        if category:
            rows = self.conn.execute(
                "SELECT * FROM tags WHERE category=? ORDER BY name COLLATE NOCASE", (category,))
        else:
            rows = self.conn.execute("SELECT * FROM tags ORDER BY category, name COLLATE NOCASE")
        return [Tag(**dict(r)) for r in rows]

    def rename_tag(self, old: str, new: str) -> Tag:
        tag = self.get_tag(old)
        if not tag:
            raise KeyError(f"no tag {old!r}")
        new = new.strip()
        if not new:
            raise ValueError("empty tag name")
        clash = self.get_tag(new)
        if clash and clash.id != tag.id:
            raise ValueError(f"tag {clash.name!r} already exists (names are case-insensitive)")
        self.conn.execute("UPDATE tags SET name=? WHERE id=?", (new, tag.id))
        self.conn.commit()
        return Tag(id=tag.id, name=new.strip(), category=tag.category)

    def set_tag_category(self, name: str, category: str) -> Tag:
        tag = self.get_tag(name)
        if not tag:
            raise KeyError(f"no tag {name!r}")
        if category not in TAG_CATEGORIES:
            raise ValueError(f"bad tag category {category!r}")
        self.conn.execute("UPDATE tags SET category=? WHERE id=?", (category, tag.id))
        self.conn.commit()
        return Tag(id=tag.id, name=tag.name, category=category)

    def attach_tags(self, trade_id: int, names: list[str], phase: str) -> list[Tag]:
        """Attach tags (creating unknown ones as OTHER). Duplicates are ignored."""
        if phase not in TAG_PHASES:
            raise ValueError(f"bad phase {phase!r}")
        out = []
        for name in names:
            tag = self.get_or_create_tag(name)
            self.conn.execute(
                "INSERT OR IGNORE INTO trade_tags(trade_id, tag_id, phase) VALUES (?,?,?)",
                (trade_id, tag.id, phase),
            )
            out.append(tag)
        self.conn.commit()
        return out

    def detach_tag(self, trade_id: int, name: str, phase: str | None = None) -> None:
        tag = self.get_tag(name)
        if not tag:
            return
        if phase:
            self.conn.execute("DELETE FROM trade_tags WHERE trade_id=? AND tag_id=? AND phase=?",
                              (trade_id, tag.id, phase))
        else:
            self.conn.execute("DELETE FROM trade_tags WHERE trade_id=? AND tag_id=?",
                              (trade_id, tag.id))
        self.conn.commit()

    # ── trades ──────────────────────────────────────────────────────────
    def add_trade(
        self,
        symbol: str,
        direction: str,
        *,
        entry_tags: list[str] | None = None,
        opened_ts: int | None = None,
        **fields: Any,
    ) -> Trade:
        """Insert an OPEN trade. Unknown-to-the-user fields stay NULL — nothing is
        guessed. ``risk_pct`` is filled from ``risk_amount`` when ``account_size``
        is configured and the caller did not give it."""
        direction = direction.upper()
        if direction not in DIRECTIONS:
            raise ValueError(f"direction must be LONG or SHORT, got {direction!r}")
        bad = set(fields) - set(_TRADE_COLUMNS)
        if bad:
            raise ValueError(f"unknown trade fields: {sorted(bad)}")
        ts = now_ms()
        opened = opened_ts if opened_ts is not None else ts
        row: dict[str, Any] = {c: None for c in _TRADE_COLUMNS}
        row.update(fields)
        row.update(symbol=symbol, direction=direction, status=fields.get("status", "OPEN"),
                   opened_ts=opened)
        if row["risk_pct"] is None and row["risk_amount"] is not None and self.account_size:
            row["risk_pct"] = row["risk_amount"] / self.account_size * 100
        if row["ctx_session"] is None:
            row["ctx_session"] = analytics.session_of(opened)
        cols = list(_TRADE_COLUMNS) + ["created_ts", "updated_ts"]
        vals = [row[c] for c in _TRADE_COLUMNS] + [ts, ts]
        cur = self.conn.execute(
            f"INSERT INTO trades({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", vals
        )
        trade_id = int(cur.lastrowid)
        if entry_tags:
            self.attach_tags(trade_id, entry_tags, "ENTRY")
        self.conn.commit()
        return self.get_trade(trade_id)  # type: ignore[return-value]

    def close_trade(
        self,
        trade_id: int,
        exit_price: float,
        *,
        outcome: str | None = None,
        exit_reason: str | None = None,
        exit_tags: list[str] | None = None,
        closed_ts: int | None = None,
        fees: float | None = None,
        emotion_after: str | None = None,
    ) -> Trade:
        """Close a trade. R and PnL are computed by ``analytics``; ``outcome``
        defaults to the sign of R but the user may override it."""
        t = self.get_trade(trade_id)
        if t is None:
            raise KeyError(f"no trade #{trade_id}")
        if t.status != "OPEN":
            raise ValueError(f"trade #{trade_id} is {t.status}, not OPEN")
        if outcome is not None:
            outcome = outcome.upper()
            if outcome not in OUTCOMES:
                raise ValueError(f"outcome must be one of {OUTCOMES}")
        fees = fees if fees is not None else t.fees
        r = analytics.r_multiple(t.direction, t.entry_price, t.sl_price, exit_price)
        pnl = analytics.pnl_amount(t.direction, t.entry_price, exit_price,
                                   t.position_size, t.risk_amount, r, fees)
        if outcome is None:
            outcome = analytics.derive_outcome(r)
        ts = now_ms()
        self.conn.execute(
            "UPDATE trades SET status='CLOSED', exit_price=?, outcome=?, exit_reason=?, "
            "closed_ts=?, fees=?, r_multiple=?, pnl_amount=?, emotion_after=?, updated_ts=? WHERE id=?",
            (exit_price, outcome, exit_reason, closed_ts if closed_ts is not None else ts,
             fees, r, pnl, emotion_after, ts, trade_id),
        )
        if exit_tags:
            self.attach_tags(trade_id, exit_tags, "EXIT")
        self.conn.commit()
        return self.get_trade(trade_id)  # type: ignore[return-value]

    def skip_signal(self, signal_id: int, reason: str | None = None,
                    tags: list[str] | None = None) -> Trade:
        """Record a deliberate non-trade on a signal (status SKIPPED)."""
        sig = self.get_signal(signal_id)
        if sig is None:
            raise KeyError(f"no signal #{signal_id}")
        direction = "LONG" if sig.event == "break_up" else "SHORT"
        return self.add_trade(
            sig.symbol, direction, status="SKIPPED", signal_id=sig.id, tf=sig.tf,
            entry_reason=reason, ctx_rsi=sig.rsi, ctx_atr_dist=sig.atr_dist,
            ctx_vol_ratio=sig.vol_ratio, entry_tags=tags,
        )

    def update_trade(self, trade_id: int, **fields: Any) -> Trade:
        """Edit arbitrary columns (no R/PnL recomputation — use close_trade for
        exits). ``risk_pct`` is filled from a newly set ``risk_amount`` exactly as
        ``add_trade`` does, so a risk stated after the fact still reaches the
        "max risk %" rule instead of leaving it permanently not-applicable."""
        bad = set(fields) - set(_TRADE_COLUMNS)
        if bad:
            raise ValueError(f"unknown trade fields: {sorted(bad)}")
        if not fields:
            return self.get_trade(trade_id)  # type: ignore[return-value]
        if (fields.get("risk_amount") is not None and "risk_pct" not in fields
                and self.account_size):
            fields["risk_pct"] = fields["risk_amount"] / self.account_size * 100
        sets = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(
            f"UPDATE trades SET {sets}, updated_ts=? WHERE id=?",
            [*fields.values(), now_ms(), trade_id],
        )
        self.conn.commit()
        return self.get_trade(trade_id)  # type: ignore[return-value]

    def delete_trade(self, trade_id: int) -> None:
        self.conn.execute("DELETE FROM trades WHERE id=?", (trade_id,))
        self.conn.commit()

    def get_trade(self, trade_id: int) -> Trade | None:
        row = self.conn.execute("SELECT * FROM trades WHERE id=?", (trade_id,)).fetchone()
        return self._hydrate(row) if row else None

    def list_trades(
        self,
        *,
        symbol: str | None = None,
        tf: str | None = None,
        direction: str | None = None,
        status: str | None = None,
        outcome: str | None = None,
        tags: list[str] | None = None,
        since: int | None = None,
        until: int | None = None,
        signal_linked: bool | None = None,
        limit: int | None = None,
    ) -> list[Trade]:
        """Filter trades. ``since``/``until`` apply to ``opened_ts``. ``tags``
        requires ALL listed tags (any phase). Newest first."""
        where, args = self._where({"symbol": symbol, "tf": tf, "direction": direction,
                                   "status": status, "outcome": outcome})
        if since is not None:
            where.append("opened_ts >= ?"); args.append(since)
        if until is not None:
            where.append("opened_ts < ?"); args.append(until)
        if signal_linked is True:
            where.append("signal_id IS NOT NULL")
        elif signal_linked is False:
            where.append("signal_id IS NULL")
        for name in tags or []:
            where.append("id IN (SELECT tt.trade_id FROM trade_tags tt JOIN tags tg ON tg.id=tt.tag_id "
                         "WHERE tg.name = ? COLLATE NOCASE)")
            args.append(name)
        sql = "SELECT * FROM trades"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY opened_ts DESC, id DESC"
        if limit:
            sql += " LIMIT ?"; args.append(limit)
        return [self._hydrate(r) for r in self.conn.execute(sql, args)]

    # ── events & screenshots ────────────────────────────────────────────
    def add_event(self, trade_id: int, type_: str, data: dict | None = None,
                  event_ts: int | None = None) -> TradeEvent:
        ts = event_ts if event_ts is not None else now_ms()
        cur = self.conn.execute(
            "INSERT INTO trade_events(trade_id, type, data, event_ts) VALUES (?,?,?,?)",
            (trade_id, type_, json.dumps(data or {}, ensure_ascii=False), ts),
        )
        self.conn.execute("UPDATE trades SET updated_ts=? WHERE id=?", (now_ms(), trade_id))
        self.conn.commit()
        return TradeEvent(id=int(cur.lastrowid), trade_id=trade_id, type=type_,
                          data=data or {}, event_ts=ts)

    def add_screenshot(self, trade_id: int, phase: str, path: str) -> Screenshot:
        ts = now_ms()
        cur = self.conn.execute(
            "INSERT INTO screenshots(trade_id, phase, path, created_ts) VALUES (?,?,?,?)",
            (trade_id, phase.upper(), path, ts),
        )
        self.conn.commit()
        return Screenshot(id=int(cur.lastrowid), trade_id=trade_id, phase=phase.upper(),
                          path=path, created_ts=ts)

    # ── internals ───────────────────────────────────────────────────────
    @staticmethod
    def _where(eq: dict[str, Any]) -> tuple[list[str], list[Any]]:
        where, args = [], []
        for col, val in eq.items():
            if val is not None:
                where.append(f"{col} = ?")
                args.append(val)
        return where, args

    def _hydrate(self, row: sqlite3.Row) -> Trade:
        t = Trade(**dict(row))
        for r in self.conn.execute(
            "SELECT tg.name, tt.phase FROM trade_tags tt JOIN tags tg ON tg.id = tt.tag_id "
            "WHERE tt.trade_id=? ORDER BY tg.name COLLATE NOCASE", (t.id,)
        ):
            (t.entry_tags if r["phase"] == "ENTRY" else t.exit_tags).append(r["name"])
        t.events = [
            TradeEvent(id=r["id"], trade_id=r["trade_id"], type=r["type"],
                       data=json.loads(r["data"] or "{}"), event_ts=r["event_ts"])
            for r in self.conn.execute(
                "SELECT * FROM trade_events WHERE trade_id=? ORDER BY event_ts, id", (t.id,))
        ]
        t.screenshots = [
            Screenshot(**dict(r)) for r in self.conn.execute(
                "SELECT * FROM screenshots WHERE trade_id=? ORDER BY created_ts, id", (t.id,))
        ]
        if t.signal_id is not None:
            t.signal = self.get_signal(t.signal_id)
        return t

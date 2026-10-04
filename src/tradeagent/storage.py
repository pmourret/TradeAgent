"""Journal et état persistant (SQLite).

Tout ce qui doit survivre à un redémarrage vit ici : solde simulé, état du kill switch,
plus-haut d'equity. Un bot mort ne doit pas ressusciter parce qu'on relance le script.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .models import Decision, Fill

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    agent TEXT NOT NULL,
    action TEXT NOT NULL,
    symbol TEXT,
    amount_quote REAL,
    reasoning TEXT,
    approved INTEGER,
    verdict TEXT
);
CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    source TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    quantity REAL NOT NULL,
    price REAL NOT NULL,
    fee REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS equity (
    ts REAL NOT NULL,
    equity REAL NOT NULL,
    cash REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    ts REAL NOT NULL,
    level TEXT NOT NULL,
    message TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS llm_calls (
    ts REAL NOT NULL,
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cache_read_tokens INTEGER NOT NULL,
    cache_write_tokens INTEGER NOT NULL,
    cost_eur REAL NOT NULL
);
"""


class Storage:
    def __init__(self, path: str | Path) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path))
        self._db.row_factory = sqlite3.Row
        self._db.executescript(SCHEMA)
        self._db.commit()

    # -- état clé/valeur -------------------------------------------------
    def get(self, key: str, default: Any = None) -> Any:
        row = self._db.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return default if row is None else json.loads(row["value"])

    def set(self, key: str, value: Any) -> None:
        self._db.execute(
            "INSERT INTO kv (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value)),
        )
        self._db.commit()

    def delete(self, key: str) -> None:
        self._db.execute("DELETE FROM kv WHERE key = ?", (key,))
        self._db.commit()

    # -- journal ---------------------------------------------------------
    def record_decision(self, ts: float, agent: str, decision: Decision,
                        approved: bool | None, verdict: str) -> None:
        self._db.execute(
            "INSERT INTO decisions (ts, agent, action, symbol, amount_quote, reasoning, approved, verdict) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (ts, agent, decision.action, decision.symbol, decision.amount_quote,
             decision.reasoning, None if approved is None else int(approved), verdict),
        )
        self._db.commit()

    def record_fill(self, fill: Fill, source: str) -> None:
        self._db.execute(
            "INSERT INTO fills (ts, source, symbol, side, quantity, price, fee) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (fill.timestamp, source, fill.symbol, fill.side, fill.quantity, fill.price, fill.fee),
        )
        self._db.commit()

    def record_equity(self, ts: float, equity: float, cash: float) -> None:
        self._db.execute("INSERT INTO equity (ts, equity, cash) VALUES (?, ?, ?)", (ts, equity, cash))
        self._db.commit()

    def record_event(self, ts: float, level: str, message: str) -> None:
        self._db.execute("INSERT INTO events (ts, level, message) VALUES (?, ?, ?)", (ts, level, message))
        self._db.commit()

    def record_llm_call(self, ts: float, model: str, input_tokens: int, output_tokens: int,
                        cache_read_tokens: int, cache_write_tokens: int, cost_eur: float) -> None:
        self._db.execute(
            "INSERT INTO llm_calls (ts, model, input_tokens, output_tokens, cache_read_tokens, "
            "cache_write_tokens, cost_eur) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (ts, model, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, cost_eur),
        )
        self._db.commit()

    def llm_spend_since(self, ts: float) -> float:
        row = self._db.execute(
            "SELECT COALESCE(SUM(cost_eur), 0) AS total FROM llm_calls WHERE ts >= ?", (ts,)
        ).fetchone()
        return float(row["total"])

    # -- lecture ---------------------------------------------------------
    def _rows(self, query: str, params: tuple = ()) -> list[dict[str, Any]]:
        return [dict(r) for r in self._db.execute(query, params).fetchall()]

    def recent_decisions(self, n: int = 10) -> list[dict[str, Any]]:
        return self._rows("SELECT * FROM decisions ORDER BY id DESC LIMIT ?", (n,))

    def recent_fills(self, n: int = 10) -> list[dict[str, Any]]:
        return self._rows("SELECT * FROM fills ORDER BY id DESC LIMIT ?", (n,))

    def recent_events(self, n: int = 10) -> list[dict[str, Any]]:
        return self._rows("SELECT rowid AS id, * FROM events ORDER BY rowid DESC LIMIT ?", (n,))

    def last_equity(self) -> dict[str, Any] | None:
        rows = self._rows("SELECT * FROM equity ORDER BY rowid DESC LIMIT 1")
        return rows[0] if rows else None

    def equity_series(self) -> list[float]:
        """Toutes les valeurs d'equity enregistrées, dans l'ordre (pour le drawdown d'un backtest)."""
        return [float(r[0]) for r in self._db.execute("SELECT equity FROM equity ORDER BY rowid")]

    def fills_totals(self) -> dict[str, float]:
        row = self._db.execute("SELECT COUNT(*) AS orders, COALESCE(SUM(fee), 0) AS fees FROM fills").fetchone()
        return {"orders": int(row["orders"]), "fees": float(row["fees"])}

    def count(self, table: str) -> int:
        if table not in {"decisions", "fills", "equity", "events", "llm_calls"}:
            raise ValueError(table)
        return self._db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def close(self) -> None:
        self._db.close()

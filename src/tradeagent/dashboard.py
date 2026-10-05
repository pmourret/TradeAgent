"""Instantané en LECTURE SEULE de l'état du bot, pour l'interface web.

Garanties :
- la base est ouverte en mode `ro` : même un bug ici ne peut rien écrire ;
- une connexion par appel (le serveur web est multi-thread) ;
- rien de secret n'y figure : ni clé API, ni variable d'environnement, ni chemin de fichier ;
- les textes écrits par le LLM (`reasoning`) y sont des données brutes : c'est à l'affichage
  de les traiter comme du texte et jamais comme du HTML.
"""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .advice import idle_advice
from .board import STATE_KEY as BOARD_STATE_KEY, clean_state
from .budget import day_start_ts
from .config import Config
from .portfolio import base_currency

MAX_SERIES_POINTS = 240
JOURNAL_ROWS = 60   # les « attentes » consécutives sont regroupées à l'affichage
FILL_ROWS = 20
EVENT_ROWS = 20
API_DAYS = 7


class DashboardError(RuntimeError):
    """La base existe mais n'a pas pu être lue (verrouillée, corrompue...)."""


def _connect(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=2.0)
    db.row_factory = sqlite3.Row
    return db


def _kv(db: sqlite3.Connection, key: str, default: Any = None) -> Any:
    row = db.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return default if row is None else json.loads(row["value"])


def _rows(db: sqlite3.Connection, query: str, params: tuple = ()) -> list[dict[str, Any]]:
    return [dict(r) for r in db.execute(query, params).fetchall()]


def downsample(points: list[list[float]], limit: int = MAX_SERIES_POINTS) -> list[list[float]]:
    """Réduit une série à `limit` points au plus en gardant toujours le premier et le dernier."""
    if len(points) <= limit or limit < 2:
        return points
    step = (len(points) - 1) / (limit - 1)
    picked = [points[round(i * step)] for i in range(limit)]
    picked[-1] = points[-1]
    return picked


def _empty(cfg: Config, now: float) -> dict[str, Any]:
    ks = cfg.killswitch
    return {
        "has_data": False,
        "generated_at": now,
        "mode": cfg.mode,
        "exchange": cfg.exchange,
        "quote_currency": cfg.quote_currency,
        "symbols": list(cfg.symbols),
        "cycle_seconds": cfg.cycle_seconds,
        "thresholds": {
            "cautious_drawdown_pct": cfg.risk_tiers.cautious_drawdown_pct,
            "defensive_drawdown_pct": cfg.risk_tiers.defensive_drawdown_pct,
            "max_drawdown_pct": ks.max_drawdown_pct,
            "max_total_loss_pct": ks.max_total_loss_pct,
            "max_daily_loss_pct": cfg.guardrails.max_daily_loss_pct,
            "max_buys_per_day": cfg.guardrails.max_buys_per_day,
        },
    }


def build_snapshot(cfg: Config, now: float | None = None) -> dict[str, Any]:
    now = time.time() if now is None else now
    snap = _empty(cfg, now)
    path = Path(cfg.database)
    if not path.exists():
        return snap
    try:
        db = _connect(path)
    except sqlite3.Error as exc:
        raise DashboardError(f"base illisible : {exc}") from exc
    try:
        return _fill(snap, db, cfg, now)
    except (sqlite3.Error, json.JSONDecodeError) as exc:
        raise DashboardError(f"lecture impossible : {exc}") from exc
    finally:
        db.close()


def _fill(snap: dict[str, Any], db: sqlite3.Connection, cfg: Config, now: float) -> dict[str, Any]:
    life = _kv(db, "life")
    last = _rows(db, "SELECT ts, equity, cash FROM equity ORDER BY rowid DESC LIMIT 1")
    if not life or not last:
        return snap

    ccy = cfg.quote_currency
    stake, started = float(life["stake"]), float(life.get("started", 0.0))
    equity = float(last[0]["equity"])
    # Le cash vient des soldes COURANTS (après une liquidation, le dernier point d'equity est antérieur à la vente).
    balances = _kv(db, "paper_balances", {}) or {}
    cash = float(balances.get(cfg.quote_currency, last[0]["cash"]))
    # -- coûts API ----------------------------------------------------------
    spent_life = _spend(db, started)
    # Le kill switch et les paliers jugent l'equity nette du loyer (equity moins le coût d'API de cette vie) :
    # le plus-haut, le drawdown et les lignes de palier sont donc exprimés dans cette même grandeur.
    net_equity = equity - spent_life
    peak = max(float(_kv(db, "peak_equity", stake)), net_equity)
    drawdown = (1 - net_equity / peak) * 100 if peak > 0 else 0.0
    spent_total = _spend(db, 0.0)
    spent_today = _spend(db, day_start_ts(now))
    llm_last = _kv(db, "llm_last_call")
    wake = _kv(db, "llm_wake") or {}
    idle = _kv(db, "llm_idle")
    idle = idle if isinstance(idle, dict) else {}
    # Le prochain appel possible : le réveil choisi par l'agent s'il dort, sinon l'intervalle minimal du palier
    # (palier « économie » : il s'allonge en prudent et en défensif).
    steps = {"cautious": 1, "defensive": 2}.get(_kv(db, "risk_tier", "normal"), 0)
    min_interval = min(cfg.llm.call_every_seconds * cfg.llm.economy_call_factor ** steps, cfg.llm.max_call_interval_seconds)
    next_call = max(float(wake.get("at") or 0.0), llm_last + min_interval) if llm_last else None
    calls_life = db.execute("SELECT COUNT(*) FROM llm_calls WHERE ts >= ?", (started,)).fetchone()[0]

    # -- positions ------------------------------------------------------------
    quotes = _kv(db, "last_quotes", {}) or {}
    positions = []
    for symbol in cfg.symbols:
        qty = float(balances.get(base_currency(symbol), 0.0))
        price = quotes.get(symbol)
        value = qty * float(price) if price is not None else None
        positions.append({
            "symbol": symbol, "quantity": qty, "price": price, "value": value,
            "share_pct": (value / equity * 100) if value is not None and equity > 0 else None,
        })

    # -- jour en cours --------------------------------------------------------
    day = _kv(db, "day")
    today = datetime.fromtimestamp(now, tz=timezone.utc).date().isoformat()
    day_view = None
    if day and day.get("date") == today:
        pnl = equity - float(day["start_equity"])
        day_view = {
            "date": today, "start_equity": float(day["start_equity"]), "pnl": pnl,
            "pnl_pct": (pnl / float(day["start_equity"]) * 100) if day["start_equity"] else 0.0,
            "buys": int(day.get("buys", 0)), "trades": int(day.get("trades", 0)),
        }

    ks = _kv(db, "killswitch") or {"status": "alive", "reason": "", "ts": None}
    series = _net_series(db, started)
    fills = _rows(db, "SELECT * FROM fills ORDER BY id DESC LIMIT ?", (FILL_ROWS,))
    for f in fills:
        f["notional"] = f["quantity"] * f["price"]
    latest_decision = _rows(db, "SELECT agent FROM decisions ORDER BY id DESC LIMIT 1")
    agent = latest_decision[0]["agent"] if latest_decision else None
    board_state = _kv(db, BOARD_STATE_KEY)

    snap.update({
        "has_data": True,
        "agent": agent,
        "status": {"state": ks["status"], "reason": ks.get("reason", ""), "since": ks.get("ts")},
        "risk_tier": _kv(db, "risk_tier", "normal"),
        "life": {"stake": stake, "started": started},
        "money": {
            "equity": equity, "net_equity": net_equity, "cash": cash, "stake": stake,
            "change_pct": (equity / stake - 1) * 100 if stake else 0.0,
            "api_spent_life": spent_life,
            "net_result": equity - stake - spent_life,
            "peak_equity": peak, "drawdown_pct": drawdown,
            "death_floor": stake * (1 - cfg.killswitch.max_total_loss_pct / 100),
            "last_update": last[0]["ts"],
            # Niveaux d'equity où chaque palier s'enclenche, d'après le plus haut actuel.
            "tier_lines": {
                "cautious": peak * (1 - cfg.risk_tiers.cautious_drawdown_pct / 100),
                "defensive": peak * (1 - cfg.risk_tiers.defensive_drawdown_pct / 100),
                "death": max(stake * (1 - cfg.killswitch.max_total_loss_pct / 100),
                             peak * (1 - cfg.killswitch.max_drawdown_pct / 100)),
            },
        },
        "day": day_view,
        # Agent à l'arrêt : bilan et conseil pour l'utilisateur, qui décide. Texte fabriqué par le code, jamais par l'agent.
        "advice": idle_advice(str(idle["cause"]), stake, equity, spent_life, ccy) if idle.get("cause") else None,
        "positions": positions,
        # Ce que réclament les sous-agents du board (None pour tout autre agent). Aucun texte de la base n'y passe :
        # des nombres, le nom du sous-agent (fixé par le code) et des symboles de la config.
        "board": _board_claims(board_state, quotes, cfg.symbols) if agent == "board" else None,
        "equity_series": downsample(series),
        "api": {
            "provider": cfg.llm.provider, "model": cfg.llm.model,
            "calls_life": calls_life, "spent_life": spent_life,
            "spent_total": spent_total, "spent_today": spent_today,
            "daily_budget": cfg.llm.daily_budget_eur, "total_budget": cfg.llm.total_budget_eur,
            "call_every_seconds": cfg.llm.call_every_seconds,
            "last_call": llm_last,
            "next_call": next_call,
            "wake_if_move_pct": wake.get("move_pct") if llm_last and wake.get("at", 0.0) > now else None,
            "prompt_version": _kv(db, "llm_prompt_version"),
            "min_interval_seconds": min_interval,
            "idle_since": idle.get("since"),        # l'agent n'est plus appelé : aucun ordre possible
            "per_day": _api_per_day(db, now),
        },
        "journal": _rows(db, "SELECT * FROM decisions ORDER BY id DESC LIMIT ?", (JOURNAL_ROWS,)),
        "fills": fills,
        "events": _rows(db, "SELECT rowid AS id, * FROM events ORDER BY rowid DESC LIMIT ?", (EVENT_ROWS,)),
        "counts": {
            "decisions": db.execute("SELECT COUNT(*) FROM decisions").fetchone()[0],
            "fills": db.execute("SELECT COUNT(*) FROM fills").fetchone()[0],
            "llm_calls": db.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0],
        },
    })
    return snap


def _board_claims(stored: Any, quotes: dict[str, Any], symbols: tuple[str, ...] | list[str]) -> list[dict[str, Any]]:
    """Une ligne par position réclamée : le sous-agent, la quantité, le niveau de sortie et la marge avant ce niveau."""
    claims = []
    for sleeve, part in clean_state(stored).items():
        for symbol in symbols:
            quantity = part["qty"].get(symbol)
            if quantity is None:
                continue
            price, stop = quotes.get(symbol), part["stops"].get(symbol)
            price = float(price) if isinstance(price, (int, float)) and not isinstance(price, bool) and price > 0 else None
            claims.append({
                "sleeve": sleeve, "symbol": symbol, "quantity": quantity, "stop": stop,
                "value": quantity * price if price is not None else None,
                "stop_margin_pct": (price / stop - 1) * 100 if price is not None and stop else None,
            })
    return claims


def _net_series(db: sqlite3.Connection, started: float) -> list[list[float]]:
    """Courbe de la vie en cours, nette du loyer : chaque point d'equity moins le coût d'API cumulé jusque-là."""
    calls = db.execute("SELECT ts, cost_eur FROM llm_calls WHERE ts >= ? ORDER BY ts", (started,)).fetchall()
    series, rent, i = [], 0.0, 0
    for row in db.execute("SELECT ts, equity FROM equity WHERE ts >= ? ORDER BY rowid", (started,)):
        # Strictement avant : un appel daté du cycle lui-même a été payé APRÈS la photo de ce cycle.
        while i < len(calls) and calls[i]["ts"] < row["ts"]:
            rent += calls[i]["cost_eur"]
            i += 1
        series.append([row["ts"], row["equity"] - rent])
    return series


def _spend(db: sqlite3.Connection, since: float) -> float:
    row = db.execute("SELECT COALESCE(SUM(cost_eur), 0) FROM llm_calls WHERE ts >= ?", (since,)).fetchone()
    return float(row[0])


def _api_per_day(db: sqlite3.Connection, now: float) -> list[dict[str, Any]]:
    """Dépense API par jour UTC sur les `API_DAYS` derniers jours, les jours vides compris."""
    first = day_start_ts(now) - (API_DAYS - 1) * 86_400
    buckets = {first + i * 86_400: [0.0, 0] for i in range(API_DAYS)}
    for ts, cost in db.execute("SELECT ts, cost_eur FROM llm_calls WHERE ts >= ?", (first,)):
        key = day_start_ts(ts)
        if key in buckets:
            buckets[key][0] += cost
            buckets[key][1] += 1
    return [
        {"date": datetime.fromtimestamp(k, tz=timezone.utc).date().isoformat(), "cost": v[0], "calls": v[1]}
        for k, v in sorted(buckets.items())
    ]

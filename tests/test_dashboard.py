import hashlib
import json
import sqlite3

import pytest

from helpers import START, FakeClock, ScriptedAgent, ScriptedFeed, default_cfg, make_engine, permissive_cfg
from tradeagent.config import config_from_dict
from tradeagent.dashboard import MAX_SERIES_POINTS, DashboardError, _connect, build_snapshot, downsample
from tradeagent.models import Decision
from tradeagent.storage import Storage

BUY_BTC = Decision("buy", "BTC/EUR", 20.0, "test")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(tmp_path, cycles=3, agent=None, cfg=None, **overrides):
    """Fait tourner le vrai moteur sur une vraie base fichier et renvoie (cfg, engine, feed, clock, storage)."""
    path = tmp_path / "agent.db"
    cfg = cfg or default_cfg(database=str(path), **overrides)
    clock = FakeClock()
    storage = Storage(str(path))
    agent = agent or ScriptedAgent([BUY_BTC, BUY_BTC])
    engine, feed, clock, storage = make_engine(cfg, agent, clock=clock, storage=storage)
    for _ in range(cycles):
        engine.run_cycle()
        clock.advance(900)
    return cfg, engine, feed, clock, storage


# -- cas vides -------------------------------------------------------------------

def test_no_database_means_no_data_but_still_shows_thresholds(tmp_path):
    cfg = default_cfg(database=str(tmp_path / "missing.db"))
    snap = build_snapshot(cfg, START)
    assert snap["has_data"] is False
    assert snap["thresholds"]["max_drawdown_pct"] == cfg.killswitch.max_drawdown_pct
    assert not (tmp_path / "missing.db").exists()   # lire ne crée jamais la base


def test_empty_database_means_no_data(tmp_path):
    cfg = default_cfg(database=str(tmp_path / "empty.db"))
    Storage(cfg.database).close()
    assert build_snapshot(cfg, START)["has_data"] is False


# -- contenu -----------------------------------------------------------------------

def test_snapshot_reflects_a_real_run(tmp_path):
    cfg, engine, feed, clock, storage = run(tmp_path)
    storage.record_llm_call(clock(), "m", 1000, 100, 0, 0, 0.01)
    snap = build_snapshot(cfg, clock())

    assert snap["has_data"] and snap["status"]["state"] == "alive" and snap["risk_tier"] == "normal"
    money = snap["money"]
    assert money["stake"] == 100.0 and money["equity"] == pytest.approx(100, abs=1.0)
    assert money["net_result"] == pytest.approx(money["equity"] - money["stake"] - 0.01)
    assert money["api_spent_life"] == pytest.approx(0.01)
    assert money["tier_lines"]["cautious"] == pytest.approx(money["peak_equity"] * 0.85)
    assert money["tier_lines"]["defensive"] == pytest.approx(money["peak_equity"] * 0.75)
    assert money["tier_lines"]["death"] == pytest.approx(max(50.0, money["peak_equity"] * 0.6))

    btc = next(p for p in snap["positions"] if p["symbol"] == "BTC/EUR")
    assert btc["quantity"] > 0 and btc["price"] == 60_000.0
    assert btc["value"] == pytest.approx(btc["quantity"] * 60_000.0)
    assert snap["agent"] == "scripted"
    assert snap["counts"]["decisions"] == 3 and snap["counts"]["fills"] == 2
    assert len(snap["equity_series"]) == 3
    assert snap["journal"][0]["ts"] >= snap["journal"][-1]["ts"]   # le plus récent d'abord


def test_cash_comes_from_current_balances_not_from_the_last_equity_row(tmp_path):
    # Régression : après une liquidation, le dernier point d'equity précède la vente.
    cfg = permissive_cfg(max_total_loss_pct=50, max_drawdown_pct=90)
    cfg = config_from_dict({"database": str(tmp_path / "agent.db"),
                            "guardrails": {"max_order_pct": 100, "max_position_pct": 100, "max_total_exposure_pct": 100,
                                           "max_daily_loss_pct": 100, "max_buys_per_day": 100},
                            "killswitch": {"max_total_loss_pct": 30, "max_drawdown_pct": 90},
                            "risk_tiers": {"cautious_drawdown_pct": 20, "defensive_drawdown_pct": 25}})
    agent = ScriptedAgent([Decision("buy", "BTC/EUR", 90.0, "all in")])
    _, engine, feed, clock, storage = run(tmp_path, cycles=1, agent=agent, cfg=cfg)
    feed.set("BTC/EUR", 30_000.0)          # -45 % sur 90 EUR : perte totale > 30 % -> mort + liquidation
    clock.advance(86_400)
    assert engine.run_cycle().status == "dead"

    snap = build_snapshot(cfg, clock())
    balances = storage.get("paper_balances")
    assert snap["status"]["state"] == "dead"
    assert snap["money"]["cash"] == pytest.approx(balances["EUR"])
    assert snap["money"]["cash"] > snap["money"]["equity"] * 0.95      # tout est revenu en cash
    assert all(p["quantity"] == 0 for p in snap["positions"])


def test_api_per_day_has_seven_buckets_including_empty_days(tmp_path):
    cfg, _, _, clock, storage = run(tmp_path, cycles=1)
    now = clock()
    storage.record_llm_call(now, "m", 1, 1, 0, 0, 0.5)
    storage.record_llm_call(now - 86_400, "m", 1, 1, 0, 0, 0.25)
    storage.record_llm_call(now - 86_400 * 30, "m", 1, 1, 0, 0, 9.0)       # hors fenêtre de 7 jours
    api = build_snapshot(cfg, now)["api"]
    per_day = api["per_day"]
    assert len(per_day) == 7 and per_day[-1]["cost"] == pytest.approx(0.5) and per_day[-2]["cost"] == pytest.approx(0.25)
    assert sum(d["calls"] for d in per_day) == 2
    assert api["spent_total"] == pytest.approx(9.75)        # le plafond total compte toutes les vies


def test_next_call_follows_the_cadence(tmp_path):
    cfg, _, _, clock, storage = run(tmp_path, cycles=1)
    storage.set("llm_last_call", clock())
    api = build_snapshot(cfg, clock())["api"]
    assert api["next_call"] == pytest.approx(clock() + cfg.llm.call_every_seconds)


def test_series_is_limited_to_the_current_life(tmp_path):
    cfg, engine, _, clock, storage = run(tmp_path, cycles=4)
    from tradeagent.app import reset_life
    reset_life(storage, clock())
    clock.advance(10)
    engine2, *_ = make_engine(cfg, ScriptedAgent(), clock=clock, feed=ScriptedFeed(clock), storage=storage)
    engine2.run_cycle()
    assert len(build_snapshot(cfg, clock())["equity_series"]) == 1


# -- séries --------------------------------------------------------------------------

def test_downsample_keeps_first_and_last_and_the_limit():
    points = [[float(i), float(i)] for i in range(1000)]
    out = downsample(points)
    assert len(out) == MAX_SERIES_POINTS and out[0] == points[0] and out[-1] == points[-1]
    assert [p[0] for p in out] == sorted(p[0] for p in out)
    short = points[:10]
    assert downsample(short) == short


# -- garanties de sécurité ------------------------------------------------------------

def test_reading_never_modifies_the_database(tmp_path):
    cfg, _, _, clock, storage = run(tmp_path)
    storage.close()
    before = digest(tmp_path / "agent.db")
    for _ in range(3):
        build_snapshot(cfg, clock())
    assert digest(tmp_path / "agent.db") == before


def test_connection_is_read_only(tmp_path):
    run(tmp_path)
    db = _connect(tmp_path / "agent.db")
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        db.execute("INSERT INTO events (ts, level, message) VALUES (1, 'info', 'x')")
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        db.execute("DELETE FROM kv")
    db.close()


def test_snapshot_contains_no_secret_and_no_file_path(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-super-secret")
    cfg, _, _, clock, _ = run(tmp_path)
    text = json.dumps(build_snapshot(cfg, clock()))
    assert "sk-ant-super-secret" not in text
    assert str(tmp_path) not in text and "agent.db" not in text


def test_unreadable_database_raises_a_clean_error(tmp_path):
    path = tmp_path / "broken.db"
    path.write_bytes(b"ceci n'est pas une base sqlite" * 50)
    with pytest.raises(DashboardError):
        build_snapshot(default_cfg(database=str(path)), START)

import itertools
from pathlib import Path

import pytest

from helpers import default_cfg
from tradeagent.app import AGENT_KINDS
from tradeagent.config import ConfigError
from tradeagent.profiles import DEFAULT_PROFILE, LIVE, PROFILES, apply_profile, database_for, get_profile


def test_the_default_profile_is_the_free_reference():
    default = get_profile(DEFAULT_PROFILE)
    assert default.agent == "hold" and not default.costs_money


def test_only_the_llm_profile_costs_money():
    # Ce qui est facturé doit être explicite : les scripts et l'interface s'appuient sur ce drapeau.
    assert {p.name for p in PROFILES.values() if p.costs_money} == {"llm"}
    assert all((p.agent == "llm") == p.costs_money for p in PROFILES.values())


@pytest.mark.parametrize("profile", PROFILES.values(), ids=list(PROFILES))
def test_profiles_only_use_known_agents_and_feeds(profile):
    assert profile.agent in AGENT_KINDS
    assert profile.feed in ("ccxt", "synthetic")
    assert profile.description


def test_the_offline_profile_never_touches_the_network_or_the_paid_api():
    demo = get_profile("demo")
    assert demo.feed == "synthetic" and demo.agent != "llm" and not demo.costs_money


@pytest.mark.parametrize("a,b", list(itertools.combinations(PROFILES.values(), 2)), ids=lambda p: p.name)
def test_two_profiles_never_share_a_port_or_a_database(a, b):
    cfg = default_cfg()
    assert a.port != b.port
    assert database_for(cfg, a) != database_for(cfg, b)


def test_a_profile_gets_its_own_database_next_to_the_configured_one():
    cfg = default_cfg(database="data/agent.db")
    # Comparaison en Path : le séparateur dépend du système (data\paper-llm.db sous Windows).
    assert Path(database_for(cfg, get_profile("llm"))) == Path("data/paper-llm.db")
    assert Path(apply_profile(cfg, get_profile("hold")).database) == Path("data/paper-hold.db")


def test_a_profile_never_changes_the_mode_or_the_stake():
    cfg = default_cfg()
    for profile in PROFILES.values():
        applied = apply_profile(cfg, profile)
        assert applied.mode == "paper" and applied.stake == cfg.stake and applied.guardrails == cfg.guardrails


def test_only_the_demo_profile_changes_the_cadence():
    cfg = default_cfg(cycle_seconds=900)
    assert apply_profile(cfg, get_profile("demo")).cycle_seconds == 2.0
    assert apply_profile(cfg, get_profile("hold")).cycle_seconds == 900
    assert apply_profile(cfg, get_profile("llm")).cycle_seconds == 900


def test_live_is_refused_with_the_prerequisites():
    with pytest.raises(ConfigError) as err:
        get_profile(LIVE)
    message = str(err.value)
    assert "n'existe pas encore" in message
    assert "confirmation manuelle" in message and "SANS droit de retrait" in message


def test_live_is_not_a_profile():
    assert LIVE not in PROFILES


def test_unknown_profile_lists_the_choices():
    with pytest.raises(ConfigError, match="hold.*llm.*demo"):
        get_profile("yolo")

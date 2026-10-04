import pytest

from tradeagent.config import KillSwitchConfig
from tradeagent.killswitch import ALIVE, DEAD, HALTED, KillSwitch, KillSwitchError
from tradeagent.storage import Storage


def make(storage=None, **cfg):
    storage = storage or Storage(":memory:")
    return KillSwitch(KillSwitchConfig(**cfg), storage, stake=100.0), storage


def test_starts_alive():
    ks, _ = make()
    assert ks.status == ALIVE and not ks.active


def test_total_loss_threshold():
    ks, _ = make(max_total_loss_pct=50, max_drawdown_pct=100)
    assert ks.check_financial(equity=50.01, peak_equity=100) is None
    assert "perte totale" in ks.check_financial(equity=50.0, peak_equity=100)
    assert "perte totale" in ks.check_financial(equity=0.0, peak_equity=100)


def test_drawdown_threshold_is_measured_from_the_peak():
    ks, _ = make(max_total_loss_pct=90, max_drawdown_pct=40)
    # le bot a monté à 200 puis redescend : -40 % depuis le plus haut = 120, bien au-dessus de la mise
    assert ks.check_financial(equity=121, peak_equity=200) is None
    assert "drawdown" in ks.check_financial(equity=120, peak_equity=200)


def test_die_is_permanent_and_keeps_first_reason():
    ks, _ = make()
    ks.die("raison 1")
    ks.die("raison 2")
    assert ks.status == DEAD and ks.reason == "raison 1" and ks.active


def test_death_survives_a_restart():
    ks, storage = make()
    ks.die("perdu")
    reborn, _ = make(storage=storage)
    assert reborn.status == DEAD and reborn.active


def test_dead_cannot_resume():
    ks, _ = make()
    ks.die("perdu")
    with pytest.raises(KillSwitchError):
        ks.resume()
    assert ks.status == DEAD


def test_halt_does_not_override_death():
    ks, _ = make()
    ks.die("perdu")
    ks.halt("erreurs")
    assert ks.status == DEAD


def test_halted_can_resume():
    ks, _ = make()
    ks.halt("erreurs")
    assert ks.status == HALTED and ks.active
    ks.resume()
    assert ks.status == ALIVE


def test_consecutive_errors_halt_at_the_threshold():
    ks, _ = make(max_consecutive_errors=3)
    assert ks.note_error("a") is False
    assert ks.note_error("b") is False
    assert ks.status == ALIVE
    assert ks.note_error("c") is True
    assert ks.status == HALTED and "3 erreurs" in ks.reason


def test_success_resets_the_error_streak():
    ks, _ = make(max_consecutive_errors=3)
    ks.note_error("a")
    ks.note_error("b")
    ks.note_success()
    ks.note_error("c")
    ks.note_error("d")
    assert ks.status == ALIVE

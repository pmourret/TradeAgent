import re
from pathlib import Path

import pytest

from tradeagent import cli
from tradeagent.lock import InstanceLock
from tradeagent.profiles import PROFILES
from tradeagent.storage import Storage

ROOT_CONFIG = Path(__file__).resolve().parent.parent / "config.yaml"


@pytest.fixture
def project(tmp_path, monkeypatch):
    """Un dossier de travail vierge avec la vraie config.yaml. Aucun .env, aucune clé : rien ne fuit d'un test à l'autre."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    (tmp_path / "config.yaml").write_text(ROOT_CONFIG.read_text())
    return tmp_path


def run(argv, capsys):
    code = cli.main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def offline(profile, *extra):
    # --cycle-seconds 0 : explicite, donc prioritaire sur les 2 s du profil demo ; les tests n'attendent pas.
    return ["run", "--profile", profile, "--feed", "synthetic", "--seed", "1", "--cycle-seconds", "0",
            "--max-cycles", "3", *extra]


def dbs(project):
    return sorted(p.name for p in (project / "data").glob("*.db"))


# -- un profil = une vie séparée ------------------------------------------------------------------------------

def test_a_profile_writes_to_its_own_database_never_to_the_default_one(project, capsys):
    code, out, _ = run(offline("demo"), capsys)
    assert code == 0 and "profil=demo" in out
    assert dbs(project) == ["paper-demo.db"]          # pas de data/agent.db


def test_profiles_keep_separate_lives(project, capsys):
    run(offline("hold"), capsys)
    run(offline("demo", "--agent", "chaos"), capsys)
    assert dbs(project) == ["paper-demo.db", "paper-hold.db"]
    hold = Storage(str(project / "data" / "paper-hold.db"))
    demo = Storage(str(project / "data" / "paper-demo.db"))
    assert hold.count("fills") == 0                    # hold ne fait rien
    assert demo.count("decisions") == 3                # demo a tourné de son côté


def test_reset_only_touches_the_chosen_profile(project, capsys):
    run(offline("demo", "--agent", "chaos"), capsys)
    run(offline("hold"), capsys)
    before = Storage(str(project / "data" / "paper-demo.db")).get("paper_balances")
    code, _, _ = run(["reset", "--profile", "hold", "--yes"], capsys)
    assert code == 0
    assert Storage(str(project / "data" / "paper-demo.db")).get("paper_balances") == before


def test_explicit_agent_and_feed_win_over_the_profile(project, capsys):
    code, out, _ = run(["run", "--profile", "demo", "--agent", "hold", "--feed", "synthetic", "--cycle-seconds", "0",
                        "--max-cycles", "1"], capsys)
    assert code == 0 and "agent=hold" in out and "flux=synthetic" in out


def test_without_a_profile_nothing_changes(project, capsys):
    code, out, _ = run(["run", "--feed", "synthetic", "--cycle-seconds", "0", "--max-cycles", "1"], capsys)
    assert code == 0 and "profil=" not in out and "agent=hold" in out
    assert dbs(project) == ["agent.db"]


# -- le mode réel est refusé, sans rien laisser derrière ----------------------------------------------------------------

@pytest.mark.parametrize("argv", [
    ["run", "--profile", "live"], ["web", "--profile", "live"], ["status", "--profile", "live"],
    ["reset", "--profile", "live", "--yes"], ["resume", "--profile", "live"], ["up", "live"],
])
def test_live_is_refused_everywhere_and_creates_nothing(project, capsys, argv):
    code, out, err = run(argv, capsys)
    assert code == 2 and "n'existe pas encore" in err and not out
    assert not (project / "data").exists()


# -- clé API : contrôlée avant toute écriture --------------------------------------------------------------------------

def test_llm_without_a_key_is_refused_and_leaves_no_trace(project, capsys):
    code, _, err = run(["run", "--profile", "llm", "--max-cycles", "1"], capsys)
    assert code == 2 and "ANTHROPIC_API_KEY" in err
    assert not (project / "data").exists()             # ni base, ni verrou : rien qui ressemble à un profil « vivant »


def test_the_key_is_read_from_the_dotenv_file(project, capsys, monkeypatch):
    (project / ".env").write_text("ANTHROPIC_API_KEY=sk-ant-faux-pour-le-test\n")
    # Avec une clé, on passe le contrôle ; on s'arrête avant tout appel réseau en coupant le flux.
    monkeypatch.setattr(cli, "build_feed", lambda *a, **k: (_ for _ in ()).throw(cli.ConfigError("stop")))
    code, _, err = run(["run", "--profile", "llm", "--max-cycles", "1"], capsys)
    assert code == 2 and "stop" in err and "ANTHROPIC_API_KEY" not in err


# -- un seul bot par base -----------------------------------------------------------------------------------------------

def test_a_second_run_on_the_same_profile_is_refused_then_allowed_after_the_first_stops(project, capsys):
    held = InstanceLock(project / "data" / "paper-demo.db").acquire()
    code, _, err = run(offline("demo"), capsys)
    assert code == 2 and "un autre `tradeagent run`" in err
    held.release()
    assert run(offline("demo"), capsys)[0] == 0


def test_another_profile_can_run_while_one_is_running(project, capsys):
    with InstanceLock(project / "data" / "paper-hold.db"):
        assert run(offline("demo"), capsys)[0] == 0


def test_the_lock_is_released_when_run_ends(project, capsys):
    run(offline("demo"), capsys)
    InstanceLock(project / "data" / "paper-demo.db").acquire().release()


# -- status --all -----------------------------------------------------------------------------------------------------

def test_status_all_with_nothing_says_so(project, capsys):
    code, out, _ = run(["status", "--all"], capsys)
    assert code == 0 and "aucun profil n'a encore tourné" in out


def test_status_all_shows_only_the_profiles_that_ran(project, capsys):
    run(offline("demo"), capsys)
    _, out, _ = run(["status", "--all"], capsys)
    assert "=== profil demo" in out and "=== profil hold" not in out and "=== profil llm" not in out


def test_status_all_is_read_only(project, capsys):
    code, _, _ = run(["status", "--all"], capsys)
    assert code == 0 and not (project / "data").exists()


def test_status_all_and_profile_are_exclusive(project, capsys):
    code, _, err = run(["status", "--all", "--profile", "hold"], capsys)
    assert code == 2 and "s'excluent" in err


# -- web : le port suit le profil ---------------------------------------------------------------------------------------

class _FakeServer:
    server_address = ("127.0.0.1", 0)

    def serve_forever(self):
        raise KeyboardInterrupt

    def server_close(self):
        pass


@pytest.mark.parametrize("argv,expected", [
    (["web"], 8765),
    (["web", "--profile", "hold"], 8765),
    (["web", "--profile", "llm"], 8766),
    (["web", "--profile", "demo"], 8767),
    (["web", "--profile", "llm", "--port", "9000"], 9000),
])
def test_web_port_follows_the_profile(project, capsys, monkeypatch, argv, expected):
    seen = {}

    def fake_make_server(cfg, host, port):
        seen.update(cfg=cfg, host=host, port=port)
        return _FakeServer()

    monkeypatch.setattr(cli, "make_server", fake_make_server)
    assert run(argv, capsys)[0] == 0
    assert seen["port"] == expected and seen["host"] == "127.0.0.1"


def test_web_reads_the_database_of_the_profile(project, capsys, monkeypatch):
    seen = {}
    monkeypatch.setattr(cli, "make_server", lambda cfg, host, port: seen.update(db=cfg.database) or _FakeServer())
    run(["web", "--profile", "llm"], capsys)
    assert seen["db"].endswith("paper-llm.db")


def test_the_profile_ports_are_the_ones_the_ui_scripts_advertise():
    ports = {p.name: p.port for p in PROFILES.values()}
    assert ports == {"hold": 8765, "llm": 8766, "demo": 8767, "board": 8768}


# -- up ---------------------------------------------------------------------------------------------------------------

@pytest.fixture
def jobs_seen(monkeypatch):
    seen = []
    monkeypatch.setattr(cli, "run_jobs", lambda jobs, **kw: seen.append(jobs) or 0)
    return seen


def test_up_defaults_to_the_free_reference_with_its_interface(project, capsys, jobs_seen):
    code, out, _ = run(["up"], capsys)
    assert code == 0 and "hold" in out and "http://localhost:8765/" in out
    kinds = [(j.profile, j.kind) for j in jobs_seen[0]]
    assert kinds == [("hold", "bot"), ("hold", "ui")] and "--open" in jobs_seen[0][1].argv


def test_up_can_compare_two_profiles(project, capsys, jobs_seen, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-faux-pour-le-test")
    code, out, _ = run(["up", "hold", "llm", "--no-open"], capsys)
    assert code == 0
    assert [(j.profile, j.kind) for j in jobs_seen[0]] == [("hold", "bot"), ("hold", "ui"), ("llm", "bot"), ("llm", "ui")]
    assert not any("--open" in j.argv for j in jobs_seen[0])
    assert "facturée" in out and "0.25" in out and "10.00" in out    # le plafond annoncé est celui de la config


def test_up_only_warns_about_cost_for_the_paid_profile(project, capsys, jobs_seen):
    _, out, _ = run(["up", "demo"], capsys)
    assert "facturée" not in out


def test_up_without_ui(project, capsys, jobs_seen):
    run(["up", "demo", "--no-ui"], capsys)
    assert [j.kind for j in jobs_seen[0]] == ["bot"]


def test_up_llm_without_key_starts_nothing(project, capsys, jobs_seen):
    code, _, err = run(["up", "hold", "llm"], capsys)
    assert code == 2 and "ANTHROPIC_API_KEY" in err and jobs_seen == []


def test_up_forwards_the_config_path(project, capsys, jobs_seen):
    run(["up", "demo", "--config", "config.yaml"], capsys)
    for job in jobs_seen[0]:
        assert job.argv[job.argv.index("--config") + 1] == "config.yaml"


def test_help_lists_the_commands_and_profiles(capsys):
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    out = capsys.readouterr().out
    assert re.search(r"\bup\b", out) and "run" in out and "web" in out

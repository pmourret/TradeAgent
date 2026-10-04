import io
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from helpers import default_cfg
from tradeagent import launcher
from tradeagent.config import ConfigError
from tradeagent.launcher import Job, build_jobs, describe, preflight, resolve_profiles, run_jobs
from tradeagent.llm import LLMError
from tradeagent.profiles import PROFILES

SRC = str(Path(__file__).resolve().parent.parent / "src")
POSIX = os.name == "posix"


def py(code: str, kind="bot", profile="x") -> Job:
    return Job(profile, kind, (sys.executable, "-u", "-c", code))


# -- choix des profils -----------------------------------------------------------------------------------------------

def test_no_profile_means_the_free_reference():
    assert [p.name for p in resolve_profiles([])] == ["hold"]


def test_duplicates_are_launched_once_and_order_is_kept():
    assert [p.name for p in resolve_profiles(["llm", "hold", "llm"])] == ["llm", "hold"]


def test_live_and_unknown_profiles_are_refused():
    with pytest.raises(ConfigError, match="n'existe pas encore"):
        resolve_profiles(["hold", "live"])
    with pytest.raises(ConfigError, match="profil inconnu"):
        resolve_profiles(["yolo"])


# -- contrôles avant lancement ---------------------------------------------------------------------------------------

def test_paid_profile_needs_a_key_before_anything_starts():
    with pytest.raises(LLMError, match="ANTHROPIC_API_KEY"):
        preflight(resolve_profiles(["hold", "llm"]), bots=True, environ={})
    preflight(resolve_profiles(["hold", "llm"]), bots=True, environ={"ANTHROPIC_API_KEY": "sk-ant-faux"})


def test_free_profiles_never_ask_for_a_key():
    preflight(resolve_profiles(["hold", "demo"]), bots=True, environ={})


def test_empty_key_counts_as_missing():
    with pytest.raises(LLMError):
        preflight(resolve_profiles(["llm"]), bots=True, environ={"ANTHROPIC_API_KEY": ""})


# -- ce qui est lancé ------------------------------------------------------------------------------------------------

def test_each_profile_gets_a_bot_and_an_interface_started_exactly_like_by_hand():
    jobs = build_jobs(resolve_profiles(["hold", "llm"]), "config.yaml", python="PY")
    assert [(j.profile, j.kind) for j in jobs] == [("hold", "bot"), ("hold", "ui"), ("llm", "bot"), ("llm", "ui")]
    assert jobs[0].argv == ("PY", "-u", "-m", "tradeagent", "run", "--profile", "hold", "--config", "config.yaml")
    assert jobs[3].argv == ("PY", "-u", "-m", "tradeagent", "web", "--profile", "llm", "--config", "config.yaml", "--open")


def test_the_launcher_never_overrides_what_the_profile_decides():
    for job in build_jobs(list(PROFILES.values()), "config.yaml"):
        assert "--agent" not in job.argv and "--feed" not in job.argv and "--port" not in job.argv
        assert "live" not in job.argv


def test_options_remove_the_interface_or_the_browser():
    profiles = resolve_profiles(["demo"])
    assert [j.kind for j in build_jobs(profiles, "c.yaml", ui=False)] == ["bot"]
    assert "--open" not in build_jobs(profiles, "c.yaml", open_browser=False)[1].argv


def test_tags_tell_bot_and_interface_apart():
    jobs = build_jobs(resolve_profiles(["llm"]), "c.yaml")
    assert [j.tag for j in jobs] == ["llm", "llm ui"]


def test_description_shows_ports_and_warns_only_for_the_paid_profile():
    cfg = default_cfg()
    text = "\n".join(describe(resolve_profiles(["hold", "llm"]), cfg, bots=True, ui=True))
    assert "http://localhost:8765/" in text and "http://localhost:8766/" in text
    assert text.count("⚠") == 1 and "plafond 0.25 €/jour" in text and text.index("⚠") > text.index("llm")
    assert "⚠" not in "\n".join(describe(resolve_profiles(["hold", "demo"]), cfg, bots=True, ui=True))
    assert "⚠" not in "\n".join(describe(resolve_profiles(["llm"]), cfg, bots=False, ui=True))   # sans bot, pas d'API appelée


# -- supervision -----------------------------------------------------------------------------------------------------

def run_capture(jobs, **kw):
    out = io.StringIO()
    code = run_jobs(jobs, out=out, poll=0.02, grace=3, **kw)
    return code, out.getvalue()


def test_output_is_prefixed_and_aligned():
    code, out = run_capture([py("print('bonjour é')", profile="hold"), py("print('salut')", kind="ui", profile="llm")])
    assert code == 0
    assert "[hold  ] bonjour é" in out and "[llm ui] salut" in out


def test_stderr_is_merged_with_stdout():
    _, out = run_capture([py("import sys; print('oups', file=sys.stderr)", profile="hold")])
    assert "[hold] oups" in out


def test_a_failing_bot_gives_its_exit_code_and_is_announced():
    code, out = run_capture([py("import sys; sys.exit(3)", profile="hold"), py("print('ok')", profile="llm")])
    assert code == 3 and "[hold] s'est arrêté avec le code 3" in out


def test_one_failing_job_does_not_stop_the_others():
    _, out = run_capture([py("import sys; sys.exit(1)", profile="a"), py("import time; time.sleep(0.4); print('encore là')", profile="b")])
    assert "encore là" in out


def test_a_crashing_interface_does_not_change_the_bots_exit_code():
    code, _ = run_capture([py("pass", profile="hold"), py("import sys; sys.exit(9)", kind="ui", profile="hold")])
    assert code == 0


def test_when_bots_finish_the_interface_stays_and_the_user_is_told():
    code, out = run_capture([py("print('fini')", profile="hold"),
                             py("import time; time.sleep(0.7)", kind="ui", profile="hold")])
    assert code == 0 and "Les bots sont arrêtés" in out


def test_no_message_about_stopped_bots_when_there_is_no_interface():
    _, out = run_capture([py("print('fini')", profile="hold")])
    assert "Les bots sont arrêtés" not in out


def test_children_get_unbuffered_utf8_output():
    bare = {k: v for k, v in os.environ.items() if k not in ("PYTHONUNBUFFERED", "PYTHONUTF8")}
    _, out = run_capture([py("import os; print(os.environ['PYTHONUNBUFFERED'], os.environ['PYTHONUTF8'])", profile="h")],
                         env=bare)
    assert "[h] 1 1" in out


# -- arrêt : un seul niveau de politesse par étape --------------------------------------------------------------------

@pytest.mark.skipif(not POSIX, reason="signaux POSIX")
def test_stop_escalates_from_interrupt_to_terminate_to_kill():
    stubborn = subprocess.Popen(
        [sys.executable, "-u", "-c",
         "import signal, time; signal.signal(signal.SIGINT, signal.SIG_IGN); signal.signal(signal.SIGTERM, signal.SIG_IGN);"
         "print('prêt', flush=True); time.sleep(60)"],
        stdout=subprocess.PIPE, text=True, start_new_session=True)
    try:
        assert stubborn.stdout.readline().strip() == "prêt"
        started = time.monotonic()
        launcher._stop([stubborn], grace=0.3, term_grace=0.3)
        assert stubborn.poll() == -signal.SIGKILL and time.monotonic() - started < 5
    finally:
        stubborn.kill()
        stubborn.wait()
        stubborn.stdout.close()


@pytest.mark.skipif(not POSIX, reason="signaux POSIX")
def test_stop_lets_a_well_behaved_child_clean_up():
    child = subprocess.Popen(
        [sys.executable, "-u", "-c",
         "import time\ntry:\n    print('prêt', flush=True); time.sleep(60)\nexcept KeyboardInterrupt:\n    print('nettoyé', flush=True)"],
        stdout=subprocess.PIPE, text=True, start_new_session=True)
    try:
        assert child.stdout.readline().strip() == "prêt"
        launcher._stop([child], grace=5)
        assert child.stdout.read().strip() == "nettoyé" and child.returncode == 0
    finally:
        child.kill()
        child.wait()
        child.stdout.close()


# -- de bout en bout, avec un vrai Ctrl+C -------------------------------------------------------------------------------

@pytest.fixture
def workdir(tmp_path):
    cfg = Path(__file__).resolve().parent.parent / "config.yaml"
    (tmp_path / "config.yaml").write_text(cfg.read_text())
    return tmp_path


def start_up(workdir, *args):
    env = {**os.environ, "PYTHONPATH": SRC, "PYTHONUNBUFFERED": "1"}
    env.pop("ANTHROPIC_API_KEY", None)
    return subprocess.Popen([sys.executable, "-u", "-m", "tradeagent", "up", *args], cwd=workdir, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def read_until(proc, *needles, timeout=20):
    """Lit la sortie jusqu'à avoir vu chaque fragment (dans n'importe quel ordre) ; renvoie tout ce qui a été lu."""
    lines, deadline = [], time.time() + timeout
    while time.time() < deadline:
        line = proc.stdout.readline()
        if not line:
            break
        lines.append(line)
        text = "".join(lines)
        if all(n in text for n in needles):
            return text
    raise AssertionError(f"{needles} jamais vus. Sortie :\n{''.join(lines)}")


def port_is_free(port: int) -> bool:
    import socket

    with socket.socket() as probe:
        return probe.connect_ex(("127.0.0.1", port)) != 0


needs_demo_port = pytest.mark.skipif(not port_is_free(8767), reason="le port 8767 (interface demo) est déjà utilisé")


def assert_clean_shutdown(proc, sig):
    proc.send_signal(sig)
    rest = proc.stdout.read()
    assert proc.wait(timeout=20) == 0
    assert "arrêt demandé" in rest and "état : ALIVE" in rest      # le bot a eu le temps d'afficher son état final


def assert_base_is_free(workdir):
    from tradeagent.lock import InstanceLock

    InstanceLock(workdir / "data" / "paper-demo.db").acquire().release()     # plus aucun processus ne tient la base


@pytest.mark.skipif(not POSIX, reason="signaux POSIX")
@pytest.mark.parametrize("sig", [getattr(signal, n) for n in ("SIGINT", "SIGTERM", "SIGHUP") if hasattr(signal, n)],
                         ids=lambda s: s.name)   # SIGHUP n'existe pas sous Windows : la liste doit rester évaluable
def test_up_stops_the_bot_cleanly_on_a_signal(workdir, sig):
    proc = start_up(workdir, "demo", "--no-ui")
    try:
        read_until(proc, "cycle 1")
        assert_clean_shutdown(proc, sig)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        proc.stdout.close()
    assert_base_is_free(workdir)


@needs_demo_port
@pytest.mark.skipif(not POSIX, reason="signaux POSIX")
def test_up_stops_bot_and_interface_together_on_ctrl_c(workdir):
    proc = start_up(workdir, "demo", "--no-open")
    try:
        read_until(proc, "[demo ui] interface web en lecture seule", "[demo   ] ")
        assert not port_is_free(8767)                                   # l'interface écoute vraiment
        assert_clean_shutdown(proc, signal.SIGINT)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        proc.stdout.close()
    assert port_is_free(8767)                                           # ... et elle a bien rendu le port
    assert_base_is_free(workdir)


@pytest.mark.skipif(not POSIX, reason="signaux POSIX")
def test_up_refuses_a_second_bot_on_the_same_profile(workdir):
    first = start_up(workdir, "demo", "--no-open", "--no-ui")
    try:
        read_until(first, "cycle 1")
        second = start_up(workdir, "demo", "--no-open", "--no-ui")
        try:
            out = second.stdout.read()
            assert second.wait(timeout=20) == 2 and "un autre `tradeagent run`" in out
        finally:
            if second.poll() is None:
                second.kill()
                second.wait()
            second.stdout.close()
        assert first.poll() is None                                  # le premier n'a pas été dérangé
    finally:
        first.send_signal(signal.SIGINT)
        first.stdout.read()
        first.wait(timeout=20)
        first.stdout.close()


@needs_demo_port
@pytest.mark.skipif(not POSIX, reason="signaux POSIX")
def test_up_reports_a_port_already_in_use_and_keeps_the_bot_running(workdir):
    import socket

    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 8767))
    blocker.listen()
    proc = start_up(workdir, "demo", "--no-open")
    try:
        seen = read_until(proc, "cycle 1", "port 8767", "déjà lancée", "[demo ui] s'est arrêté avec le code 2")
        assert "Traceback" not in seen
        assert proc.poll() is None                                      # le bot, lui, continue
    finally:
        blocker.close()
        proc.send_signal(signal.SIGINT)
        proc.stdout.read()
        proc.wait(timeout=20)
        proc.stdout.close()


@pytest.mark.skipif(not POSIX, reason="signaux POSIX")
def test_signal_handlers_are_given_back_after_the_run():
    watched = (signal.SIGTERM, signal.SIGHUP)
    before = {s: signal.getsignal(s) for s in watched}
    run_capture([py("pass")])
    assert {s: signal.getsignal(s) for s in watched} == before

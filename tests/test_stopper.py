import io
import os
import queue
import subprocess
import sys
import time
from pathlib import Path

import pytest

from helpers import ScriptedAgent, default_cfg, make_engine
from tradeagent.lock import InstanceLock
from tradeagent.stopper import StdinStop

SRC = str(Path(__file__).resolve().parent.parent / "src")
ROOT_CONFIG = Path(__file__).resolve().parent.parent / "config.yaml"


class LiveStream:
    """Une entrée standard qu'on alimente ligne par ligne ; `None` = fermeture."""

    def __init__(self):
        self.lines = queue.Queue()

    def __iter__(self):
        return self

    def __next__(self):
        line = self.lines.get(timeout=5)
        if line is None:
            raise StopIteration
        return line


def wait_for(stop, timeout=5.0):
    deadline = time.time() + timeout
    while not stop.requested() and time.time() < deadline:
        time.sleep(0.005)
    return stop.requested()


# -- le signal d'arrêt -----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text", ["stop\n", "STOP\r\n", "  stop  \n", "bruit\nstop\nautre\n"])
def test_the_word_stop_asks_for_a_stop(text):
    stop = StdinStop(io.StringIO(text)).start()
    assert wait_for(stop) and stop.reason == "arrêt demandé"


@pytest.mark.parametrize("text", ["", "bonjour\n", "stopper\nnon stop\n"])
def test_a_closed_stdin_is_also_a_stop_because_nobody_is_watching_anymore(text):
    stop = StdinStop(io.StringIO(text)).start()
    assert wait_for(stop) and stop.reason == "entrée standard fermée"


def test_anything_else_is_ignored_this_is_not_a_command_channel():
    stream = LiveStream()
    stop = StdinStop(stream).start()
    for line in ["reset --yes\n", "resume\n", "buy BTC/EUR 50\n", "\n"]:
        stream.lines.put(line)
    stream.lines.put("stop\n")          # sert de repère : tout ce qui précède a été lu, sans effet
    assert wait_for(stop) and stop.reason == "arrêt demandé"


def test_nothing_is_requested_while_stdin_stays_open_and_silent():
    stream = LiveStream()
    stop = StdinStop(stream).start()
    started = time.monotonic()
    stop.wait(0.05)
    assert not stop.requested() and time.monotonic() - started >= 0.04
    stream.lines.put(None)


def test_waiting_between_cycles_ends_as_soon_as_a_stop_is_requested():
    stop = StdinStop(io.StringIO("stop\n")).start()
    assert wait_for(stop)
    started = time.monotonic()
    stop.wait(60)
    assert time.monotonic() - started < 1


def test_an_unreadable_stdin_stops_the_bot_instead_of_crashing_the_thread():
    class Broken:
        def __iter__(self):
            raise ValueError("I/O operation on closed file")

    stop = StdinStop(Broken()).start()
    assert wait_for(stop) and stop.reason == "entrée standard fermée"


# -- côté moteur : on ne coupe jamais un cycle en deux -------------------------------------------------------------------

def test_engine_checks_for_a_stop_only_between_cycles():
    agent = ScriptedAgent()
    engine, *_ = make_engine(default_cfg(), agent)
    sleeps = []
    answers = iter([False, False, False, False, True])   # avant cycle 1, après, avant cycle 2, après, avant cycle 3
    assert engine.run_forever(sleep=sleeps.append, should_stop=lambda: next(answers)) == 2
    assert agent.calls == 2 and len(sleeps) == 2


def test_engine_does_not_sleep_once_a_stop_is_requested():
    agent = ScriptedAgent()
    engine, *_ = make_engine(default_cfg(), agent)
    sleeps = []
    answers = iter([False, True, True])
    assert engine.run_forever(sleep=sleeps.append, should_stop=lambda: next(answers)) == 1
    assert sleeps == []


def test_engine_without_a_stop_source_behaves_as_before():
    engine, *_ = make_engine(default_cfg(), ScriptedAgent())
    sleeps = []
    assert engine.run_forever(max_cycles=3, sleep=sleeps.append) == 3
    assert len(sleeps) == 2


# -- de bout en bout, comme l'application de bureau (fonctionne aussi sous Windows) ------------------------------------

@pytest.fixture
def workdir(tmp_path):
    (tmp_path / "config.yaml").write_text(ROOT_CONFIG.read_text(encoding="utf-8"), encoding="utf-8")
    return tmp_path


def start_bot(workdir, *extra):
    env = {**os.environ, "PYTHONPATH": SRC, "PYTHONUNBUFFERED": "1", "PYTHONUTF8": "1"}
    env.pop("ANTHROPIC_API_KEY", None)
    return subprocess.Popen([sys.executable, "-u", "-m", "tradeagent", "run", "--profile", "demo", *extra],
                            cwd=workdir, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, encoding="utf-8")


def read_until(proc, needle):
    lines = []
    for line in proc.stdout:
        lines.append(line)
        if needle in line:
            return "".join(lines)
    raise AssertionError(f"{needle!r} jamais vu. Sortie :\n{''.join(lines)}")


def finish(proc):
    try:
        rest = proc.stdout.read()
        return proc.wait(timeout=20), rest
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        proc.stdout.close()


@pytest.mark.parametrize("how, reason", [("word", "arrêt demandé"), ("close", "entrée standard fermée")])
def test_a_running_bot_stops_cleanly_without_waiting_for_the_next_cycle(workdir, how, reason):
    proc = start_bot(workdir, "--cycle-seconds", "600", "--stop-on-stdin")
    read_until(proc, "cycle 1")
    started = time.monotonic()
    if how == "word":
        proc.stdin.write("stop\n")
        proc.stdin.flush()
    else:
        proc.stdin.close()
    code, rest = finish(proc)
    assert code == 0 and time.monotonic() - started < 15       # sans attendre les 600 s du cycle suivant
    assert reason in rest and "état : ALIVE" in rest           # le bot a affiché son état final
    assert "Traceback" not in rest
    InstanceLock(workdir / "data" / "paper-demo.db").acquire().release()   # la base est libérée


def test_without_the_option_stdin_is_never_read(workdir):
    proc = start_bot(workdir, "--cycle-seconds", "0", "--max-cycles", "3")
    proc.stdin.close()                                          # fermée d'entrée : sans l'option, aucun effet
    code, rest = finish(proc)
    assert code == 0 and "cycle 3" in rest and "entrée standard fermée" not in rest

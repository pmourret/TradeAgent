"""`tradeagent up` : bot(s) + interface(s) web dans un seul terminal, arrêtés ensemble par Ctrl+C.

Chaque tâche est un vrai sous-processus `tradeagent run|web --profile X`, exactement ce que tu lancerais à la
main : pas de chemin de code particulier, un bot qui plante n'emporte pas les autres, et chaque ligne de sortie
est préfixée par son profil.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import IO, Callable

from .config import Config, ConfigError
from .llm import require_api_key
from .profiles import Profile, get_profile

POSIX = os.name == "posix"


@dataclass(frozen=True)
class Job:
    profile: str
    kind: str            # "bot" | "ui"
    argv: tuple[str, ...]

    @property
    def tag(self) -> str:
        return self.profile if self.kind == "bot" else f"{self.profile} ui"


def resolve_profiles(names: list[str]) -> list[Profile]:
    """Sans nom : la référence `hold`. `live` et les noms inconnus lèvent ConfigError."""
    seen: dict[str, Profile] = {}
    for name in names or ["hold"]:
        seen.setdefault(name, get_profile(name))
    return list(seen.values())


def preflight(profiles: list[Profile], *, bots: bool, environ=None) -> None:
    """Tout ce qui peut échouer se vérifie AVANT de lancer quoi que ce soit."""
    if bots and any(p.agent == "llm" for p in profiles):
        require_api_key(environ)


def build_jobs(profiles: list[Profile], config: str, *, bots: bool = True, ui: bool = True,
               open_browser: bool = True, python: str | None = None) -> list[Job]:
    base = (python or sys.executable, "-u", "-m", "tradeagent")
    jobs: list[Job] = []
    for p in profiles:
        if bots:
            jobs.append(Job(p.name, "bot", (*base, "run", "--profile", p.name, "--config", config)))
        if ui:
            argv = (*base, "web", "--profile", p.name, "--config", config)
            jobs.append(Job(p.name, "ui", argv + (("--open",) if open_browser else ())))
    return jobs


def describe(profiles: list[Profile], cfg: Config, *, bots: bool, ui: bool) -> list[str]:
    lines = []
    for p in profiles:
        what = " + ".join(x for x in ("bot" if bots else "", f"interface http://localhost:{p.port}/" if ui else "") if x)
        lines.append(f"  {p.name:<5} {what}")
        lines.append(f"        {p.description}")
        if p.costs_money and bots:
            lines.append(f"        ⚠ l'API Anthropic est facturée : plafond {cfg.llm.daily_budget_eur:.2f} €/jour "
                         f"et {cfg.llm.total_budget_eur:.2f} € au total (config.yaml), appliqué par le code.")
    return lines


# -- supervision -------------------------------------------------------------------------------------------------

def _pump(proc: subprocess.Popen, tag: str, width: int, out: IO[str], lock: threading.Lock) -> None:
    assert proc.stdout is not None
    for line in proc.stdout:
        with lock:
            out.write(f"[{tag:<{width}}] {line.rstrip()}\n")
            out.flush()


def _stop(procs: list[subprocess.Popen], grace: float, term_grace: float = 3.0) -> None:
    """Un arrêt propre (le bot affiche son état final), puis TERM, puis KILL."""
    live = [p for p in procs if p.poll() is None]
    for p in live:
        try:
            if POSIX:
                p.send_signal(signal.SIGINT)   # les enfants sont dans leur propre session : un seul SIGINT, le nôtre
            else:
                p.terminate()                  # Windows : Ctrl+C a déjà atteint la console entière
        except OSError:
            pass
    deadline = time.monotonic() + grace
    for p in live:
        try:
            p.wait(max(0.1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            p.terminate()
            try:
                p.wait(term_grace)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()


class _Shutdown(Exception):
    pass


def run_jobs(jobs: list[Job], *, out: IO[str] | None = None, poll: float = 0.25, grace: float = 10.0,
             env: dict | None = None, cwd: str | None = None,
             on_event: Callable[[str], None] | None = None) -> int:
    """Lance les tâches et attend. Retourne 0 si l'utilisateur a arrêté, sinon le pire code de sortie d'un bot."""
    out = out or sys.stdout
    say = on_event or (lambda msg: print(msg, file=out, flush=True))
    width = max((len(j.tag) for j in jobs), default=0)
    child_env = {**(os.environ if env is None else env), "PYTHONUNBUFFERED": "1", "PYTHONUTF8": "1"}
    popen_kwargs: dict = {"start_new_session": True} if POSIX else {}

    previous: dict = {}

    def _on_signal(signum, _frame):
        raise _Shutdown(signal.Signals(signum).name)

    if threading.current_thread() is threading.main_thread():
        for name in ("SIGTERM", "SIGHUP", "SIGBREAK"):
            if hasattr(signal, name):
                previous[getattr(signal, name)] = signal.signal(getattr(signal, name), _on_signal)

    procs: list[subprocess.Popen] = []
    threads: list[threading.Thread] = []
    write_lock = threading.Lock()
    interrupted = False
    bots_done_announced = False
    try:
        for job in jobs:
            proc = subprocess.Popen(list(job.argv), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                                    env=child_env, cwd=cwd, **popen_kwargs)
            procs.append(proc)
            thread = threading.Thread(target=_pump, args=(proc, job.tag, width, out, write_lock), daemon=True)
            thread.start()
            threads.append(thread)

        finished: set[int] = set()
        while True:
            for i, proc in enumerate(procs):
                code = proc.poll()
                if code is not None and i not in finished:
                    finished.add(i)
                    if code != 0:
                        say(f"[{jobs[i].tag:<{width}}] s'est arrêté avec le code {code}")
            if len(finished) == len(procs):
                break
            bots = [i for i, j in enumerate(jobs) if j.kind == "bot"]
            if bots and not bots_done_announced and all(i in finished for i in bots):
                bots_done_announced = True
                say("Les bots sont arrêtés ; l'interface reste ouverte pour consulter l'état final. Ctrl+C pour quitter.")
            time.sleep(poll)
    except (KeyboardInterrupt, _Shutdown) as exc:
        interrupted = True
        say(f"\narrêt demandé{' (' + str(exc) + ')' if str(exc) else ''} : j'arrête proprement…")
    finally:
        try:
            _stop(procs, grace)
        except (KeyboardInterrupt, _Shutdown):   # on insiste : plus de politesse
            for p in procs:
                if p.poll() is None:
                    p.kill()
        for thread in threads:
            thread.join(timeout=2)
        for signum, handler in previous.items():
            signal.signal(signum, handler)

    if interrupted:
        return 0
    return max((p.returncode or 0 for p, j in zip(procs, jobs) if j.kind == "bot"), default=0)

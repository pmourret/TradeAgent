"""Les scripts de lancement ne se testent pas tous ici (les .bat ne tournent que sous Windows), mais on vérifie
ce qui peut dériver sans bruit : qu'ils appellent des commandes qui existent, que chaque .sh a son .bat, et que
`live` refuse partout.
"""
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tradeagent.cli import _parser
from tradeagent.profiles import PROFILES

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
SH = sorted(SCRIPTS.glob("*.sh"))
BAT = sorted(SCRIPTS.glob("*.bat"))
PUBLIC = [p for p in SH if not p.name.startswith("_")]


def subcommands() -> set[str]:
    sub = next(a for a in _parser()._actions if a.dest == "command")
    return set(sub.choices)


def code_lines(path: Path):
    """Lignes exécutables : sans commentaires (# en .sh, rem en .bat)."""
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and not stripped.lower().startswith("rem "):
            yield stripped


def test_there_are_scripts_for_every_use():
    assert {p.stem for p in PUBLIC} == {"setup", "paper", "ui", "start", "status", "live"}


@pytest.mark.parametrize("sh", SH, ids=lambda p: p.name)
def test_every_shell_script_has_a_windows_twin(sh):
    assert sh.with_suffix(".bat").exists()


@pytest.mark.parametrize("bat", BAT, ids=lambda p: p.name)
def test_every_windows_script_has_a_shell_twin(bat):
    assert bat.with_suffix(".sh").exists()


@pytest.mark.parametrize("sh", PUBLIC, ids=lambda p: p.name)
def test_public_shell_scripts_are_executable_and_declare_their_interpreter(sh):
    assert os.access(sh, os.X_OK), "chmod +x manquant : `./scripts/x.sh` échouerait"
    assert sh.read_text().startswith("#!/usr/bin/env sh\n")


@pytest.mark.skipif(shutil.which("sh") is None, reason="pas de sh")
@pytest.mark.parametrize("sh", SH, ids=lambda p: p.name)
def test_shell_scripts_have_valid_syntax(sh):
    assert subprocess.run(["sh", "-n", str(sh)], capture_output=True).returncode == 0


@pytest.mark.parametrize("bat", BAT, ids=lambda p: p.name)
def test_windows_scripts_are_crlf_and_ascii(bat):
    # cmd.exe lit selon la page de code de la console : pas d'accent dans le fichier, et CRLF pour les étiquettes.
    raw = bat.read_bytes()
    raw.decode("ascii")
    assert raw.count(b"\n") == raw.count(b"\r\n") and raw.endswith(b"\r\n")


@pytest.mark.parametrize("script", [*SH, *BAT], ids=lambda p: p.name)
def test_scripts_only_call_commands_that_exist(script):
    valid = subcommands()
    for line in code_lines(script):
        for command in re.findall(r"-m tradeagent (\S+)", line):
            assert command in valid, f"{script.name} appelle `tradeagent {command}`, qui n'existe pas"


@pytest.mark.parametrize("script", [*SH, *BAT], ids=lambda p: p.name)
def test_scripts_never_override_what_the_profile_decides(script):
    text = "\n".join(code_lines(script))
    assert "--agent" not in text and "--feed" not in text and "--yes" not in text and "--force" not in text


@pytest.mark.parametrize("script", [*SH, *BAT], ids=lambda p: p.name)
def test_every_call_goes_through_the_virtualenv_never_the_system_python(script):
    calls = [line for line in code_lines(script) if "-m tradeagent" in line]
    if script.stem not in ("setup", "_common"):
        assert calls, f"{script.name} ne lance rien"
    for line in calls:
        assert re.search(r'"\$PY"|"%PY%"|\.venv', line), f"{script.name} : {line}"


def test_scripts_advertise_the_real_ports():
    for ext in ("sh", "bat"):
        text = (SCRIPTS / f"ui.{ext}").read_text()
        for p in PROFILES.values():
            assert f"{p.name} {p.port}" in text, f"ui.{ext} annonce un mauvais port pour {p.name}"


def test_setup_checks_the_python_version_the_project_requires():
    pyproject = (ROOT / "pyproject.toml").read_text()
    minimum = re.search(r'requires-python\s*=\s*">=(\d+)\.(\d+)"', pyproject).groups()
    for ext in ("sh", "bat"):
        assert f"({minimum[0]}, {minimum[1]})" in (SCRIPTS / f"setup.{ext}").read_text()


# -- le mode réel ------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("script", [SCRIPTS / "live.sh", SCRIPTS / "live.bat"], ids=lambda p: p.name)
def test_live_scripts_only_ever_ask_for_the_refused_profile(script):
    calls = [line for line in code_lines(script) if "tradeagent" in line]
    assert calls and all("--profile live" in line for line in calls)
    assert not any(w in " ".join(calls) for w in (" up ", " web ", " status ", "--profile hold", "--profile llm"))


@pytest.mark.skipif(shutil.which("sh") is None, reason="pas de sh")
def test_live_script_refuses_with_an_explanation_and_creates_nothing(tmp_path):
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    before = set(ROOT.glob("data/*"))
    done = subprocess.run(["sh", str(SCRIPTS / "live.sh")], capture_output=True, text=True, env=env, cwd=tmp_path)
    assert done.returncode == 2
    assert "n'existe pas encore" in done.stderr
    assert set(ROOT.glob("data/*")) == before


@pytest.mark.skipif(shutil.which("sh") is None, reason="pas de sh")
def test_other_scripts_tell_you_to_run_setup_when_there_is_no_environment(tmp_path):
    # Une copie du dossier scripts/ sans .venv à côté : le message doit dire quoi faire, pas planter.
    project = tmp_path / "proj"
    shutil.copytree(SCRIPTS, project / "scripts")
    for name in ("paper", "ui", "start", "status"):
        done = subprocess.run(["sh", str(project / "scripts" / f"{name}.sh")], capture_output=True, text=True)
        assert done.returncode == 1 and "scripts/setup.sh" in done.stderr, name
    done = subprocess.run(["sh", str(project / "scripts" / "live.sh")], capture_output=True, text=True)
    assert done.returncode == 2 and "n'existe pas encore" in done.stderr

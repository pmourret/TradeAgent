import os
import subprocess
import sys
import time

import pytest

from tradeagent.config import ConfigError
from tradeagent.lock import InstanceLock


def test_second_holder_is_refused_until_the_first_lets_go(tmp_path):
    db = tmp_path / "agent.db"
    first = InstanceLock(db).acquire()
    with pytest.raises(ConfigError, match="un autre `tradeagent run`"):
        InstanceLock(db).acquire()
    first.release()
    InstanceLock(db).acquire().release()


def test_lock_files_are_per_database(tmp_path):
    with InstanceLock(tmp_path / "paper-hold.db"), InstanceLock(tmp_path / "paper-llm.db"):
        pass


def test_the_message_does_not_blame_a_stale_file(tmp_path):
    db = tmp_path / "agent.db"
    with InstanceLock(db):
        with pytest.raises(ConfigError) as err:
            InstanceLock(db).acquire()
    assert str(db) in str(err.value) and ".lock" not in str(err.value)


def test_pid_is_written_for_debugging(tmp_path):
    lock = InstanceLock(tmp_path / "agent.db").acquire()
    assert lock.path.read_text().strip() == str(os.getpid())
    lock.release()


def test_creates_the_data_directory(tmp_path):
    with InstanceLock(tmp_path / "data" / "nested" / "agent.db") as lock:
        assert lock.path.parent.is_dir()


def test_release_is_idempotent(tmp_path):
    lock = InstanceLock(tmp_path / "agent.db").acquire()
    lock.release()
    lock.release()


def test_lock_dies_with_its_process_so_a_crash_never_blocks_the_next_start(tmp_path):
    db = tmp_path / "agent.db"
    code = ("import sys, time; from tradeagent.lock import InstanceLock; "
            "lock = InstanceLock(sys.argv[1]).acquire(); print('locked', flush=True); time.sleep(60)")
    proc = subprocess.Popen([sys.executable, "-c", code, str(db)], stdout=subprocess.PIPE, text=True,
                            env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)})
    try:
        assert proc.stdout.readline().strip() == "locked"
        with pytest.raises(ConfigError):
            InstanceLock(db).acquire()
        proc.kill()                      # arrêt brutal : aucun nettoyage possible
        proc.wait(timeout=10)
        deadline = time.time() + 5
        while True:
            try:
                InstanceLock(db).acquire().release()
                break
            except ConfigError:
                assert time.time() < deadline, "le verrou aurait dû disparaître avec le processus"
                time.sleep(0.05)
    finally:
        proc.kill()
        proc.wait(timeout=10)
        proc.stdout.close()

"""The systemd templates under deploy/. No network, no systemd.

Every template is filled in by replacing CLONE with the clone's absolute path.
The check fills it with a path that holds a space and asks whether ExecStart
still names whole paths, because an unquoted one splits at the space and the
unit fails with status=203/EXEC on every run.
"""

import configparser
import shlex
from pathlib import Path

DEPLOY = Path(__file__).resolve().parent.parent / "deploy"
SPACED = "/home/someone/My Clone"


def _units():
    return sorted(DEPLOY.glob("*.service")) + sorted(DEPLOY.glob("*.timer"))


def _read(path, clone=SPACED):
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.optionxform = str
    parser.read_string(path.read_text().replace("CLONE", clone))
    return parser


def _splits_a_path(exec_start, clone=SPACED):
    """True when systemd's word split would cut the clone path in two."""
    head = clone.split(" ")[0]
    return any(head in word and clone not in word for word in shlex.split(exec_start))


def test_there_are_units_to_check():
    names = {p.name for p in _units()}
    assert {"ibkr-http.service", "lacuna-ibkr-update.service",
            "lacuna-ibkr-update.timer"} <= names


def test_no_exec_start_splits_a_clone_path_with_a_space():
    for path in DEPLOY.glob("*.service"):
        service = _read(path)["Service"]
        assert not _splits_a_path(service["ExecStart"]), path.name


def test_the_check_catches_the_laptop_bug():
    clone = "/home/niclom/repos/Lacuna Analysis"
    bad = f"{clone}/.venv/bin/python services/ibkr-data/cli.py update"
    assert _splits_a_path(bad, clone)
    assert not _splits_a_path(f'"{clone}/.venv/bin/python" services/ibkr-data/cli.py update', clone)


def test_the_update_timer_runs_the_update_after_the_close():
    import cli
    timer = _read(DEPLOY / "lacuna-ibkr-update.timer")["Timer"]
    assert timer["Unit"] == "lacuna-ibkr-update.service"
    assert timer["OnCalendar"].endswith("America/New_York")
    hour = int(timer["OnCalendar"].split()[1].split(":")[0])
    assert hour >= cli.SESSION_FINAL_HOUR
    service = _read(DEPLOY / "lacuna-ibkr-update.service")["Service"]
    assert shlex.split(service["ExecStart"])[1:] == [
        f"{SPACED}/services/ibkr-data/cli.py", "update"]
    assert service["Type"] == "oneshot"

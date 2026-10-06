"""Exercise the real updater with isolated command boundaries, never a live checkout."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "deploy/update.sh"
_BOUNDARY = r'''
import json
import os
import sys
from pathlib import Path

name = Path(sys.argv[0]).name
args = sys.argv[1:]
root = Path(os.environ["APP_DIR"])
head = root / "head"
if name == "sudo":
    os.execvp(args[2], args[2:])
elif name == "git":
    if args[:2] == ["rev-parse", "HEAD"]:
        print(head.read_text().strip())
    elif args[:2] == ["rev-parse", "origin/main"]:
        print("new")
    elif args and args[0] == "reset":
        revision = args[-1]
        head.write_text(revision)
        (root / "config/watchlists.yaml").write_text("new repository seed")
        with (root / "resets").open("a") as stream:
            stream.write(revision + "\n")
elif name == "pip":
    if head.read_text() == "new" and os.environ.get("FAIL_INSTALL") == "1":
        raise SystemExit(1)
elif name == "curl":
    endpoint = args[-1].rsplit("/", 1)[-1]
    revision = head.read_text()
    with (root / "http_requests").open("a") as stream:
        stream.write(revision + " " + endpoint + "\n")
    status = 200
    if endpoint == "ready":
        if os.environ.get("BASELINE_UNREADY") == "1":
            status = 503
        elif revision == "old" and os.environ.get("LEGACY_BASELINE") == "1":
            status = 404
        elif revision == "new":
            status = int(os.environ.get("CANDIDATE_STATUS", "200"))
    elif endpoint != "health":
        raise SystemExit("unexpected endpoint")
    body = json.dumps({"status": "ok" if status == 200 else "not_ready"})
    if "-o" in args:
        Path(args[args.index("-o") + 1]).write_text(body)
        print(status, end="")
    else:
        print(body)
elif name == "systemctl":
    with (root / "restarts").open("a") as stream:
        stream.write(head.read_text() + "\n")
'''


@pytest.fixture
def updater_env(tmp_path: Path) -> dict[str, str]:
    app = tmp_path / "app with spaces"
    (app / "config").mkdir(parents=True)
    (app / "config/watchlists.yaml").write_text("user edited watchlist")
    (app / "head").write_text("old")
    (app / ".env").write_text("EDIT_TOKEN=not-logged\nWATCHLIST_PATH=./config/watchlists.yaml\n")
    (app / ".env").chmod(0o644)
    binaries = tmp_path / "bin"
    binaries.mkdir()
    (binaries / "python3").symlink_to(sys.executable)
    for name in ("sudo", "git", "curl", "systemctl", "sleep", "flock"):
        path = binaries / name
        path.write_text("#!/usr/bin/env python3\n" + _BOUNDARY)
        path.chmod(0o755)
    pip = app / ".venv/bin/pip"
    pip.parent.mkdir(parents=True)
    pip.write_text("#!/usr/bin/env python3\n" + _BOUNDARY)
    pip.chmod(0o755)
    return {
        **os.environ,
        "PATH": str(binaries) + os.pathsep + os.environ["PATH"],
        "APP_DIR": str(app),
        "UPDATE_LOCK": str(tmp_path / "update.lock"),
    }


def _run(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(_SCRIPT)], env=env, capture_output=True, text=True, timeout=60, check=False
    )


def test_first_update_seeds_ready_rollback_and_preserves_legacy_edits(
    updater_env: dict[str, str],
) -> None:
    updater_env["FAIL_INSTALL"] = "1"
    result = _run(updater_env)
    app = Path(updater_env["APP_DIR"])
    assert result.returncode == 1
    assert (app / ".deployed-rev").read_text().strip() == "old"
    assert (app / "head").read_text() == "old"
    assert (app / "resets").read_text().splitlines() == ["new", "old"]
    assert (app / "data/watchlists.yaml").read_text() == "user edited watchlist"
    assert "WATCHLIST_PATH=./data/watchlists.yaml" in (app / ".env").read_text()
    assert (app / ".env").stat().st_mode & 0o777 == 0o600
    assert "not-logged" not in result.stdout + result.stderr


def test_successful_update_never_overwrites_existing_runtime_watchlist(
    updater_env: dict[str, str],
) -> None:
    app = Path(updater_env["APP_DIR"])
    (app / "data").mkdir()
    (app / "data/watchlists.yaml").write_text("new runtime edits")
    result = _run(updater_env)
    assert result.returncode == 0
    assert (app / ".deployed-rev").read_text().strip() == "new"
    assert (app / "data/watchlists.yaml").read_text() == "new runtime edits"


def test_missing_marker_refuses_reset_without_ready_baseline(updater_env: dict[str, str]) -> None:
    updater_env["BASELINE_UNREADY"] = "1"
    result = _run(updater_env)
    app = Path(updater_env["APP_DIR"])
    assert result.returncode == 1
    assert not (app / ".deployed-rev").exists()
    assert not (app / "resets").exists()
    assert (app / "config/watchlists.yaml").read_text() == "user edited watchlist"


@pytest.mark.parametrize("candidate_status", ["503", "404"])
def test_healthy_markerless_legacy_rolls_back_failed_strict_candidate(
    updater_env: dict[str, str], candidate_status: str
) -> None:
    updater_env["LEGACY_BASELINE"] = "1"
    updater_env["CANDIDATE_STATUS"] = candidate_status
    result = _run(updater_env)
    app = Path(updater_env["APP_DIR"])
    assert result.returncode == 1
    assert (app / ".deployed-rev").read_text().strip() == "old"
    assert (app / "head").read_text() == "old"
    assert (app / "resets").read_text().splitlines() == ["new", "old"]
    assert (app / "restarts").read_text().splitlines() == ["new", "old"]
    requests = (app / "http_requests").read_text().splitlines()
    assert "old health" in requests
    assert "new health" not in requests
    assert "Rollback failed" not in result.stderr


def test_present_but_unready_baseline_cannot_use_healthy_legacy_endpoint(
    updater_env: dict[str, str],
) -> None:
    updater_env["BASELINE_UNREADY"] = "1"
    updater_env["LEGACY_BASELINE"] = "1"
    result = _run(updater_env)
    app = Path(updater_env["APP_DIR"])
    assert result.returncode == 1
    assert not (app / ".deployed-rev").exists()
    assert not (app / "resets").exists()
    assert "old health" not in (app / "http_requests").read_text().splitlines()

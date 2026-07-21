"""Unit tests for scripts/vault_report_uploader.py (droplet auto-uploader).

The script is not a package module, so it is spec-loaded straight from the
scripts/ directory. run() binds its config/state path defaults at import
time, so tests rebind the functions' ``__defaults__`` to tmp paths instead
of patching the CONFIG_PATH/STATE_PATH globals (which run() never re-reads).
"""

import hashlib
import importlib.util
import json
import os
import time
import urllib.error
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "vault_report_uploader.py"
_SPEC = importlib.util.spec_from_file_location("vault_report_uploader", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
uploader = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(uploader)

_TODAY_TEXT = date.today().isoformat()
_YESTERDAY_TEXT = (date.today() - timedelta(days=1)).isoformat()
_STALE_TEXT = (date.today() - timedelta(days=40)).isoformat()


def _sha(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _write_report(vault: Path, name: str, body: str, age_seconds: float = 120.0) -> Path:
    """Write a vault file with an mtime safely older than the settle window."""
    path = vault / name
    path.write_text(body, encoding="utf-8")
    stamp = time.time() - age_seconds
    os.utime(path, (stamp, stamp))
    return path


def _wire_paths(monkeypatch: pytest.MonkeyPatch, config_path: Path, state_path: Path) -> None:
    monkeypatch.setattr(uploader.load_config, "__defaults__", (config_path,))
    monkeypatch.setattr(uploader.load_state, "__defaults__", (state_path,))
    monkeypatch.setattr(uploader.save_state, "__defaults__", (state_path,))


class _PostRecorder:
    """Stands in for the network boundary; records outgoing report payloads."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str, str, str]] = []
        self.fail_times = 0

    def __call__(
        self,
        base_url: str,
        token: str,
        title: str,
        date_text: str,
        body: str,
    ) -> dict[str, object]:
        self.calls.append((base_url, token, title, date_text, body))
        if self.fail_times > 0:
            self.fail_times -= 1
            raise urllib.error.URLError("connection refused")
        return {"id": 7, "slug": "brief"}


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    vault = tmp_path / "vault"
    vault.mkdir()
    config_path = tmp_path / "uploader.env"
    config_path.write_text(
        "BOARD_URL=https://board.test\n"
        "EDIT_TOKEN=sekrit\n"
        f"VAULT_DIR={vault}\n"
        "MAX_AGE_DAYS=30\n"
        "REPORT_TITLES=Morning Brief, Empty Shell, Wrap\n",
        encoding="utf-8",
    )
    state_path = tmp_path / "state" / "vault-uploads.json"
    _wire_paths(monkeypatch, config_path, state_path)
    post = _PostRecorder()
    monkeypatch.setattr(uploader, "post_report", post)
    return SimpleNamespace(vault=vault, config=config_path, state=state_path, post=post)


def _read_state(env: SimpleNamespace) -> dict[str, str]:
    return cast(dict[str, str], json.loads(env.state.read_text(encoding="utf-8")))


# --- parse_report_name -------------------------------------------------------


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        pytest.param(
            "2026-07-10 Biotech Pharma Brief.md",
            ("2026-07-10", "Biotech Pharma Brief"),
            id="dated-title",
        ),
        pytest.param(
            "2026-07-10 v2.0 Update.md",
            ("2026-07-10", "v2.0 Update"),
            id="dot-in-title-greedy-to-final-md",
        ),
        pytest.param(
            "2026-07-10  Padded Title .md",
            ("2026-07-10", "Padded Title"),
            id="title-whitespace-stripped",
        ),
        pytest.param("2026-01-01  .md", None, id="blank-title-rejected"),
        pytest.param("Biotech Brief.md", None, id="undated-rejected"),
        pytest.param("2026-02-30 Ghost.md", None, id="impossible-day-rejected"),
        pytest.param("2026-13-01 Bad Month.md", None, id="impossible-month-rejected"),
        pytest.param("2026-7-1 Loose Date.md", None, id="unpadded-date-rejected"),
        pytest.param("2026-07-10 Notes.txt", None, id="non-md-rejected"),
        pytest.param("2026-07-10.md", None, id="date-only-rejected"),
    ],
)
def test_parse_report_name(filename: str, expected: tuple[str, str] | None) -> None:
    assert uploader.parse_report_name(filename) == expected


# --- within_age --------------------------------------------------------------


@pytest.mark.parametrize(
    ("date_text", "max_age_days", "expected"),
    [
        pytest.param("2026-07-10", 30, True, id="same-day"),
        pytest.param("2026-06-10", 30, True, id="boundary-day-inclusive"),
        pytest.param("2026-06-09", 30, False, id="one-past-boundary"),
        pytest.param("2026-07-12", 30, True, id="future-within-skew-tolerance"),
        pytest.param("2026-07-13", 30, False, id="future-beyond-skew-tolerance"),
        pytest.param("2026-07-10", 0, True, id="zero-window-same-day"),
        pytest.param("2026-07-09", 0, False, id="zero-window-yesterday"),
    ],
)
def test_within_age(date_text: str, max_age_days: int, expected: bool) -> None:
    assert uploader.within_age(date_text, max_age_days, today=date(2026, 7, 10)) is expected


# --- scan_vault ---------------------------------------------------------------


def test_scan_vault_filters_and_sorts(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    # Created out of name order on purpose; output must be name-sorted.
    zulu = _write_report(vault, f"{_TODAY_TEXT} Zulu.md", "z")
    alpha = _write_report(vault, f"{_TODAY_TEXT} Alpha.md", "a")
    earlier = _write_report(vault, f"{_YESTERDAY_TEXT} Wrap.md", "w")
    _write_report(vault, f"{_STALE_TEXT} Ancient.md", "too old")
    _write_report(vault, "untitled.md", "undated")
    _write_report(vault, "notes.txt", "wrong extension")
    (vault / f"{_TODAY_TEXT} Directory.md").mkdir()  # dated dir must not match
    nested = vault / "nested"
    nested.mkdir()
    _write_report(nested, f"{_TODAY_TEXT} Buried.md", "not in vault root")

    assert uploader.scan_vault(vault, max_age_days=30, allowed_titles=None) == [
        (earlier, _YESTERDAY_TEXT, "Wrap"),
        (alpha, _TODAY_TEXT, "Alpha"),
        (zulu, _TODAY_TEXT, "Zulu"),
    ]


def test_scan_vault_title_allowlist_is_case_insensitive(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    brief = _write_report(vault, f"{_TODAY_TEXT} Biotech Pharma Brief.md", "cron output")
    _write_report(vault, f"{_TODAY_TEXT} SEC Counterparty Relationship Map.md", "manual note")

    allowed = uploader.parse_title_allowlist("biotech pharma brief")
    assert uploader.scan_vault(vault, max_age_days=30, allowed_titles=allowed) == [
        (brief, _TODAY_TEXT, "Biotech Pharma Brief"),
    ]


# --- parse_title_allowlist ------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Biotech Pharma Brief", {"biotech pharma brief"}),
        ("A, B ,c", {"a", "b", "c"}),
        ("", set()),
        (" , ", set()),
        ("*", None),
        ("A, *", None),
    ],
)
def test_parse_title_allowlist(value: str, expected: set[str] | None) -> None:
    assert uploader.parse_title_allowlist(value) == expected


# --- content_hash -------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "digest"),
    [
        pytest.param(
            "hello",
            "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824",
            id="ascii-known-vector",
        ),
        pytest.param(
            "café",
            "850f7dc43910ff890f8879c0ed26fe697c93a067ad93a7d50f466a7028a9bf4e",
            id="utf8-known-vector",
        ),
    ],
)
def test_content_hash_is_sha256_of_utf8(body: str, digest: str) -> None:
    assert uploader.content_hash(body) == digest


# --- state persistence ---------------------------------------------------------


def test_state_roundtrip_creates_parents_and_leaves_no_temp(tmp_path: Path) -> None:
    state_path = tmp_path / "deep" / "nested" / "uploads.json"
    saved = {"2026-07-10 Brief.md": "deadbeef", "2026-07-09 Wrap.md": "cafe"}
    uploader.save_state(saved, state_path)
    assert uploader.load_state(state_path) == saved
    assert [p.name for p in state_path.parent.iterdir()] == [state_path.name]


def test_save_state_overwrites_previous_contents(tmp_path: Path) -> None:
    state_path = tmp_path / "uploads.json"
    uploader.save_state({"a.md": "1"}, state_path)
    uploader.save_state({"b.md": "2"}, state_path)
    assert uploader.load_state(state_path) == {"b.md": "2"}


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        pytest.param("{not json", {}, id="corrupt-json"),
        pytest.param("[1, 2]", {}, id="non-dict-json"),
        pytest.param('{"x.md": 5}', {"x.md": "5"}, id="values-coerced-to-str"),
    ],
)
def test_load_state_tolerates_bad_files(
    tmp_path: Path,
    content: str,
    expected: dict[str, str],
) -> None:
    state_path = tmp_path / "uploads.json"
    state_path.write_text(content, encoding="utf-8")
    assert uploader.load_state(state_path) == expected


def test_load_state_missing_file_returns_empty(tmp_path: Path) -> None:
    assert uploader.load_state(tmp_path / "absent.json") == {}

def test_prune_state_drops_only_expired_dated_reports() -> None:
    today = date(2026, 7, 10)
    state = {
        "2026-06-09 Old Brief.md": "old",
        "2026-06-10 Boundary Brief.md": "boundary",
        "2026-07-10 Current Brief.md": "current",
        "2026-07-12 Future Brief.md": "future",
        "manual-note.md": "unknown",
    }

    assert uploader.prune_state(state, 30, today=today) == {
        "2026-06-10 Boundary Brief.md": "boundary",
        "2026-07-10 Current Brief.md": "current",
        "2026-07-12 Future Brief.md": "future",
        "manual-note.md": "unknown",
    }


# --- load_config ---------------------------------------------------------------


def test_load_config_parses_key_values_and_skips_noise(tmp_path: Path) -> None:
    config_path = tmp_path / "uploader.env"
    config_path.write_text(
        "# deployment settings\n"
        "\n"
        "  BOARD_URL = http://board.test:8787  \n"
        "EDIT_TOKEN=abc=def\n"
        "this line has no equals sign\n"
        "MAX_AGE_DAYS=7\n",
        encoding="utf-8",
    )
    assert uploader.load_config(config_path) == {
        "BOARD_URL": "http://board.test:8787",
        "EDIT_TOKEN": "abc=def",
        "MAX_AGE_DAYS": "7",
    }


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://board.example", True),
        ("http://localhost:8787", True),
        ("http://127.0.0.1:8787", True),
        ("http://board.example:8787", False),
        ("file:///tmp/report", False),
    ],
)
def test_secure_board_url_policy(url: str, expected: bool) -> None:
    assert uploader._is_secure_board_url(url) is expected


def test_load_config_missing_file_returns_empty(tmp_path: Path) -> None:
    assert uploader.load_config(tmp_path / "absent.env") == {}


# --- run() ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("config_body", "case"),
    [
        pytest.param(None, "config file missing", id="no-config-file"),
        pytest.param("EDIT_TOKEN=sekrit\n", "no BOARD_URL", id="missing-board-url"),
        pytest.param("BOARD_URL=http://board.test:8787\n", "no EDIT_TOKEN", id="missing-token"),
    ],
)
def test_run_exits_2_on_incomplete_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config_body: str | None, case: str
) -> None:
    config_path = tmp_path / "uploader.env"
    if config_body is not None:
        config_path.write_text(config_body, encoding="utf-8")
    state_path = tmp_path / "uploads.json"
    _wire_paths(monkeypatch, config_path, state_path)
    post = _PostRecorder()
    monkeypatch.setattr(uploader, "post_report", post)

    assert uploader.run([]) == 2, case
    assert post.calls == []
    assert not state_path.exists()


def test_run_exits_2_when_vault_dir_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "uploader.env"
    config_path.write_text(
        "BOARD_URL=https://board.test\n"
        "EDIT_TOKEN=sekrit\n"
        f"VAULT_DIR={tmp_path / 'no-such-vault'}\n",
        encoding="utf-8",
    )
    state_path = tmp_path / "uploads.json"
    _wire_paths(monkeypatch, config_path, state_path)
    post = _PostRecorder()
    monkeypatch.setattr(uploader, "post_report", post)

    assert uploader.run([]) == 2
    assert post.calls == []
    assert not state_path.exists()


def test_baseline_records_hashes_without_uploading(env: SimpleNamespace) -> None:
    _write_report(env.vault, f"{_TODAY_TEXT} Morning Brief.md", "morning body")
    _write_report(env.vault, f"{_YESTERDAY_TEXT} Wrap.md", "wrap body")

    assert uploader.run(["--baseline"]) == 0
    assert env.post.calls == []
    assert _read_state(env) == {
        f"{_TODAY_TEXT} Morning Brief.md": _sha("morning body"),
        f"{_YESTERDAY_TEXT} Wrap.md": _sha("wrap body"),
    }


def test_burst_of_fresh_reports_settles_once(
    env: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env.config.write_text(
        env.config.read_text(encoding="utf-8").replace(
            "REPORT_TITLES=Morning Brief, Empty Shell, Wrap",
            "REPORT_TITLES=*",
        ),
        encoding="utf-8",
    )
    clock = [time.time()]
    paths = [
        env.vault / f"{_TODAY_TEXT} Brief {index}.md"
        for index in range(4)
    ]
    for path in paths:
        path.write_text(f"body {path.stem}", encoding="utf-8")
        os.utime(path, (clock[0], clock[0]))

    sleeps: list[float] = []

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr(uploader.time, "time", lambda: clock[0])
    monkeypatch.setattr(uploader.time, "sleep", fake_sleep)

    assert uploader.run([]) == 0
    assert sleeps == [uploader.SETTLE_SECONDS]
    assert len(env.post.calls) == 4


def test_partially_aged_report_sleeps_only_remaining_delta(
    env: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = time.time()
    path = env.vault / f"{_TODAY_TEXT} Morning Brief.md"
    path.write_text("fresh body", encoding="utf-8")
    os.utime(path, (clock - 1.0, clock - 1.0))
    sleeps: list[float] = []
    monkeypatch.setattr(uploader.time, "time", lambda: clock)
    monkeypatch.setattr(uploader.time, "sleep", sleeps.append)

    assert uploader.run([]) == 0
    assert sleeps == [uploader.SETTLE_SECONDS - 1.0]


def test_new_file_uploads_once_then_skips(env: SimpleNamespace) -> None:
    body = "# Morning\n\nRotation continues.\n"
    name = f"{_TODAY_TEXT} Morning Brief.md"
    _write_report(env.vault, name, body)
    _write_report(env.vault, f"{_STALE_TEXT} Ancient.md", "past the age window")

    assert uploader.run([]) == 0
    assert env.post.calls == [
        ("https://board.test", "sekrit", "Morning Brief", _TODAY_TEXT, body)
    ]
    assert _read_state(env) == {name: _sha(body)}

    assert uploader.run([]) == 0  # second pass: hash matches, nothing re-sent
    assert len(env.post.calls) == 1


def test_changed_content_reuploads(env: SimpleNamespace) -> None:
    name = f"{_TODAY_TEXT} Morning Brief.md"
    _write_report(env.vault, name, "draft one")
    assert uploader.run([]) == 0
    _write_report(env.vault, name, "draft two, revised")

    assert uploader.run([]) == 0
    assert [call[4] for call in env.post.calls] == ["draft one", "draft two, revised"]
    assert _read_state(env)[name] == _sha("draft two, revised")


def test_failed_post_leaves_hash_unrecorded_and_retries(env: SimpleNamespace) -> None:
    name = f"{_TODAY_TEXT} Morning Brief.md"
    body = "flaky network pass"
    _write_report(env.vault, name, body)
    env.post.fail_times = 1

    assert uploader.run([]) == 1
    assert name not in _read_state(env)

    assert uploader.run([]) == 0  # boundary healed: same file retried
    assert len(env.post.calls) == 2
    assert _read_state(env) == {name: _sha(body)}


def test_empty_body_files_are_skipped(env: SimpleNamespace) -> None:
    _write_report(env.vault, f"{_TODAY_TEXT} Empty Shell.md", "   \n\n")

    assert uploader.run([]) == 0
    assert env.post.calls == []
    assert _read_state(env) == {}  # no hash recorded; file re-checked next pass


def test_dry_run_changes_nothing_on_disk(env: SimpleNamespace) -> None:
    env.state.parent.mkdir(parents=True)
    seeded = json.dumps({"2020-01-01 Old.md": "abc"})
    env.state.write_text(seeded, encoding="utf-8")
    _write_report(env.vault, f"{_TODAY_TEXT} Morning Brief.md", "would upload")

    assert uploader.run(["--dry-run"]) == 0
    assert env.post.calls == []
    assert env.state.read_text(encoding="utf-8") == seeded


def test_run_skips_titles_outside_allowlist(env: SimpleNamespace) -> None:
    _write_report(env.vault, f"{_TODAY_TEXT} Morning Brief.md", "cron output")
    _write_report(env.vault, f"{_TODAY_TEXT} World Models.md", "manual research note")

    assert uploader.run([]) == 0
    assert [(call[2], call[4]) for call in env.post.calls] == [("Morning Brief", "cron output")]
    # No hash recorded for the skipped note: allowlisting it later uploads it.
    assert set(_read_state(env)) == {f"{_TODAY_TEXT} Morning Brief.md"}


def test_run_defaults_allowlist_to_known_cron_titles(env: SimpleNamespace) -> None:
    kept = [
        ln
        for ln in env.config.read_text(encoding="utf-8").splitlines()
        if not ln.startswith("REPORT_TITLES")
    ]
    env.config.write_text("".join(line + "\n" for line in kept), encoding="utf-8")
    body = (
        f"---\ndate: {_TODAY_TEXT}\ntype: research\ntags: [biotech]\n"
        "status: draft\n---\n## Biotech Pharma Brief\n"
        "---FEED-STATUS---\n## Feed Status\n"
    )
    _write_report(env.vault, f"{_TODAY_TEXT} Biotech Pharma Brief.md", body)
    _write_report(env.vault, f"{_TODAY_TEXT} Morning Brief.md", "not a known cron title")

    assert uploader.run([]) == 0
    assert [call[2] for call in env.post.calls] == ["Biotech Pharma Brief"]


def test_run_star_allowlist_uploads_everything(env: SimpleNamespace) -> None:
    env.config.write_text(
        env.config.read_text(encoding="utf-8").replace(
            "REPORT_TITLES=Morning Brief, Empty Shell", "REPORT_TITLES=*"
        ),
        encoding="utf-8",
    )
    _write_report(env.vault, f"{_TODAY_TEXT} Anything Goes.md", "body")

    assert uploader.run([]) == 0
    assert [call[2] for call in env.post.calls] == ["Anything Goes"]


def test_run_malformed_max_age_days_falls_back_to_30(env: SimpleNamespace) -> None:
    env.config.write_text(
        env.config.read_text(encoding="utf-8").replace(
            "MAX_AGE_DAYS=30", "MAX_AGE_DAYS=30 days"
        ),
        encoding="utf-8",
    )
    _write_report(env.vault, f"{_TODAY_TEXT} Morning Brief.md", "fresh body")
    _write_report(env.vault, f"{_STALE_TEXT} Morning Brief.md", "stale body")

    # Malformed value must not crash the run; the 30-day default still filters.
    assert uploader.run([]) == 0
    assert [call[2] for call in env.post.calls] == ["Morning Brief"]
    assert set(_read_state(env)) == {f"{_TODAY_TEXT} Morning Brief.md"}


@pytest.mark.parametrize(
    ("title", "date_text", "body", "expected"),
    [
        (
            "Macro Tape Brief",
            "2026-07-21",
            "legacy body without a contract",
            None,
        ),
        ("Manual Note", "2026-07-22", "anything", None),
        (
            "Macro Tape Brief",
            "2026-07-22",
            "Now save to the Obsidian vault",
            "missing YAML frontmatter",
        ),
        (
            "US Asia Close",
            "2026-07-22",
            "---\ndate: 2026-07-22\ntype: research\ntags: [macro]\n"
            "status: draft\n---\n## Executive Tape Read\n## Feed Status\n",
            "report missing \"Today's Calendar\"",
        ),
        (
            "AI Semis Morning Brief",
            "2026-07-22",
            "---\ndate: 2026-07-22\ntype: research\ntags: [ai]\n"
            "status: draft\n---\n---FEED-STATUS---\n## Feed Status\n"
            "---FEED-STATUS---\n",
            "report must contain exactly one FEED-STATUS delimiter",
        ),
        (
            "Macro Tape Brief",
            "2026-07-22",
            "---\ndate: 2026-07-22\ntype: research\ntags: [macro]\n"
            "status: draft\n---\n## Macro Tape Brief\n## Feed Status\n",
            None,
        ),
        (
            "Fringe Corner",
            "2026-07-22",
            "---\ndate: 2026-07-22\ntype: research\ntags: [fringe]\n"
            "status: draft\n---\n## Fringe Corner\n"
            "- HOLD LONG AMD - thesis [horizon: 2w]\n## Rationale\nEvidence.\n",
            None,
        ),
    ],
)
def test_validate_report_body_contract(
    title: str, date_text: str, body: str, expected: str | None
) -> None:
    assert uploader.validate_report_body(title, date_text, body) == expected


def test_invalid_current_cron_report_is_not_uploaded(env: SimpleNamespace) -> None:
    env.config.write_text(
        env.config.read_text(encoding="utf-8").replace(
            "REPORT_TITLES=Morning Brief, Empty Shell, Wrap",
            "REPORT_TITLES=Macro Tape Brief",
        ),
        encoding="utf-8",
    )
    name = "2026-07-22 Macro Tape Brief.md"
    _write_report(env.vault, name, "Now save to the Obsidian vault")

    assert uploader.run([]) == 1
    assert env.post.calls == []
    assert _read_state(env) == {}
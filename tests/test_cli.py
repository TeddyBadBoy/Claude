from __future__ import annotations

from pathlib import Path

import pytest
from helpers import MEMBERS

from drivezip.cli import human, main


def run(*argv: str) -> int:
    return main(list(argv))


def test_human_readable_sizes() -> None:
    assert human(0) == "0 B"
    assert human(512) == "512 B"
    assert human(1536) == "1.5 KiB"
    assert human(6 * 1024**3) == "6.0 GiB"


def test_ls_lists_members(archive_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert run("ls", str(archive_path)) == 0
    listed = capsys.readouterr().out.split()
    assert set(MEMBERS) <= set(listed)
    assert "empty_dir/" not in listed  # directories hidden unless --all


def test_ls_all_includes_directories(
    archive_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run("ls", str(archive_path), "--all") == 0
    assert "empty_dir/" in capsys.readouterr().out.split()


def test_ls_with_glob(archive_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert run("ls", str(archive_path), "logs/*.log") == 0
    listed = capsys.readouterr().out.split()
    assert listed == ["logs/2026-08-17.log", "logs/2026-08-18.log"]


def test_ls_long_shows_size_and_method(
    archive_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run("ls", str(archive_path), "readme.txt", "--long") == 0
    line = capsys.readouterr().out.strip()
    assert "readme.txt" in line
    assert "deflate" in line


def test_ls_with_unmatched_glob_fails_cleanly(
    archive_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run("ls", str(archive_path), "nothing/*.xyz") == 2
    assert "nothing/*.xyz" in capsys.readouterr().err


def test_info_reports_totals(archive_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert run("info", str(archive_path), "--stats") == 0
    captured = capsys.readouterr()
    assert "entries" in captured.out
    assert "ratio" in captured.out
    assert "transferred" in captured.err  # --stats goes to stderr, so pipes stay clean


def test_cat_streams_bytes(archive_path: Path, capsysbinary: pytest.CaptureFixture[bytes]) -> None:
    assert run("cat", str(archive_path), "data/small.json") == 0
    assert capsysbinary.readouterr().out == MEMBERS["data/small.json"]


def test_cat_unknown_member(archive_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert run("cat", str(archive_path), "missing.txt") == 2
    assert "missing.txt" in capsys.readouterr().err


def test_get_extracts_matching_members(archive_path: Path, tmp_path: Path) -> None:
    assert run("get", str(archive_path), "logs/*", "-d", str(tmp_path)) == 0
    extracted = sorted(path.name for path in (tmp_path / "logs").iterdir())
    assert extracted == ["2026-08-17.log", "2026-08-18.log"]


def test_get_everything(archive_path: Path, tmp_path: Path) -> None:
    assert run("get", str(archive_path), "-d", str(tmp_path)) == 0
    for name, payload in MEMBERS.items():
        assert (tmp_path / name).read_bytes() == payload


def test_get_refuses_to_overwrite_without_the_flag(
    archive_path: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run("get", str(archive_path), "readme.txt", "-d", str(tmp_path)) == 0
    assert run("get", str(archive_path), "readme.txt", "-d", str(tmp_path)) == 2
    assert "already exists" in capsys.readouterr().err
    assert run("get", str(archive_path), "readme.txt", "-d", str(tmp_path), "--overwrite") == 0


def test_get_flatten(archive_path: Path, tmp_path: Path) -> None:
    assert run("get", str(archive_path), "logs/*", "-d", str(tmp_path), "--flatten") == 0
    assert (tmp_path / "2026-08-17.log").is_file()
    assert not (tmp_path / "logs").exists()


def test_local_prefix_target(archive_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert run("ls", f"local:{archive_path}") == 0
    assert "readme.txt" in capsys.readouterr().out


def test_unknown_command_exits_with_usage() -> None:
    with pytest.raises(SystemExit):
        run("nope")

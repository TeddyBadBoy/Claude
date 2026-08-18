"""Shared fixtures: a real ZIP file on disk plus a request-counting range source."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest
from helpers import MEMBERS, CountingSource

from drivezip.ranges import LocalFileRangeSource


@pytest.fixture(scope="session")
def archive_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("archives") / "sample.zip"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("empty_dir/", b"")
        for name, payload in MEMBERS.items():
            zf.writestr(name, payload)
    return path


@pytest.fixture
def counting_source(archive_path: Path) -> CountingSource:
    return CountingSource(LocalFileRangeSource(archive_path))

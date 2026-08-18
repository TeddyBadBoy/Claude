# CLAUDE.md

Context for Claude Code sessions working in this repository.

## What this repo is

`drivezip` — a CLI and library for reading large ZIP archives stored on Google
Drive without downloading them. The whole design rests on one idea: a ZIP's
central directory lives at the end of the file, and the Drive API serves HTTP
`Range` requests, so `zipfile.ZipFile` can parse a remote archive through a
seekable file object that fetches only the bytes it is asked for.

## Layout

| Path | Role |
| --- | --- |
| `drivezip/ranges.py` | `RangeSource` protocol, `LocalFileRangeSource`, `TransferStats` |
| `drivezip/reader.py` | `RangeReader` — seekable stream over a range source, LRU block cache, read-ahead, tail prefetch |
| `drivezip/drive.py` | Drive backend: file-id/URL parsing, metadata, ranged GETs, retries |
| `drivezip/archive.py` | `ZipArchive` — listing, reading, extraction, zip-slip guards |
| `drivezip/auth.py` | credential resolution (token → service account → cached token → OAuth flow) |
| `drivezip/cli.py` | `info` / `ls` / `cat` / `get` / `login` |
| `tests/` | full suite, network-free |

## Invariants worth protecting

- **Nothing downloads the whole archive.** `tests/test_archive.py` and
  `tests/test_drive.py` assert on bytes transferred; if a change starts
  streaming entire files, those tests fail. Keep it that way.
- **Memory is bounded** by `block_size × cache_blocks`, and a single fetch never
  exceeds what the cache can hold — otherwise a read evicts its own blocks.
- **Extraction paths are validated** by `_safe_destination` before any write.
  Absolute paths, `..` traversal and drive letters must stay rejected.
- **Google libraries are imported lazily** (in `auth.py` and `drive.py`), so the
  package and the test suite work with only the standard library plus
  `requests`. Do not hoist those imports to module scope.
- **Stream defaults are resolved at call time**, not as argument defaults —
  binding `sys.stderr` at import time breaks output capture.

## Conventions

- Python 3.11+; CI tests 3.11, 3.12, 3.13.
- `ruff check .` and `ruff format --check .` must pass. Note that ruff also
  formats Python code blocks inside `README.md`.
- Tests live in `tests/`, named `test_*.py`. Shared doubles go in
  `tests/helpers.py` (imported as `from helpers import ...`; pytest puts the
  test directory on `sys.path`), fixtures in `tests/conftest.py`.
- No network access in tests. The Drive backend is exercised through
  `FakeSession`, which serves real ranges out of a real ZIP on disk.

## Commands

```bash
pip install -e ".[google,dev]"
ruff check . && ruff format .
pytest

# manual smoke test against a local archive — the CLI accepts local paths
python -m drivezip info ./some.zip --stats
python -m drivezip ls ./some.zip -l
```

## Reading real archives from a session

Sessions reach Drive only if the cloud environment's network policy allows it.
`www.googleapis.com` is usually reachable; `drive.usercontent.google.com` and
`drive.google.com` are not, under the default **Trusted** access level. A 403 on
`CONNECT` is an environment policy denial — report it, do not route around it
(`/root/.ccr/README.md` says the same).

Credentials, in the order `auth.resolve` tries them: `DRIVEZIP_ACCESS_TOKEN`,
a service-account key file, `DRIVEZIP_SERVICE_ACCOUNT_JSON` (raw or base64 —
the shape that fits a cloud environment's variables), a cached user token, then
the interactive flow (which cannot run headless). README's "Running from a
Claude Code cloud session" has the full setup.

## Git

- Default branch is `main`; work happens on feature branches and lands via PR.
- Keep `.gitignore` as the upstream GitHub Python template plus additions at the
  bottom.

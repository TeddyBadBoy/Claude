# CLAUDE.md

Context for Claude Code sessions working in this repository.

## What this repo is

`TeddyBadBoy/Claude` — a Python project workspace. As of now it contains only
scaffolding (README, LICENSE, CI, this file, and GitHub's standard Python
`.gitignore`). There is no application code, no `pyproject.toml`, and no test
suite yet.

If you are asked to add the first real code, ask which stack/framework is
intended before scaffolding one — the empty repo does not imply it.

## Conventions

- **Python 3.11+.** CI tests against 3.11, 3.12, and 3.13.
- **Lint and format with `ruff`**; both `ruff check .` and `ruff format --check .`
  must pass. Default ruff settings apply until a `pyproject.toml` sets otherwise.
- **Tests with `pytest`**, in `tests/`, named `test_*.py`.
- Application code lives in a top-level package directory, not at repo root.

## Commands

```bash
ruff check .          # lint
ruff format .         # format
pytest                # test
```

Nothing is installed by default in a fresh checkout — `pip install ruff pytest`
first, ideally inside a virtualenv.

## CI

`.github/workflows/ci.yml` runs two jobs on every push to `main` and on every
pull request:

- `lint` — ruff check + ruff format --check on Python 3.12
- `test` — pytest across the 3.11/3.12/3.13 matrix; the step no-ops while no
  `tests/` directory exists, so CI stays green on a code-free repo

The test job installs `requirements.txt` and/or the local package if either
appears later; add dev dependencies to a `[project.optional-dependencies] dev`
extra so the existing `pip install -e ".[dev]"` step picks them up.

## Git

- Default branch is `main`.
- Do not commit directly to `main`; branch, then open a pull request.
- Keep `.gitignore` as the upstream GitHub Python template plus project-specific
  additions at the bottom.

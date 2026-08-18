# Claude

A Python project workspace.

> This repository is currently a scaffold — no application code has landed yet.
> The sections below describe the conventions any code added here is expected to follow.

## Requirements

- Python 3.11 or newer
- [`ruff`](https://docs.astral.sh/ruff/) for linting and formatting
- [`pytest`](https://docs.pytest.org/) for tests

## Getting started

```bash
git clone https://github.com/TeddyBadBoy/Claude.git
cd Claude

python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

pip install ruff pytest
```

## Development

```bash
ruff check .        # lint
ruff format .       # format
pytest              # run tests
```

Continuous integration runs the same three commands on every push and pull
request — see [`.github/workflows/ci.yml`](.github/workflows/ci.yml).

## Layout

```
.
├── .github/workflows/ci.yml   # lint + test on push and PR
├── CLAUDE.md                  # context for Claude Code sessions
├── LICENSE                    # MIT
└── README.md
```

Application code belongs in a top-level package directory; tests belong in
`tests/` and are named `test_*.py`.

## Contributing

1. Branch off `main`.
2. Keep `ruff check .` and `pytest` green.
3. Open a pull request describing what changed and why.

## License

Released under the [MIT License](LICENSE).

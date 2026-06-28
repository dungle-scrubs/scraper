# Contributing

## Setup

```bash
uv sync
```

## Code quality

This project uses the [Astral](https://astral.sh) toolchain:

```bash
# Lint
uv run ruff check .

# Format
uv run ruff format .

# Type check
uv run ty check

# Tests
uv run pytest tests/ -v
```

All five checks run in CI on every pull request.

## Commits

Use [Conventional Commits](https://www.conventionalcommits.org/):

- `feat:` — new feature
- `fix:` — bug fix
- `docs:` — documentation
- `refactor:` — code change that neither fixes a bug nor adds a feature
- `test:` — adding or updating tests
- `chore:` — maintenance

## Pull requests

- One logical change per PR
- All checks must pass (lint, format, type check, tests)
- Write a clear description of what changed and why

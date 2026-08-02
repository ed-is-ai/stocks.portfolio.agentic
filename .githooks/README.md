# Git hooks

Version-controlled hooks for this repo. Git does not use them automatically —
each clone must opt in **once**:

```sh
git config core.hooksPath .githooks
```

## Hooks

- **pre-push** — runs the full test suite (`uv run pytest -q`) before every
  push. If tests fail, the push is aborted. Override with
  `git push --no-verify` when you need to bypass it.

This is a fast local safety net; the authoritative gate is the GitHub Actions
`tests` workflow (`.github/workflows/tests.yml`), which also runs on every PR.

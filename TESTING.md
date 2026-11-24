# Testing contract

Animas's test suite is the executable evidence for conformance with the port
target recorded in [PARITY.md](PARITY.md) (canonical Cordis `v4.0.0-rc.13`).

## Commands

```sh
uv run pytest          # 1347 tests, 100% statement + branch coverage gate
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy src
```

`pyproject.toml` sets `filterwarnings = ["error", "ignore::ResourceWarning"]`,
so unawaited coroutines and other unexpected warnings fail the suite. The
shared pre-commit hook additionally runs the cross-repo `check_all.py --fast`.

## Structure

- `tests/unit/animas/` mirrors `src/animas/` module by module.
- `tests/integration/` covers end-to-end component pipelines and the earlier
  rc.1 acceptance/conformance suites (historical target).
- `tests/unit/animas/test_rc13_core_conformance.py` and
  `tests/unit/animas/test_rc13_hmr_reconcile.py` are the rc.13 conformance
  probes. Each cites the upstream `file:line` or paper algorithm it checks.

## Rules

- A conformance gap is closed only by an executable probe plus a green full
  gate. Recorded traces without a probe are not evidence of parity.
- 100% statement and branch coverage is enforced by `--cov-fail-under=100`.
  New code must ship with tests for every branch; `# pragma: no cover` is
  reserved for provably unreachable defensive code.
- Never add a mock or stub that creates an awaitable and discards it.

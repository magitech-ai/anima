# Parity

## Current port target

Anima's port target is **canonical Cordis `v4.0.0-rc.13`** (`cordiverse/cordis`,
commit `bc9fae7`, published 2026-11-12), with the paper
["A Programming Paradigm for Spatiotemporal Composability"](https://arxiv.org/abs/2608.25512)
as the theory reference. This supersedes the former rc.1 pin by decision
[ADR 0013](docs/adr/0013-rc13-port-target.md), dated 2026-11-23. DeepSeek
Harness at `fcc0ad46` (`dsh v0.2.2-rc.1`) remains historical provenance and a
harness conformance source, not the port target.

The 1.0 gate recorded in `magitech-harness/PARITY.md` was a verdict against the
frozen rc.1 target. It is **not** a claim of parity with rc.13 and is not
reused here.

## Verified baseline at this target (2026-11-23)

- `uv run pytest`: 1328 passed, 100% statement and branch coverage.
- `uv run ruff check src tests`, `uv run ruff format --check src tests`,
  `uv run python -m mypy src`: green.
- `filterwarnings = ["error", "ignore::ResourceWarning"]` is configured, so
  unawaited coroutines and other unexpected warnings fail the suite.
- 34 executable rc.13 core conformance probes live in
  `tests/unit/anima/test_rc13_core_conformance.py`. Each cites the upstream
  `file:line` it validates.

Items proven against rc.13 so far:

| Item | Verdict | Evidence |
|---|---|---|
| Effect shapes: disposer, iterable, awaitable, async iterable | DONE | `src/anima/core.py` `Context.effect`/`Fiber._collect_effect`; probes in `test_rc13_core_conformance.py` (upstream `core/src/fiber.ts:60-76,240-289`) |
| Async-generator effect closed on epoch change | DONE | `Fiber._collect_effect` `finally: aclose()`; probe `test_rc13_async_generator_effect_is_closed_on_epoch_change` (upstream `#268`, `fiber.ts:267-285`) |
| Per-step epoch guard for effect iterators | DONE | same code and probes (paper Algorithm 1) |
| `parallel` aggregates every failure (one → `ExceptionGroup` of one) | DONE | `src/anima/events.py` (upstream `core/src/events.ts:110-119`); rc.13 probe |
| `waterfall` rejects a second `next()` call | DONE | `src/anima/events.py` (upstream `events.ts:155-157`); rc.13 probe |
| `registry.delete()` removed | DONE | deleted from `src/anima/registry.py`; disposal is the public path (upstream `registry.ts` has no `delete`) |
| FAILED fiber recovers only via `update()` | DONE | `Fiber._setEpoch` error guard; rc.13 probe (upstream `fiber.ts:435-436`) |
| `notify` respects isolation realms | DONE | `ReflectService.notify` default filter; rc.13 probe (upstream `reflect.ts:251-254`) |
| `set` rejects an unprovided name | DONE | `Context.set` delegates to `ReflectService.set`; rc.13 probe (upstream `reflect.ts:200-211`) |
| `bail` rejects async listeners inside a running loop | DONE (Python adaptation) | `src/anima/events.py`; rc.13 probe. JS returns a Promise; Python closes the coroutine and fails loud rather than leaking it |
| `internal/commit` receives raw config, including on non-ACTIVE fibers | DONE | `Fiber.update`; rc.13 probes (upstream `fiber.ts:536-542`) |
| Entry rebuild on `name` / `inject` change | DONE | `Entry.update`; rc.13 probe (upstream `loader/src/config/entry.ts:243-248`) |

Verified counts at this checkpoint: 1348 passed, 100% statement and branch coverage.

## Open gaps against rc.13

These are recorded as **OPEN**, not DONE or PARTIAL-with-accepted-gap. They are
tracked work, not hidden divergences:

| Item | Status | Upstream evidence |
|---|---|---|
| Programmatic `ctx.hmr.reconcile()` returning `{settled, aborted, skipped}` | DONE | `src/anima/hmr.py`; 9 probes in `test_rc13_hmr_reconcile.py` (upstream `hmr/src/index.ts:286-301,525-753,785-836`) |
| Per-unit commit/abort isolation in HMR | DONE | stale modules are independent units; aborted units restore their module cache and do not block commits (upstream `index.ts:674-721`) |
| Host-module / native-addon skip reasons | DONE | `SkipReason` enum and `_is_host_module`; probes in `test_rc13_hmr_reconcile.py` (upstream `index.ts:546-553,819-836`) |
| `fs` service with mountable providers and `ctx.fs.watch()` | OPEN | `packages/fs`, `packages/fs-node` |
| Loader explicit injection scopes (outer/inner fiber) and config-file error locations | OPEN | `loader/src/index.ts` |
| `entry.fiber` → `entry.innerFiber` rename | DONE (alias) | `Entry.innerFiber` read/write alias; rc.13 probe (release notes, "Breaking Changes"). The legacy `fiber` name remains as internal storage until callers migrate |
| Volatile updates, `rawConfig`/`effectiveConfig`, include journal | VERIFYING | `vendor/cordis/README.md`; `src/anima/volatile.py`, `include.py` |

A gap is closed only when an executable probe citing the upstream source is
added and the full gate is green. Agent-accepted reasons are not accepted
substitutes for that evidence.

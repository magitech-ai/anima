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

Verified counts at this checkpoint: 1369 passed, 100% statement and branch coverage.

## rc.13 work items and current verdicts

Items marked **OPEN** are tracked work, not hidden divergences. A gap is closed
only by an executable probe plus a green full gate; agent-accepted reasons are
not substitutes for that evidence.

| Item | Status | Upstream evidence |
|---|---|---|
| Programmatic `ctx.hmr.reconcile()` returning `{settled, aborted, skipped}` | DONE | `src/anima/hmr.py`; 9 probes in `test_rc13_hmr_reconcile.py` (upstream `hmr/src/index.ts:286-301,525-753,785-836`) |
| Per-unit commit/abort isolation in HMR | DONE | stale modules are independent units; aborted units restore their module cache and do not block commits (upstream `index.ts:674-721`) |
| Host-module / native-addon skip reasons | DONE | `SkipReason` enum and `_is_host_module`; probes in `test_rc13_hmr_reconcile.py` (upstream `index.ts:546-553,819-836`) |
| Fiber-scoped `internal/update` listeners | DONE | `EventBus` owner filtering with explicit target fiber; rc.13 probe (upstream `events.ts:54-79`) |
| `fs` service with mountable providers and `ctx.fs.watch()` | DONE | `src/anima/fs.py`; 14 probes in `test_fs.py` (upstream `packages/fs/src/index.ts:39-148`, `watcher.ts:35-111`, `packages/fs-node/src/index.ts:23-29`) |
| Global isolate realms: a string label shares bindings across entries | DONE | `Context._shared_isolated` root table and cross-context wake-up; rc.13 probe (paper §5.2.1 / Algorithm 7) |
| Loader explicit injection scopes (outer/inner fiber) and config-file error locations | OPEN | `loader/src/index.ts` |
| `entry.fiber` → `entry.innerFiber` rename | DONE (alias) | `Entry.innerFiber` read/write alias; rc.13 probe (release notes, "Breaking Changes"). The legacy `fiber` name remains as internal storage until callers migrate |
| HMR file watching opt-in (`watch.enabled`, `ctx.fs.watch()`) | DONE (rc.13 path) | `HmrService._start_fs_watch` uses `ctx.fs.watch()` and calls `reconcile()`; enabled/disabled/config validation probes (upstream `hmr/src/index.ts:192-243,756-779`). Legacy watcher remains when no `watch` block is supplied |
| Unload drains notified dependents before inverses | OPEN | `core/src/fiber.ts:481-487`; context-level `provide` drains, reflect-provided services do not. A naive wait inside the provide disposer deadlocked nested unloads and was reverted |
| Volatile updates, `rawConfig`/`effectiveConfig`, include journal | EXTENSION (not a port item) | Canonical rc.13 has no volatile mechanism; this is DSH-fork behavior kept as an Anima extension — ADR 0014 |

Anima-original extensions (volatile, `WatcherService`, `PkgService`) are kept
but are explicitly not counted as rc.13 parity; see ADR 0014.

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

Verified counts at this checkpoint: 1399 passed, 100% statement and branch coverage.

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
| Loader explicit injection scopes: entry inject runs in an outer fiber, component in an inner fiber | DONE | `Entry._init` outer fiber with entry inject; inner component fiber on the entry registry; probes in `test_rc13_core_conformance.py` (upstream `loader/src/config/entry.ts:35-46,353-372`) |
| Config-path error locations for failed expressions | DONE | `JsExprError.path`, `interpolate()` path annotation, loader entry-id note; probes in `test_rc13_core_conformance.py` (upstream `loader/config/utils.ts:64-96`) |
| `entry.fiber` → `entry.innerFiber` rename | DONE (alias) | `Entry.innerFiber` read/write alias; rc.13 probe (release notes, "Breaking Changes"). The legacy `fiber` name remains as internal storage until callers migrate |
| HMR file watching opt-in (`watch.enabled`, `ctx.fs.watch()`) | DONE (rc.13 path) | `HmrService._start_fs_watch` uses `ctx.fs.watch()` and calls `reconcile()`; enabled/disabled/config validation probes (upstream `hmr/src/index.ts:192-243,756-779`). Legacy watcher remains when no `watch` block is supplied |
| Unload drains notified dependents before inverses | DONE | `Impl.dependents` / `release` protocol in `core.py`/`reflect.py`; probe asserts dependents' disposers run before the provider's (paper Algorithm 5, upstream `fiber.ts:481-487`) |
| Intercept and inject config values are interpolated | DONE | `LoaderService._on_patch_context` and `Entry._init` evaluate `__jsExpr` values; probes (upstream `loader/config/entry.ts:155,169-179`) |
| Include anonymous-id stability across reads | DONE | `Include._assign_ids()` reuses ids for deep-equal rows with the same parent; probe (upstream `include/index.ts:628-675`) |
| `EntryGroup.remove` adoption guard | DONE | `entry.parent is self` check; probe (upstream `loader/config/group.ts:64-68`) |
| Entry-options validation | DONE | `validate_options()` / `EntryValidationError` on create paths; probes (upstream `loader/config/utils.ts:163-209`) |
| `emit()` propagates listener exceptions | DONE | `EventBus.emit` no longer swallows sync errors; probes (upstream `core/src/events.ts:121-126`) |
| Volatile updates, `rawConfig`/`effectiveConfig`, include journal | EXTENSION (not a port item) | Canonical rc.13 has no volatile mechanism; this is DSH-fork behavior kept as an Anima extension — ADR 0014 |

Anima-original and DSH-fork extensions (volatile, `WatcherService`, `PkgService`)
are kept but are explicitly not counted as rc.13 parity; see ADR 0014. The port
target and divergence policy were ratified by the user in ADR 0015.

## Ratified decisions

The user ratified the port target (canonical `cordis@4.0.0-rc.13`) and decided
that all four remaining divergences were to be fixed, not accepted. Recorded in
ADR 0015. All four now have executable upstream-cited probes:

| Item | Status | Evidence |
|---|---|---|
| Deferred/enriched access-error stacks | DONE | `_enhance_error()` rebuilds the error without kernel frames (upstream `core/src/reflect.ts:20-36`) |
| `await_settle()` only observes PENDING fibers | DONE | `Fiber.await_settle` no longer calls `load()`; probe (upstream `core/src/fiber.ts:520-526`) |
| Legacy `hmr/change` / `hmr/reload` events removed | DONE | HMR reports through `reconcile()` / `last_reload`; probes assert event names absent (upstream `hmr/src/index.ts`, release notes) |
| `HmrService` injects `fs` | DONE | `HmrService.inject == ["fs"]`; probe (upstream `hmr/src/index.ts:163-185`) |

The outer/inner-fiber scopes and config-path reporting are now ported. Two
earlier outer-fiber attempts were reverted (broken registry identity, then
a LOADING-state race) before the working design was found.

| Item | Status | Upstream evidence |
|---|---|---|
| YAML line/column source mapping for expression errors | DONE | `load_yaml_with_locations()` retains PyYAML `start_mark`; `Include.get_location()` and `Entry.get_location()`; loader adds `file:line:column` notes (upstream `loader/config/tree.ts:254-260` and Include's override) |

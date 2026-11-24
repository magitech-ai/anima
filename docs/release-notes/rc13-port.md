# Cordis rc.13 port — in progress

Date: 2026-11-23
Target: canonical Cordis `v4.0.0-rc.13` (commit `bc9fae7`).

This note records the work completed after the target moved from the frozen
DeepSeek Harness rc.1 pin to the latest published Cordis release (ADR 0013).
It is a work-in-progress record, not a claim of completed parity; open gaps
are listed in [PARITY.md](../../PARITY.md).

## Landed

- **Core effects** (upstream `core/src/fiber.ts:60-76,240-289`): `ctx.effect`
  now accepts disposers, iterables, awaitables and async iterables; iterators
  are guarded at every step, async generators are closed deterministically on
  epoch change, and effects bind to the owning context's fiber.
- **Events** (`core/src/events.ts:110-119,155-157`): every `parallel` failure is
  aggregated into an `ExceptionGroup`; a second `next()` call in a waterfall
  is rejected. Sync `bail` fails loud for async listeners in a running loop.
- **Fiber lifecycle** (`fiber.ts:435-436,401-416`): a FAILED fiber no longer
  reloads on notification; replacement clears the error explicitly.
- **Reflect** (`reflect.ts:200-211,251-254`): `set` rejects unprovided names;
  `notify` respects isolation realms.
- **Config persistence** (`fiber.ts:536-542`): `internal/commit` receives the
  raw config and fires on non-ACTIVE fibers, preserving expressions and
  defaults in stored profiles.
- **Loader** (`loader/src/config/entry.ts:243-248`): changing an entry's name
  or inject rebuilds it.
- **HMR** (`hmr/src/index.ts:286-301,525-753,785-836`): programmatic
  `ctx.hmr.reconcile()` with single-flight transactions, nested-transaction
  rejection, per-unit commit/abort, result object (`settled`/`aborted`/
  `skipped`), host-module and native-addon skip reasons, and in-place fiber
  replacement through `registry.replace()`.
- **Standards**: `registry.delete()` removed (rc.13 removed it), warning-free
  tests configured, MIT attributions for schemastery and cosmokit added to
  NOTICE.

## Verification

At the last checkpoint: 1331 tests passed with 100% statement and branch
coverage; `ruff check`, `ruff format --check` and `mypy src` all green.
`tests/unit/anima/test_rc13_core_conformance.py` (34 probes) and
`tests/unit/anima/test_rc13_hmr_reconcile.py` (9 probes) carry upstream
citations for each behavior.

## Extension removal (ADR 0016)

By user decision the DSH-fork-only surfaces were deleted: `volatile.py`,
`watcher.py`, `pkg.py`, `Schema.volatile()`, `loader/volatile-update`,
`loader.builtins` and `loader.exit()`. HMR's legacy watch path now uses
`ctx.fs.watch()`. The Magitech-original `†` modules were later removed before
1.0.0 by user decision (ADR 0018); they were never part of the port.

# 0016. Remove the DSH-fork extension set

Date: 2026-11-23
Status: accepted (user decision)

## Context

ADR 0014 recorded that Anima shipped surfaces not present in the ratified
target, canonical Cordis v4.0.0-rc.13: the volatile config mechanism (from the
older DeepSeek Harness fork), the legacy polling `WatcherService`, the
`uv`-based `PkgService`, and `loader.builtins` / `loader.exit()`.

The user was asked whether to delete them and answered "delete all four, update
harness too". `magitech-harness` used `loader/volatile-update` in its pi-ai
component; that consumer is being moved to an rc.13-native config lifecycle
hook in a coordinated change.

## Decision

- `src/anima/volatile.py`, `src/anima/watcher.py`, and `src/anima/pkg.py` are
  deleted, together with their exports and tests.
- `Schema.volatile()`, `equal_except_volatile()`, `loader/volatile-update`,
  `loader.builtins`, and `loader.exit()` are removed.
- HMR's legacy start path now uses `ctx.fs.watch()`; the `fs` service is still
  rc.13-native.
- Magitech-original modules marked `†` in the README (`server*`, `http*`,
  `cli*`, `cli_env`) are **not** part of this removal: they were never claimed
  as Cordis parity.
- ADR 0014 is superseded as policy; it remains as the historical record of why
  these surfaces were once excluded from parity.

## Consequences

- Anima's public surface is the rc.13 port plus the `†` Magitech additions;
  no DSH-fork-only config semantics remain.
- Downstream consumers of the volatile event must use rc.13 config lifecycle
  hooks (`internal/update` / `internal/commit`) instead.
- `PARITY.md` no longer carries an EXTENSION row for this set.

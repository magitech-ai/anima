# 0014. Anima-original extensions are not parity items

Date: 2026-11-23
Status: accepted

## Context

Canonical Cordis `v4.0.0-rc.13` (the port target, ADR 0013) does not contain
several surfaces that Anima ships. They came from the older DeepSeek Harness
fork pin or were added by Magitech. Counting them as "missing rc.13 features"
would be wrong; presenting them as rc.13 parity would be equally wrong.

Verified by direct search of the upstream rc.13 tree:

- `volatile`, `rawConfig`, `effectiveConfig` and `loader/volatile-update` have
  **no occurrences** in `/tmp/opencode/cordis-upstream`. They exist in the DSH
  fork (`@deepseek-ai/cordis` pin), where Anima's implementation matches the
  documented no-restart commit and owner-scoped notification behavior.
- `WatcherService` is an Anima-original polling watcher. rc.13 moves file
  watching to the `fs` service (`ctx.fs.watch()`), now ported in `src/anima/fs.py`.
- `PkgService` is an Anima-original `uv` package manager; rc.13 has a different
  `packageManager` service for npm/pnpm/yarn.
- `loader.builtins` / `loader.exit()` are retained for backward compatibility
  but are not rc.13 APIs.

## Decision

- `PARITY.md` marks these surfaces as `VERIFYING`/extension, not `DONE` against
  rc.13. They may remain in Anima, but they are not evidence of parity.
- New work ports the rc.13 equivalent (for example `fs` and `reconcile()`)
  alongside the extension, and documents the relationship in the release note.
- A parity item is `DONE` only with an executable probe citing the upstream
  rc.13 source. An extension cannot close a parity item.

## Consequences

- The parity map distinguishes "ported from rc.13", "platform adaptation", and
  "Anima-original extension" instead of collapsing them into one DONE column.
- Removing an extension requires a migration note; keeping one does not imply
  the upstream feature is absent.

# 0008. Config resolution at activation

Date: 2026-11-11
Status: accepted

## Context

`Fiber._resolve_config` is dormant. Its sole call site is `_reload`,
which is unreachable in practice: `_store` is always empty (`_checkImpl`
has zero callers), so `_refresh` always computes the inactive epoch for
any fiber with dependencies, and `load()` never calls it — it spreads the
raw config straight into the plugin after synchronous validation in
`__init__`. The live path therefore bypasses the `internal/config`
waterfall entirely, so any plugin relying on `internal/config` hooks gets
silently wrong config.

Upstream resolves config live on every activation through a synchronous
waterfall: validation runs synchronously and throws on an asynchronous
result; the resolved config is set inside the waterfall default.

## Decision

Wire it in, as part of chunk P1-1 (the epoch-machine chunk): `_reload`
executes the runner directly, `_checkImpl` callers populate `_store`, and
config resolution runs at activation. Validation runs synchronously at
activation, matching upstream; the waterfall itself stays asynchronous
because anima's waterfall mechanism is asynchronous throughout. If wiring
proves unworkable inside P1-1, the fallback is deletion of
`_resolve_config` and its dead paths with an amendment to this record —
not silent coverage of dead code.

## Consequences

- Plugins relying on `internal/config` hooks get correct config at
  activation; the waterfall becomes a real seam instead of a dead one.
- Deleting the seam instead would diverge from upstream activation
  semantics in a way that surfaces as a gap in session events (which
  carry validated config) and in plugin Configs.
- Chunk P1-1 owns the wiring; this record is the decision it implements.

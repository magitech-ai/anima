# 0015. Ratified rc.13 target and remaining divergences

Date: 2026-11-23
Status: accepted (user decision)

## Context

ADR 0013 selected canonical `cordis@4.0.0-rc.13` as the port target from
evidence: npm `latest` resolves to it and the GitHub release is dated
2026-11-12. The newer `@deepseek-ai/cordis@4.2.0-alpha.1` is a downstream fork,
not the `cordis` package. The user was asked to ratify the choice because an
agent's selection is not an accepted decision.

`PARITY.md` also listed four behavioral differences from rc.13 that a previous
revision had self-classified as "adaptations". The user was asked whether to
accept or fix them.

## Decision

- The port target is **canonical Cordis v4.0.0-rc.13** (commit `bc9fae7`).
  The DSH fork remains a documented secondary conformance source.
- All four remaining divergences are **to be fixed**, not accepted:
  1. deferred/enriched access-error stacks (`core/src/reflect.ts:20-36`);
  2. `await_settle()` must observe, not drive, a PENDING fiber
     (`core/src/fiber.ts:520-526`);
  3. remove the legacy `hmr/change` / `hmr/reload` events in favor of the
     `reconcile()` result object;
  4. `HmrService` injects `fs`, not the legacy `watcher`.

No agent-authored "accepted gap" is valid without this ratification; new
divergences must be presented to the user.

## Consequences

- `PARITY.md` may mark these four DONE only with executable probes citing the
  upstream source and a green full gate.
- Compatibility shims that depend on the removed behaviors must be updated in
  the same change.

# Agent Guidelines

## Anima Architecture & Invariants

- **Repository Role**: `anima` is the foundational asynchronous micro-kernel and event pipeline engine. It provides:
  - Asynchronous event waterfalls (`ctx.waterfall()`) with middleware delegation.
  - Hierarchical execution contexts with dependency injection.
  - Dynamic component and lifecycle management.
- **Waterfall Discipline**: Waterfall event interceptors must await `next_fn(payload)` to delegate down the chain; returning without awaiting short-circuits execution.

## Shared Development Guidance

Follow the shared development guidance in [agents-home AGENTS](../agents-home/AGENTS.md).

## Anima-Specific References

### Issue tracker
GitHub Issues via `gh` CLI (`anima` organization). See `docs/agents/issue-tracker.md`.

### Triage labels
Canonical five-role triage vocabulary. See `docs/agents/triage-labels.md`.

### Domain docs
Single-context layout (`CONTEXT.md` + `docs/adr/`). See `docs/agents/domain.md`.

### Review standards
Shared reviewer rules (cross-platform tests, warning-free mocks, guard contracts): `../agents-home/CODING_STANDARDS.md`.

# Agent Guidelines

## Anima Architecture & Invariants

- **Repository Role**: `anima` is the foundational asynchronous micro-kernel and event pipeline engine. It provides:
  - Asynchronous event waterfalls (`ctx.waterfall()`) with middleware delegation.
  - Hierarchical execution contexts with dependency injection.
  - Dynamic component and lifecycle management.
- **Waterfall Discipline**: Waterfall event interceptors must await `next_fn(payload)` to delegate down the chain; returning without awaiting short-circuits execution.

## Shared Development Guidance

Shared standards, workflow and quality gates:
[agents-home AGENTS](../agents-home/AGENTS.md) → `docs/ENGINEERING.md`,
`docs/WORKFLOWS.md`, `CODING_STANDARDS.md`, and `scripts/check_all.py`.

### Local checks
Run `python3 ../agents-home/scripts/check_all.py --fast` before committing;
CI runs the full gate.

## Anima-Specific References

### Issue tracker
GitHub Issues via `gh` CLI (`anima` organization). See `docs/agents/issue-tracker.md`.

### Triage labels
Canonical five-role triage vocabulary. See `docs/agents/triage-labels.md`.

### Domain docs
Single-context layout (`CONTEXT.md` + `docs/adr/`). See `docs/agents/domain.md`.


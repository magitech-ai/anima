# 0003. Hierarchical Context Inheritance and Service Scoping

Date: 2026-11-07
Status: accepted

## Context

Complex agent applications and turn loops require isolated, transient execution scopes (such as turn-level variables, subagent execution contexts, or temporary event interceptors). Using global mutable state or mutating a shared root context causes state leakage, race conditions in concurrent turns, and complicated cleanup logic.

## Decision

We implement hierarchical context inheritance via child contexts (`ctx.extend()`):
1. A child context maintains a pointer to its parent context.
2. Service lookups and event registrations traverse the ancestor chain, allowing child contexts to access parent services transparently.
3. Services registered or overridden in a child context are scoped strictly to that child and never mutate the parent context.
4. Disposing of a child context unregisters all local listeners and services without affecting parent state.

## Consequences

- Turn execution and subagents can run with scoped overrides safely in isolation.
- Completely eliminates global mutable state in library and application code.
- Service resolution incurs a minor ancestor walk, which is negligible in typical shallow hierarchies.

# 0011. Async disposal of fibers out of scope

Date: 2026-11-11
Status: accepted

## Context

Anima has no async-dispose protocol on `Fiber` — no `__aenter__` /
`__aexit__`, no async-dispose symbol; disposal is `await
fiber.dispose()`. Upstream has no async-dispose symbol anywhere in its
core either: `Fiber.dispose` is a plain async dispose returning a
promise. Only the effect disposer is thenable upstream, not the fiber
itself.

## Decision

Do not invent an async-dispose protocol. `async with fiber` is out of
scope for Phase 1. The thenable fiber (`await fiber`, resolving once the
fiber settles) is in scope via chunk P1-1, which is the upstream shape.

## Consequences

- Disposal stays `await fiber.dispose()`; no new protocol is introduced
  to be parity against.
- If a future phase needs scoped fiber lifetimes, that is a new decision
  with its own record.

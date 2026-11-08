# 0002. Disposer Callables for Registration Teardown

Date: 2026-11-07
Status: accepted

## Context

In dynamic agent environments, plugins, guards, and temporary listeners are loaded and unloaded during runtime. Without deterministic disposal mechanics, unregistered listeners or leaked references cause memory bloat and unintended side effects.

## Decision

Every registration method on `AnimaContext` (`ctx.on`, `ctx.waterfall`, `ctx.provide`) returns a synchronous, zero-argument `Disposer` callable:
1. Invoking `disposer()` immediately removes the listener, service, or interceptor from the context.
2. Disposers are idempotent; calling them multiple times is safe and produces no side effects.
3. Plugins compose disposers during `apply(ctx)` to guarantee complete, clean teardown when deactivated.

## Consequences

- Lifecycle management is deterministic and leak-free.
- Callers and plugins are responsible for holding disposer references if they require explicit dynamic unregistration before context destruction.

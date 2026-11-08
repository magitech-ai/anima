# Anima Micro-Kernel

The lightweight asynchronous micro-kernel and event pipeline engine providing context injection, waterfall middleware, and lifecycle management.

## Language

### Context & Services

**Context**:
An execution scope holding service instances, event bindings, and lifecycle hooks with hierarchical inheritance.
_Avoid_: Container, scope, environment

**Child Context**:
A scoped execution context spawned from a parent context that inherits parent services without mutating them.
_Avoid_: Sub-scope, local environment, child container

**Service**:
A reusable singleton instance mounted onto a context accessible by name.
_Avoid_: Component, manager, module

**Plugin Entrypoint**:
The standard `apply(ctx)` callable exported by an Anima plugin module to mount services and event bindings onto a context.
_Avoid_: Setup hook, loader, init function

### Event Pipelines

**Waterfall**:
An asynchronous event pipeline where registered middleware handlers sequentially inspect, transform, or delegate payloads.
_Avoid_: Pipeline, hook chain, event emitter

**Next Function**:
The asynchronous delegation callback passed to a waterfall listener that must be awaited to invoke subsequent handlers.
_Avoid_: Middleware next, delegate, runner

**Disposer**:
A zero-argument callable returned upon registration that unregisters a listener, service, or effect when invoked.
_Avoid_: Unsubscribe callback, teardown hook, unregister function

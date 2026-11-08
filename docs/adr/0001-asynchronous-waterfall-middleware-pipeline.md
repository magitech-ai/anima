# 0001. Asynchronous Waterfall Middleware Pipeline

Date: 2026-11-07
Status: accepted

## Context

Extensible agent architectures need to allow plugins and guards to intercept, inspect, transform, or abort operations (such as tool execution, turn dispatching, and streaming). Standard publish-subscribe event emitters notify listeners in parallel or sequentially without providing a clean way for a listener to wrap execution, modify downstream payloads, or short-circuit the pipeline.

## Decision

We implement an asynchronous waterfall event mechanism (`ctx.waterfall()`):
1. Waterfall listeners receive both the event payload and a `next_fn(payload)` callable.
2. Handlers must explicitly await `next_fn(...)` to delegate to subsequent handlers and capture their results.
3. Returning a value without awaiting `next_fn` short-circuits the pipeline, enabling guards to halt execution or return cached results.
4. If no handlers intercept the call or the chain completes, the default terminal handler executes.

## Consequences

- Guards and plugins can cleanly wrap operations with pre- and post-processing logic (e.g. token limits, telemetry, error recovery).
- Handlers must strictly adhere to the waterfall protocol by awaiting `next_fn`, avoiding unintended short-circuiting.

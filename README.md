# Animas

The lightweight, composable micro-kernel powering Magitech. Everything is a Component.

[![CI](https://github.com/magitech-ai/animas/actions/workflows/ci.yml/badge.svg)](https://github.com/magitech-ai/animas/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/animas)](https://pypi.org/project/animas/)
[![Python](https://img.shields.io/pypi/pyversions/animas)](https://pypi.org/project/animas/)
[![License: MIT](https://img.shields.io/pypi/l/animas)](LICENSE)

Animas implements the paradigm formalized in
[*"A Programming Paradigm for Spatiotemporal Composability"*](https://arxiv.org/abs/2608.25512).
It is a library kernel: hosts build their own CLI, server and agent surfaces on top.

## Status

`2.0.0` — stable public API under a
[SemVer compatibility policy](docs/COMPATIBILITY.md): additions and fixes in
`1.x`, breaking changes only in `2.0.0` with deprecation where feasible.

## Install

```sh
uv add animas
```

Requires Python 3.12+.

## Quickstart

```python
import asyncio

from animas import Context, Service


class GreeterService(Service):
    def __init__(self, ctx: Context):
        super().__init__(ctx, "greeter")

    def greet(self, name: str) -> str:
        return f"Greetings, {name}!"


async def main():
    ctx = Context()
    ctx.use(GreeterService)
    await ctx.flush()
    print(ctx.greeter.greet("Magitech"))


asyncio.run(main())
```

## Why Animas

Two mechanisms make composability operational rather than aspirational. Neither
is individually unique — what is distinctive is that both are tied to a
component's lifecycle in the same context.

### Reversible effects

`ctx.effect()` registers cleanup that unwinds **automatically** when the owning
component unloads, in LIFO order. Effects registered during unload are drained
too. No caller has to remember a `try/finally`.

```python
ctx.effect(lambda: lambda: print("cleaned up on unload"))
```

### Waterfall middleware

`ctx.waterfall()` is an around-pipeline: each listener chooses whether to call
`next_fn`, so it can inspect, transform, veto or short-circuit the payload.
Unlike `emit` (observation-only), it has a return path.

```python
from animas import EventMode


async def add_prefix(value, next_fn):
    return await next_fn(f"prefix_{value}")


ctx.on("format", add_prefix, mode=EventMode.WATERFALL)
result = await ctx.waterfall("format", "val")  # "prefix_val"
```

Together these let a component contribute capabilities and intercept behaviour
without patching call sites, then disappear cleanly.

## Configuration

Profiles compose components from YAML. Each entry is a `module:component`
target, or a mapping with a `config` block:

```yaml
components:
  - my_app:greeter
  - name: my_app:worker
    config:
      mode: turbo
```

Use `disabled_on: [win32]` or `enabled_on: [linux, darwin]` to gate an entry by
host platform; unknown platform names fail at load time.

Load with `load_profile(ctx, "animas.yaml")`. Environment variables interpolate
as `${VAR}`, `${env.VAR}` or `${VAR:default}`; a full-match interpolation coerces
`"true"` / `"false"` / numbers to `bool` / `int` / `float`.

## Primitives

* **`Context`** — dependency container and service registry. `ctx.extend()` and
  `ctx.isolate()` create hierarchical and isolated scopes.
* **`Service`** — base class for named capabilities mounted on `ctx`.
* **`Fiber`** — lifecycle state machine (`PENDING → LOADING → ACTIVE → UNLOADING → DISPOSED`,
  plus `FAILED`) with deferred dependency injection. `await ctx.flush()` drains
  scheduled work; `ctx.check_pending_cycles()` fails loud on an unresolved boot.
* **Events** — `emit` (sync broadcast), `parallel` (async concurrent, aggregated
  failures), `serial` and `bail` (first-result pipelines), and `waterfall`.

## Subsystems

Every capability is a service mounted on a `Context`.

| Area | Modules | What they do |
| --- | --- | --- |
| Core | `core`, `events`, `tree`, `registry`, `reflect` | Context, event modes, entry tree, component runtime registry, introspection |
| Loading & config | `loader`, `include`, `schema`, `utils` | YAML profiles, module resolution, multi-file includes with patches, schema validation |
| Runtime services | `timer`, `logger`, `hmr`, `fs` | Reversible timers, structured logging, hot module replacement, mountable filesystem providers and routed watches |

## Contributing

See [AGENTS.md](AGENTS.md) for engineering standards. The gates are
`pytest` (100% statement and branch coverage), `ruff check`, `ruff format --check`
and `mypy`. Report bugs and request features via
[GitHub Issues](https://github.com/magitech-ai/anima/issues).

## License

MIT License — Copyright (c) 2026 Magitech. Upstream notices are in [NOTICE](NOTICE).

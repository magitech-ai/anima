# Anima

The standalone, lightweight composable micro-kernel powering **Magitech**.

Built around the core philosophy: **"Everything is a Plugin"**.

[![Tests](https://img.shields.io/badge/coverage-100%25-brightgreen)](tests/)
[![Python](https://img.shields.io/badge/python-3.12%2B-blue)](pyproject.toml)

---

## Primitives

* **`Context`**: Central dependency container and service registry. Sub-contexts (`ctx.extend()`, `ctx.isolate()`) provide isolated realms for subagents and sandboxed plugins.
* **`Service`**: Base class for named capabilities mounted onto `ctx`.
* **`Fiber`**: Runtime state machine (`PENDING` ➔ `LOADING` ➔ `ACTIVE` ➔ `UNLOADING` ➔ `DISPOSED`). Handles deferred loading via `inject = [...]`.
* **Reversible Effects (`ctx.effect`)**: Any registration, tool addition, or watcher returns a disposer. When a plugin unloads, all effects unwind cleanly in reverse order (LIFO).
* **The 5 Event Modes**:
  * `emit`: Synchronous broadcast (fire-and-forget).
  * `parallel`: Asynchronous concurrent broadcast (`asyncio.gather`).
  * `serial`: Sequential async execution; bails on first non-None result.
  * `bail`: Synchronous version of serial.
  * `waterfall`: Around-middleware pipeline passing `(*args, next)`. Enables non-invasive request rewrites, security guards, and vetoes.
* **Service Isolation Realms (`ctx.isolate`)**: Child scopes that prevent service leakage or pollution to/from parent contexts.
* **Declarative YAML Profiles (`load_profile`)**: Composes ordered bundles and plugin entries dynamically from YAML configuration.

---

## Quickstart

```python
import asyncio
from anima import Context, Service, EventMode

class GreeterService(Service):
    def __init__(self, ctx: Context):
        super().__init__(ctx, "greeter")

    def greet(self, name: str) -> str:
        return f"Greetings, {name}!"

async def main():
    ctx = Context()
    ctx.plugin(GreeterService)
    await ctx.flush()

    print(ctx.greeter.greet("Magitech"))

asyncio.run(main())
```

---

## Testing & Quality Gate

`anima` maintains **100% statement and 100% branch test coverage** across both unit and integration suites:

```bash
uv run pytest
```

* `tests/unit/anima/test_init.py`: Top-level package namespace and symbol exports.
* `tests/unit/anima/test_core.py`: IoC container, Fiber lifecycle, Reversible Effects, Isolation Realms, Tree dumps.
* `tests/unit/anima/test_events.py`: All 5 Event Modes (Emit, Parallel, Serial, Bail, Waterfall with Next/Varargs/Kwonly).
* `tests/unit/anima/test_loader.py`: Module and YAML declarative profile loader.
* `tests/integration/test_pipeline.py`: Full end-to-end multi-plugin agent pipeline, cascade disposal, and subagent isolation.

---

## License
MIT

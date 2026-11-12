# Anima

The standalone, lightweight composable micro-kernel powering **Magitech**.

Built around the core philosophy: **"Everything is a Component"**.

Documentation: [AGENTS.md](AGENTS.md) · [CONTEXT.md](CONTEXT.md) · [docs/adr/](docs/adr/)

[![Coverage](https://img.shields.io/badge/coverage-100%25-brightgreen)](.github/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.12%2B-blue)](pyproject.toml)

---

## Status

Anima is at `0.2.0` and under active development. **The API is not yet stable and may change without notice.**

---

## Install

Anima is not published to PyPI. Install from a repository checkout:

```sh
git clone https://github.com/magitech-ai/anima.git
cd anima
uv sync
```

Requires Python 3.12+ and [`uv`](https://docs.astral.sh/uv/).

## Run

Run a declarative profile (default `anima.yaml`):

```sh
anima run
anima run my-profile.yaml
```

From a repository checkout:

```sh
uv run anima run
uv run anima run my-profile.yaml
```

The profile file must exist: if it does not, the command exits with code 0 and loads nothing.

---

## Quickstart

```python
import asyncio

from anima import Context, Service


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

## Configuration

Profiles compose components from YAML. Each entry is a `module:component` target, or a mapping with a `config` block:

```yaml
components:
  - my_app:greeter
  - name: my_app:worker
    config:
      mode: turbo
```

Load a profile with `load_profile(ctx, "anima.yaml")`. Environment variables interpolate as `${VAR}`, `${env.VAR}`, or `${VAR:default}`; a full-match interpolation coerces `"true"` / `"false"` (any case) / numbers to `bool` / `int` / `float`. See `src/anima/loader.py`.

---

## Primitives

* **`Context`**: Central dependency container and service registry. Sub-contexts (`ctx.extend()`, `ctx.isolate()`) provide isolated realms for subagents and sandboxed components.

* **`Service`**: Base class for named capabilities mounted onto `ctx`.

* **`Fiber`**: Runtime state machine (`PENDING` ➔ `LOADING` ➔ `ACTIVE` ➔ `UNLOADING` ➔ `DISPOSED`; `FAILED` on error). Handles deferred loading via `inject = [...]`.

* **Reversible Effects (`ctx.effect`)**: Any registration, tool addition, or watcher returns a disposer. When a component unloads, all effects unwind cleanly in reverse order (LIFO).

* **The 5 Event Modes**:
  * `emit`: Synchronous broadcast (fire-and-forget).
  * `parallel`: Asynchronous concurrent broadcast (`asyncio.gather`).
  * `serial`: Sequential async execution; bails on first non-None, non-False result.
  * `bail`: Synchronous version of serial.
  * `waterfall`: Around-middleware pipeline passing `(*args, next)`. Enables non-invasive request rewrites, security guards, and vetoes.

* **Service Isolation Realms (`ctx.isolate`)**: Child scopes that prevent service leakage or pollution to/from parent contexts.

* **Declarative YAML Profiles (`load_profile`)**: Composes ordered bundles and component entries dynamically from YAML configuration.

---

## Subsystems

Every capability is a service mounted on a `Context`. The kernel ships these modules under `src/anima/`:

| Area | Modules | What they do |
| --- | --- | --- |
| Core | `core`, `events`, `tree`, `registry`, `reflect` | Context and service registry, the 5 event modes, loader entry tree and composite component groups, component runtime registry, service introspection |
| Loading & config | `loader`, `include`, `schema`, `pkg`†, `cli_env`†, `utils` | YAML profile loading, module resolution, multi-file includes with journaling and patches, declarative schema validation, `uv` package management, cascading environment variables, shared utility helpers (plain-object checks, time, binary) |
| Server & network | `server`†, `server_acl`†, `server_echo`†, `server_proxy`†, `server_static`†, `http`†, `http_socks`† | Async HTTP and WebSocket server, host/loopback access control, echo, reverse proxy, and static file routes; async HTTP client with SOCKS4/5 support |
| Runtime services | `timer`, `watcher`†, `logger`, `hmr` | Reversible timers (timeouts, intervals, debounce, throttle), filesystem change detection, structured namespaced logging, hot module replacement |
| Interface | `cli`†, `cli_create`† | Command router and profile execution substrate; interactive project scaffolding via the `create` subcommand (`anima create <name>`, default `my-app`) or the `Scaffold` library API (`from anima.cli_create import Scaffold`; `Scaffold.start()` runs the flow; template fetched from the npm registry) |

† Anima-original surface: not part of the ported kernel.

---

## Development

`anima` enforces **100% statement and 100% branch test coverage** (`--cov-fail-under=100` in `pyproject.toml`):

```bash
uv run pytest
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy src
```

* `tests/unit/anima/`: Mirrors `src/` 1:1 — core, events, loader, CLI, server, HTTP, timer, watcher, and every other module.
* `tests/integration/`: End-to-end multi-component agent pipeline, cascade disposal, subagent isolation, and extension seams.

See [AGENTS.md](AGENTS.md) for the full engineering standards.

---

## Documentation

* [AGENTS.md](AGENTS.md) — agent guidelines and engineering standards.
* [CONTEXT.md](CONTEXT.md) — canonical vocabulary: context, service, component entrypoint, waterfall, disposer.
* [docs/adr/](docs/adr/) — architecture decision records (waterfall pipeline, disposer callables, hierarchical contexts).
* [docs/agents/](docs/agents/) — domain docs, issue tracker, and triage labels.

## Contributing

Follow [AGENTS.md](AGENTS.md): strict TDD, 100% coverage floor, `ruff`, strict `mypy`, `uv` exclusively.

## Community

Report bugs and request features via [GitHub Issues](https://github.com/magitech-ai/anima/issues).

---

## License

MIT License — Copyright (c) 2026 Magitech. See [LICENSE](LICENSE).

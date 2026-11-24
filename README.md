# Animas

The standalone, lightweight composable micro-kernel powering **Magitech**.

Built around the core philosophy: **"Everything is a Component"**.

Documentation: [AGENTS.md](AGENTS.md) · [CONTEXT.md](CONTEXT.md) · [docs/adr/](docs/adr/)

[![Coverage](https://img.shields.io/badge/coverage-100%25-brightgreen)](.github/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.12%2B-blue)](pyproject.toml)

---

## Status

Animas is at `1.0.0`. **The public API is now covered by a compatibility policy:** additions and fixes may arrive in `1.x`, but breaking changes require `2.0.0` and a deprecation period where feasible. See [COMPATIBILITY.md](docs/COMPATIBILITY.md). Animas is an independent project; the research paper is its design invariant and the Cordis conformance work is a completed historical milestone (see [Lineage](#lineage)).

---

## Lineage

Animas implements the paradigm formalized in
*"A Programming Paradigm for Spatiotemporal Composability"*
([arXiv:2608.25512](https://arxiv.org/abs/2608.25512)). The paper is the design
invariant; the asyncio runtime, component surface and product built on top are
ours. Animas is an independent project — see
[ADR 0017](docs/adr/0017-independence-and-stability.md).

During 2026-11 the kernel was conformance-checked against canonical Cordis
`v4.0.0-rc.13` (commit `bc9fae7`), with
[DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) at pin
`fcc0ad46` (`dsh v0.2.2-rc.1`) as the earlier conformance reference, retained as
historical provenance and a secondary conformance source. That milestone is
complete and recorded in [PARITY.md](PARITY.md),
[ADRs 0013–0016](docs/adr/) and the
[magitech-harness parity map](https://github.com/magitech-ai/magitech-harness/blob/main/PARITY.md).
No upstream release is a forward target.

Upstream projects are MIT-licensed; their copyright and permission notices are
reproduced verbatim in [NOTICE](NOTICE). Provenance is recorded, not erased.

---

## Install

Animas is not published to PyPI at this time (1.0.0 is the release candidate for a publisher to be added by the maintainer). Install from a repository checkout:

```sh
git clone https://github.com/magitech-ai/animas.git
cd animas
uv sync
```

Requires Python 3.12+ and [`uv`](https://docs.astral.sh/uv/).

## Profiles

Load a declarative profile from Python with `load_profile(ctx, path)`. Animas is a library kernel: it does not ship a CLI, HTTP server, or scaffolding command. Host applications build those surfaces on top.

---

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

## Configuration

Profiles compose components from YAML. Each entry is a `module:component` target, or a mapping with a `config` block:

```yaml
components:
  - my_app:greeter
  - name: my_app:worker
    config:
      mode: turbo
```

Load a profile with `load_profile(ctx, "animas.yaml")`. Environment variables interpolate as `${VAR}`, `${env.VAR}`, or `${VAR:default}`; a full-match interpolation coerces `"true"` / `"false"` (any case) / numbers to `bool` / `int` / `float`. See `src/animas/loader.py`.

---

## Primitives

* **`Context`**: Central dependency container and service registry. Sub-contexts (`ctx.extend()`, `ctx.isolate()`) provide isolated realms for subagents and sandboxed components.

* **`Service`**: Base class for named capabilities mounted onto `ctx`.

* **`Fiber`**: Runtime state machine (`PENDING` ➔ `LOADING` ➔ `ACTIVE` ➔ `UNLOADING` ➔ `DISPOSED`; `FAILED` on error). Handles deferred loading via `inject = [...]`.

  `flush()` drains scheduled tasks and returns, so a component whose injected service never appears simply stays `PENDING`. Call `ctx.check_pending_cycles()` after a boot drain to raise instead, with the services each stuck component waits for and, when the components declare what they serve (`provide` / `provides` / `services`), the closed loop by name: `circular dependency between components: a -> b -> a`.

* **Reversible Effects (`ctx.effect`)**: Any registration, tool addition, or watcher returns a disposer. When a component unloads, all effects unwind cleanly in reverse order (LIFO).

* **The 5 Event Modes**:
  * `emit`: Synchronous broadcast (fire-and-forget).
  * `parallel`: Asynchronous concurrent broadcast. Every listener settles before anything is reported: one failure re-raises the original exception unchanged, two or more raise an `ExceptionGroup` naming each failing listener in registration order.
  * `serial`: Sequential async execution; bails on first non-None, non-False result.
  * `bail`: Synchronous version of serial.
  * `waterfall`: Around-middleware pipeline passing `(*args, next)`. Enables non-invasive request rewrites, security guards, and vetoes.

* **Service Isolation Realms (`ctx.isolate`)**: Child scopes that prevent service leakage or pollution to/from parent contexts.

* **Declarative YAML Profiles (`load_profile`)**: Composes ordered bundles and component entries dynamically from YAML configuration.

---

## Subsystems

Every capability is a service mounted on a `Context`. The kernel ships these modules under `src/animas/`:

| Area | Modules | What they do |
| --- | --- | --- |
| Core | `core`, `events`, `tree`, `registry`, `reflect` | Context and service registry, the 5 event modes, loader entry tree and composite component groups, component runtime registry, service introspection |
| Loading & config | `loader`, `include`, `schema`, `utils` | YAML profile loading, module resolution, multi-file includes with journaling and patches, declarative schema validation, shared utility helpers (plain-object checks, time, binary) |
| Runtime services | `timer`, `logger`, `hmr`, `fs` | Reversible timers (timeouts, intervals, debounce, throttle), structured namespaced logging, hot module replacement, and the filesystem service with mountable providers and routed watches |

---

## Development

`animas` enforces **100% statement and 100% branch test coverage** (`--cov-fail-under=100` in `pyproject.toml`):

```bash
uv run pytest
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy src
```

* `tests/unit/animas/`: Mirrors `src/` 1:1 — core, events, loader, timer, fs, and every other module.
* `tests/integration/`: End-to-end multi-component pipeline, cascade disposal, and subagent isolation.

See [AGENTS.md](AGENTS.md) for the full engineering standards.

---

## Documentation

* [AGENTS.md](AGENTS.md) — agent guidelines and engineering standards.
* [CONTEXT.md](CONTEXT.md) — canonical vocabulary: context, service, component entrypoint, waterfall, disposer.
* [docs/COMPATIBILITY.md](docs/COMPATIBILITY.md) — 1.0 public API and versioning policy.
* [docs/adr/](docs/adr/) — architecture decision records (waterfall pipeline, disposer callables, hierarchical contexts, independence, citation and attribution policy).
* [docs/agents/](docs/agents/) — domain docs, issue tracker, and triage labels.
* [NOTICE](NOTICE) — upstream MIT attributions and lineage.
* [PARITY.md](PARITY.md) — completed rc.13 conformance milestone and verdicts (historical record).

## Contributing

Follow [AGENTS.md](AGENTS.md): strict TDD, 100% coverage floor, `ruff`, strict `mypy`, `uv` exclusively.

## Community

Report bugs and request features via [GitHub Issues](https://github.com/magitech-ai/animas/issues).

---

## License

MIT License — Copyright (c) 2026 Magitech. See [LICENSE](LICENSE).

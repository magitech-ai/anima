# Kernel

The standalone, zero-dependency composable micro-kernel powering **Something From Nothing**.

Built around the core philosophy: **"Everything is a Plugin"**.

---

## Primitives
* **`Context`**: Central dependency container and service registry. Sub-contexts (`ctx.extend()`) provide isolated realms for child plugins.
* **`Service`**: Base class for named capabilities mounted onto `ctx`.
* **`Fiber`**: Runtime state machine (`PENDING` ➔ `LOADING` ➔ `ACTIVE` ➔ `UNLOADING` ➔ `DISPOSED`). Handles deferred loading via `inject = [...]`.
* **Reversible Effects (`ctx.effect`)**: Any registration, tool addition, or watcher returns a disposer. When a plugin unloads, all effects unwind cleanly in reverse order (LIFO).
* **The 5 Event Modes**:
  * `emit`: Synchronous broadcast (fire-and-forget).
  * `parallel`: Asynchronous concurrent broadcast (`asyncio.gather`).
  * `serial`: Sequential async execution; bails on first non-None result.
  * `bail`: Synchronous version of serial.
  * `waterfall`: Around-middleware pipeline passing `(*args, next)`. Enables non-invasive request rewrites, security guards, and vetoes.
* **YAML Profile Loader**: Composes ordered bundles and plugin entries dynamically.

---

## Quickstart

```bash
cd /mnt/storage/projects/AI/kernel

# Run the test suite
uv run pytest -v
```

---

## License
MIT

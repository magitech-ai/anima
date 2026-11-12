"""
Hot Module Replacement (HMR) service for dynamic live reloading.

Ports the oracle's graph reload: a changed module is classified with its
transitive dependents (accepted) against everything else (declined);
accepted modules reload in dependency order, a failure rolls the module
state back, and the reload is reported with the oracle events
``hmr/change(url)`` and ``hmr/reload(reloads)``.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import importlib
import importlib.util
import inspect
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any, ClassVar

from anima.core import Context, Fiber, FiberState, Service
from anima.events import Disposer
from anima.include import Include

if TYPE_CHECKING:
    from anima.registry import ComponentRuntime

logger = logging.getLogger("anima.hmr")

_CONFIG_SUFFIXES = (".json", ".yaml", ".yml")
# (mtime, size) of a module file, or None when it cannot be read.
_Stat = tuple[float, int] | None


@dataclass
class Reload:
    """One reloaded plugin entry, as reported by the ``hmr/reload`` event."""

    filename: str
    runtime: ComponentRuntime | None = None


def _code_frame(lines: list[str], lineno: int, offset: int | None, message: str) -> str:
    """Render the offending source line with a caret pointer, if located."""
    width = len(str(min(len(lines), lineno + 1)))
    out: list[str] = []
    for index in range(max(0, lineno - 2), min(len(lines), lineno + 1)):
        out.append(f"{index + 1:>{width}} | {lines[index]}")
        if index + 1 == lineno and offset is not None:
            out.append(f"{' ' * width} | {' ' * (offset - 1)}^ {message}")
    return "\n".join(out)


def handle_error(exc: BaseException) -> None:
    """Log a reload failure, with a source frame for syntax errors.

    Mirrors the oracle's error reporting: a syntax error is rendered with
    its file location and code frame, anything else is a plain warning.
    """
    if isinstance(exc, SyntaxError) and exc.filename and exc.lineno is not None:
        try:
            lines = Path(exc.filename).read_text(encoding="utf-8").splitlines()
        except OSError:
            logger.warning("HMR reload failed: %s", exc)
            return
        frame = _code_frame(lines, exc.lineno, exc.offset, exc.msg or str(exc))
        logger.warning(
            "File: %s:%s:%s\n%s", exc.filename, exc.lineno, exc.offset or 0, frame
        )
        return
    logger.warning("HMR reload failed: %s", exc)


def _target_module_name(target: Any) -> str | None:
    """Return the module a component target was defined in, if any."""
    module = getattr(target, "__module__", None)
    return module if isinstance(module, str) else None


class HmrService(Service):
    """Hot Module Replacement service coordinating watcher events and fiber reloads."""

    inject: ClassVar[list[str]] = ["watcher"]

    def __init__(
        self,
        ctx: Context,
        config: dict[str, Any] | None = None,
    ) -> None:
        cfg = dict(config or {})
        debounce = cfg.get("debounce", 100)
        if isinstance(debounce, bool) or not isinstance(debounce, (int, float)):
            raise TypeError(
                f"debounce must be a number of milliseconds, got {debounce!r}"
            )
        if debounce < 0:
            raise ValueError(f"debounce must be >= 0, got {debounce}")
        full_reload = cfg.get("full_reload")
        if full_reload is not None and not callable(full_reload):
            raise TypeError(
                f"full_reload must be a callable or None, got {full_reload!r}"
            )
        externals = cfg.get("externals", [])
        if not isinstance(externals, (list, tuple)):
            raise TypeError(f"externals must be a list, got {externals!r}")
        root = cfg.get("root")
        ignored = cfg.get("ignored")
        interval = cfg.get("interval", 0.5)
        super().__init__(ctx, "hmr")
        self.root = Path(root).resolve() if root else Path.cwd().resolve()
        external_paths: set[Path] = set()
        external_names: set[str] = set()
        for entry in externals:
            if not isinstance(entry, (str, os.PathLike)):
                raise TypeError(
                    f"externals entries must be paths or names, got {entry!r}"
                )
            text = os.fspath(entry)
            if "/" in text or text.endswith(".py"):
                candidate = Path(text)
                if not candidate.is_absolute():
                    candidate = self.root / candidate
                external_paths.add(candidate.resolve())
            else:
                external_names.add(text)
        self.ignored = (
            ignored
            if ignored is not None
            else ["*.pyc", "__pycache__", ".git", ".pytest_cache", ".venv", "tests"]
        )
        self.interval = interval
        self._debounce_ms = float(debounce)
        self._full_reload = full_reload
        self._externals_paths = external_paths
        self._externals_names = external_names
        self._disposer: Disposer | None = None
        self._active = False
        # Pending changes: module name -> file stat at stash time.
        self._stashed: dict[str, _Stat] = {}
        # Stats already applied by a completed cycle.
        self._consumed: dict[str, _Stat] = {}
        self._reload_task: asyncio.Task[dict[str, bool]] | None = None
        self._debounce_task: asyncio.Task[None] | None = None
        # Parse cache: source path -> (stat, imported in-root modules).
        self._graph_cache: dict[Path, tuple[_Stat, frozenset[str]]] = {}
        self.ctx.effect(lambda: self.stop)

    def init(self) -> None:
        """Lifecycle hook when mounted as a component."""
        self.start()

    def start(self) -> None:
        """Start watching root directory for Python module changes."""
        if self._active:
            return

        watcher = self.ctx.watcher
        self._disposer = watcher.watch(
            self.root,
            self._on_file_change,
            interval=self.interval,
            ignored=self.ignored,
            ignore_initial=True,
        )
        self._active = True

    def stop(self) -> None:
        """Stop watching and cleanup active handlers."""
        task = self._debounce_task
        self._debounce_task = None
        if task is not None:
            task.cancel()
        if self._disposer is not None:
            self._disposer()
            self._disposer = None
        self._active = False

    # -- dependency graph (L13) ------------------------------------------------

    def _build_graph(self) -> dict[str, set[str]]:
        """Map each loaded in-root module to the in-root modules it imports.

        Modules outside the watched root (framework, stdlib, site-packages)
        are external leaves: never traversed, never reloaded.
        """
        nodes: dict[str, tuple[Path, _Stat]] = {}
        for name, mod in list(sys.modules.items()):
            if mod is None:
                continue
            mod_file = getattr(mod, "__file__", None)
            if not isinstance(mod_file, str):
                continue
            path = Path(mod_file)
            if path.suffix != ".py":
                continue
            try:
                resolved = path.resolve()
                stat = path.stat()
            except (ValueError, OSError):
                continue
            if not resolved.is_relative_to(self.root):
                continue
            nodes[name] = (resolved, (stat.st_mtime, stat.st_size))
        node_paths = {name: path for name, (path, _stat) in nodes.items()}
        return {
            name: self._parse_imports(name, path, stat, node_paths)
            for name, (path, stat) in nodes.items()
        }

    def _parse_imports(
        self,
        name: str,
        path: Path,
        stat: _Stat,
        nodes: dict[str, Path],
    ) -> set[str]:
        """Return the in-root modules imported by one source file."""
        cached = self._graph_cache.get(path)
        if cached is not None and cached[0] == stat:
            return set(cached[1])
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError, ValueError):
            edges: set[str] = set()
        else:
            edges = self._collect_imports(name, tree, nodes)
        self._graph_cache[path] = (stat, frozenset(edges))
        return edges

    def _collect_imports(
        self, name: str, tree: ast.Module, nodes: dict[str, Path]
    ) -> set[str]:
        """Collect imported in-root module names from a parsed module."""
        mod = sys.modules.get(name)
        package = getattr(mod, "__package__", None) or name.rpartition(".")[0]
        edges: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    target = self._longest_loaded(alias.name, nodes)
                    if target is not None:
                        edges.add(target)
            elif isinstance(node, ast.ImportFrom):
                base = self._relative_base(node, package)
                if base is None:
                    continue
                if node.module:
                    dotted = f"{base}.{node.module}" if base else node.module
                    target = self._longest_loaded(dotted, nodes)
                    if target is not None:
                        edges.add(target)
                for alias in node.names:
                    if alias.name == "*":
                        continue
                    candidate = f"{base}.{alias.name}" if base else alias.name
                    if candidate in nodes:
                        edges.add(candidate)
        return edges

    @staticmethod
    def _relative_base(node: ast.ImportFrom, package: str) -> str | None:
        """Resolve a relative import's base package, or None if it escapes."""
        if node.level == 0:
            return ""
        parts = package.split(".") if package else []
        if node.level - 1 > len(parts):
            return None
        return ".".join(parts[: len(parts) - (node.level - 1)])

    @staticmethod
    def _longest_loaded(dotted: str, nodes: dict[str, Path]) -> str | None:
        """Return the longest dotted prefix that is a tracked module."""
        parts = dotted.split(".")
        for end in range(len(parts), 0, -1):
            candidate = ".".join(parts[:end])
            if candidate in nodes:
                return candidate
        return None

    def _analyze_changes(
        self, stashed: set[str], graph: dict[str, set[str]] | None = None
    ) -> tuple[set[str], set[str]]:
        """Classify changed files into accepted and declined sets.

        A file is accepted when it changed or when it (transitively)
        imports a changed file; everything else tracked is declined.
        """
        if graph is None:
            graph = self._build_graph()
        dependents: dict[str, set[str]] = {}
        for mod, imports in graph.items():
            for dep in imports:
                dependents.setdefault(dep, set()).add(mod)
        accepted = set(stashed)
        queue = list(stashed)
        while queue:
            current = queue.pop()
            for dependent in dependents.get(current, ()):
                if dependent not in accepted:
                    accepted.add(dependent)
                    queue.append(dependent)
        declined = set(graph) - accepted
        return accepted, declined

    @staticmethod
    def _topological_order(accepted: set[str], graph: dict[str, set[str]]) -> list[str]:
        """Order accepted modules dependencies-first for reloading.

        A cycle falls back to deterministic (sorted) order for the
        remaining modules; bindings across a cycle may need a second
        change event to settle.
        """
        remaining = {
            mod: {dep for dep in graph.get(mod, ()) if dep in accepted}
            for mod in accepted
        }
        order: list[str] = []
        ready = sorted(mod for mod, deps in remaining.items() if not deps)
        while ready:
            current = ready.pop(0)
            order.append(current)
            for mod, deps in remaining.items():
                if current in deps:
                    deps.discard(current)
                    if not deps:
                        ready.append(mod)
            ready.sort()
        if len(order) < len(accepted):
            order.extend(sorted(accepted - set(order)))
        return order

    # -- change intake (L14) ----------------------------------------------------

    def _is_external_path(self, res_path: Path) -> bool:
        """Check whether a resolved path is an external (framework) file."""
        if res_path in self._externals_paths:
            return True
        try:
            res_path.relative_to(self.root)
        except ValueError:
            return True
        return False

    async def _on_external(self, url: str) -> None:
        """Handle a change to an external file via the full-reload hook."""
        if self._full_reload is None:
            logger.warning(
                "External file changed (%s); full reload requested but no "
                "full_reload hook is configured",
                url,
            )
            return
        result = self._full_reload(url)
        if inspect.isawaitable(result):
            await result

    async def _refresh_include(self, res_path: Path) -> bool:
        """Refresh the Include backing a config file, if one is mounted."""
        loader = getattr(self.ctx, "loader", None)
        if loader is None:
            return False
        for entry in loader.entries():
            subtree = entry.subtree
            if not isinstance(subtree, Include):
                continue
            if subtree.filename != res_path:
                continue
            await subtree.refresh()
            return True
        return False

    async def _on_file_change(self, action: str, path: Path) -> None:
        """Process file change events emitted by the watcher."""
        if action not in ("change", "add"):
            return
        try:
            res_path = path.resolve()
        except OSError:
            return
        url = res_path.as_uri()
        if self._is_external_path(res_path):
            await self._on_external(url)
            return
        if res_path.suffix in _CONFIG_SUFFIXES:
            if await self._refresh_include(res_path):
                return
            self.ctx.emit("hmr/change", url)
            return
        if res_path.suffix != ".py":
            self.ctx.emit("hmr/change", url)
            return
        match = self._find_module_by_path(res_path)
        if match is None:
            # Unloaded module declared external by name: derive the
            # candidate name from the path (same derivation as the
            # fallback in _find_module_by_path) and check it before
            # falling through to hmr/change. By-path externals are
            # already handled above via _is_external_path, and the
            # path is under root here, so relative_to cannot fail.
            candidate = ".".join(res_path.relative_to(self.root).with_suffix("").parts)
            if candidate in self._externals_names:
                await self._on_external(url)
                return
            self.ctx.emit("hmr/change", url)
            return
        mod_name, _mod = match
        if mod_name in self._externals_names:
            await self._on_external(url)
            return
        self._stash(mod_name, self._target_stat(mod_name, path=res_path))
        self._schedule_partial_reload()

    def _find_module_by_path(self, path: Path) -> tuple[str, ModuleType] | None:
        """Resolve loaded sys.modules entry corresponding to a filesystem path."""
        try:
            res_path = path.resolve()
        except OSError:
            return None
        for name, mod in list(sys.modules.items()):
            if mod is None:
                continue
            mod_file = getattr(mod, "__file__", None)
            if mod_file is not None:
                try:
                    if Path(mod_file).resolve() == res_path:
                        return name, mod
                except (ValueError, OSError):
                    pass

        try:
            rel = res_path.relative_to(self.root)
            mod_candidate = ".".join(rel.with_suffix("").parts)
            if mod_candidate in sys.modules and sys.modules[mod_candidate] is not None:
                return mod_candidate, sys.modules[mod_candidate]
        except ValueError:
            pass

        return None

    def _target_stat(
        self,
        mod_name: str,
        *,
        mod: ModuleType | None = None,
        path: Path | None = None,
    ) -> _Stat:
        """(mtime, size) of the module file, or None if it cannot be read."""
        target_path = path
        if target_path is None:
            target_mod = mod if mod is not None else sys.modules.get(mod_name)
            if target_mod is None:
                return None
            m_file = getattr(target_mod, "__file__", None)
            if not m_file:
                return None
            target_path = Path(m_file)
        try:
            st = target_path.stat()
        except OSError:
            return None
        return (st.st_mtime, st.st_size)

    # -- reload machinery (L13/L15) ----------------------------------------------

    def _stash(self, mod_name: str, stat: _Stat) -> None:
        """Record a pending change for one module."""
        self._stashed[mod_name] = stat

    def _schedule_partial_reload(self) -> None:
        """(Re)start the debounce timer for a partial reload."""
        task = self._debounce_task
        self._debounce_task = None
        if task is not None:
            task.cancel()
        loop = asyncio.get_running_loop()
        self._debounce_task = loop.create_task(self._debounced_reload())

    async def _debounced_reload(self) -> None:
        """Wait out the debounce window, then drain the stash."""
        try:
            await asyncio.sleep(self._debounce_ms / 1000)
        except asyncio.CancelledError:
            return
        # The timer has fired: hand the drain over to the shared loop.
        # Clear the slot now, so a later reschedule cannot cancel this
        # task while it awaits the drain: cancelling a task awaiting the
        # drain cancels the drain itself (asyncio cancels the awaited
        # task too), and with it the in-flight fiber transitions.
        self._debounce_task = None
        await self._ensure_reload_loop()

    async def _ensure_reload_loop(self) -> dict[str, bool]:
        """Run partial-reload cycles until the stash is empty (single-flight).

        Concurrent callers share one drain task. A cycle consumes the stash
        snapshot; a file that changed again mid-cycle is picked up by a
        second cycle for the fresh content, while a re-stash of unchanged
        content is skipped via the consumed stats. Callers await the drain
        through a shield, so a cancelled caller cannot kill the shared
        drain (and its in-flight fiber transitions) with it.
        """
        task = self._reload_task
        if task is not None and not task.done():
            return await asyncio.shield(task)

        async def _drain() -> dict[str, bool]:
            outcomes: dict[str, bool] = {}
            try:
                while self._stashed:
                    snapshot = dict(self._stashed)
                    self._stashed.clear()
                    fresh = {
                        name: stat
                        for name, stat in snapshot.items()
                        if name not in self._consumed or self._consumed[name] != stat
                    }
                    if not fresh:
                        continue
                    ok = await self._partial_reload(fresh)
                    for name, stat in fresh.items():
                        outcomes[name] = ok
                        if ok:
                            self._consumed[name] = stat
            finally:
                self._reload_task = None
            return outcomes

        task = asyncio.ensure_future(_drain())
        self._reload_task = task
        return await asyncio.shield(task)

    async def reload_module(
        self,
        mod_name: str,
        *,
        mod: ModuleType | None = None,
        path: Path | None = None,
    ) -> ModuleType | None:
        """Reload a Python module and update associated active fibers.

        The module is stashed and reloaded through the shared graph cycle,
        so its dependents reload too. Concurrent reloads of unchanged
        content share one cycle; a reload that started before the file
        changed again is followed by a second cycle for the current
        content. Returns the module, or None when it is unknown or its
        cycle failed.
        """
        target_mod = mod if mod is not None else sys.modules.get(mod_name)
        if target_mod is None:
            return None
        stat = self._target_stat(mod_name, mod=mod, path=path)
        self._stash(mod_name, stat)
        outcomes = await self._ensure_reload_loop()
        if outcomes.get(mod_name, mod_name in self._consumed):
            return sys.modules.get(mod_name)
        return None

    async def _partial_reload(self, stashed: dict[str, _Stat]) -> bool:
        """Reload the accepted set for one stash snapshot; True on success."""
        graph = self._build_graph()
        accepted, _declined = self._analyze_changes(set(stashed), graph)
        order = self._topological_order(accepted, graph)
        backups: dict[str, tuple[ModuleType, dict[str, Any]]] = {}
        for name in order:
            mod = sys.modules.get(name)
            if mod is not None:
                backups[name] = (mod, dict(mod.__dict__))
        modules_before = set(sys.modules)
        # CPython's reload re-finds the spec via sys.path; make the parent
        # directories of the reloaded files importable for the cycle.
        added_dirs: list[str] = []
        for name in order:
            mod = sys.modules.get(name)
            if mod is None:
                continue
            mod_file = getattr(mod, "__file__", None)
            if not isinstance(mod_file, str):
                continue
            parent = str(Path(mod_file).parent)
            if parent not in sys.path and parent not in added_dirs:
                sys.path.insert(0, parent)
                added_dirs.append(parent)
        try:
            importlib.invalidate_caches()
            for name in order:
                mod = sys.modules.get(name)
                if mod is None:
                    continue
                # Delete the cached bytecode first: reload reuses it
                # when the source mtime (integer seconds) and size
                # match, so two same-size edits inside one second
                # would silently execute stale code. A missing or
                # undeletable cache is not an error.
                mod_file = getattr(mod, "__file__", None)
                if isinstance(mod_file, str):
                    with contextlib.suppress(ValueError, OSError):
                        cache = importlib.util.cache_from_source(mod_file)
                        Path(cache).unlink(missing_ok=True)
                importlib.reload(mod)
        except Exception as exc:  # noqa: BLE001 - any reload failure rolls back
            self._rollback(backups, modules_before)
            handle_error(exc)
            return False
        finally:
            for parent in added_dirs:
                if parent in sys.path:
                    sys.path.remove(parent)
        old_targets: list[tuple[Fiber, Any]] = []
        try:
            reloads = await self._reconcile(accepted, old_targets)
        except Exception as exc:  # noqa: BLE001 - any reconcile failure rolls back
            # Roll back module state BEFORE restoring fibers: the restore
            # re-executes the old component, and it must read the old
            # (rolled-back) module state, not the new broken one.
            self._rollback(backups, modules_before)
            await self._restore_fibers(old_targets)
            handle_error(exc)
            return False
        self.ctx.emit("hmr/reload", reloads)
        return True

    def _rollback(
        self,
        backups: dict[str, tuple[ModuleType, dict[str, Any]]],
        modules_before: set[str],
    ) -> None:
        """Restore pre-cycle module state: dict snapshots and sys.modules.

        The snapshot is shallow: re-bound names are restored, but
        in-place mutations of pre-existing mutable objects survive —
        the snapshot holds the same objects. A failed reload whose new
        code mutates shared mutable module state in place leaves that
        mutation behind.
        """
        for name in reversed(list(backups)):
            mod, snapshot = backups[name]
            mod.__dict__.clear()
            mod.__dict__.update(snapshot)
        for name in list(sys.modules):
            if name not in modules_before:
                sys.modules.pop(name, None)

    async def _reconcile(
        self, accepted: set[str], old_targets: list[tuple[Fiber, Any]]
    ) -> dict[Any, Reload]:
        """Re-mount fibers bound to reloaded modules; return the reload map."""
        reloads: dict[Any, Reload] = {}
        for fiber in self.ctx.root.all_fibers():
            if fiber.state == FiberState.DISPOSED:
                continue
            target = fiber.target
            mod_name = _target_module_name(target)
            if mod_name is None or mod_name not in accepted:
                continue
            reloaded = sys.modules.get(mod_name)
            new_target: Any = None
            if reloaded is not None and (isinstance(target, type) or callable(target)):
                attr_name = getattr(target, "__name__", None)
                if attr_name:
                    new_target = getattr(reloaded, attr_name, None)
            # Resolve the runtime before re-mounting: the registry is keyed
            # by the old target, and replace() does not re-key it.
            entry = self._make_reload(target, mod_name)
            if new_target is not None:
                old_targets.append((fiber, target))
                await fiber.replace(new_target)
            else:
                await fiber.restart()
            reloads.setdefault(fiber.target, entry)
        return reloads

    async def _restore_fibers(self, old_targets: list[tuple[Fiber, Any]]) -> None:
        """Best-effort restore of fibers already re-mounted in a failed cycle."""
        for fiber, old_target in old_targets:
            try:
                await fiber.replace(old_target)
            except Exception:
                logger.exception(
                    "Failed to restore fiber %s after reload rollback", fiber.name
                )

    def _make_reload(self, component: Any, mod_name: str) -> Reload:
        """Build one reload-map entry for a reloaded component."""
        mod = sys.modules.get(mod_name)
        filename = ""
        if mod is not None:
            mod_file = getattr(mod, "__file__", None)
            if mod_file:
                try:
                    filename = Path(mod_file).resolve().as_uri()
                except (ValueError, OSError, TypeError):
                    filename = str(mod_file)
        return Reload(filename=filename, runtime=self.ctx.registry.get(component))

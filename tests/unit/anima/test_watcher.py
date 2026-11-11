import asyncio
import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from anima.core import Context
from anima.watcher import WatchEntry, WatcherService


@pytest.mark.asyncio
async def test_watcher_service_directory_lifecycle(tmp_path: Path):
    ctx = Context()
    ctx.use(WatcherService)
    await ctx.flush()
    watcher: WatcherService = ctx.watcher

    events: list[tuple[str, Path]] = []
    bus_events: list[tuple[str, Path]] = []

    def on_change(ev_type: str, path: Path):
        events.append((ev_type, path))

    ctx.on("watcher/all", lambda ev_type, path: bus_events.append((ev_type, path)))

    dir_to_watch = tmp_path / "watch_dir"
    dir_to_watch.mkdir()

    initial_file = dir_to_watch / "init.txt"
    initial_file.write_text("v1")

    # Start watching with ignore_initial=True (default)
    disposer = watcher.watch_dir(dir_to_watch, on_change, interval=0.01)
    await asyncio.sleep(0.03)
    assert events == []
    assert bus_events == []

    # Modify existing file
    initial_file.write_text("v2_modified")
    await asyncio.sleep(0.04)
    assert any(ev == ("change", initial_file.resolve()) for ev in events)
    assert any(ev == ("change", initial_file.resolve()) for ev in bus_events)

    # Add a new file
    events.clear()
    bus_events.clear()
    new_file = dir_to_watch / "added.txt"
    new_file.write_text("hello")
    await asyncio.sleep(0.04)
    assert any(ev == ("add", new_file.resolve()) for ev in events)
    assert any(ev == ("add", new_file.resolve()) for ev in bus_events)

    # Delete the new file
    events.clear()
    bus_events.clear()
    new_file.unlink()
    await asyncio.sleep(0.04)
    assert any(ev == ("unlink", new_file.resolve()) for ev in events)
    assert any(ev == ("unlink", new_file.resolve()) for ev in bus_events)

    # Disposer terminates watching
    disposer()
    events.clear()
    initial_file.write_text("v3_after_dispose")
    await asyncio.sleep(0.04)
    assert events == []


@pytest.mark.asyncio
async def test_watcher_ignore_initial_false_and_ignored_rules(tmp_path: Path):
    ctx = Context()
    watcher = WatcherService(ctx)

    root = tmp_path / "root"
    root.mkdir()

    f1 = root / "valid.txt"
    f1.write_text("valid")

    f2 = root / "ignore.tmp"
    f2.write_text("temp")

    ignored_dir = root / ".git"
    ignored_dir.mkdir()
    (ignored_dir / "config").write_text("git config")

    events: list[tuple[str, Path]] = []

    async def async_cb(ev_type: str, path: Path):
        await asyncio.sleep(0.001)
        events.append((ev_type, path))

    # Test list ignored patterns and ignore_initial=False
    disposer = watcher.watch(
        root,
        async_cb,
        interval=0.01,
        ignored=["*.tmp", ".git"],
        ignore_initial=False,
    )

    await asyncio.sleep(0.03)
    # Should only report valid.txt on initial scan
    paths_reported = [p.name for _, p in events]
    assert "valid.txt" in paths_reported
    assert "ignore.tmp" not in paths_reported
    assert "config" not in paths_reported

    disposer()

    # Test callable ignored
    events.clear()
    disposer2 = watcher.watch(
        root,
        async_cb,
        interval=0.01,
        ignored=lambda p: p.name.startswith("valid"),
        ignore_initial=False,
    )
    await asyncio.sleep(0.03)
    paths_reported2 = [p.name for _, p in events]
    assert "valid.txt" not in paths_reported2
    disposer2()


@pytest.mark.asyncio
async def test_watcher_file_target(tmp_path: Path):
    ctx = Context()
    watcher = WatcherService(ctx)

    single_file = tmp_path / "single.txt"
    single_file.write_text("start")

    events: list[tuple[str, Path]] = []

    disposer = watcher.watch_file(
        single_file,
        lambda ev_type, p: events.append((ev_type, p)),
        interval=0.01,
    )

    await asyncio.sleep(0.03)
    assert events == []

    # Modify
    single_file.write_text("start updated")
    await asyncio.sleep(0.04)
    assert any(ev == ("change", single_file.resolve()) for ev in events)

    # Delete
    events.clear()
    single_file.unlink()
    await asyncio.sleep(0.04)
    assert any(ev == ("unlink", single_file.resolve()) for ev in events)

    disposer()


@pytest.mark.asyncio
async def test_watcher_error_handling_and_edge_cases(tmp_path: Path):
    ctx = Context()
    watcher = WatcherService(ctx)

    # Non-existent root
    non_existent = tmp_path / "does_not_exist"
    entry = WatchEntry(
        target_path=non_existent,
        callback=None,
        interval=0.01,
        ignored=None,
        ignore_initial=True,
    )
    assert watcher._scan(non_existent, entry) == {}

    # File target that throws on stat
    f = tmp_path / "f.txt"
    f.write_text("content")
    file_entry = WatchEntry(
        target_path=f,
        callback=None,
        interval=0.01,
        ignored=None,
        ignore_initial=True,
    )
    with patch.object(watcher, "_stat_file", side_effect=FileNotFoundError):
        assert watcher._scan(f, file_entry) == {}

    # Directory walk with stat error
    d = tmp_path / "d"
    d.mkdir()
    (d / "file.txt").write_text("hi")
    dir_entry = WatchEntry(
        target_path=d,
        callback=None,
        interval=0.01,
        ignored=None,
        ignore_initial=True,
    )
    with patch.object(watcher, "_stat_file", side_effect=PermissionError):
        assert watcher._scan(d, dir_entry) == {}

    # Directory walk with os.walk error
    with patch("os.walk", side_effect=FileNotFoundError):
        assert watcher._scan(d, dir_entry) == {}

    # Callback raising exception
    def failing_cb(ev, p):
        raise RuntimeError("boom")

    test_file = tmp_path / "throw.txt"
    test_file.write_text("v1")

    # Rewrite with a strictly increasing mtime: _scan keys state on
    # (st_mtime, st_size), and same-size writes in quick succession can
    # share one mtime tick, so a change would go undetected and the
    # branches below would never run.
    mtime = [time.time() + 2]

    def rewrite(content: str) -> None:
        test_file.write_text(content)
        mtime[0] += 1
        os.utime(test_file, (mtime[0], mtime[0]))

    poll_entry = WatchEntry(
        target_path=test_file,
        callback=failing_cb,
        interval=0.01,
        ignored=None,
        ignore_initial=True,
    )
    await watcher._poll_once(poll_entry, initial=True)
    rewrite("v2")
    # Shouldn't raise
    await watcher._poll_once(poll_entry, initial=False)

    # Context parallel raising exception
    with patch.object(ctx, "parallel", side_effect=RuntimeError("bus fail")):
        rewrite("v3")
        await watcher._poll_once(poll_entry, initial=False)

    # Stopped midway through event emission
    stop_entry = WatchEntry(
        target_path=test_file,
        callback=None,
        interval=0.01,
        ignored=None,
        ignore_initial=True,
    )
    await watcher._poll_once(stop_entry, initial=True)
    rewrite("v4")
    stop_entry.stopped = True
    await watcher._poll_once(stop_entry, initial=False)

    # Watch when no running event loop
    with patch("asyncio.get_running_loop", side_effect=RuntimeError):
        disp = watcher.watch(test_file, interval=0.01)
        disp()

    # Context disposal effect triggers watcher.stop
    new_ctx = Context()
    fib = new_ctx.use(WatcherService)
    await new_ctx.flush()
    watcher_instance: WatcherService = new_ctx.watcher
    watcher_instance.watch(test_file, interval=0.01)
    assert len(watcher_instance._entries) == 1
    await fib.dispose()
    assert len(watcher_instance._entries) == 0

    # Test file target when file matches ignore pattern
    ignored_file_entry = WatchEntry(
        target_path=test_file,
        callback=None,
        interval=0.01,
        ignored=["*.txt"],
        ignore_initial=True,
    )
    assert watcher._scan(test_file, ignored_file_entry) == {}

    # Test poll_once with callback=None
    no_cb_entry = WatchEntry(
        target_path=test_file,
        callback=None,
        interval=0.01,
        ignored=None,
        ignore_initial=False,
    )
    await watcher._poll_once(no_cb_entry, initial=False)

    # Test _poll_loop breaking on entry.stopped
    loop_entry = WatchEntry(
        target_path=test_file,
        callback=None,
        interval=0.02,
        ignored=None,
        ignore_initial=True,
    )
    loop_task = asyncio.create_task(watcher._poll_loop(loop_entry))
    await asyncio.sleep(0.005)
    loop_entry.stopped = True
    await loop_task

    # Test _poll_loop exiting while condition directly
    exit_entry = WatchEntry(
        target_path=test_file,
        callback=None,
        interval=0.01,
        ignored=None,
        ignore_initial=True,
    )
    exit_entry.stopped = True
    await watcher._poll_loop(exit_entry)

    # Test stop() with entry without task or with done task
    entry_without_task = WatchEntry(
        target_path=test_file,
        callback=None,
        interval=0.01,
        ignored=None,
        ignore_initial=True,
    )
    watcher._entries.add(entry_without_task)
    watcher.stop()
    assert len(watcher._entries) == 0

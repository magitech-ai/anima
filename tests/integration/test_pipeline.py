from pathlib import Path
from typing import Any

import pytest
import yaml

from anima.cli import CliService
from anima.core import Context, EventMode, FiberState, Service
from anima.http import HttpService
from anima.include import IncludeService, Journal
from anima.loader import load_profile
from anima.logger import Exporter, Logger, Message
from anima.schema import SchemaError, z
from anima.server import ServerService, WebSocket


class MockModelProvider(Service):
    def __init__(self, ctx: Context, model_name: str = "mock-gpt") -> None:
        super().__init__(ctx, "provider")
        self.model_name = model_name
        self.generation_count = 0

    async def generate(self, prompt: str) -> str:
        self.generation_count += 1
        return f"[{self.model_name}] response to: {prompt}"


class MockToolRegistry(Service):
    def __init__(self, ctx: Context) -> None:
        super().__init__(ctx, "tools")
        self.tools: dict[str, Any] = {}

    def register(self, name: str, fn: Any) -> None:
        self.tools[name] = fn

    def execute(self, name: str, *args: Any, **kwargs: Any) -> Any:
        if name not in self.tools:
            raise KeyError(f"Tool {name} not found")
        return self.tools[name](*args, **kwargs)


class MockAgent(Service):
    def __init__(self, ctx: Context) -> None:
        super().__init__(ctx, "agent")
        self.history: list[dict[str, str]] = []

    async def run(self, user_prompt: str) -> str:
        # Use waterfall middleware pipeline for request processing
        processed_prompt = await self.ctx.waterfall("agent/prompt", user_prompt)
        raw_response = await self.ctx.provider.generate(processed_prompt)
        final_response = await self.ctx.waterfall("agent/response", raw_response)
        self.history.append({"prompt": processed_prompt, "response": final_response})
        return final_response


MockAgent.inject = ["provider", "tools"]


@pytest.mark.asyncio
async def test_end_to_end_agent_pipeline():
    """Verify an end-to-end agent pipeline with IoC injection, waterfall middleware, and effects."""
    ctx = Context()

    # 1. Mount provider and tool registry
    _provider_fib = ctx.plugin(MockModelProvider, model_name="magitech-core-v1")
    _tools_fib = ctx.plugin(MockToolRegistry)
    agent_fib = ctx.plugin(MockAgent)

    await ctx.flush()

    assert ctx.has("provider")
    assert ctx.has("tools")
    assert ctx.has("agent")
    assert agent_fib.state == FiberState.ACTIVE

    # Register a tool
    ctx.tools.register("calculator", lambda a, b: a + b)
    assert ctx.tools.execute("calculator", 3, 4) == 7

    # 2. Add waterfall middleware for prompt augmentation and response filtering
    async def prompt_sanitizer(prompt: str, next_fn):
        clean = prompt.strip()
        augmented = f"<system>Safe</system> {clean}"
        return await next_fn(augmented)

    async def response_tagger(response: str, next_fn):
        res = await next_fn(response)
        return f"{res} [verified]"

    ctx.on("agent/prompt", prompt_sanitizer, mode=EventMode.WATERFALL)
    ctx.on("agent/response", response_tagger, mode=EventMode.WATERFALL)

    # 3. Execute agent
    reply = await ctx.agent.run("  hello magitech!  ")
    assert (
        reply
        == "[magitech-core-v1] response to: <system>Safe</system> hello magitech! [verified]"
    )
    assert ctx.provider.generation_count == 1
    assert len(ctx.agent.history) == 1


@pytest.mark.asyncio
async def test_agent_cascade_disposal_and_recovery():
    """Verify cascading service disposal when an upstream dependency is unmounted."""
    ctx = Context()

    provider_fib = ctx.plugin(MockModelProvider)
    _tools_fib = ctx.plugin(MockToolRegistry)
    agent_fib = ctx.plugin(MockAgent)
    await ctx.flush()

    assert agent_fib.state == FiberState.ACTIVE

    # Now unmount provider -> agent should automatically unload and become DISPOSED
    await provider_fib.dispose()
    await ctx.flush()

    assert not ctx.has("provider")
    assert agent_fib.state == FiberState.DISPOSED

    # Re-mount a new provider instance
    _new_provider_fib = ctx.plugin(MockModelProvider, model_name="magitech-v2")
    # Re-mount agent
    new_agent_fib = ctx.plugin(MockAgent)
    await ctx.flush()

    assert ctx.has("provider")
    assert new_agent_fib.state == FiberState.ACTIVE
    assert ctx.provider.model_name == "magitech-v2"


@pytest.mark.asyncio
async def test_subagent_service_isolation_realms():
    """Verify that child contexts with isolated realms run independent tools without parent pollution."""
    parent_ctx = Context()

    # Root services
    parent_ctx.plugin(MockModelProvider, model_name="global-llm")
    parent_ctx.plugin(MockToolRegistry)
    await parent_ctx.flush()

    parent_ctx.tools.register("global_tool", lambda: "global_tool_output")

    # Spawn subagent context with isolated 'tools' service
    subagent_ctx = parent_ctx.isolate("tools", label="isolated_subagent")

    assert subagent_ctx.has("provider")
    assert not subagent_ctx.has("tools")

    # Subagent gets its own tools service
    subagent_ctx.plugin(MockToolRegistry)
    await subagent_ctx.flush()

    assert subagent_ctx.has("tools")
    subagent_ctx.tools.register("sandbox_tool", lambda: "sandbox_success")

    # Subagent can execute sandbox tool, but parent cannot
    assert subagent_ctx.tools.execute("sandbox_tool") == "sandbox_success"
    with pytest.raises(KeyError):
        parent_ctx.tools.execute("sandbox_tool")

    # Parent has global_tool, subagent's isolated tools does not
    assert parent_ctx.tools.execute("global_tool") == "global_tool_output"
    with pytest.raises(KeyError):
        subagent_ctx.tools.execute("global_tool")


@pytest.mark.asyncio
async def test_declarative_profile_integration(tmp_path: Path, monkeypatch):
    """Verify loading an entire agent configuration declaratively from a profile YAML file."""
    import sys
    import types

    agent_pkg = types.ModuleType("agent_demo_pkg")
    agent_pkg.MockModelProvider = MockModelProvider
    agent_pkg.MockToolRegistry = MockToolRegistry
    agent_pkg.MockAgent = MockAgent
    monkeypatch.setitem(sys.modules, "agent_demo_pkg", agent_pkg)

    profile_content = [
        "agent_demo_pkg:MockModelProvider",
        "agent_demo_pkg:MockToolRegistry",
        "agent_demo_pkg:MockAgent",
    ]

    profile_file = tmp_path / "agent_profile.yaml"
    profile_file.write_text(yaml.dump(profile_content), encoding="utf-8")

    ctx = Context()
    load_profile(ctx, profile_file)
    await ctx.flush()

    assert ctx.has("provider")
    assert ctx.has("tools")
    assert ctx.has("agent")

    res = await ctx.agent.run("automated test")
    assert "[mock-gpt] response to: automated test" in res

    # Introspect context hierarchy
    tree = ctx.dump_tree()
    assert set(tree["services"]) == {
        "provider",
        "tools",
        "agent",
        "loader",
        "registry",
        "reflect",
    }
    assert len(tree["fibers"]) == 3


@pytest.mark.asyncio
async def test_full_microkernel_ecosystem_integration(tmp_path: Path):
    """Verify integration of logger, timer, group, watcher, hmr, and loader."""
    import asyncio

    profile_data = {
        "env": {"TEST_INTERVAL": "0.01", "CUSTOM_LABEL": "eco_test"},
        "plugins": [
            {"id": "log_svc", "target": "logger"},
            {"id": "time_svc", "target": "timer"},
            {"id": "watch_svc", "target": "watcher"},
            {
                "id": "work_group",
                "target": "group",
                "entries": [
                    {
                        "id": "group_plugin",
                        "target": "tests.integration.test_pipeline:MockToolRegistry",
                    }
                ],
            },
        ],
    }

    profile_file = tmp_path / "ecosystem_profile.yaml"
    profile_file.write_text(yaml.dump(profile_data), encoding="utf-8")

    ctx = Context()
    loader = load_profile(ctx, profile_file)
    await ctx.flush()

    # Verify all expected services are mounted
    assert ctx.has("logger")
    assert ctx.has("timer")
    assert ctx.has("watcher")
    assert ctx.has("loader")
    assert "work_group" in loader.fibers

    # Use timer to debounce a contextual log
    log_messages = []

    class TestExporter(Exporter):
        def export(self, msg: Message) -> None:
            log_messages.append(Logger.format(self, msg))

    ctx.logger.exporter(TestExporter())

    debounced_log = ctx.timer.debounce(
        lambda: ctx.logger("test").info("system ready: %s", "online"), 0.01
    )
    debounced_log()
    debounced_log()
    await asyncio.sleep(0.03)

    assert any("system ready: online" in m for m in log_messages)

    # Watcher detecting file creation in tmp_path
    detected_files = []
    disp_watch = ctx.watcher.watch(
        tmp_path,
        lambda ev, p: detected_files.append((ev, p.name)),
        interval=0.01,
        ignore_initial=True,
    )
    await asyncio.sleep(0.02)

    marker = tmp_path / "marker.txt"
    marker.write_text("ok")
    await asyncio.sleep(0.03)
    assert any(name == "marker.txt" for _, name in detected_files)
    disp_watch()

    # Dynamic group disable and re-enable
    group_entry = loader.fibers["work_group"].target
    assert "group_plugin" in group_entry.entries
    await group_entry.disable("group_plugin")
    assert group_entry.entries["group_plugin"].disabled is True
    await group_entry.enable("group_plugin")
    assert group_entry.entries["group_plugin"].disabled is False

    # Unload and clean up root
    await ctx.root.flush()


@pytest.mark.asyncio
async def test_extended_ecosystem_network_cli_and_schema_integration(tmp_path: Path):
    """End-to-end integration scenario across Schema, Include, Server, HTTP, CLI, and IoC."""
    import asyncio

    # 1. Config Schema Validation on Plugin
    class SecureGatewayPlugin:
        name = "secure_gateway"
        Config = z.object(
            {
                "port": z.integer().min(1024).max(65535).default(8999),
                "api_key": z.string().required(),
                "rate_limit": z.integer().default(100),
            }
        )

        def __init__(self, ctx: Context, **kwargs: Any) -> None:
            self.ctx = ctx
            self.config = ctx.fiber.config

    ctx = Context()

    # Valid config
    fib_valid = ctx.plugin(SecureGatewayPlugin, port=9000, api_key="secret-key-123")
    assert fib_valid.config["port"] == 9000
    assert fib_valid.config["api_key"] == "secret-key-123"
    assert fib_valid.config["rate_limit"] == 100

    # Invalid config (missing required field api_key causes fiber to enter FAILED state)
    fib_invalid = ctx.plugin(SecureGatewayPlugin, port=9000)
    assert fib_invalid.state == FiberState.FAILED
    assert isinstance(fib_invalid.error, SchemaError)
    assert "missing required field 'api_key'" in str(fib_invalid.error)

    # 2. Multi-File Configuration Resolution ($include and Journal)
    inc = IncludeService(ctx)
    auth_file = tmp_path / "auth_config.yaml"
    auth_file.write_text("auth_mode: token\nrealm: internal\n", encoding="utf-8")

    cluster_file = tmp_path / "cluster.yaml"
    cluster_file.write_text(
        "cluster_name: prod-east\nsecurity:\n  $include: ./auth_config.yaml\n  max_retries: 3\n",
        encoding="utf-8",
    )

    resolved_config = inc.resolve(cluster_file)
    assert resolved_config["cluster_name"] == "prod-east"
    assert resolved_config["security"]["auth_mode"] == "token"
    assert resolved_config["security"]["realm"] == "internal"
    assert resolved_config["security"]["max_retries"] == 3

    # Journal dynamic patch tracking
    journal = Journal()
    journal.record_upsert("cluster_name", {"status": "operational"})
    entries = [{"id": "cluster_name", "status": "initializing"}]
    updated_entries = journal.apply(entries)
    assert updated_entries[0]["status"] == "operational"

    # 3. Server Subsystem (HTTP + WebSocket) and HTTP Client Communication
    http_svc = HttpService(ctx)
    server_svc = ServerService(ctx)

    # HTTP Route
    server_svc.get(
        "/api/v1/health",
        lambda req, res: res.json(
            {"status": "ok", "cluster": resolved_config["cluster_name"]}
        ),
    )

    # WebSocket Echo Route
    received_ws_messages: list[str] = []

    async def ws_echo_handler(ws: WebSocket) -> None:
        async for msg in ws:
            received_ws_messages.append(msg)
            await ws.send_text(f"ACK:{msg}")

    server_svc.ws("/ws/telemetry", ws_echo_handler)

    await server_svc.listen(host="127.0.0.1", port=0)
    assert server_svc.port > 0

    try:
        # Client HTTP GET via ctx.http
        health_resp = await http_svc.get(
            f"{server_svc.url}/api/v1/health", response_type="json"
        )
        assert health_resp == {"status": "ok", "cluster": "prod-east"}

        # Client WebSocket Handshake & Frame Exchange
        reader, writer = await asyncio.open_connection("127.0.0.1", server_svc.port)
        key = "dGhlIHNhbXBsZSBub25jZQ=="
        upgrade_request = (
            f"GET /ws/telemetry HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{server_svc.port}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n\r\n"
        )
        writer.write(upgrade_request.encode("ascii"))
        await writer.drain()

        # Read handshake response
        handshake_data = await reader.readuntil(b"\r\n\r\n")
        assert b"101 Switching Protocols" in handshake_data

        # Send masked text frame "ping"
        payload = b"ping"
        mask = b"\xaa\xbb\xcc\xdd"
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        frame = bytes([0x81, 0x80 | len(payload)]) + mask + masked
        writer.write(frame)
        await writer.drain()

        # Read server text response frame
        header = await reader.readexactly(2)
        assert header[0] == 0x81  # text frame
        payload_len = header[1] & 0x7F
        msg_bytes = await reader.readexactly(payload_len)
        assert msg_bytes.decode("utf-8") == "ACK:ping"
        assert received_ws_messages == ["ping"]

        # Close client connection
        close_frame = bytes([0x88, 0x80]) + mask
        writer.write(close_frame)
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    finally:
        await server_svc.close()

    # 4. CLI Subsystem Execution
    cli_svc = CliService(ctx)
    cli_executed = []

    def handle_status(argv: Any, target: str, format: str = "json") -> str:
        verbose = argv.options.get("verbose", False)
        cli_executed.append((target, format, verbose))
        return f"Status of {target}: format={format}, verbose={verbose}"

    cli_svc.command("inspect <target> [format]").option("-v, --verbose").action(
        handle_status
    )

    cmd_res = await cli_svc.execute(["inspect", "gateway-cluster", "yaml", "--verbose"])
    assert cmd_res == "Status of gateway-cluster: format=yaml, verbose=True"
    assert cli_executed == [("gateway-cluster", "yaml", True)]

    # Final root flush
    await ctx.root.flush()

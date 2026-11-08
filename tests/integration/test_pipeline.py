from pathlib import Path
from typing import Any

import pytest
import yaml

from anima.core import Context, EventMode, FiberState, Service
from anima.loader import load_profile


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
    assert set(tree["services"]) == {"provider", "tools", "agent"}
    assert len(tree["fibers"]) == 3

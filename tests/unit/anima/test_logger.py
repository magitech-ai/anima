import io

import pytest

from anima.core import Context
from anima.logger import (
    ConsoleExporter,
    Exporter,
    Logger,
    LoggerLevel,
    LoggerService,
    Message,
)


def test_logger_level_enum_and_message():
    assert LoggerLevel.ERROR < LoggerLevel.WARN < LoggerLevel.INFO < LoggerLevel.DEBUG
    msg = Message(
        sn=1,
        ts=123.456,
        name="test",
        type="info",
        level=LoggerLevel.INFO,
        args=["hello"],
    )
    assert msg.sn == 1
    assert msg.args == ["hello"]


def test_base_exporter_not_implemented():
    exp = Exporter()
    msg = Message(sn=1, ts=0.0, name="x", type="info", level=1)
    with pytest.raises(NotImplementedError):
        exp.export(msg)


def test_format_token_interpolation_and_edge_cases():
    exp = Exporter(colors=True, max_length=1000)

    # Empty args
    msg_empty = Message(sn=1, ts=0.0, name="n", type="info", level=1, args=[])
    assert Logger.format(exp, msg_empty) == ""

    # First arg is exception
    msg_exc = Message(
        sn=1,
        ts=0.0,
        name="err",
        type="error",
        level=0,
        args=[ValueError("first exception")],
    )
    assert "first exception" in Logger.format(exp, msg_exc)

    # Custom formatter and escaped %%
    custom_exp = Exporter(
        colors=True,
        formatters={"u": lambda v, _e, _m: v.upper()},
    )
    msg_custom = Message(
        sn=2,
        ts=0.0,
        name="custom",
        type="info",
        level=1,
        args=["%% %u", "lower"],
    )
    assert "% LOWER" in Logger.format(custom_exp, msg_custom)

    # Tokens: %s, %d, %i, %f, %o, %O, %c, %C, %%, %z (unknown token)
    msg_tokens = Message(
        sn=2,
        ts=0.0,
        name="formatter",
        type="info",
        level=2,
        args=[
            "%s %d %i %f %o %c %C %% %z",
            "text",
            "12.8",
            "42",
            "3.14",
            {"key": "val"},
            "ignored",
            "colored",
        ],
    )
    res = Logger.format(exp, msg_tokens)
    assert "text" in res
    assert "12" in res
    assert "42" in res
    assert "3.14" in res
    assert '{"key": "val"}' in res

    # Bad numbers fallback
    msg_bad_nums = Message(
        sn=3,
        ts=0.0,
        name="bad",
        type="info",
        level=2,
        args=["%d %f", "not_a_num", "not_a_float"],
    )
    bad_res = Logger.format(exp, msg_bad_nums)
    assert "0" in bad_res
    assert "0.0" in bad_res

    # Non-serializable object for %o fallback
    class NonSerializable:
        def __repr__(self):
            return "<unserializable>"

    msg_non_json = Message(
        sn=4,
        ts=0.0,
        name="unjson",
        type="info",
        level=2,
        args=["%o", NonSerializable()],
    )
    assert "<unserializable>" in Logger.format(exp, msg_non_json)

    # Tail args formatting
    msg_tail = Message(
        sn=5,
        ts=0.0,
        name="tail",
        type="info",
        level=2,
        args=["msg", {"a": 1}, 123],
    )
    tail_res = Logger.format(exp, msg_tail)
    assert '{"a": 1}' in tail_res
    assert "123" in tail_res

    # Line clipping past max_length
    exp_clipped = Exporter(max_length=15)
    long_line = "a" * 50
    msg_long = Message(
        sn=6, ts=0.0, name="long", type="info", level=2, args=[long_line]
    )
    clipped = Logger.format(exp_clipped, msg_long)
    assert len(clipped) == 18  # 15 + "..."
    assert clipped.endswith("...")

    # Colors disabled for %C
    exp_nocolor = Exporter(colors=False)
    msg_c = Message(
        sn=7, ts=0.0, name="tag", type="info", level=2, args=["%C", "plain"]
    )
    assert Logger.format(exp_nocolor, msg_c) == "plain"

    # First arg is non-string primitive (e.g. integer or dict)
    msg_first_nonstr = Message(
        sn=8, ts=0.0, name="tag", type="info", level=2, args=[{"first": "item"}]
    )
    assert '{"first": "item"}' in Logger.format(exp_nocolor, msg_first_nonstr)


def test_console_exporter_stream_output(monkeypatch):
    err_buf = io.StringIO()
    monkeypatch.setattr("sys.stderr", err_buf)
    default_exp = ConsoleExporter()
    default_msg = Message(
        sn=1, ts=0.0, name="def", type="info", level=2, args=["default stream"]
    )
    default_exp.export(default_msg)
    assert "default stream" in err_buf.getvalue()

    buf_color = io.StringIO()
    exp_color = ConsoleExporter(stream=buf_color, colors=2)
    msg = Message(
        sn=1,
        ts=0.0,
        name="my_svc",
        type="warn",
        level=LoggerLevel.WARN,
        args=["warning payload"],
    )
    exp_color.export(msg)
    out = buf_color.getvalue()
    assert "WARN" in out
    assert "[my_svc]" in out
    assert "warning payload" in out

    buf_plain = io.StringIO()
    exp_plain = ConsoleExporter(stream=buf_plain, colors=0)
    exp_plain.export(msg)
    plain_out = buf_plain.getvalue()
    assert "WARN [my_svc] warning payload\n" == plain_out


@pytest.mark.asyncio
async def test_logger_service_buffer_and_filtering():
    ctx = Context()
    svc = LoggerService(ctx, {"buffer_size": 3})
    ctx.provide("logger", svc)

    collected = []

    class MemoryExporter(Exporter):
        def export(self, message: Message):
            collected.append(message)

    d = svc.exporter(
        MemoryExporter(
            levels={"special": LoggerLevel.DEBUG, "default": LoggerLevel.WARN}
        )
    )

    log_root = svc()
    log_special = svc("special")

    # info on root should be filtered out because default level is WARN
    log_root.info("ignored info")
    assert len(collected) == 0

    # warn and error on root are emitted
    log_root.warning("warning item")
    log_root.error("error item")
    assert len(collected) == 2
    assert collected[0].args == ["warning item"]
    assert collected[1].args == ["error item"]

    # debug on special is emitted because levels["special"] is DEBUG
    log_special.debug("debug message")
    assert len(collected) == 3
    assert collected[2].args == ["debug message"]

    # Direct methods on service
    svc.error("direct error")
    svc.warning("direct warn")
    svc.info("direct info")
    svc.debug("direct debug")

    # Buffer size should not exceed 3
    assert len(svc.buffer) == 3

    # Disposing exporter
    d()
    svc.error("after unregister")
    # collected didn't grow because exporter was removed
    assert len(collected) == 5


@pytest.mark.asyncio
async def test_logger_exception_unwrapping():
    ctx = Context()
    svc = LoggerService(ctx)
    ctx.provide("logger", svc)

    logs = []

    class CapturingExporter(Exporter):
        def export(self, message: Message):
            logs.append(message)

    svc.exporter(CapturingExporter())

    # Exception with cause
    try:
        try:
            raise ValueError("root cause")
        except ValueError as cause:
            raise RuntimeError("outer error") from cause
    except RuntimeError as err:
        svc.error(err)

    assert len(logs) == 1
    assert "root cause" in str(logs[0].args[0])

    # Exception group / exceptions attribute
    class MockExceptionGroup(Exception):
        def __init__(self, msg, exceptions):
            super().__init__(msg)
            self.exceptions = exceptions

    eg = MockExceptionGroup("bundle", [ValueError("sub1"), KeyError("sub2")])
    svc.error(eg)
    assert len(logs) == 3
    assert "sub1" in str(logs[1].args[0])
    assert "sub2" in str(logs[2].args[0])


@pytest.mark.asyncio
async def test_logger_intercept_scoped_name_and_level():
    ctx = Context()
    ctx.use(LoggerService)
    await ctx.flush()

    messages = []

    class InterceptExporter(Exporter):
        def export(self, message: Message):
            messages.append(message)

    ctx.logger.exporter(InterceptExporter())

    # Scoped child context with intercepted logger configuration
    child = ctx.intercept(
        "logger", {"name": "scoped_agent", "level": LoggerLevel.DEBUG}
    )
    child_logger = child.logger()
    assert child_logger.name == "scoped_agent"
    assert child_logger.level == LoggerLevel.DEBUG

    child_logger.debug("scoped debug output")
    assert len(messages) == 1
    assert messages[0].name == "scoped_agent"
    assert messages[0].args == ["scoped debug output"]

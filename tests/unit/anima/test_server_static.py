"""Unit tests for Server Static extension."""

import asyncio
from pathlib import Path

import pytest

from anima.core import Context
from anima.http import HttpService
from anima.server import ServerService
from anima.server_static import ServerStatic, StaticConfig


@pytest.fixture
def fixtures_dir(tmp_path: Path) -> Path:
    # Set up fixtures structure
    root = tmp_path / "static_fixtures"
    root.mkdir()

    # Fixtures are byte-exact: write_bytes (not write_text) so the on-disk
    # bytes are identical on every platform. write_text translates "\n" to
    # "\r\n" on Windows and the static server serves bytes verbatim.
    (root / "hello.html").write_bytes(b"<h1>Hello</h1>\n")
    (root / "noext").write_bytes(b"plain text\n")
    (root / "page").write_bytes(b"<h1>Page without extension</h1>\n")
    (root / "page.html").write_bytes(b"<h1>Page with extension</h1>\n")
    (root / "index.html").write_bytes(b"<h1>Index</h1>\n")
    (root / ".secret").write_text("classified", encoding="utf-8")

    sub = root / "sub"
    sub.mkdir()
    (sub / "index.html").write_bytes(b"<h1>Sub Index</h1>\n")

    errors = root / "errors"
    errors.mkdir()
    (errors / "404.html").write_bytes(b"<h1>Not Found</h1>\n")

    return root


@pytest.mark.asyncio
async def test_static_serving_files(fixtures_dir: Path):
    ctx = Context()
    HttpService(ctx)
    srv = ServerService(ctx)
    ServerStatic(ctx, StaticConfig(root=str(fixtures_dir)))
    await srv.listen(host="127.0.0.1", port=0)

    try:
        # Existing file
        res1 = await ctx.http.get(f"{srv.url}/hello.html", response_type="text")
        assert res1 == "<h1>Hello</h1>\n"

        # Missing file -> 404
        with pytest.raises(Exception) as exc:
            await ctx.http.get(f"{srv.url}/nonexistent.html")
        assert exc.value.status == 404

        # File without extension
        res2 = await ctx.http.get(f"{srv.url}/noext", response_type="text")
        assert res2 == "plain text\n"
    finally:
        await srv.close()


@pytest.mark.asyncio
async def test_static_extensions_fallback(fixtures_dir: Path):
    ctx = Context()
    HttpService(ctx)
    srv = ServerService(ctx)
    ServerStatic(ctx, StaticConfig(root=str(fixtures_dir), extensions=[".html"]))
    await srv.listen(host="127.0.0.1", port=0)

    try:
        # Fallback to .html
        res = await ctx.http.get(f"{srv.url}/hello", response_type="text")
        assert res == "<h1>Hello</h1>\n"

        # Prefer exact file over extension fallback
        res_page = await ctx.http.get(f"{srv.url}/page", response_type="text")
        assert res_page == "<h1>Page without extension</h1>\n"
    finally:
        await srv.close()


@pytest.mark.asyncio
async def test_static_index_files_and_redirect(fixtures_dir: Path):
    ctx = Context()
    HttpService(ctx)
    srv = ServerService(ctx)
    ServerStatic(ctx, StaticConfig(root=str(fixtures_dir)))
    await srv.listen(host="127.0.0.1", port=0)

    try:
        # Root index
        res_root = await ctx.http.get(f"{srv.url}/", response_type="text")
        assert res_root == "<h1>Index</h1>\n"

        # Subdirectory index
        res_sub = await ctx.http.get(f"{srv.url}/sub/", response_type="text")
        assert res_sub == "<h1>Sub Index</h1>\n"

        # Subdirectory without trailing slash redirects 301
        reader, writer = await asyncio.open_connection("127.0.0.1", srv.port)
        writer.write(
            b"GET /sub HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n"
        )
        await writer.drain()
        raw_resp = await reader.read()
        writer.close()
        await writer.wait_closed()

        assert b"301 Moved Permanently" in raw_resp
        assert b"Location: /sub/" in raw_resp
    finally:
        await srv.close()


@pytest.mark.asyncio
async def test_static_redirect_disabled(fixtures_dir: Path):
    ctx = Context()
    HttpService(ctx)
    srv = ServerService(ctx)
    ServerStatic(ctx, StaticConfig(root=str(fixtures_dir), redirect=False))
    await srv.listen(host="127.0.0.1", port=0)

    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", srv.port)
        writer.write(
            b"GET /sub HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n"
        )
        await writer.drain()
        raw_resp = await reader.read()
        writer.close()
        await writer.wait_closed()

        assert b"301 Moved Permanently" not in raw_resp
    finally:
        await srv.close()


@pytest.mark.asyncio
async def test_static_path_traversal_protection(fixtures_dir: Path):
    ctx = Context()
    HttpService(ctx)
    srv = ServerService(ctx)
    ServerStatic(ctx, StaticConfig(root=str(fixtures_dir)))
    await srv.listen(host="127.0.0.1", port=0)

    try:
        # Raw HTTP request with path traversal
        reader, writer = await asyncio.open_connection("127.0.0.1", srv.port)
        writer.write(
            b"GET /..%2F..%2Fetc/passwd HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n"
        )
        await writer.drain()
        raw_resp = await reader.read()
        writer.close()
        await writer.wait_closed()

        assert b"403 Forbidden" in raw_resp
    finally:
        await srv.close()


@pytest.mark.asyncio
async def test_static_fallthrough(fixtures_dir: Path):
    ctx = Context()
    HttpService(ctx)
    srv = ServerService(ctx)
    ServerStatic(ctx, StaticConfig(root=str(fixtures_dir), fallthrough=True))

    srv.get("{/*path}", lambda req, res: res.text("fallback-handler"))
    await srv.listen(host="127.0.0.1", port=0)

    try:
        res = await ctx.http.get(f"{srv.url}/missing", response_type="text")
        assert res == "fallback-handler"
    finally:
        await srv.close()


@pytest.mark.asyncio
async def test_static_error_pages(fixtures_dir: Path):
    ctx = Context()
    HttpService(ctx)
    srv = ServerService(ctx)
    ServerStatic(
        ctx,
        StaticConfig(
            root=str(fixtures_dir),
            error_pages={404: "errors/404.html"},
        ),
    )
    await srv.listen(host="127.0.0.1", port=0)

    try:
        with pytest.raises(Exception) as exc:
            await ctx.http.get(f"{srv.url}/nonexistent")
        assert exc.value.status == 404
        assert exc.value.response.text() == "<h1>Not Found</h1>\n"
    finally:
        await srv.close()


@pytest.mark.asyncio
async def test_static_exclude_patterns(fixtures_dir: Path):
    ctx = Context()
    HttpService(ctx)
    srv = ServerService(ctx)
    ServerStatic(
        ctx,
        StaticConfig(
            root=str(fixtures_dir),
            exclude=["**/.*", "*.html"],
        ),
    )
    await srv.listen(host="127.0.0.1", port=0)

    try:
        # Excluded hidden file
        with pytest.raises(Exception) as exc1:
            await ctx.http.get(f"{srv.url}/.secret")
        assert exc1.value.status == 404

        # Excluded html file
        with pytest.raises(Exception) as exc2:
            await ctx.http.get(f"{srv.url}/hello.html")
        assert exc2.value.status == 404

        # Allowed file
        res = await ctx.http.get(f"{srv.url}/noext", response_type="text")
        assert res == "plain text\n"
    finally:
        await srv.close()


@pytest.mark.asyncio
async def test_static_download_mode(fixtures_dir: Path):
    ctx = Context()
    HttpService(ctx)
    srv = ServerService(ctx)
    ServerStatic(ctx, StaticConfig(root=str(fixtures_dir), download=True))
    await srv.listen(host="127.0.0.1", port=0)

    try:
        resp = await ctx.http.get(f"{srv.url}/hello.html", response_type="response")
        assert "attachment" in resp.headers.get("content-disposition", "")
    finally:
        await srv.close()


@pytest.mark.asyncio
async def test_static_coverage_edge_cases(fixtures_dir: Path):
    ctx = Context()
    HttpService(ctx)
    srv = ServerService(ctx)

    from anima.server_static import apply as static_apply

    # 1. Mount via dict config
    ServerStatic(
        ctx,
        {
            "root": str(fixtures_dir),
            "extensions": [".html"],
            "exclude": ["hello.html", "**/sub/index.html", "isolated_file"],
            "error_pages": {404: "missing_err.html"},
        },
    )

    # Mount via apply helper with file:// root
    ctx_helper = Context()
    HttpService(ctx_helper)
    ServerService(ctx_helper)
    static_apply(ctx_helper, root=f"file://{fixtures_dir}")

    # 2. Empty dir
    empty_dir = fixtures_dir / "empty"
    empty_dir.mkdir(exist_ok=True)
    (fixtures_dir / "sub" / "isolated_file").write_text("isolated", encoding="utf-8")

    await srv.listen(host="127.0.0.1", port=0)

    try:
        # Directory without index files -> 404
        with pytest.raises(Exception) as exc1:
            await ctx.http.get(f"{srv.url}/empty/")
        assert exc1.value.status == 404

        # Excluded index file -> 404
        with pytest.raises(Exception) as exc2:
            await ctx.http.get(f"{srv.url}/sub/")
        assert exc2.value.status == 404

        # Excluded extension fallback -> 404
        with pytest.raises(Exception) as exc3:
            await ctx.http.get(f"{srv.url}/hello")
        assert exc3.value.status == 404

        # Excluded basename pattern in subfolder
        with pytest.raises(Exception) as exc4:
            await ctx.http.get(f"{srv.url}/sub/isolated_file")
        assert exc4.value.status == 404

        # Unknown mime type (file without extension, e.g. /noext)
        resp2 = await ctx.http.get(f"{srv.url}/noext", response_type="response")
        assert resp2.headers.get("content-type") == "application/octet-stream"
    finally:
        await srv.close()

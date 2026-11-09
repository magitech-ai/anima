import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

from anima.core import Context
from anima.http import HttpConfig, HttpError, HttpService


class MockServerHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # Suppress console logging

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/json":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(
                json.dumps(
                    {"message": "success", "query": parse_qs(parsed.query)}
                ).encode("utf-8")
            )
        elif parsed.path == "/text":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"plain text response")
        elif parsed.path == "/error":
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": "server error"}).encode("utf-8"))
        elif parsed.path == "/headers":
            self.send_response(200)
            custom_hdr = self.headers.get("X-Custom", "")
            self.send_header("X-Echo", custom_hdr)
            self.end_headers()
            self.wfile.write(b"ok")
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        content_len = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_len).decode("utf-8") if content_len > 0 else ""
        self.send_response(201)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(
            json.dumps({"received": body, "method": "POST"}).encode("utf-8")
        )

    def do_PUT(self):
        content_len = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_len).decode("utf-8") if content_len > 0 else ""
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(
            json.dumps({"received": body, "method": "PUT"}).encode("utf-8")
        )

    def do_PATCH(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"method": "PATCH"}).encode("utf-8"))

    def do_DELETE(self):
        self.send_response(204)
        self.end_headers()

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("X-Head-Check", "true")
        self.end_headers()


@pytest.fixture(scope="module")
def local_server():
    server = HTTPServer(("127.0.0.1", 0), MockServerHandler)
    host, port = server.server_address
    base_url = f"http://{host}:{port}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield base_url
    server.shutdown()


@pytest.mark.asyncio
async def test_http_service_registration():
    ctx = Context()
    http_svc = HttpService(ctx)
    assert ctx.has("http")
    assert ctx.http == http_svc


@pytest.mark.asyncio
async def test_http_get_json_and_query_params(local_server: str):
    ctx = Context()
    HttpService(ctx)

    data = await ctx.http.get(
        f"{local_server}/json",
        params={"page": "1", "q": "test"},
        response_type="json",
    )
    assert data["message"] == "success"
    assert data["query"]["page"] == ["1"]
    assert data["query"]["q"] == ["test"]


@pytest.mark.asyncio
async def test_http_get_text_and_bytes(local_server: str):
    ctx = Context()
    HttpService(ctx)

    text = await ctx.http.get(f"{local_server}/text", response_type="text")
    assert text == "plain text response"

    raw_bytes = await ctx.http.get(f"{local_server}/text", response_type="bytes")
    assert raw_bytes == b"plain text response"


@pytest.mark.asyncio
async def test_http_post_put_patch_delete(local_server: str):
    ctx = Context()
    HttpService(ctx)

    # POST JSON
    post_res = await ctx.http.post(
        f"{local_server}/post",
        data={"name": "anima"},
        response_type="json",
    )
    assert post_res["method"] == "POST"
    assert json.loads(post_res["received"]) == {"name": "anima"}

    # PUT
    put_res = await ctx.http.put(
        f"{local_server}/put",
        data="raw-payload",
        response_type="json",
    )
    assert put_res["method"] == "PUT"
    assert put_res["received"] == "raw-payload"

    # PATCH
    patch_res = await ctx.http.patch(
        f"{local_server}/patch",
        response_type="json",
    )
    assert patch_res["method"] == "PATCH"

    # DELETE
    del_res = await ctx.http.delete(f"{local_server}/delete")
    assert del_res.status == 204


@pytest.mark.asyncio
async def test_http_head_and_headers_response(local_server: str):
    ctx = Context()
    HttpService(ctx)

    hdrs = await ctx.http.head(f"{local_server}/head", response_type="headers")
    assert hdrs.get("x-head-check") == "true"


@pytest.mark.asyncio
async def test_http_status_validation_and_errors(local_server: str):
    ctx = Context()
    HttpService(ctx)

    # 404 raises HttpError
    with pytest.raises(HttpError) as exc_404:
        await ctx.http.get(f"{local_server}/not_found")
    assert exc_404.value.status == 404

    # 500 raises HttpError
    with pytest.raises(HttpError) as exc_500:
        await ctx.http.get(f"{local_server}/error")
    assert exc_500.value.status == 500
    assert exc_500.value.response is not None
    assert exc_500.value.response.json() == {"error": "server error"}

    # Custom validate_status allowing 500
    res = await ctx.http.get(
        f"{local_server}/error",
        validate_status=lambda s: True,
    )
    assert res.status == 500


@pytest.mark.asyncio
async def test_http_intercept_and_base_url(local_server: str):
    ctx = Context()
    HttpService(ctx)

    # Child context with base_url and custom header intercept
    child = ctx.intercept(
        "http",
        {
            "base_url": local_server,
            "headers": {"X-Custom": "injected-header"},
        },
    )

    resp = await child.http.get("/headers", response_type="response")
    assert resp.status == 200
    assert resp.headers.get("x-echo") == "injected-header"


@pytest.mark.asyncio
async def test_http_waterfall_middleware_interception(local_server: str):
    ctx = Context()
    HttpService(ctx)

    intercepted = []

    async def auth_middleware(req, next_fn):
        intercepted.append(req.url)
        # Modify request headers
        req.headers["X-Custom"] = "middleware-auth"
        return await next_fn(req)

    ctx.on("http/fetch", auth_middleware)

    resp = await ctx.http.get(f"{local_server}/headers")
    assert len(intercepted) == 1
    assert resp.headers.get("x-echo") == "middleware-auth"


@pytest.mark.asyncio
async def test_http_direct_callable(local_server: str):
    ctx = Context()
    HttpService(ctx)

    # Invoke directly: ctx.http(url)
    res = await ctx.http(f"{local_server}/text", response_type="text")
    assert res == "plain text response"

    # Invoke directly with method: ctx.http("POST", url, data=...)
    res2 = await ctx.http(
        "POST",
        f"{local_server}/post",
        data={"val": 123},
        response_type="json",
    )
    assert res2["method"] == "POST"


@pytest.mark.asyncio
async def test_http_edge_cases_and_network_error(local_server: str):
    ctx = Context()
    http_svc = HttpService(ctx)

    # 1. merge_intercept branches
    merged = http_svc.merge_intercept(
        "non-dict",  # non-dict skipped
        {"timeout": 15.0},
        {"base_url": "http://example.com", "headers": {"H": "1"}},
    )
    assert merged["timeout"] == 15.0
    assert merged["base_url"] == "http://example.com"
    assert merged["headers"] == {"H": "1"}

    # Intercept with headers=None exercises the False branch in request
    no_hdr_child = ctx.intercept("http", {"headers": None})
    res_no_hdr = await no_hdr_child.http.get(
        f"{local_server}/text", response_type="text"
    )
    assert res_no_hdr == "plain text response"

    # 2. resolve_url with base_url without trailing slash, list params
    url = http_svc.resolve_url(
        "path", base_url="http://example.com", params={"tag": ["x", "y"]}
    )
    assert url == "http://example.com/path?tag=x&tag=y"

    # 3. POST with raw bytes and explicit timeout
    res = await ctx.http.post(
        f"{local_server}/post",
        data=b"byte-data",
        timeout=10.0,
        response_type="json",
    )
    assert res["received"] == "byte-data"

    # POST with explicit headers, pre-existing Content-Type, and integer data
    res_int = await ctx.http.post(
        f"{local_server}/post",
        data=999,
        headers={"Content-Type": "text/plain", "X-Req-Header": "req-val"},
        response_type="json",
    )
    assert res_int["received"] == "999"

    # POST dict with pre-existing Content-Type
    res_dict = await ctx.http.post(
        f"{local_server}/post",
        data={"k": "v"},
        headers={"Content-Type": "application/json; charset=utf-8"},
        response_type="json",
    )
    assert json.loads(res_dict["received"]) == {"k": "v"}

    # 4. Network error (connection refused on dead port)
    with pytest.raises(HttpError) as exc_net:
        await ctx.http.get("http://127.0.0.1:1/invalid_host", timeout=1.0)
    assert exc_net.value.code == "NETWORK_ERROR"

    # 5. Invalid arguments to __call__
    with pytest.raises(ValueError):
        await ctx.http(12345)

    # 6. HttpConfig dataclass instantiation
    cfg = HttpConfig(base_url="http://test.com", timeout=5.0)
    assert cfg.base_url == "http://test.com"
    assert cfg.timeout == 5.0

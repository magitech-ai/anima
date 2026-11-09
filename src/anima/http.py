"""Asynchronous HTTP client service substrate for Anima microkernel."""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from anima.core import Context, Service


class HttpError(Exception):
    """Raised when an HTTP request fails or status validation is rejected."""

    def __init__(
        self,
        message: str,
        status: int | None = None,
        response: HttpResponse | None = None,
        code: str = "HTTP_ERROR",
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.response = response
        self.code = code


@dataclass
class HttpResponse:
    """Encapsulation of an HTTP response."""

    status: int
    reason: str
    headers: dict[str, str]
    data: bytes
    url: str

    def text(self, encoding: str = "utf-8") -> str:
        """Decode response body bytes to string."""
        return self.data.decode(encoding)

    def json(self) -> Any:
        """Decode response body as parsed JSON."""
        return json.loads(self.text())


@dataclass
class HttpRequest:
    """Outgoing HTTP request specification."""

    method: str
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    data: bytes | None = None
    timeout: float = 30.0


@dataclass
class HttpConfig:
    """Configuration options for HTTP client requests."""

    base_url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    timeout: float = 30.0
    params: dict[str, Any] = field(default_factory=dict)
    data: Any = None
    validate_status: Callable[[int], bool] | None = None
    response_type: str = "response"


class HttpService(Service):
    """Contextual HTTP client with interceptor waterfalls and type decoders."""

    def __init__(self, ctx: Context, name: str = "http") -> None:
        super().__init__(ctx, name)

    def merge_intercept(self, *configs: dict[str, Any]) -> dict[str, Any]:
        """Deep merge intercept configurations for HTTP client."""
        merged: dict[str, Any] = {"headers": {}}
        for cfg in configs:
            if not isinstance(cfg, dict):
                continue
            if cfg.get("base_url"):
                merged["base_url"] = cfg["base_url"]
            if "timeout" in cfg and cfg["timeout"] is not None:
                merged["timeout"] = cfg["timeout"]
            if "headers" in cfg and isinstance(cfg["headers"], dict):
                merged["headers"].update(cfg["headers"])
        return merged

    def resolve_url(
        self,
        url: str,
        base_url: str | None = None,
        params: dict[str, Any] | None = None,
    ) -> str:
        """Combine base URL, path, and query parameters."""
        final_url = url
        if base_url:
            if not base_url.endswith("/") and not url.startswith("/"):
                base_url = base_url + "/"
            final_url = urllib.parse.urljoin(base_url, url)

        if params:
            parsed = urllib.parse.urlparse(final_url)
            existing_query = urllib.parse.parse_qsl(parsed.query)
            new_params: list[tuple[str, str]] = []
            for k, v in params.items():
                if isinstance(v, (list, tuple)):
                    for item in v:
                        new_params.append((k, str(item)))
                else:
                    new_params.append((k, str(v)))
            combined = existing_query + new_params
            query_string = urllib.parse.urlencode(combined)
            final_url = urllib.parse.urlunparse(
                (
                    parsed.scheme,
                    parsed.netloc,
                    parsed.path,
                    parsed.params,
                    query_string,
                    parsed.fragment,
                )
            )

        return final_url

    async def request(
        self,
        method: str,
        url: str,
        *,
        base_url: str | None = None,
        params: dict[str, Any] | None = None,
        data: Any = None,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
        validate_status: Callable[[int], bool] | None = None,
        response_type: str = "response",
    ) -> Any:
        """Execute an asynchronous HTTP request through waterfall pipeline."""
        intercept = self.resolve_intercept()
        effective_base_url = base_url or intercept.get("base_url")
        effective_timeout = (
            timeout if timeout is not None else intercept.get("timeout", 30.0)
        )

        merged_headers: dict[str, str] = {}
        headers_intercept = intercept.get("headers")
        if headers_intercept:
            merged_headers.update(headers_intercept)
        if headers:
            merged_headers.update(headers)

        final_url = self.resolve_url(url, effective_base_url, params)

        # Body serialization
        body_bytes: bytes | None = None
        if data is not None:
            if isinstance(data, (dict, list)):
                body_bytes = json.dumps(data).encode("utf-8")
                # Normalize Content-Type header
                has_content_type = any(
                    k.lower() == "content-type" for k in merged_headers
                )
                if not has_content_type:
                    merged_headers["Content-Type"] = "application/json"
            elif isinstance(data, str):
                body_bytes = data.encode("utf-8")
            elif isinstance(data, bytes):
                body_bytes = data
            else:
                body_bytes = str(data).encode("utf-8")

        req = HttpRequest(
            method=method.upper(),
            url=final_url,
            headers=merged_headers,
            data=body_bytes,
            timeout=effective_timeout,
        )

        async def _default_fetch(current_req: HttpRequest) -> HttpResponse:
            def _blocking_call() -> HttpResponse:
                urllib_req = urllib.request.Request(
                    url=current_req.url,
                    data=current_req.data,
                    headers=current_req.headers,
                    method=current_req.method,
                )
                try:
                    with urllib.request.urlopen(
                        urllib_req, timeout=current_req.timeout
                    ) as resp:
                        status = resp.status
                        reason = resp.reason
                        hdrs = {k.lower(): v for k, v in resp.headers.items()}
                        raw_data = resp.read()
                        return HttpResponse(
                            status=status,
                            reason=reason,
                            headers=hdrs,
                            data=raw_data,
                            url=current_req.url,
                        )
                except urllib.error.HTTPError as err:
                    err_hdrs = {k.lower(): v for k, v in err.headers.items()}
                    err_data = err.read()
                    return HttpResponse(
                        status=err.code,
                        reason=err.reason,
                        headers=err_hdrs,
                        data=err_data,
                        url=current_req.url,
                    )
                except urllib.error.URLError as err:
                    raise HttpError(
                        f"HTTP request to '{current_req.url}' failed: {err.reason}",
                        status=None,
                        code="NETWORK_ERROR",
                    ) from err

            return await asyncio.to_thread(_blocking_call)

        response: HttpResponse = await self.ctx.waterfall(
            "http/fetch", req, default_handler=_default_fetch
        )

        status_validator = validate_status or (lambda s: s < 400)
        if not status_validator(response.status):
            raise HttpError(
                f"HTTP {response.status} {response.reason} for {response.url}",
                status=response.status,
                response=response,
                code="STATUS_ERROR",
            )

        if response_type == "json":
            return response.json()
        if response_type == "text":
            return response.text()
        if response_type == "bytes":
            return response.data
        if response_type == "headers":
            return response.headers
        return response

    async def get(self, url: str, **kwargs: Any) -> Any:
        """Perform a GET request."""
        return await self.request("GET", url, **kwargs)

    async def post(self, url: str, data: Any = None, **kwargs: Any) -> Any:
        """Perform a POST request."""
        return await self.request("POST", url, data=data, **kwargs)

    async def put(self, url: str, data: Any = None, **kwargs: Any) -> Any:
        """Perform a PUT request."""
        return await self.request("PUT", url, data=data, **kwargs)

    async def patch(self, url: str, data: Any = None, **kwargs: Any) -> Any:
        """Perform a PATCH request."""
        return await self.request("PATCH", url, data=data, **kwargs)

    async def delete(self, url: str, **kwargs: Any) -> Any:
        """Perform a DELETE request."""
        return await self.request("DELETE", url, **kwargs)

    async def head(self, url: str, **kwargs: Any) -> Any:
        """Perform a HEAD request."""
        return await self.request("HEAD", url, **kwargs)

    async def __call__(self, *args: Any, **kwargs: Any) -> Any:
        """Dispatch HTTP request directly via callable syntax."""
        if (
            len(args) >= 2
            and isinstance(args[0], str)
            and args[0].upper()
            in (
                "GET",
                "POST",
                "PUT",
                "PATCH",
                "DELETE",
                "HEAD",
                "OPTIONS",
            )
        ):
            return await self.request(args[0], args[1], **kwargs)
        if len(args) >= 1 and isinstance(args[0], str):
            return await self.request("GET", args[0], **kwargs)
        raise ValueError(
            "Invalid arguments to http callable: expected (url, ...) or (method, url, ...)"
        )

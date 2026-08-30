"""Security and cache policy headers for the same-origin local console."""

from __future__ import annotations

from typing import Final

from starlette.types import ASGIApp, Message, Receive, Scope, Send

_APPLICATION_CSP: Final = (
    "default-src 'self'; "
    "base-uri 'none'; "
    "connect-src 'self'; "
    "font-src 'self'; "
    "form-action 'self'; "
    "frame-ancestors 'none'; "
    "img-src 'self' data:; "
    "object-src 'none'; "
    "script-src 'self'; "
    "style-src 'self'"
)
_DOCUMENTATION_CSP: Final = (
    "default-src 'self'; "
    "base-uri 'self'; "
    "connect-src 'self'; "
    "font-src 'self' https://cdn.jsdelivr.net; "
    "form-action 'self'; "
    "frame-ancestors 'none'; "
    "img-src 'self' data: https://fastapi.tiangolo.com; "
    "object-src 'none'; "
    "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net"
)
_STATIC_PREFIXES: Final = ("/assets/", "/static/")
_DOCS_PATHS: Final = frozenset({"/docs", "/docs/oauth2-redirect", "/redoc"})


def _set_header(headers: list[tuple[bytes, bytes]], name: bytes, value: bytes) -> None:
    lowered = name.lower()
    headers[:] = [(key, item) for key, item in headers if key.lower() != lowered]
    headers.append((name, value))


class SecurityHeadersMiddleware:
    """Apply a restrictive browser policy and prevent financial response caching."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        documentation = path in _DOCS_PATHS
        static_asset = any(path.startswith(prefix) for prefix in _STATIC_PREFIXES)

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", ()))
                _set_header(
                    headers,
                    b"content-security-policy",
                    (_DOCUMENTATION_CSP if documentation else _APPLICATION_CSP).encode(
                        "ascii"
                    ),
                )
                _set_header(headers, b"x-content-type-options", b"nosniff")
                _set_header(headers, b"x-frame-options", b"DENY")
                _set_header(headers, b"referrer-policy", b"no-referrer")
                _set_header(
                    headers,
                    b"permissions-policy",
                    b"camera=(), geolocation=(), microphone=(), payment=(), usb=()",
                )
                if not static_asset:
                    _set_header(
                        headers,
                        b"cache-control",
                        b"private, no-store, no-transform",
                    )
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_headers)


__all__ = ["SecurityHeadersMiddleware"]

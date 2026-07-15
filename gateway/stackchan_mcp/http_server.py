"""Streamable HTTP MCP daemon wiring for the StackChan gateway."""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import os
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import jsonschema
from mcp.server.streamable_http import MCP_SESSION_ID_HEADER
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.types import CallToolRequest, CallToolResult, ErrorData, ServerResult, TextContent
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

from .notify_config import NotifyConfig
from .queue import CommandQueue, QueueFull, QueueItem, build_queue_full_error
from .stdio_server import _dispatch_mcp_tool, create_server

# Follower lifecycle operations must remain callable when the ESP32 is
# disconnected so HTTP clients can supervise the background task.
BYPASS_TOOLS = frozenset({"get_status", "stackchan_follow_pose_stream"})
MCP_HTTP_ALLOWED_HOSTS_ENV = "MCP_HTTP_ALLOWED_HOSTS"
AUTH_FAILURE_MESSAGE = "Unauthorized: missing or invalid bearer token"
HOST_FAILURE_MESSAGE = "Forbidden: invalid Host header"
ORIGIN_FAILURE_MESSAGE = "Forbidden: invalid Origin header"
NON_LOOPBACK_TOKEN_REQUIRED_MESSAGE = (
    "stackchan-mcp: refusing non-loopback MCP_HTTP_HOST without "
    "STACKCHAN_TOKEN or BEARER_TOKEN"
)
DISCONNECTED_DEVICE_PAYLOAD = {
    "error": "No ESP32 device connected. Please check the device."
}
SERVER_SHUTDOWN_ERROR_CODE = -32000
SERVER_SHUTDOWN_ERROR_MESSAGE = "stackchan MCP HTTP server is shutting down"

DispatchFn = Callable[[QueueItem], Awaitable[list[TextContent]]]

# ---------------------------------------------------------------------------
# Dashboard HTML (loaded once at import time)
# ---------------------------------------------------------------------------

_DASHBOARD_PATH = Path(__file__).parent / "dashboard.html"


def _load_dashboard_html() -> str | None:
    if _DASHBOARD_PATH.is_file():
        return _DASHBOARD_PATH.read_text()
    return None


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


def get_configured_token() -> str | None:
    """Return the configured HTTP bearer token, if any."""
    return os.getenv("STACKCHAN_TOKEN") or os.getenv("BEARER_TOKEN") or None


def is_wildcard_bind_host(host: str) -> bool:
    """Return whether ``host`` binds all local interfaces."""
    normalized = host.strip().lower()
    return normalized in {"", "0.0.0.0", "::"}


def is_loopback_bind_host(host: str) -> bool:
    """Return whether ``host`` is a loopback-only bind target."""
    normalized = host.strip().lower()
    if normalized in {"localhost", "127.0.0.1", "::1"}:
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def validate_bind_safety(host: str, token: str | None) -> None:
    """Reject non-loopback daemon binds when no HTTP bearer token is set."""
    if not token and not is_loopback_bind_host(host):
        raise ValueError(NON_LOOPBACK_TOKEN_REQUIRED_MESSAGE)


def make_dispatch_fn(gateway: Any) -> DispatchFn:
    """Build the single-flight ESP32 dispatcher used by the command queue."""

    async def dispatch(item: QueueItem) -> list[TextContent]:
        if not gateway.esp32.device_connected:
            return [
                TextContent(
                    type="text",
                    text=json.dumps(DISCONNECTED_DEVICE_PAYLOAD),
                )
            ]
        return await _dispatch_mcp_tool(item.tool_name, item.arguments, gateway)

    return dispatch


# ---------------------------------------------------------------------------
# API helper: call ESP32 tool + extract clean JSON response
# ---------------------------------------------------------------------------


async def _extract_result_text(
    gateway: Any, esp32_name: str, esp32_args: dict[str, Any]
) -> JSONResponse:
    """Call an ESP32 tool and extract text content into a clean API response."""
    if not gateway.esp32.device_connected:
        return JSONResponse(
            {"ok": False, "error": "No ESP32 device connected"}, status_code=503
        )
    result, error = await gateway.esp32.call_tool(esp32_name, esp32_args)
    if error:
        return JSONResponse(
            {"ok": False, "error": error.get("message", str(error))}, status_code=500
        )
    text = _parse_tool_result_text(result)
    return JSONResponse({"ok": True, "data": text})


async def _safe_json_body(request: Request) -> dict[str, Any] | None:
    """Parse JSON request body safely, returning None on failure."""
    try:
        return await request.json()
    except Exception:
        return None


def _parse_tool_result_text(result: Any) -> Any:
    """Extract user-facing text from an MCP tool call result dict.

    The ESP32 returns JSON-RPC results like
    ``{"content": [{"type": "text", "text": "..."}]}``.
    This function attempts to parse the inner text as JSON;
    if that fails, it returns the raw text string.
    """
    if not isinstance(result, dict) or "content" not in result:
        return result
    content = result["content"]
    if isinstance(content, list) and content:
        item = content[0]
        if isinstance(item, dict) and item.get("type") == "text":
            text = item.get("text", "")
            try:
                return json.loads(text)
            except (json.JSONDecodeError, TypeError):
                return text
    return result


def _get_gateway(request: Request) -> Any:
    return request.app.state.gateway


# ---------------------------------------------------------------------------
# Dashboard / API route handlers
# ---------------------------------------------------------------------------


async def api_device_info(request: Request) -> JSONResponse:
    return await _extract_result_text(_get_gateway(request), "self.get_device_status", {})


async def api_brightness(request: Request) -> JSONResponse:
    body = await _safe_json_body(request)
    if body is None:
        return JSONResponse({"ok": False, "error": "Invalid JSON body"}, status_code=400)
    brightness = body.get("brightness")
    if not isinstance(brightness, int) or brightness < 0 or brightness > 100:
        return JSONResponse(
            {"ok": False, "error": "brightness must be an integer 0-100"}, status_code=400
        )
    return await _extract_result_text(
        _get_gateway(request), "self.screen.set_brightness", {"brightness": brightness}
    )


async def api_volume(request: Request) -> JSONResponse:
    body = await _safe_json_body(request)
    if body is None:
        return JSONResponse({"ok": False, "error": "Invalid JSON body"}, status_code=400)
    volume = body.get("volume")
    if not isinstance(volume, int) or volume < 0 or volume > 100:
        return JSONResponse(
            {"ok": False, "error": "volume must be an integer 0-100"}, status_code=400
        )
    return await _extract_result_text(
        _get_gateway(request), "self.audio_speaker.set_volume", {"volume": volume}
    )


async def api_avatar(request: Request) -> JSONResponse:
    body = await _safe_json_body(request)
    if body is None:
        return JSONResponse({"ok": False, "error": "Invalid JSON body"}, status_code=400)
    face = body.get("face", "")
    valid = {"idle", "happy", "thinking", "sad", "surprised", "embarrassed", "off"}
    if face not in valid:
        return JSONResponse(
            {"ok": False, "error": f"face must be one of: {', '.join(sorted(valid))}"},
            status_code=400,
        )
    return await _extract_result_text(
        _get_gateway(request), "self.display.set_avatar", {"face": face}
    )


async def api_head_angles_get(request: Request) -> JSONResponse:
    return await _extract_result_text(_get_gateway(request), "self.robot.get_head_angles", {})


async def api_head_angles_post(request: Request) -> JSONResponse:
    body = await _safe_json_body(request)
    if body is None:
        return JSONResponse({"ok": False, "error": "Invalid JSON body"}, status_code=400)
    yaw = body.get("yaw")
    pitch = body.get("pitch")
    speed = body.get("speed", "mid")
    if not isinstance(yaw, int) or yaw < -90 or yaw > 90:
        return JSONResponse(
            {"ok": False, "error": "yaw must be an integer -90..90"}, status_code=400
        )
    if not isinstance(pitch, int) or pitch < 5 or pitch > 85:
        return JSONResponse(
            {"ok": False, "error": "pitch must be an integer 5..85"}, status_code=400
        )
    args: dict[str, Any] = {"yaw": yaw, "pitch": pitch}
    if isinstance(speed, int) and 1 <= speed <= 10000:
        args["speed"] = speed
    elif isinstance(speed, str) and speed in ("low", "mid", "high"):
        args["speed"] = speed
    return await _extract_result_text(
        _get_gateway(request), "self.robot.set_head_angles", args
    )


async def api_leds_all(request: Request) -> JSONResponse:
    body = await _safe_json_body(request)
    if body is None:
        return JSONResponse({"ok": False, "error": "Invalid JSON body"}, status_code=400)
    r = body.get("r", 0)
    g = body.get("g", 0)
    b = body.get("b", 0)
    for v in (r, g, b):
        if not isinstance(v, int) or v < 0 or v > 255:
            return JSONResponse(
                {"ok": False, "error": "r, g, b must be integers 0-255"}, status_code=400
            )
    return await _extract_result_text(
        _get_gateway(request), "self.led.set_all", {"r": r, "g": g, "b": b}
    )


async def api_leds_clear(request: Request) -> JSONResponse:
    return await _extract_result_text(_get_gateway(request), "self.led.clear", {})


async def api_leds_set(request: Request) -> JSONResponse:
    body = await _safe_json_body(request)
    if body is None:
        return JSONResponse({"ok": False, "error": "Invalid JSON body"}, status_code=400)
    colors = body.get("colors")
    if not isinstance(colors, list):
        return JSONResponse(
            {"ok": False, "error": "colors must be an array of [r,g,b] triples"},
            status_code=400,
        )
    return await _extract_result_text(
        _get_gateway(request), "self.led.set_many", {"colors": json.dumps(colors)}
    )


async def api_torque_get(request: Request) -> JSONResponse:
    return await _extract_result_text(
        _get_gateway(request), "self.robot.check_vm_en", {}
    )


async def api_torque_post(request: Request) -> JSONResponse:
    body = await _safe_json_body(request)
    if body is None:
        return JSONResponse({"ok": False, "error": "Invalid JSON body"}, status_code=400)
    yaw_enabled = body.get("yaw_enabled")
    pitch_enabled = body.get("pitch_enabled")
    if not isinstance(yaw_enabled, bool) or not isinstance(pitch_enabled, bool):
        return JSONResponse(
            {"ok": False, "error": "yaw_enabled and pitch_enabled must be booleans"},
            status_code=400,
        )
    return await _extract_result_text(
        _get_gateway(request),
        "self.robot.set_servo_torque",
        {"yaw_enabled": yaw_enabled, "pitch_enabled": pitch_enabled},
    )


async def api_auto_torque_release_post(request: Request) -> JSONResponse:
    body = await _safe_json_body(request)
    if body is None:
        return JSONResponse({"ok": False, "error": "Invalid JSON body"}, status_code=400)
    enabled = body.get("enabled")
    timeout_ms = body.get("timeout_ms", 5000)
    if not isinstance(enabled, bool):
        return JSONResponse(
            {"ok": False, "error": "enabled must be a boolean"}, status_code=400
        )
    if not isinstance(timeout_ms, int) or timeout_ms < 500 or timeout_ms > 600000:
        return JSONResponse(
            {"ok": False, "error": "timeout_ms must be 500..600000"}, status_code=400
        )
    return await _extract_result_text(
        _get_gateway(request),
        "self.robot.set_auto_torque_release",
        {"enabled": enabled, "timeout_ms": timeout_ms},
    )


async def api_touch_get(request: Request) -> JSONResponse:
    return await _extract_result_text(_get_gateway(request), "self.touch.get_touch_state", {})


async def api_touch_enabled_get(request: Request) -> JSONResponse:
    return await _extract_result_text(
        _get_gateway(request), "self.robot.get_touch_sensor_enabled", {}
    )


async def api_touch_enabled_post(request: Request) -> JSONResponse:
    body = await _safe_json_body(request)
    if body is None:
        return JSONResponse({"ok": False, "error": "Invalid JSON body"}, status_code=400)
    enabled = body.get("enabled")
    if not isinstance(enabled, bool):
        return JSONResponse(
            {"ok": False, "error": "enabled must be a boolean"}, status_code=400
        )
    return await _extract_result_text(
        _get_gateway(request), "self.robot.set_touch_sensor_enabled", {"enabled": enabled}
    )


async def api_blink(request: Request) -> JSONResponse:
    body = await _safe_json_body(request)
    if body is None:
        return JSONResponse({"ok": False, "error": "Invalid JSON body"}, status_code=400)
    enabled = body.get("enabled")
    if not isinstance(enabled, bool):
        return JSONResponse(
            {"ok": False, "error": "enabled must be a boolean"}, status_code=400
        )
    return await _extract_result_text(
        _get_gateway(request), "self.display.set_blink", {"enabled": enabled}
    )


async def api_call_tool(request: Request) -> JSONResponse:
    """Generic tool call: POST /api/call {"tool": "self.robot.uart_diag", "args": {}}"""
    body = await _safe_json_body(request)
    if body is None:
        return JSONResponse({"ok": False, "error": "Invalid JSON body"}, status_code=400)
    tool = body.get("tool")
    args = body.get("args", {})
    if not isinstance(tool, str) or not tool:
        return JSONResponse(
            {"ok": False, "error": "tool must be a non-empty string"}, status_code=400
        )
    if not isinstance(args, dict):
        return JSONResponse(
            {"ok": False, "error": "args must be a dict"}, status_code=400
        )
    return await _extract_result_text(_get_gateway(request), tool, args)


async def dashboard_handler(_request: Request) -> HTMLResponse:
    html = _load_dashboard_html()
    if html is None:
        return HTMLResponse(
            "<h1>Dashboard not found</h1>"
            "<p>dashboard.html is missing from the gateway package.</p>",
            status_code=404,
        )
    return HTMLResponse(
        html,
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


# ---------------------------------------------------------------------------
# Shared dashboard routes
# ---------------------------------------------------------------------------


def _dashboard_routes() -> list[Route]:
    return [
        Route("/api/device-info", endpoint=api_device_info, methods=["GET"]),
        Route("/api/brightness", endpoint=api_brightness, methods=["POST"]),
        Route("/api/volume", endpoint=api_volume, methods=["POST"]),
        Route("/api/avatar", endpoint=api_avatar, methods=["POST"]),
        Route("/api/head-angles", endpoint=api_head_angles_get, methods=["GET"]),
        Route("/api/head-angles", endpoint=api_head_angles_post, methods=["POST"]),
        Route("/api/leds/all", endpoint=api_leds_all, methods=["POST"]),
        Route("/api/leds/clear", endpoint=api_leds_clear, methods=["POST"]),
        Route("/api/leds", endpoint=api_leds_set, methods=["POST"]),
        Route("/api/touch", endpoint=api_touch_get, methods=["GET"]),
        Route("/api/touch/enabled", endpoint=api_touch_enabled_get, methods=["GET"]),
        Route("/api/touch/enabled", endpoint=api_touch_enabled_post, methods=["POST"]),
        Route("/api/torque", endpoint=api_torque_get, methods=["GET"]),
        Route("/api/torque", endpoint=api_torque_post, methods=["POST"]),
        Route("/api/torque/auto-release", endpoint=api_auto_torque_release_post, methods=["POST"]),
        Route("/api/blink", endpoint=api_blink, methods=["POST"]),
        Route("/api/call", endpoint=api_call_tool, methods=["POST"]),
        Route("/", endpoint=dashboard_handler, methods=["GET"]),
    ]


# ---------------------------------------------------------------------------
# build_dashboard_app: for stdio mode (no MCP endpoint, no queue)
# ---------------------------------------------------------------------------


def build_dashboard_app(
    *,
    gateway: Any,
    host: str,
    port: int,
    token: str | None = None,
) -> _GuardedASGIApp:
    """Build a Starlette app with dashboard + API routes (stdio mode).

    This is a lightweight variant of ``build_app`` that omits the MCP
    Streamable HTTP transport endpoint and command queue.  It is used
    when the MCP protocol runs over stdio and the HTTP server only needs
    to serve the web dashboard and REST API.
    """
    dashboard = _load_dashboard_html()

    async def healthz(_request: Request) -> JSONResponse:
        return JSONResponse({"ok": True})

    routes: list[Route] = [
        Route("/healthz", endpoint=healthz, methods=["GET"]),
        *_dashboard_routes(),
    ]
    app = Starlette(routes=routes)
    app.state.gateway = gateway
    app.state.dashboard_html = dashboard
    return _GuardedASGIApp(
        app,
        token=token,
        allowed_hosts=_allowed_host_values(host, port),
    )


# ---------------------------------------------------------------------------
# build_app: full Streamable HTTP MCP + dashboard
# ---------------------------------------------------------------------------


def build_app(
    queue: CommandQueue,
    *,
    gateway: Any,
    owner_id: str,
    host: str,
    port: int,
    token: str | None = None,
    dispatch_fn: DispatchFn | None = None,
    notify_config: NotifyConfig | None = None,
) -> _GuardedASGIApp:
    """Build the ASGI app for Streamable HTTP MCP plus health endpoints."""
    server = create_server(notify_config=notify_config)
    session_manager = StreamableHTTPSessionManager(
        app=server,
        json_response=True,
        stateless=False,
    )
    pending_items: dict[str, QueueItem] = {}
    _install_queue_tool_handler(
        server,
        queue=queue,
        gateway=gateway,
        pending_items=pending_items,
    )

    async def status(_request: Request) -> JSONResponse:
        raw_status = gateway.esp32.get_status()
        status_payload = dict(raw_status) if isinstance(raw_status, dict) else {}
        if not isinstance(raw_status, dict):
            status_payload["status"] = raw_status
        status_payload.update(
            {
                "esp32_connected": bool(gateway.esp32.device_connected),
                "queue_depth": queue.depth,
                "queue_capacity": queue.capacity,
                "owner_id": owner_id,
                "connected_clients": _connected_client_count(session_manager),
            }
        )
        return JSONResponse(status_payload)

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette):
        dispatcher_task: asyncio.Task[None] | None = None
        async with session_manager.run():
            if dispatch_fn is not None:
                dispatcher_task = asyncio.create_task(
                    queue.run_dispatcher(_skip_done_dispatch(dispatch_fn))
                )
            try:
                yield
            finally:
                if dispatcher_task is not None:
                    dispatcher_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await dispatcher_task
                _complete_pending_items_for_shutdown(pending_items)
                _drain_queued_items_for_shutdown(queue)

    async def healthz(_request: Request) -> JSONResponse:
        return JSONResponse({"ok": True})

    routes = [
        Route(
            "/mcp",
            endpoint=_StreamableHTTPASGIApp(session_manager),
            methods=["GET", "POST", "DELETE"],
        ),
        Route("/healthz", endpoint=healthz, methods=["GET"]),
        Route("/status", endpoint=status, methods=["GET"]),
        *_dashboard_routes(),
    ]
    app = Starlette(routes=routes, lifespan=lifespan)
    app.state.command_queue = queue
    app.state.session_manager = session_manager
    app.state.gateway = gateway
    return _GuardedASGIApp(
        app,
        token=token,
        allowed_hosts=_allowed_host_values(host, port),
    )


# ---------------------------------------------------------------------------
# Queue tool handler (streamable-http only)
# ---------------------------------------------------------------------------


def _install_queue_tool_handler(
    server: Any,
    *,
    queue: CommandQueue,
    gateway: Any,
    pending_items: dict[str, QueueItem],
) -> None:
    async def handler(req: CallToolRequest) -> ServerResult | ErrorData:
        tool_name = req.params.name
        arguments = req.params.arguments or {}
        tool = await server._get_cached_tool_definition(tool_name)
        if tool is not None:
            try:
                jsonschema.validate(instance=arguments, schema=tool.inputSchema)
            except jsonschema.ValidationError as exc:
                return server._make_error_result(
                    f"Input validation error: {exc.message}"
                )

        if tool_name in BYPASS_TOOLS:
            content = await _dispatch_mcp_tool(tool_name, arguments, gateway)
            return _tool_result(content)

        context = server.request_context
        request = context.request
        client_session_id = None
        if isinstance(request, Request):
            client_session_id = request.headers.get(MCP_SESSION_ID_HEADER)

        response_future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        item = QueueItem(
            correlation_id=str(uuid.uuid4()),
            client_session_id=client_session_id,
            client_request_id=context.request_id,
            tool_name=tool_name,
            arguments=arguments,
            response_future=response_future,
            enqueued_at=time.monotonic(),
        )
        try:
            queue.enqueue(item)
        except QueueFull as exc:
            return ErrorData(**build_queue_full_error(exc.queue_depth))

        pending_items[item.correlation_id] = item
        try:
            content_or_error = await response_future
        except asyncio.CancelledError:
            response_future.cancel()
            raise
        finally:
            if response_future.done():
                pending_items.pop(item.correlation_id, None)

        if isinstance(content_or_error, ErrorData):
            return content_or_error
        return _tool_result(content_or_error)

    server.request_handlers[CallToolRequest] = handler


def _skip_done_dispatch(dispatch_fn: DispatchFn) -> DispatchFn:
    async def dispatch(item: QueueItem) -> list[TextContent]:
        if item.response_future.done():
            return []
        return await dispatch_fn(item)

    return dispatch


def _complete_pending_items_for_shutdown(
    pending_items: dict[str, QueueItem],
) -> None:
    for item in list(pending_items.values()):
        _complete_item_with_shutdown_error(item)
    pending_items.clear()


def _drain_queued_items_for_shutdown(queue: CommandQueue) -> None:
    raw_queue = getattr(queue, "_queue")
    while True:
        try:
            item = raw_queue.get_nowait()
        except asyncio.QueueEmpty:
            return
        _complete_item_with_shutdown_error(item)
        raw_queue.task_done()


def _complete_item_with_shutdown_error(item: QueueItem) -> None:
    if not item.response_future.done():
        item.response_future.set_result(_server_shutdown_error())


def _server_shutdown_error() -> ErrorData:
    return ErrorData(
        code=SERVER_SHUTDOWN_ERROR_CODE,
        message=SERVER_SHUTDOWN_ERROR_MESSAGE,
        data={"reason": "server_shutdown"},
    )


def _tool_result(content: list[TextContent]) -> ServerResult:
    return ServerResult(
        CallToolResult(
            content=content,
            isError=False,
        )
    )


def _connected_client_count(session_manager: StreamableHTTPSessionManager) -> int:
    return len(getattr(session_manager, "_server_instances", {}))


def _allowed_host_values(host: str, port: int) -> set[str]:
    hosts = {host.strip().lower()}
    if is_loopback_bind_host(host) or is_wildcard_bind_host(host):
        hosts.update({"127.0.0.1", "localhost", "::1"})

    values: set[str] = set()
    for item in hosts:
        values.add(item)
        values.add(_host_with_port(item, port))
    values.update(_allowed_hosts_from_env(port))
    return values


def _allowed_hosts_from_env(port: int) -> set[str]:
    raw_hosts = os.getenv(MCP_HTTP_ALLOWED_HOSTS_ENV, "")
    values: set[str] = set()
    for raw_item in raw_hosts.split(","):
        item = raw_item.strip().lower()
        if not item:
            continue
        parsed = urlparse(item)
        if parsed.scheme in {"http", "https"} and parsed.netloc:
            item = parsed.netloc.lower()
        values.add(item)
        if ":" not in item or (item.startswith("[") and "]:" not in item):
            values.add(_host_with_port(item, port))
    return values


def _host_with_port(host: str, port: int) -> str:
    if ":" in host and not host.startswith("["):
        return f"[{host}]:{port}"
    return f"{host}:{port}"


def _is_allowed_host_header(value: str | None, allowed_hosts: set[str]) -> bool:
    if not value:
        return False
    return value.strip().lower() in allowed_hosts


def _is_allowed_origin(value: str | None, allowed_hosts: set[str]) -> bool:
    if not value:
        return True
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return False
    return _is_allowed_host_header(parsed.netloc, allowed_hosts)


# ---------------------------------------------------------------------------
# Auth-guarded ASGI middleware
# ---------------------------------------------------------------------------


class _GuardedASGIApp:
    def __init__(
        self,
        app: Starlette,
        *,
        token: str | None,
        allowed_hosts: set[str],
    ) -> None:
        self._app = app
        self._token = token
        self._allowed_hosts = allowed_hosts
        self.state = app.state

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        request = Request(scope, receive)
        if not _is_allowed_host_header(request.headers.get("host"), self._allowed_hosts):
            await PlainTextResponse(HOST_FAILURE_MESSAGE, status_code=403)(
                scope,
                receive,
                send,
            )
            return
        if not _is_allowed_origin(request.headers.get("origin"), self._allowed_hosts):
            await PlainTextResponse(ORIGIN_FAILURE_MESSAGE, status_code=403)(
                scope,
                receive,
                send,
            )
            return
        path = scope.get("path", "")
        if self._token and (
            path in {"/mcp", "/status"} or path.startswith("/api/")
        ):
            expected = f"Bearer {self._token}"
            if request.headers.get("authorization") != expected:
                await PlainTextResponse(
                    AUTH_FAILURE_MESSAGE,
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )(scope, receive, send)
                return

        await self._app(scope, receive, send)

    async def router_startup(self) -> None:
        await self._app.router.startup()

    @property
    def router(self) -> Any:
        return self._app.router


class _StreamableHTTPASGIApp:
    def __init__(self, session_manager: StreamableHTTPSessionManager) -> None:
        self._session_manager = session_manager

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self._session_manager.handle_request(scope, receive, send)

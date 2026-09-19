import asyncio
import hmac
import json
import logging
import os
from dataclasses import dataclass
from typing import Any

from aiohttp import web

from companion_api.agent import ask_companion
from companion_api.context import build_shared_context, get_companion_guild_id
from database.db import get_pool
from google_drive.client import google_drive_is_configured
from jira.client import jira_is_configured

logger = logging.getLogger(__name__)
MAX_HISTORY_ITEMS = 40
MAX_MESSAGE_CHARACTERS = 12_000
MAX_HISTORY_CHARACTERS = 60_000
MAX_UNREAL_TOOLS = 40
MAX_TOOL_SCHEMA_CHARACTERS = 120_000
API_KEYS_KEY = web.AppKey("companion_api_keys", tuple)
CHAT_SEMAPHORE_KEY = web.AppKey("companion_chat_semaphore", asyncio.Semaphore)


@dataclass(frozen=True)
class CompanionAPIServer:
    runner: web.AppRunner
    host: str
    port: int

    async def close(self) -> None:
        await self.runner.cleanup()


def companion_api_is_configured() -> bool:
    return bool(_configured_api_keys())


async def start_companion_api() -> CompanionAPIServer:
    api_keys = _configured_api_keys()
    if not api_keys:
        logger.warning(
            "Companion chat is locked until COMPANION_API_KEYS or "
            "COMPANION_API_KEY is configured"
        )
    if not os.getenv("COMPANION_GUILD_ID", "").strip():
        logger.warning(
            "Companion chat is unavailable until COMPANION_GUILD_ID is configured"
        )
    host = os.getenv("COMPANION_API_HOST", "0.0.0.0")
    port = int(os.getenv("PORT", os.getenv("COMPANION_API_PORT", "8080")))
    app = create_app(api_keys)
    runner = web.AppRunner(app, access_log=logger)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    logger.info("Companion API listening on %s:%s", host, port)
    return CompanionAPIServer(runner=runner, host=host, port=port)


def create_app(api_keys: tuple[str, ...] | None = None) -> web.Application:
    app = web.Application(
        client_max_size=512 * 1024,
        middlewares=[_error_middleware, _auth_middleware],
    )
    app[API_KEYS_KEY] = api_keys or _configured_api_keys()
    app[CHAT_SEMAPHORE_KEY] = asyncio.Semaphore(4)
    app.router.add_get("/health", _health)
    app.router.add_get("/api/v1/health", _health)
    app.router.add_post("/api/v1/chat", _chat)
    return app


@web.middleware
async def _auth_middleware(request: web.Request, handler):
    if request.path == "/health":
        return await handler(request)

    header = request.headers.get("Authorization", "")
    supplied = header[7:].strip() if header.startswith("Bearer ") else ""
    expected_keys = request.app[API_KEYS_KEY]
    if not supplied or not any(
        hmac.compare_digest(supplied, expected) for expected in expected_keys
    ):
        raise web.HTTPUnauthorized(
            text=json.dumps({"error": "Invalid companion API key"}),
            content_type="application/json",
        )
    return await handler(request)


@web.middleware
async def _error_middleware(request: web.Request, handler):
    try:
        return await handler(request)
    except web.HTTPException:
        raise
    except Exception:
        logger.exception("Companion API request failed")
        return web.json_response(
            {"error": "The companion service could not complete this request."},
            status=500,
        )


async def _health(request: web.Request) -> web.Response:
    database_ok = False
    try:
        database_ok = await get_pool().fetchval("SELECT TRUE") is True
    except Exception:
        logger.exception("Companion health check could not reach PostgreSQL")

    status = 200 if database_ok else 503
    return web.json_response(
        {
            "status": "ok" if database_ok else "degraded",
            "service": "jas-ai-companion-api",
            "database": database_ok,
            "companion_auth": companion_api_is_configured(),
            "discord_memory": bool(os.getenv("COMPANION_GUILD_ID", "").strip()),
            "google_drive": google_drive_is_configured(),
            "jira": jira_is_configured(),
        },
        status=status,
    )


async def _chat(request: web.Request) -> web.Response:
    try:
        get_companion_guild_id()
    except RuntimeError as exc:
        raise web.HTTPServiceUnavailable(
            text=json.dumps({"error": str(exc)}),
            content_type="application/json",
        ) from exc

    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError) as exc:
        raise web.HTTPBadRequest(text="Request body must be JSON") from exc
    if not isinstance(body, dict):
        raise web.HTTPBadRequest(text="Request body must be a JSON object")

    message = _required_string(body, "message", MAX_MESSAGE_CHARACTERS)
    history = _validate_history(body.get("history", []))
    unreal_tools = _validate_unreal_tools(body.get("unreal_tools", []))

    semaphore = request.app[CHAT_SEMAPHORE_KEY]
    async with semaphore:
        shared_context = await build_shared_context(message)
        answer = await ask_companion(
            message=message,
            history=history,
            shared_context=shared_context,
            unreal_tools=unreal_tools,
        )

    response: dict[str, Any] = {
        "type": "tool_call" if answer.tool_call else "message",
        "message": answer.text,
        "source_counts": shared_context.source_counts,
    }
    if answer.tool_call:
        response["tool_call"] = {
            "call_id": answer.tool_call.call_id,
            "name": answer.tool_call.name,
            "arguments": answer.tool_call.arguments,
        }
    return web.json_response(response)


def _required_string(body: dict[str, Any], name: str, max_length: int) -> str:
    value = body.get(name)
    if not isinstance(value, str) or not value.strip():
        raise web.HTTPBadRequest(text=f"{name} must be a non-empty string")
    if len(value) > max_length:
        raise web.HTTPRequestEntityTooLarge(
            max_size=max_length,
            actual_size=len(value),
        )
    return value.strip()


def _validate_history(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list) or len(value) > MAX_HISTORY_ITEMS:
        raise web.HTTPBadRequest(text="history must be a list of at most 40 items")

    history: list[dict[str, str]] = []
    total_characters = 0
    for item in value:
        if not isinstance(item, dict):
            raise web.HTTPBadRequest(text="Each history item must be an object")
        role = item.get("role")
        content = item.get("content")
        if role not in {"user", "assistant", "tool"} or not isinstance(content, str):
            raise web.HTTPBadRequest(text="Invalid history role or content")
        total_characters += len(content)
        history.append({"role": role, "content": content[:MAX_MESSAGE_CHARACTERS]})

    if total_characters > MAX_HISTORY_CHARACTERS:
        raise web.HTTPRequestEntityTooLarge(
            max_size=MAX_HISTORY_CHARACTERS,
            actual_size=total_characters,
        )
    return history


def _validate_unreal_tools(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > MAX_UNREAL_TOOLS:
        raise web.HTTPBadRequest(text="unreal_tools must contain at most 40 tools")
    try:
        serialized = json.dumps(value, ensure_ascii=True)
    except (TypeError, ValueError) as exc:
        raise web.HTTPBadRequest(text="unreal_tools must be valid JSON") from exc
    if len(serialized) > MAX_TOOL_SCHEMA_CHARACTERS:
        raise web.HTTPRequestEntityTooLarge(
            max_size=MAX_TOOL_SCHEMA_CHARACTERS,
            actual_size=len(serialized),
        )

    tools: list[dict[str, Any]] = []
    for tool in value:
        if not isinstance(tool, dict) or not isinstance(tool.get("name"), str):
            raise web.HTTPBadRequest(text="Each Unreal tool must have a name")
        tools.append(
            {
                "name": tool["name"],
                "description": tool.get("description", ""),
                "input_schema": tool.get("input_schema", {"type": "object"}),
            }
        )
    return tools


def _configured_api_keys() -> tuple[str, ...]:
    raw_keys = os.getenv("COMPANION_API_KEYS", "")
    if not raw_keys:
        raw_keys = os.getenv("COMPANION_API_KEY", "")
    return tuple(
        dict.fromkeys(key.strip() for key in raw_keys.split(",") if key.strip())
    )

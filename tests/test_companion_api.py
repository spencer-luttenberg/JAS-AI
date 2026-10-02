import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiohttp.test_utils import TestClient, TestServer

os.environ.setdefault("OPENAI_API_KEY", "test-key")
os.environ.setdefault("COMPANION_GUILD_ID", "123456789012345678")

from companion_api.agent import _extract_tool_call, _openai_tool
from companion_api.context import SharedContext, format_shared_context
from companion_api.server import create_app


class CompanionAPIAuthenticationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.client = TestClient(TestServer(create_app(("expected-secret",))))
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        await self.client.close()

    async def test_chat_rejects_missing_bearer_key(self) -> None:
        response = await self.client.post("/api/v1/chat", json={"message": "hello"})
        self.assertEqual(response.status, 401)

    async def test_chat_accepts_key_before_validating_request(self) -> None:
        response = await self.client.post(
            "/api/v1/chat",
            headers={"Authorization": "Bearer expected-secret"},
            json={},
        )
        self.assertEqual(response.status, 400)

    async def test_private_health_rejects_wrong_key(self) -> None:
        response = await self.client.get(
            "/api/v1/health",
            headers={"Authorization": "Bearer wrong-secret"},
        )
        self.assertEqual(response.status, 401)

    async def test_context_returns_bounded_retrieval_without_agent_call(self) -> None:
        shared = SharedContext(
            discord="D" * 20_000,
            drive="drive context",
            github="G" * 20_000,
            jira="jira context",
            source_counts={"discord_messages": 2, "github_chunks": 1},
        )
        with patch(
            "companion_api.server.build_shared_context",
            new=AsyncMock(return_value=shared),
        ) as build:
            response = await self.client.post(
                "/api/v1/context",
                headers={"Authorization": "Bearer expected-secret"},
                json={"query": "current task", "max_characters": 8_000},
            )
        self.assertEqual(response.status, 200)
        payload = await response.json()
        self.assertLessEqual(len(payload["context"]), 8_000)
        self.assertIn("## Discord", payload["context"])
        self.assertIn("## GitHub", payload["context"])
        self.assertEqual(payload["source_counts"]["github_chunks"], 1)
        build.assert_awaited_once_with("current task")


class CompanionAgentTests(unittest.TestCase):
    def test_arbitrary_unreal_tool_is_not_accepted_from_model(self) -> None:
        output = SimpleNamespace(
            output=[
                SimpleNamespace(
                    type="function_call",
                    name="not_advertised",
                    arguments=json.dumps({"path": "bad"}),
                    call_id="call-1",
                )
            ]
        )
        self.assertIsNone(_extract_tool_call(output, {"call_tool"}))

    def test_mcp_schema_becomes_non_strict_function_tool(self) -> None:
        tool = _openai_tool(
            {
                "name": "call_tool",
                "description": "Call a toolset tool",
                "input_schema": {
                    "type": "object",
                    "properties": {"tool_name": {"type": "string"}},
                },
            }
        )
        self.assertEqual(tool["name"], "call_tool")
        self.assertFalse(tool["strict"])

    def test_shared_context_uses_budget_and_keeps_populated_sources(self) -> None:
        context = SharedContext(
            discord="D" * 10_000,
            drive="short drive",
            github="G" * 10_000,
            jira="short jira",
            source_counts={},
        )
        rendered = format_shared_context(context, max_characters=4_000)
        self.assertLessEqual(len(rendered), 4_000)
        for heading in ("Discord", "Google Drive", "GitHub", "Jira"):
            self.assertIn(f"## {heading}", rendered)


if __name__ == "__main__":
    unittest.main()

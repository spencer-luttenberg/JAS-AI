import json
import os
import unittest
from types import SimpleNamespace

from aiohttp.test_utils import TestClient, TestServer

os.environ.setdefault("OPENAI_API_KEY", "test-key")
os.environ.setdefault("COMPANION_GUILD_ID", "123456789012345678")

from companion_api.agent import _extract_tool_call, _openai_tool
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


if __name__ == "__main__":
    unittest.main()

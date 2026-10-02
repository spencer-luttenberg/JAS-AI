import base64
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiohttp.test_utils import TestClient, TestServer

os.environ.setdefault("OPENAI_API_KEY", "test-key")
os.environ.setdefault("COMPANION_GUILD_ID", "123456789012345678")

from companion_api.agent import ask_companion
from companion_api.attachments import validate_attachments
from companion_api.context import SharedContext
from companion_api.server import create_app


def upload(name, data):
    return {"name": name, "data": base64.b64encode(data).decode()}


class CompanionAttachmentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = TestClient(TestServer(create_app(("expected-secret",))))
        await self.client.start_server()
        self.headers = {"Authorization": "Bearer expected-secret"}
        self.context = SharedContext(
            discord="", drive="", github="", jira="", source_counts={}
        )

    async def asyncTearDown(self):
        await self.client.close()

    async def test_current_and_historical_files_reach_model_as_content(self):
        png = upload("screenshot.png", b"\x89PNG\r\n\x1a\nimage bytes")
        pdf = upload("design.pdf", b"%PDF-1.7\n" + b"x" * 600_000)
        code = upload("actor.cpp", b"void Actor::Tick() {}")
        history = [
            {"role": "user", "content": "Look at this screenshot", "attachments": [png]}
        ]
        response_stub = SimpleNamespace(output=[], output_text="Reviewed the files.")
        with (
            patch(
                "companion_api.server.build_shared_context",
                AsyncMock(return_value=self.context),
            ),
            patch(
                "companion_api.agent.client.responses.create",
                AsyncMock(return_value=response_stub),
            ) as model,
        ):
            response = await self.client.post(
                "/api/v1/chat",
                headers=self.headers,
                json={
                    "message": "Compare with this code and PDF",
                    "history": history,
                    "attachments": [pdf, code],
                    "unreal_tools": [],
                },
            )
        self.assertEqual(response.status, 200, await response.text())
        self.assertEqual((await response.json())["message"], "Reviewed the files.")
        options = model.call_args.kwargs
        self.assertFalse(options["store"])
        parts = options["input"][0]["content"]
        self.assertEqual(
            next(part for part in parts if part["type"] == "input_image")["image_url"],
            f"data:image/png;base64,{png['data']}",
        )
        self.assertEqual(
            next(part for part in parts if part["type"] == "input_file")["file_data"],
            f"data:application/pdf;base64,{pdf['data']}",
        )
        self.assertTrue(
            any("void Actor::Tick()" in part.get("text", "") for part in parts)
        )
        self.assertNotIn(png["data"], parts[0]["text"])
        self.assertTrue(any("turn 1" in part.get("text", "") for part in parts))

    async def test_attachment_only_request_is_accepted(self):
        with (
            patch(
                "companion_api.server.build_shared_context",
                AsyncMock(return_value=self.context),
            ),
            patch(
                "companion_api.server.ask_companion",
                AsyncMock(return_value=SimpleNamespace(text="Read", tool_call=None)),
            ) as ask,
        ):
            response = await self.client.post(
                "/api/v1/chat",
                headers=self.headers,
                json={
                    "attachments": [upload("notes.md", b"Notes")],
                },
            )
        self.assertEqual(response.status, 200)
        self.assertTrue(ask.call_args.kwargs["message"])

    async def test_invalid_files_and_history_are_rejected_before_context_lookup(self):
        cases = [
            {"attachments": [{"name": "x.png", "data": "%%%%"}]},
            {
                "history": [
                    {
                        "role": "user",
                        "content": "x",
                        "attachments": [upload("x.exe", b"MZ")],
                    }
                ]
            },
            {
                "history": [
                    {
                        "role": "assistant",
                        "content": "x",
                        "attachments": [upload("x.txt", b"x")],
                    }
                ]
            },
        ]
        with patch("companion_api.server.build_shared_context", AsyncMock()) as context:
            for case in cases:
                response = await self.client.post(
                    "/api/v1/chat",
                    headers=self.headers,
                    json={"message": "Read", **case},
                )
                self.assertEqual(response.status, 400)
        context.assert_not_called()

    async def test_current_and_history_share_a_total_size_limit(self):
        file = upload("big.pdf", b"%PDF-" + b"x" * (7 * 1024 * 1024))
        response = await self.client.post(
            "/api/v1/chat",
            headers=self.headers,
            json={
                "message": "Read",
                "attachments": [file],
                "history": [
                    {"role": "user", "content": "Previous", "attachments": [file, file]}
                ],
            },
        )
        self.assertEqual(response.status, 400)
        self.assertIn("20 MB", await response.text())

    async def test_office_documents_use_file_parts(self):
        files = validate_attachments([upload("design.docx", b"PK office document")])
        with patch(
            "companion_api.agent.client.responses.create",
            AsyncMock(
                return_value=SimpleNamespace(output=[], output_text="Read"),
            ),
        ) as model:
            await ask_companion(
                message="Read",
                history=[],
                attachments=files,
                shared_context=self.context,
                unreal_tools=[],
            )
        parts = model.call_args.kwargs["input"][0]["content"]
        self.assertEqual(parts[-1]["type"], "input_file")
        self.assertEqual(parts[-1]["filename"], "design.docx")

    async def test_plain_text_chat_retains_existing_request_format(self):
        with patch(
            "companion_api.agent.client.responses.create",
            AsyncMock(
                return_value=SimpleNamespace(output=[], output_text="Hello"),
            ),
        ) as model:
            await ask_companion(
                message="Hello",
                history=[],
                shared_context=self.context,
                unreal_tools=[],
            )
        self.assertIsInstance(model.call_args.kwargs["input"], str)

import base64
import os
import unittest
from unittest.mock import patch

from github.client import (
    GitHubClient,
    get_github_repositories,
    normalize_github_repository,
)
from github.extract import decode_github_text, is_indexable_github_path


class StubGitHubClient(GitHubClient):
    def __init__(self, responses):
        super().__init__(token="test-token")
        self.responses = list(responses)

    async def _request(self, method, path, **kwargs):
        return self.responses.pop(0)


class GitHubConfigurationTests(unittest.TestCase):
    def test_normalizes_repository_names_and_urls(self) -> None:
        self.assertEqual(
            normalize_github_repository("https://github.com/JAS/Project.git"),
            "jas/project",
        )
        with patch.dict(
            os.environ,
            {
                "GITHUB_REPOSITORIES": (
                    "JAS/Project, https://github.com/jas/project.git, JAS/Tools"
                )
            },
        ):
            self.assertEqual(
                get_github_repositories(),
                ("jas/project", "jas/tools"),
            )

    def test_rejects_non_github_urls(self) -> None:
        with self.assertRaisesRegex(ValueError, "github.com"):
            normalize_github_repository("https://example.com/JAS/Project")


class GitHubExtractionTests(unittest.TestCase):
    def test_indexes_source_and_project_files(self) -> None:
        self.assertTrue(is_indexable_github_path("Source/Game/Player.cpp"))
        self.assertTrue(is_indexable_github_path("Config/DefaultEngine.ini"))
        self.assertTrue(is_indexable_github_path("Game.uproject"))
        self.assertTrue(is_indexable_github_path(".github/workflows/test.yml"))
        self.assertTrue(is_indexable_github_path("README"))

    def test_skips_generated_binary_and_sensitive_files(self) -> None:
        self.assertFalse(is_indexable_github_path("Binaries/Win64/Game.dll"))
        self.assertFalse(is_indexable_github_path("Content/Hero.uasset"))
        self.assertFalse(is_indexable_github_path(".env"))
        self.assertFalse(is_indexable_github_path("config/client-secret.json"))
        self.assertFalse(is_indexable_github_path("certificates/server.pem"))

    def test_decodes_text_and_rejects_binary_content(self) -> None:
        self.assertEqual(
            decode_github_text(b"line one\r\nline two\r\n", "README.md"),
            "line one\nline two",
        )
        with self.assertRaisesRegex(ValueError, "Binary content"):
            decode_github_text(b"header\x00payload", "Source/file.cpp")


class GitHubClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_reads_repository_tree_and_blob(self) -> None:
        client = StubGitHubClient(
            [
                {
                    "full_name": "JAS/Project",
                    "default_branch": "main",
                    "html_url": "https://github.com/JAS/Project",
                    "private": True,
                },
                {
                    "sha": "tree-sha",
                    "truncated": False,
                    "tree": [
                        {
                            "path": "README.md",
                            "type": "blob",
                            "sha": "blob-sha",
                            "size": 7,
                        },
                        {"path": "Source", "type": "tree", "sha": "dir-sha"},
                    ],
                },
                {
                    "encoding": "base64",
                    "content": base64.b64encode(b"project").decode("ascii"),
                },
            ]
        )

        repository = await client.get_repository("JAS/Project")
        tree = await client.get_tree(repository)
        content = await client.download_file(repository.full_name, tree.files[0])

        self.assertEqual(repository.default_branch, "main")
        self.assertEqual(tree.head_sha, "tree-sha")
        self.assertEqual([file.path for file in tree.files], ["README.md"])
        self.assertEqual(content, b"project")


if __name__ == "__main__":
    unittest.main()

import base64
import os
import re
from dataclasses import dataclass
from typing import Any, Self
from urllib.parse import quote, urlparse

import aiohttp

GITHUB_API_VERSION = "2022-11-28"
REPOSITORY_PATTERN = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9_.-]*[A-Za-z0-9])?/"
    r"[A-Za-z0-9_.-]+$"
)


class GitHubAPIError(RuntimeError):
    pass


@dataclass(frozen=True)
class GitHubRepository:
    full_name: str
    default_branch: str
    web_url: str
    is_private: bool


@dataclass(frozen=True)
class GitHubFile:
    path: str
    blob_sha: str
    size_bytes: int | None


@dataclass(frozen=True)
class GitHubTree:
    head_sha: str
    files: tuple[GitHubFile, ...]


class GitHubClient:
    def __init__(
        self,
        *,
        token: str = "",
        api_url: str = "https://api.github.com",
        max_file_bytes: int = 1_000_000,
    ) -> None:
        self._token = token.strip()
        self._api_url = api_url.rstrip("/")
        self._max_file_bytes = max_file_bytes
        self._session: aiohttp.ClientSession | None = None

    @classmethod
    def from_environment(cls) -> "GitHubClient":
        return cls(
            token=os.getenv("GITHUB_TOKEN", ""),
            api_url=os.getenv("GITHUB_API_URL", "https://api.github.com"),
            max_file_bytes=_positive_int_environment_value(
                "GITHUB_MAX_FILE_BYTES",
                1_000_000,
            ),
        )

    async def __aenter__(self) -> Self:
        timeout = aiohttp.ClientTimeout(total=90, connect=15)
        self._session = aiohttp.ClientSession(timeout=timeout)
        return self

    async def __aexit__(self, *_args) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def get_repository(self, full_name: str) -> GitHubRepository:
        normalized_name = normalize_github_repository(full_name)
        payload = await self._request(
            "GET",
            f"/repos/{_repository_path(normalized_name)}",
        )
        default_branch = payload.get("default_branch")
        if not isinstance(default_branch, str) or not default_branch:
            raise GitHubAPIError(f"GitHub repository {full_name} has no default branch")
        return GitHubRepository(
            full_name=normalized_name,
            default_branch=default_branch,
            web_url=str(
                payload.get("html_url") or f"https://github.com/{normalized_name}"
            ),
            is_private=bool(payload.get("private", False)),
        )

    async def get_tree(self, repository: GitHubRepository) -> GitHubTree:
        branch = quote(repository.default_branch, safe="")
        payload = await self._request(
            "GET",
            f"/repos/{_repository_path(repository.full_name)}/git/trees/{branch}",
            params={"recursive": "1"},
        )
        if payload.get("truncated"):
            raise GitHubAPIError(
                f"The GitHub tree for {repository.full_name} is too large for "
                "recursive indexing. Split the repository or narrow the integration."
            )

        files: list[GitHubFile] = []
        for item in payload.get("tree", []):
            if not isinstance(item, dict) or item.get("type") != "blob":
                continue
            path = item.get("path")
            sha = item.get("sha")
            if not isinstance(path, str) or not isinstance(sha, str):
                continue
            raw_size = item.get("size")
            files.append(
                GitHubFile(
                    path=path,
                    blob_sha=sha,
                    size_bytes=int(raw_size) if raw_size is not None else None,
                )
            )
        return GitHubTree(
            head_sha=str(payload.get("sha") or repository.default_branch),
            files=tuple(files),
        )

    async def download_file(self, repository: str, file: GitHubFile) -> bytes:
        if file.size_bytes is not None and file.size_bytes > self._max_file_bytes:
            raise ValueError(
                f"File is larger than the {self._max_file_bytes}-byte GitHub limit"
            )
        payload = await self._request(
            "GET",
            f"/repos/{_repository_path(repository)}/git/blobs/{file.blob_sha}",
        )
        if payload.get("encoding") != "base64" or not isinstance(
            payload.get("content"),
            str,
        ):
            raise GitHubAPIError(f"GitHub returned an unsupported blob for {file.path}")
        try:
            data = base64.b64decode(
                re.sub(r"\s+", "", payload["content"]),
                validate=True,
            )
        except ValueError as exc:
            raise GitHubAPIError(f"GitHub returned invalid content for {file.path}") from exc
        if len(data) > self._max_file_bytes:
            raise ValueError(
                f"Downloaded file exceeded the {self._max_file_bytes}-byte GitHub limit"
            )
        return data

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        if self._session is None:
            raise RuntimeError("GitHubClient must be used as an async context manager")

        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "JAS-AI",
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"

        try:
            async with self._session.request(
                method,
                f"{self._api_url}{path}",
                headers=headers,
                **kwargs,
            ) as response:
                try:
                    payload = await response.json()
                except (aiohttp.ContentTypeError, ValueError):
                    payload = {"message": (await response.text())[:1_000]}
                if response.status >= 400:
                    detail = payload.get("message", f"HTTP {response.status}")
                    if response.status == 404:
                        detail = (
                            f"{detail}. Check GITHUB_REPOSITORIES and confirm the "
                            "token can read this repository."
                        )
                    elif response.status == 403 and response.headers.get(
                        "X-RateLimit-Remaining"
                    ) == "0":
                        detail = "GitHub API rate limit reached; retry after it resets"
                    raise GitHubAPIError(f"GitHub API request failed: {detail}")
                if not isinstance(payload, dict):
                    raise GitHubAPIError("GitHub returned an invalid response")
                return payload
        except aiohttp.ClientError as exc:
            raise GitHubAPIError(f"Could not reach GitHub: {exc}") from exc


def github_is_configured() -> bool:
    return bool(get_github_repositories())


def get_github_repositories() -> tuple[str, ...]:
    repositories: list[str] = []
    seen: set[str] = set()
    for value in os.getenv("GITHUB_REPOSITORIES", "").split(","):
        repository = normalize_github_repository(value)
        if not repository or repository.casefold() in seen:
            continue
        repositories.append(repository)
        seen.add(repository.casefold())
    return tuple(repositories)


def normalize_github_repository(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    if "://" in value:
        parsed = urlparse(value)
        if parsed.hostname not in {"github.com", "www.github.com"}:
            raise ValueError("GITHUB_REPOSITORIES URLs must use github.com")
        value = parsed.path.strip("/")
    value = value.removesuffix(".git").strip("/")
    if not REPOSITORY_PATTERN.fullmatch(value):
        raise ValueError(
            "Each GITHUB_REPOSITORIES entry must be owner/repository or a "
            "github.com repository URL"
        )
    return value.casefold()


def get_github_sync_interval() -> int:
    raw_value = os.getenv("GITHUB_SYNC_INTERVAL_SECONDS", "900")
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ValueError("GITHUB_SYNC_INTERVAL_SECONDS must be a whole number") from exc
    return max(300, value)


def github_file_url(repository: GitHubRepository, path: str) -> str:
    branch = quote(repository.default_branch, safe="")
    encoded_path = quote(path, safe="/")
    return f"{repository.web_url}/blob/{branch}/{encoded_path}"


def _repository_path(full_name: str) -> str:
    owner, name = full_name.split("/", 1)
    return f"{quote(owner, safe='')}/{quote(name, safe='')}"


def _positive_int_environment_value(name: str, default: int) -> int:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a whole number") from exc
    if value <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return value

import asyncio
import hashlib
import logging
from dataclasses import dataclass

from database.github import (
    finish_github_repository_sync,
    get_github_file_state,
    record_github_file_error,
    record_github_repository_error,
    remove_unconfigured_github_repositories,
    replace_github_file,
    upsert_github_repository,
)
from github.client import (
    GitHubAPIError,
    GitHubClient,
    GitHubFile,
    get_github_repositories,
    get_github_sync_interval,
    github_file_url,
)
from github.extract import decode_github_text, is_indexable_github_path
from google_drive.extract import chunk_text

logger = logging.getLogger(__name__)
_sync_lock = asyncio.Lock()
PERMANENT_SKIP_PREFIXES = (
    "Binary content detected",
    "File is larger than",
    "Downloaded file exceeded",
)


@dataclass(frozen=True)
class GitHubSyncResult:
    repositories: int = 0
    repositories_failed: int = 0
    files_seen: int = 0
    files_indexed: int = 0
    files_unchanged: int = 0
    files_skipped: int = 0

    def add(self, other: "GitHubSyncResult") -> "GitHubSyncResult":
        return GitHubSyncResult(
            repositories=self.repositories + other.repositories,
            repositories_failed=(
                self.repositories_failed + other.repositories_failed
            ),
            files_seen=self.files_seen + other.files_seen,
            files_indexed=self.files_indexed + other.files_indexed,
            files_unchanged=self.files_unchanged + other.files_unchanged,
            files_skipped=self.files_skipped + other.files_skipped,
        )


async def sync_github() -> GitHubSyncResult:
    async with _sync_lock:
        configured_repositories = get_github_repositories()
        total = GitHubSyncResult()
        async with GitHubClient.from_environment() as client:
            for configured_name in configured_repositories:
                try:
                    result = await _sync_repository(client, configured_name)
                except Exception as exc:
                    logger.exception(
                        "GitHub sync failed for %s",
                        configured_name,
                    )
                    await record_github_repository_error(configured_name, str(exc))
                    result = GitHubSyncResult(repositories_failed=1)
                total = total.add(result)
        await remove_unconfigured_github_repositories(configured_repositories)
        return total


async def run_github_sync_forever() -> None:
    interval = get_github_sync_interval()
    while True:
        try:
            result = await sync_github()
            logger.info(
                "GitHub sync complete: %s repository/repositories, %s failed, "
                "%s file(s) seen, %s indexed, %s unchanged, %s skipped",
                result.repositories,
                result.repositories_failed,
                result.files_seen,
                result.files_indexed,
                result.files_unchanged,
                result.files_skipped,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("GitHub sync failed")
        await asyncio.sleep(interval)


async def _sync_repository(
    client: GitHubClient,
    configured_name: str,
) -> GitHubSyncResult:
    repository = await client.get_repository(configured_name)
    tree = await client.get_tree(repository)
    await upsert_github_repository(
        full_name=repository.full_name,
        default_branch=repository.default_branch,
        head_sha=tree.head_sha,
        web_url=repository.web_url,
        is_private=repository.is_private,
    )

    indexed_files = [
        file for file in tree.files if is_indexable_github_path(file.path)
    ]
    seen_paths: list[str] = []
    indexed = 0
    unchanged = 0
    skipped = 0

    for file in indexed_files:
        seen_paths.append(file.path)
        state = await get_github_file_state(repository.full_name, file.path)
        if _is_current(state, file):
            unchanged += 1
            continue

        web_url = github_file_url(repository, file.path)
        try:
            data = await client.download_file(repository.full_name, file)
            text = decode_github_text(data, file.path)
            chunks = chunk_text(text)
            await replace_github_file(
                repository=repository.full_name,
                path=file.path,
                blob_sha=file.blob_sha,
                size_bytes=file.size_bytes,
                web_url=web_url,
                content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                chunks=chunks,
            )
            indexed += 1
        except (GitHubAPIError, ValueError) as exc:
            skipped += 1
            logger.warning(
                "Could not index GitHub file %s/%s: %s",
                repository.full_name,
                file.path,
                exc,
            )
            await record_github_file_error(
                repository=repository.full_name,
                path=file.path,
                blob_sha=file.blob_sha,
                size_bytes=file.size_bytes,
                web_url=web_url,
                error=str(exc),
            )

    await finish_github_repository_sync(repository.full_name, seen_paths)
    return GitHubSyncResult(
        repositories=1,
        files_seen=len(indexed_files),
        files_indexed=indexed,
        files_unchanged=unchanged,
        files_skipped=skipped,
    )


def _is_current(state, file: GitHubFile) -> bool:
    if state is None or state.blob_sha != file.blob_sha:
        return False
    permanent_skip = bool(
        state.last_error and state.last_error.startswith(PERMANENT_SKIP_PREFIXES)
    )
    return state.indexed or permanent_skip

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid4

from database.db import get_pool


@dataclass(frozen=True)
class ProjectUpdateClaim:
    run_id: UUID
    previous_completed_at: datetime | None


async def claim_project_update(
    channel_id: int,
    interval_seconds: int,
    *,
    force: bool = False,
) -> ProjectUpdateClaim | None:
    run_id = uuid4()
    record = await get_pool().fetchrow(
        """
        INSERT INTO project_update_state (
            channel_id,
            current_run_id,
            last_started_at,
            last_error
        )
        VALUES ($1, $2, NOW(), NULL)
        ON CONFLICT (channel_id) DO UPDATE SET
            current_run_id = EXCLUDED.current_run_id,
            last_started_at = NOW(),
            last_error = NULL
        WHERE
            (
                $3::boolean
                OR project_update_state.last_completed_at IS NULL
                OR project_update_state.last_completed_at
                    <= NOW() - ($4 * INTERVAL '1 second')
            )
            AND (
                project_update_state.current_run_id IS NULL
                OR project_update_state.last_started_at
                    <= NOW() - INTERVAL '30 minutes'
            )
            AND (
                $3::boolean
                OR project_update_state.last_error IS NULL
                OR project_update_state.last_started_at
                    <= NOW() - INTERVAL '30 minutes'
            )
        RETURNING last_completed_at
        """,
        channel_id,
        run_id,
        force,
        interval_seconds,
    )
    if record is None:
        return None
    return ProjectUpdateClaim(
        run_id=run_id,
        previous_completed_at=record["last_completed_at"],
    )


async def complete_project_update(
    channel_id: int,
    run_id: UUID,
    last_message_id: int,
) -> None:
    await get_pool().execute(
        """
        UPDATE project_update_state
        SET
            current_run_id = NULL,
            last_completed_at = NOW(),
            last_message_id = $3,
            last_error = NULL
        WHERE channel_id = $1
          AND current_run_id = $2
        """,
        channel_id,
        run_id,
        last_message_id,
    )


async def fail_project_update(
    channel_id: int,
    run_id: UUID,
    error: str,
) -> None:
    await get_pool().execute(
        """
        UPDATE project_update_state
        SET
            current_run_id = NULL,
            last_error = $3
        WHERE channel_id = $1
          AND current_run_id = $2
        """,
        channel_id,
        run_id,
        error[:2_000],
    )

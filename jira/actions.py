from dataclasses import dataclass
import logging
from typing import Any
from uuid import UUID

import discord
from discord.ext import commands

from database.jira import (
    PendingJiraAction,
    cancel_pending_jira_action,
    claim_pending_jira_action,
    complete_pending_jira_action,
    create_pending_jira_action,
    expire_pending_jira_action,
    fail_pending_jira_action,
    get_pending_jira_action,
)
from jira.client import (
    JiraAPIError,
    JiraClient,
    get_jira_project_keys,
    validate_issue_key,
    validate_project_key,
)
from jira.sync import sync_jira_issue


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class JiraActionResult:
    issue_key: str
    message: str
    web_url: str


async def propose_jira_action(
    ctx: commands.Context,
    action_type: str,
    payload: dict[str, Any],
) -> None:
    if ctx.guild is None:
        await ctx.send("Jira changes can only be requested from a server channel.")
        return

    try:
        action, view = await _create_approval(
            requester_id=ctx.author.id,
            guild_id=ctx.guild.id,
            channel_id=ctx.channel.id,
            action_type=action_type,
            payload=payload,
        )
    except ValueError as exc:
        await ctx.send(str(exc))
        return
    message = await ctx.send(
        describe_action(action)
        + "\n\nNo Jira change has been made. This request expires in 15 minutes.",
        view=view,
        allowed_mentions=discord.AllowedMentions.none(),
    )
    view.message = message


async def propose_jira_action_from_message(
    message: discord.Message,
    action_type: str,
    payload: dict[str, Any],
) -> None:
    if message.guild is None:
        await message.reply(
            "Jira changes can only be requested from a server channel.",
            mention_author=False,
        )
        return

    try:
        action, view = await _create_approval(
            requester_id=message.author.id,
            guild_id=message.guild.id,
            channel_id=message.channel.id,
            action_type=action_type,
            payload=payload,
        )
    except ValueError as exc:
        await message.reply(str(exc), mention_author=False)
        return
    sent_message = await message.reply(
        describe_action(action)
        + "\n\nNo Jira change has been made. This request expires in 15 minutes.",
        mention_author=False,
        view=view,
        allowed_mentions=discord.AllowedMentions.none(),
    )
    view.message = sent_message


async def _create_approval(
    *,
    requester_id: int,
    guild_id: int,
    channel_id: int,
    action_type: str,
    payload: dict[str, Any],
) -> tuple[PendingJiraAction, "JiraApprovalView"]:
    normalized_payload = _normalize_jira_action(action_type, payload)
    action = await create_pending_jira_action(
        requester_id=requester_id,
        guild_id=guild_id,
        channel_id=channel_id,
        action_type=action_type,
        payload=normalized_payload,
    )
    return action, JiraApprovalView(action.action_id, action.requester_id)


class JiraApprovalView(discord.ui.View):
    def __init__(self, action_id: UUID, requester_id: int) -> None:
        super().__init__(timeout=15 * 60)
        self.action_id = action_id
        self.requester_id = requester_id
        self.message: discord.Message | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.requester_id:
            return True
        await interaction.response.send_message(
            "Only the person who requested this Jira change can approve or cancel it.",
            ephemeral=True,
        )
        return False

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.success)
    async def confirm(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        action = await claim_pending_jira_action(
            self.action_id,
            interaction.user.id,
        )
        if action is None:
            await _respond_with_unavailable_action(interaction, self.action_id)
            return

        self.stop()
        self._disable_buttons()
        await interaction.response.edit_message(
            content=describe_action(action) + "\n\nApplying the approved change...",
            view=self,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        try:
            result = await execute_jira_action(action)
        except (JiraAPIError, ValueError) as exc:
            await fail_pending_jira_action(action.action_id, str(exc))
            await interaction.edit_original_response(
                content=(
                    describe_action(action)[:1_000]
                    + "\n\nThe approved Jira change failed: "
                    + _clean_text(str(exc), 850)
                ),
                view=None,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        except Exception as exc:
            logger.exception("Approved Jira action %s failed", action.action_id)
            await fail_pending_jira_action(action.action_id, str(exc))
            await interaction.edit_original_response(
                content=(
                    describe_action(action)
                    + "\n\nThe approved Jira change failed. Check the deployment logs."
                ),
                view=None,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        await complete_pending_jira_action(action.action_id, result.message)
        await interaction.edit_original_response(
            content=_fit_discord_content(
                describe_action(action)
                + f"\n\nCompleted: [{result.issue_key}]({result.web_url}) "
                + result.message
            ),
            view=None,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger)
    async def cancel(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        cancelled = await cancel_pending_jira_action(
            self.action_id,
            interaction.user.id,
        )
        if not cancelled:
            await _respond_with_unavailable_action(interaction, self.action_id)
            return
        self.stop()
        await interaction.response.edit_message(
            content="Jira change cancelled. Nothing was changed.",
            view=None,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def on_timeout(self) -> None:
        self._disable_buttons()
        try:
            expired = await expire_pending_jira_action(self.action_id)
        except Exception:
            logger.exception("Could not expire Jira approval record")
            return
        if expired and self.message is not None:
            try:
                await self.message.edit(
                    content="Jira approval request expired. Nothing was changed.",
                    view=None,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.HTTPException:
                logger.exception("Could not expire Jira approval message")

    def _disable_buttons(self) -> None:
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True


async def execute_jira_action(action: PendingJiraAction) -> JiraActionResult:
    payload = action.payload
    _require_configured_project(action)
    async with JiraClient.from_environment() as client:
        if action.action_type == "create":
            issue_key = await client.create_issue(
                project_key=str(payload["project_key"]),
                issue_type=str(payload["issue_type"]),
                summary=str(payload["summary"]),
                description=str(payload.get("description", "")),
            )
            message = "was created."
        elif action.action_type == "edit":
            issue_key = await client.edit_issue(
                str(payload["issue_key"]),
                str(payload["field"]),
                str(payload["value"]),
            )
            message = f"field {payload['field']} was updated."
        elif action.action_type == "comment":
            issue_key = await client.add_comment(
                str(payload["issue_key"]),
                str(payload["comment"]),
            )
            message = "received the comment."
        elif action.action_type == "transition":
            issue_key = await client.transition_issue(
                str(payload["issue_key"]),
                str(payload["status"]),
            )
            message = f"was transitioned to {payload['status']}."
        elif action.action_type == "assign":
            assignee = payload.get("assignee")
            issue_key = await client.assign_issue(
                str(payload["issue_key"]),
                str(assignee) if assignee is not None else None,
            )
            message = "was assigned." if assignee is not None else "was unassigned."
        else:
            raise ValueError(f"Unsupported Jira action: {action.action_type}")

        web_url = f"{client.base_url}/browse/{issue_key}"

    try:
        await sync_jira_issue(issue_key)
    except Exception:
        logger.exception("Jira write succeeded but refresh failed for %s", issue_key)

    return JiraActionResult(issue_key, message, web_url)


def describe_action(action: PendingJiraAction) -> str:
    payload = action.payload
    labels = {
        "create": "Create Jira issue",
        "edit": "Edit Jira issue",
        "comment": "Add Jira comment",
        "transition": "Transition Jira issue",
        "assign": "Assign Jira issue",
    }
    field_order = {
        "create": ("project_key", "issue_type", "summary", "description"),
        "edit": ("issue_key", "field", "value"),
        "comment": ("issue_key", "comment"),
        "transition": ("issue_key", "status"),
        "assign": ("issue_key", "assignee"),
    }
    lines = [f"**Approval required: {labels.get(action.action_type, action.action_type)}**"]
    for field in field_order.get(action.action_type, tuple(payload)):
        value = payload.get(field)
        if action.action_type == "assign" and field == "assignee" and value is None:
            value = "Unassigned"
        lines.append(f"**{field.replace('_', ' ').title()}:** {_clean_text(value, 800)}")
    return "\n".join(lines)[:1_800]


async def _respond_with_unavailable_action(
    interaction: discord.Interaction,
    action_id: UUID,
) -> None:
    action = await get_pending_jira_action(action_id)
    if action is None:
        message = "This approval request no longer exists. Nothing was changed."
    elif action.status == "pending":
        message = "This approval request expired. Nothing was changed."
    else:
        message = f"This approval request is already {action.status}."
    await interaction.response.send_message(message, ephemeral=True)


def _clean_text(value: Any, limit: int) -> str:
    text = str(value).replace("\r", " ").strip()
    text = discord.utils.escape_markdown(text)
    if len(text) > limit:
        suffix = "... [truncated; review the original command]"
        text = text[: limit - len(suffix)] + suffix
    return text or "(empty)"


def _fit_discord_content(value: str) -> str:
    return value[:2_000]


def _require_configured_project(action: PendingJiraAction) -> None:
    if action.action_type == "create":
        project_key = str(action.payload["project_key"]).upper()
    else:
        project_key = str(action.payload["issue_key"]).upper().rsplit("-", 1)[0]
    if project_key not in get_jira_project_keys():
        raise ValueError("That issue is outside the projects in JIRA_PROJECT_KEYS")


def _normalize_jira_action(
    action_type: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    project_keys = get_jira_project_keys()
    if action_type == "create":
        project_key = validate_project_key(_required_text(payload, "project_key"))
        if project_key not in project_keys:
            raise ValueError("That project is outside JIRA_PROJECT_KEYS.")
        return {
            "project_key": project_key,
            "issue_type": _required_text(payload, "issue_type"),
            "summary": _required_text(payload, "summary"),
            "description": str(payload.get("description", "")).strip(),
        }

    issue_key = validate_issue_key(_required_text(payload, "issue_key"))
    if issue_key.rsplit("-", 1)[0] not in project_keys:
        raise ValueError("That issue is outside JIRA_PROJECT_KEYS.")

    if action_type == "edit":
        field = _required_text(payload, "field").casefold()
        if field not in {"summary", "description", "priority", "labels"}:
            raise ValueError(
                "Editable Jira fields are summary, description, priority, and labels."
            )
        value = str(payload.get("value", "")).strip()
        if not value and field not in {"description", "labels"}:
            raise ValueError(f"Jira field {field} cannot be empty.")
        return {"issue_key": issue_key, "field": field, "value": value}
    if action_type == "comment":
        return {
            "issue_key": issue_key,
            "comment": _required_text(payload, "comment"),
        }
    if action_type == "transition":
        return {
            "issue_key": issue_key,
            "status": _required_text(payload, "status"),
        }
    if action_type == "assign":
        assignee = payload.get("assignee")
        if assignee is not None:
            assignee = str(assignee).strip()
            if not assignee:
                raise ValueError("Jira assignee cannot be empty.")
        return {"issue_key": issue_key, "assignee": assignee}
    raise ValueError(f"Unsupported Jira action: {action_type}")


def _required_text(payload: dict[str, Any], field: str) -> str:
    value = str(payload.get(field, "")).strip()
    if not value:
        raise ValueError(f"Jira {field.replace('_', ' ')} cannot be empty.")
    return value

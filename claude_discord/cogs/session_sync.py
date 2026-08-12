"""CLI session sync utilities for importing sessions as Discord threads.

Extracted from SessionManageCog to keep the Cog thin.  The Cog calls
these functions; the actual thread-creation and message-posting logic
lives here so it can be tested independently of Discord slash commands.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

import discord

from ..database.repository import SessionRepository
from ..discord_ui.embeds import COLOR_INFO
from ..session_sync import CliBackend, CliSession, extract_recent_messages, scan_all_cli_sessions

if TYPE_CHECKING:
    from ..backend_settings import BackendSettings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SyncResult:
    """Outcome of a sync-sessions run."""

    total_found: int
    imported: int
    skipped: int


def cli_display_name(backend: str) -> str:
    """Return the human-facing name for a session-owning CLI backend."""
    return "Codex CLI" if backend == "codex" else "Claude Code CLI"


def cli_resume_command(backend: str | None, session_id: str) -> str:
    """Return the interactive terminal command for a native session resume."""
    if backend == "codex":
        return f"codex resume {session_id}"
    return f"claude --resume {session_id}"


async def create_sync_thread(
    channel: discord.TextChannel,
    cli_session: CliSession,
    thread_name: str,
    style: str,
) -> discord.Thread:
    """Create a thread using the configured style.

    - channel: Creates a standalone thread in the Threads panel.
    - message: Posts a summary embed, then creates a thread from it.
    """
    if style == "message":
        embed = discord.Embed(
            title=f"\U0001f5a5\ufe0f {thread_name[:80]}",
            description=(
                f"```\n{cli_resume_command(cli_session.backend, cli_session.session_id)}\n```"
            ),
            color=COLOR_INFO,
        )
        if cli_session.working_dir:
            dir_short = cli_session.working_dir.rsplit("/", 1)[-1]
            embed.add_field(name="Directory", value=f"`{dir_short}`", inline=True)
        if cli_session.timestamp:
            embed.add_field(name="Created", value=cli_session.timestamp[:10], inline=True)
        embed.set_footer(text=f"Session: {cli_session.session_id[:8]}...")

        summary_msg = await channel.send(embed=embed)
        return await summary_msg.create_thread(name=f"\U0001f5a5 {thread_name}"[:100])

    # Default: channel thread
    return await channel.create_thread(
        name=f"\U0001f5a5 {thread_name}"[:100],
        type=discord.ChannelType.public_thread,
    )


async def post_recent_messages(
    thread: discord.Thread,
    cli_sessions_path: str,
    session_id: str,
    *,
    backend: CliBackend = "claude",
) -> None:
    """Post recent conversation messages inside the thread for context."""
    recent = await asyncio.to_thread(
        extract_recent_messages,
        cli_sessions_path,
        session_id,
        backend=backend,
        count=6,
        max_content_len=500,
    )
    if not recent:
        return

    lines: list[str] = []
    for msg in recent:
        if msg.role == "user":
            lines.append(f"**You:** {msg.content}")
        else:
            assistant = "Codex" if backend == "codex" else "Claude"
            lines.append(f"**{assistant}:** {msg.content}")

    # Split into chunks that fit Discord's 2000 char limit
    chunk = ""
    for line in lines:
        candidate = f"{chunk}\n\n{line}" if chunk else line
        if len(candidate) > 1900:
            if chunk:
                await thread.send(chunk)
            chunk = line[:1900]
        else:
            chunk = candidate
    if chunk:
        await thread.send(chunk)


async def sync_cli_sessions(
    *,
    cli_sessions_path: str,
    channel: discord.TextChannel,
    repo: SessionRepository,
    thread_style: str,
    since_hours: int,
    min_results: int,
    codex_sessions_path: str | None = None,
    backend_settings: BackendSettings | None = None,
) -> SyncResult:
    """Scan CLI sessions and import them as Discord threads.

    Returns a SyncResult with counts of found/imported/skipped sessions.
    """
    # Run CPU/IO-heavy scan in a thread to avoid blocking the event loop
    cli_sessions = await asyncio.to_thread(
        scan_all_cli_sessions,
        claude_sessions_path=cli_sessions_path,
        codex_sessions_path=codex_sessions_path,
        since_hours=since_hours,
        min_results=min_results,
    )

    imported = 0
    skipped = 0

    for cli_session in cli_sessions:
        # Check if already tracked
        existing = await repo.get_by_session_id(cli_session.session_id)
        if existing:
            skipped += 1
            continue

        thread_name = (cli_session.summary or cli_session.session_id)[:100]

        # Create thread based on configured style
        thread = await create_sync_thread(channel, cli_session, thread_name, thread_style)

        # Save to DB
        await repo.save(
            thread_id=thread.id,
            session_id=cli_session.session_id,
            working_dir=cli_session.working_dir,
            origin="cli",
            summary=cli_session.summary,
            backend=cli_session.backend,
        )
        if backend_settings is not None:
            await backend_settings.set_backend(cli_session.backend, thread_id=thread.id)

        # Post info embed inside the thread (for channel-style threads
        # this is the main content; for message-style the embed is on
        # the parent message so we skip the duplicate here)
        if thread_style == "channel":
            info_embed = discord.Embed(
                title=f"\U0001f5a5\ufe0f Imported {cli_display_name(cli_session.backend)} Session",
                description=(
                    f"This thread is linked to a {cli_display_name(cli_session.backend)} session.\n"
                    f"Reply here to continue the conversation.\n\n"
                    f"```\n"
                    f"{cli_resume_command(cli_session.backend, cli_session.session_id)}\n"
                    f"```"
                ),
                color=COLOR_INFO,
            )
            if cli_session.working_dir:
                info_embed.add_field(
                    name="Working Directory",
                    value=f"`{cli_session.working_dir}`",
                    inline=True,
                )
            if cli_session.timestamp:
                info_embed.add_field(name="Created", value=cli_session.timestamp[:10], inline=True)
            info_embed.set_footer(text=f"Session: {cli_session.session_id[:8]}...")
            await thread.send(embed=info_embed)

        # Post recent conversation messages for context
        sessions_path = (
            codex_sessions_path
            if cli_session.backend == "codex" and codex_sessions_path
            else cli_sessions_path
        )
        await post_recent_messages(
            thread,
            sessions_path,
            cli_session.session_id,
            backend=cli_session.backend,
        )

        imported += 1

    return SyncResult(
        total_found=len(cli_sessions),
        imported=imported,
        skipped=skipped,
    )

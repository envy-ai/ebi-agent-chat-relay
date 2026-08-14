"""Concurrency awareness for multiple simultaneous Claude Code sessions.

An in-memory registry tracks active sessions. Prompt guidance is emitted only
when another session is actually active; optional worktree policy is built
separately so deployments can opt into it without changing the default flow.

See: https://github.com/ebibibi/ebi-agent-chat-relay/issues/52
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Layer 2: Active Session Registry
# ---------------------------------------------------------------------------


@dataclass
class ActiveSession:
    """Tracks a single active Claude Code session."""

    thread_id: int
    description: str
    working_dir: str | None = None


_OTHER_SESSIONS_HEADER = "⚠️ Other active sessions (coordinate overlapping work):"

_WORKTREE_NOTICE = """\
[SESSION WORKTREE — REQUIRED]
Before changing a Git repository, run \
`git worktree add ../wt-{thread_id} -b session/{thread_id}` and work only in \
that worktree. Do not modify the main working directory. Commit and push your \
branch before finishing.\
"""


def build_worktree_notice(thread_id: int) -> str:
    """Return the opt-in mandatory session-worktree policy."""
    return _WORKTREE_NOTICE.format(thread_id=thread_id)


class SessionRegistry:
    """Thread-safe registry of active Claude Code sessions.

    Designed to be shared across all Cogs in a single bot instance.
    """

    def __init__(self) -> None:
        self._sessions: dict[int, ActiveSession] = {}
        self._lock = threading.Lock()

    def register(
        self,
        thread_id: int,
        description: str,
        working_dir: str | None = None,
    ) -> None:
        """Register or replace an active session."""
        with self._lock:
            self._sessions[thread_id] = ActiveSession(
                thread_id=thread_id,
                description=description,
                working_dir=working_dir,
            )

    def unregister(self, thread_id: int) -> None:
        """Remove a session from the registry."""
        with self._lock:
            self._sessions.pop(thread_id, None)

    def update(
        self,
        thread_id: int,
        *,
        description: str | None = None,
        working_dir: str | None = None,
    ) -> None:
        """Update fields of an existing session. No-op if not registered."""
        with self._lock:
            session = self._sessions.get(thread_id)
            if session is None:
                return
            if description is not None:
                session.description = description
            if working_dir is not None:
                session.working_dir = working_dir

    def list_active(self) -> list[ActiveSession]:
        """Return all active sessions."""
        with self._lock:
            return list(self._sessions.values())

    def list_others(self, thread_id: int) -> list[ActiveSession]:
        """Return all active sessions except the given thread."""
        with self._lock:
            return [s for s in self._sessions.values() if s.thread_id != thread_id]

    def build_concurrency_notice(self, thread_id: int) -> str:
        """Describe other active sessions, or return an empty string."""
        others = self.list_others(thread_id)
        if not others:
            return ""

        lines = [_OTHER_SESSIONS_HEADER]
        for session in others:
            line = f"- {session.description}"
            if session.working_dir:
                line += f" (working in {session.working_dir})"
            lines.append(line)
        lines.append(
            "If your work overlaps one of these sessions, inspect its status and coordinate "
            "before editing the same files or shared resources."
        )
        return "\n".join(lines)

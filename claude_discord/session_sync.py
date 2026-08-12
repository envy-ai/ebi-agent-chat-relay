"""CLI session scanners for syncing Claude Code and Codex sessions with Discord."""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)

# Claude stores each session as ``<uuid>.jsonl``.
_SESSION_FILE_PATTERN = re.compile(r"^[a-f0-9\-]{36}\.jsonl$")
_SESSION_ID_PATTERN = re.compile(r"^[a-f0-9\-]+$")

CliBackend = Literal["claude", "codex"]

# Max summary length
_MAX_SUMMARY_LEN = 100


@dataclass(frozen=True)
class SessionMessage:
    """A single message from a CLI session conversation."""

    role: str  # "user" or "assistant"
    content: str  # Truncated text content
    timestamp: str | None


@dataclass(frozen=True)
class CliSession:
    """A session discovered from a supported CLI's local storage."""

    session_id: str
    working_dir: str | None
    summary: str | None
    timestamp: str | None
    backend: CliBackend = "claude"


def scan_cli_sessions(
    base_path: str,
    *,
    backend: CliBackend = "claude",
    limit: int = 50,
    max_lines_per_file: int = 20,
    since_days: int = 0,
    since_hours: int = 0,
    min_results: int = 0,
) -> list[CliSession]:
    """Scan one supported CLI session directory.

    Supports two-tier filtering: first returns sessions modified within
    ``since_hours``.  If fewer than ``min_results`` are found, fills up
    to ``min_results`` from the most recently modified files regardless
    of age.  This ensures there are always enough results while
    prioritising recent activity.

    Args:
        base_path: Path to ``~/.claude/projects`` or ``~/.codex/sessions``.
        backend: Transcript format stored under ``base_path``.
        limit: Maximum number of sessions to return. Files are sorted by
               modification time (newest first) and only the newest ``limit``
               files are parsed. Set to 0 for no limit.
        max_lines_per_file: Maximum lines to read per file when searching for
                            the first user message. Prevents reading entire
                            multi-MB session files.
        since_days: Only include files modified within the last N days.
                    Set to 0 (default) for no time filter.  Ignored when
                    ``since_hours`` is set.
        since_hours: Primary time filter — include files modified within the
                     last N hours.  When set together with ``min_results``,
                     enables two-tier filtering.  Set to 0 (default) for no
                     hour-based filter.
        min_results: Minimum number of results to return.  When the time
                     filter yields fewer files, the most recently modified
                     files are added until this minimum is reached (or all
                     files are exhausted).  Set to 0 (default) for no minimum.

    Returns:
        List of CliSession objects discovered, sorted by timestamp descending.
    """
    base = Path(base_path).expanduser()
    if not base.is_dir():
        return []

    jsonl_files = _collect_session_files(base, backend)

    # Sort by modification time (newest first) — needed for both filter paths
    jsonl_files.sort(key=lambda p: p.stat().st_mtime, reverse=True)

    # --- Two-tier filtering (since_hours + min_results) ---
    if since_hours > 0:
        cutoff = time.time() - (since_hours * 3600)
        recent = [p for p in jsonl_files if p.stat().st_mtime >= cutoff]
        if min_results > 0 and len(recent) < min_results:
            # Fill up to min_results from most recent (already sorted)
            seen = set(id(p) for p in recent)
            for p in jsonl_files:
                if id(p) not in seen:
                    recent.append(p)
                if len(recent) >= min_results:
                    break
            jsonl_files = recent
        else:
            jsonl_files = recent
    # --- Legacy since_days filter (backward compat) ---
    elif since_days > 0:
        cutoff = time.time() - (since_days * 86400)
        jsonl_files = [p for p in jsonl_files if p.stat().st_mtime >= cutoff]

    # Apply limit
    if limit > 0:
        jsonl_files = jsonl_files[:limit]

    sessions: list[CliSession] = []
    for jsonl_path in jsonl_files:
        parser = _parse_codex_session_file if backend == "codex" else _parse_claude_session_file
        session = parser(jsonl_path, max_lines=max_lines_per_file)
        if session:
            sessions.append(session)

    # Sort by timestamp descending (most recent first)
    sessions.sort(key=lambda s: s.timestamp or "", reverse=True)
    return sessions


def scan_all_cli_sessions(
    *,
    claude_sessions_path: str | None,
    codex_sessions_path: str | None,
    limit: int = 10,
    max_lines_per_file: int = 20,
    since_hours: int = 0,
    min_results: int = 0,
) -> list[CliSession]:
    """Scan both standard CLI stores and return one newest-first result list."""
    sessions: list[CliSession] = []
    stores: tuple[tuple[str | None, CliBackend], ...] = (
        (claude_sessions_path, "claude"),
        (codex_sessions_path, "codex"),
    )
    for path, backend in stores:
        if not path:
            continue
        sessions.extend(
            scan_cli_sessions(
                path,
                backend=backend,
                limit=limit,
                max_lines_per_file=max_lines_per_file,
                since_hours=since_hours,
                min_results=min_results,
            )
        )
    sessions.sort(key=lambda session: session.timestamp or "", reverse=True)
    return sessions[:limit] if limit > 0 else sessions


def _collect_session_files(base: Path, backend: CliBackend) -> list[Path]:
    if backend == "codex":
        return list(base.rglob("rollout-*.jsonl"))
    return [
        path
        for path in [*base.glob("*.jsonl"), *base.glob("*/*.jsonl")]
        if _SESSION_FILE_PATTERN.match(path.name)
    ]


def _parse_claude_session_file(path: Path, *, max_lines: int = 20) -> CliSession | None:
    """Parse one Claude Code session file to extract metadata.

    Reads up to ``max_lines`` lines searching for the first real user message
    (non-meta, non-XML-prefixed) to use as the session summary.
    """
    session_id = path.stem
    working_dir: str | None = None
    summary: str | None = None
    timestamp: str | None = None

    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines_read = 0
            for line in f:
                lines_read += 1
                if lines_read > max_lines:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if data.get("type") != "user":
                    continue

                # Capture timestamp from any user message
                if not timestamp and data.get("timestamp"):
                    timestamp = data["timestamp"]

                # Skip meta messages
                if data.get("isMeta"):
                    continue

                content = _extract_content_text(data.get("message", {}).get("content", "")).strip()
                if not content:
                    continue

                # Skip XML-prefixed content (internal commands)
                if content.startswith("<"):
                    continue

                # Found the first real user message
                working_dir = data.get("cwd")
                summary = content[:_MAX_SUMMARY_LEN]
                if not timestamp:
                    timestamp = data.get("timestamp")
                break

    except OSError:
        logger.debug("Failed to read session file: %s", path, exc_info=True)
        return None

    if not summary:
        return None

    return CliSession(
        session_id=session_id,
        working_dir=working_dir,
        summary=summary,
        timestamp=timestamp,
        backend="claude",
    )


def _parse_codex_session_file(path: Path, *, max_lines: int = 20) -> CliSession | None:
    """Parse one Codex rollout, excluding child/subagent conversations."""
    session_id: str | None = None
    working_dir: str | None = None
    summary: str | None = None
    timestamp: str | None = None

    try:
        with path.open(encoding="utf-8", errors="replace") as stream:
            for lines_read, line in enumerate(stream, start=1):
                if lines_read > max_lines:
                    break
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if data.get("type") == "session_meta":
                    payload = data.get("payload", {})
                    source = payload.get("source")
                    if source is not None and not isinstance(source, str):
                        return None
                    candidate_id = payload.get("id") or payload.get("session_id")
                    if isinstance(candidate_id, str) and _SESSION_ID_PATTERN.fullmatch(
                        candidate_id
                    ):
                        session_id = candidate_id
                    cwd = payload.get("cwd")
                    working_dir = cwd if isinstance(cwd, str) else None
                    meta_timestamp = payload.get("timestamp") or data.get("timestamp")
                    timestamp = meta_timestamp if isinstance(meta_timestamp, str) else None
                    continue

                if data.get("type") != "event_msg":
                    continue
                payload = data.get("payload", {})
                if payload.get("type") != "user_message":
                    continue
                content = payload.get("message")
                if not isinstance(content, str) or not content.strip():
                    continue
                summary = content.strip()[:_MAX_SUMMARY_LEN]
                if timestamp is None and isinstance(data.get("timestamp"), str):
                    timestamp = data["timestamp"]
                break
    except OSError:
        logger.debug("Failed to read Codex rollout: %s", path, exc_info=True)
        return None

    if not session_id or not summary:
        return None
    return CliSession(
        session_id=session_id,
        working_dir=working_dir,
        summary=summary,
        timestamp=timestamp,
        backend="codex",
    )


def _extract_content_text(content: object) -> str:
    """Extract plain text from a message content field.

    Content can be a string or a list of content blocks.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        return " ".join(parts) if parts else ""
    return ""


def extract_recent_messages(
    base_path: str,
    session_id: str,
    *,
    backend: CliBackend = "claude",
    count: int = 5,
    max_content_len: int = 300,
) -> list[SessionMessage]:
    """Extract the most recent user/assistant messages from a session file.

    Reads the JSONL file for the given session and returns the last ``count``
    conversation turns (user + assistant pairs).

    Args:
        base_path: The supported CLI's session storage directory.
        session_id: The session UUID to look up.
        backend: Transcript format stored under ``base_path``.
        count: Number of recent messages to return.
        max_content_len: Maximum character length per message content.

    Returns:
        List of SessionMessage, ordered chronologically (oldest first).
    """
    if not _SESSION_ID_PATTERN.fullmatch(session_id):
        return []

    base = Path(base_path).expanduser()
    pattern = f"*-{session_id}.jsonl" if backend == "codex" else f"{session_id}.jsonl"
    candidates = list(base.rglob(pattern))
    if not candidates:
        return []

    path = candidates[0]
    all_messages: list[SessionMessage] = []

    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue

                parsed = (
                    _parse_codex_message(data)
                    if backend == "codex"
                    else _parse_claude_message(data)
                )
                if parsed is None:
                    continue
                role, content, timestamp = parsed
                truncated = content[:max_content_len]
                if len(content) > max_content_len:
                    truncated += "..."

                all_messages.append(
                    SessionMessage(
                        role=role,
                        content=truncated,
                        timestamp=timestamp,
                    )
                )

    except OSError:
        logger.debug("Failed to read session file: %s", path, exc_info=True)
        return []

    # Return last N messages
    return all_messages[-count:]


def _parse_claude_message(data: dict[str, object]) -> tuple[str, str, str | None] | None:
    msg_type = data.get("type")
    if msg_type not in ("user", "assistant") or data.get("isMeta"):
        return None
    message = data.get("message", {})
    if not isinstance(message, dict):
        return None
    content = _extract_content_text(message.get("content", "")).strip()
    if not content or content.startswith("<"):
        return None
    timestamp = data.get("timestamp")
    return (
        "user" if msg_type == "user" else "assistant",
        content,
        timestamp if isinstance(timestamp, str) else None,
    )


def _parse_codex_message(data: dict[str, object]) -> tuple[str, str, str | None] | None:
    if data.get("type") != "event_msg":
        return None
    payload = data.get("payload", {})
    if not isinstance(payload, dict):
        return None
    event_type = payload.get("type")
    if event_type not in ("user_message", "agent_message"):
        return None
    if event_type == "agent_message" and payload.get("phase") == "commentary":
        return None
    content = payload.get("message")
    if not isinstance(content, str) or not content.strip():
        return None
    timestamp = data.get("timestamp")
    return (
        "user" if event_type == "user_message" else "assistant",
        content.strip(),
        timestamp if isinstance(timestamp, str) else None,
    )

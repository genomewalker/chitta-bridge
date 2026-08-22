"""Deliver messages into Claude Code's cross-session messaging (SendMessage).

Reverse-engineered from claude-code 2.1.239. Lets a bridge-driven backend
(codex) send a message that lands mid-turn in a running Claude session, exactly
as another Claude session's SendMessage would.

Wire protocol — two newline-delimited JSON frames over the session's unix socket:
    {"type":"auth","token":<peerToken>}\\n
    {"msgV":1,"msg_id":<uuid>,"type":"user","message":{"role":"user",
     "content":<str>},"priority":"next","from":<addr>}\\n

Discovery: ~/.claude/sessions/<pid>.json      -> name, sessionId, messagingSocketPath
Auth:      ~/.claude/sessions/<pid>.<sha256(socketpath)>.key -> {"peerToken": ...}

ceiling: protocol is private and version-specific (peerProtocol=1); a claude-code
update can change the frame shape. upgrade: re-derive from the CLI bundle.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
import uuid
from pathlib import Path


def _sessions_dir() -> Path:
    return Path(os.path.expanduser("~/.claude/sessions"))


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def list_sessions(*, live_only: bool = True) -> list[dict]:
    """Return descriptors for Claude sessions that expose a messaging socket."""
    out = []
    for jf in _sessions_dir().glob("*.json"):
        try:
            d = json.loads(jf.read_text())
        except (OSError, ValueError):
            continue
        sock = d.get("messagingSocketPath")
        pid = d.get("pid")
        if not sock or not pid:
            continue
        if live_only and not (_pid_alive(pid) and os.path.exists(sock)):
            continue
        out.append({
            "name": d.get("name"),
            "sessionId": d.get("sessionId"),
            "pid": pid,
            "sock": sock,
            "cwd": d.get("cwd"),
            "status": d.get("status"),
            "address": f"uds:{sock}",
        })
    return out


def _peer_token(pid: int, sock: str) -> str | None:
    h = hashlib.sha256(sock.encode()).hexdigest()
    key = _sessions_dir() / f"{pid}.{h}.key"
    try:
        return json.loads(key.read_text()).get("peerToken")
    except (OSError, ValueError):
        return None


def resolve_recipient(recipient: str) -> dict:
    """Resolve a name / sessionId / uds: address / socket path to a live target.

    Raises ValueError with an actionable message (including reachable names) when
    the recipient can't be found.
    """
    recipient = recipient.strip()
    sessions = list_sessions()

    if recipient.startswith("uds:"):
        recipient = recipient[4:]
    if recipient.startswith("/"):  # raw socket path
        for s in sessions:
            if s["sock"] == recipient:
                return s
        raise ValueError(f"No live session listening at {recipient}")

    for s in sessions:  # exact name, then sessionId
        if s["name"] == recipient:
            return s
    for s in sessions:
        if s["sessionId"] == recipient:
            return s

    names = ", ".join(sorted(s["name"] for s in sessions if s["name"])) or "(none)"
    raise ValueError(f"No live Claude session named {recipient!r}. Reachable: {names}")


async def send(
    recipient: str,
    content: str,
    *,
    from_name: str | None = None,
    priority: str = "next",
    timeout: float = 5.0,
) -> dict:
    """Deliver `content` to a Claude session. Returns the resolved target dict.

    `from_name` is prepended to the body for attribution (codex has no inbox of
    its own, so replies come back through the bridge, not SendMessage).
    """
    target = resolve_recipient(recipient)
    token = _peer_token(target["pid"], target["sock"])
    if token is None:
        raise ValueError(
            f"No messaging key for session {target['name']!r} "
            f"(pid {target['pid']}) — cannot authenticate to its inbox"
        )

    body = f"[from {from_name}]\n{content}" if from_name else content
    frame = {
        "msgV": 1,
        "msg_id": str(uuid.uuid4()),
        "type": "user",
        "message": {"role": "user", "content": body},
        "priority": priority,
        "from": f"uds:codex:{from_name}" if from_name else "uds:codex",
    }
    payload = (
        json.dumps({"type": "auth", "token": token}) + "\n"
        + json.dumps(frame) + "\n"
    )

    reader, writer = await asyncio.wait_for(
        asyncio.open_unix_connection(path=target["sock"]), timeout=timeout
    )
    try:
        writer.write(payload.encode())
        await asyncio.wait_for(writer.drain(), timeout=timeout)
    finally:
        writer.close()
        try:
            await asyncio.wait_for(writer.wait_closed(), timeout=timeout)
        except (asyncio.TimeoutError, OSError):
            pass
    return {**target, "msg_id": frame["msg_id"], "at": time.time()}

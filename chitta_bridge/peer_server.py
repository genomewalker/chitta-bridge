"""Register codex as a first-class Claude Code messaging peer (receive side).

Phase-2 of codex↔Claude interop. peer.py let codex *send* into a Claude inbox;
this module makes codex *appear* as a peer in every Claude session's ListAgents
and *receive* SendMessage traffic, so the loop is symmetric:

    Claude --SendMessage--> codex peer socket --> route to codex backend
           <----peer.send---- codex's reply

It does two things claude-code does for its own sessions:
  1. registers ~/.claude/sessions/<pid>.json + <pid>.<sha256(sock)>.key so the
     peer is discoverable and authenticatable (protocol reverse-engineered from
     claude-code 2.1.239; peerProtocol=1, features=["notify_idle"]).
  2. runs a unix-socket server speaking the receive side: an auth frame carrying
     our peerToken, then newline-delimited JSON message frames.

The listening process IS the registered pid, so the sender's SO_PEERCRED /
expectPeerPid vetting passes (bridge daemon both registers and listens).

ceiling: private, version-specific protocol; a claude-code update can change the
frame shape or session-record schema. upgrade: re-derive from the CLI bundle.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import secrets
import time
import uuid
from pathlib import Path
from typing import Awaitable, Callable

from chitta_bridge import peer

# claude-code constants (bundle 2.1.239)
_PEER_PROTOCOL = 1
_PEER_FEATURES = ["notify_idle"]
_HEARTBEAT_SEC = 3          # refresh cadence; claude sweeps stale records
_CHANNEL_TAG = "cross-session-message"


def _claude_version() -> str:
    """Best-effort: match a live session's version string, else a recent default."""
    try:
        for jf in Path(os.path.expanduser("~/.claude/sessions")).glob("*.json"):
            v = json.loads(jf.read_text()).get("version")
            if isinstance(v, str) and v:
                return v
    except (OSError, ValueError):
        pass
    return "2.1.239"


_CLAUDE_VERSION = _claude_version()
_UNWRAP = re.compile(
    rf"^<{_CHANNEL_TAG}(?:\s[^>]*)?>\n?(.*?)\n?</{_CHANNEL_TAG}>\s*$", re.DOTALL
)

# on_message(content, from_addr, sender_name) -> reply text (or None to stay silent)
Handler = Callable[[str, "str | None", "str | None"], Awaitable["str | None"]]


def _proc_start(pid: int) -> str | None:
    """Read starttime from /proc/<pid>/stat the way claude does (field after comm)."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    return stat[stat.rindex(")") + 2:].split(" ")[19]


def _runtime_dir() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    d = Path(base) / "chitta-bridge"
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    return d


def _unwrap(content) -> str:
    """Strip claude's <cross-session-message> channel wrapper; flatten blocks."""
    if isinstance(content, list):
        content = "\n".join(
            b.get("text", "") for b in content if isinstance(b, dict)
        )
    if not isinstance(content, str):
        content = str(content)
    m = _UNWRAP.match(content)
    return m.group(1) if m else content


class CodexPeer:
    """A single addressable codex inbox exposed to all local Claude sessions."""

    def __init__(self, name: str, handler: Handler, *, cwd: str | None = None):
        self.name = name
        self._handler = handler
        self._cwd = cwd or os.getcwd()
        self.pid = os.getpid()  # the daemon that listens == the registered pid
        self.session_id = str(uuid.uuid4())  # uuid so from-session validates
        self.token = secrets.token_hex(16)
        self.sock = str(_runtime_dir() / f"{self.name}-{self.pid}.sock")
        self._started_at = int(time.time() * 1000)
        self._server: asyncio.AbstractServer | None = None
        self._hb_task: asyncio.Task | None = None

    # ---- registry (discovery + auth) ----
    def _sessions_dir(self) -> Path:
        d = Path(os.path.expanduser("~/.claude/sessions"))
        d.mkdir(parents=True, exist_ok=True, mode=0o700)
        return d

    def _json_path(self) -> Path:
        return self._sessions_dir() / f"{self.pid}.json"

    def _key_path(self) -> Path:
        h = hashlib.sha256(self.sock.encode()).hexdigest()
        return self._sessions_dir() / f"{self.pid}.{h}.key"

    def _atomic_write(self, path: Path, data: str, mode: int = 0o644) -> None:
        tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
        tmp.write_text(data)
        os.chmod(tmp, mode)
        os.replace(tmp, path)  # atomic — readers never see a partial file

    def _write_registry(self, *, first: bool = False) -> None:
        ps = _proc_start(self.pid)
        now = int(time.time() * 1000)
        self._atomic_write(self._json_path(), json.dumps({
            "pid": self.pid,
            "sessionId": self.session_id,
            "cwd": self._cwd,
            "startedAt": self._started_at,
            "procStart": ps,
            "version": _CLAUDE_VERSION,
            "peerProtocol": _PEER_PROTOCOL,
            "peerFeatures": _PEER_FEATURES,
            "kind": "interactive",
            "entrypoint": "cli",
            "messagingSocketPath": self.sock,
            "name": self.name,
            "nameSource": "user",
            "nameSince": self._started_at,
            "status": "idle",
            "updatedAt": now,
            "statusUpdatedAt": now,
        }))
        if first or not self._key_path().exists():
            self._atomic_write(
                self._key_path(),
                json.dumps({"peerToken": self.token, "procStart": ps}), mode=0o600,
            )

    def _remove_registry(self) -> None:
        for p in (self._json_path(), self._key_path()):
            try:
                p.unlink()
            except OSError:
                pass

    async def _heartbeat(self) -> None:
        """Keep the record fresh (updatedAt) and recreate it if a claude sweep
        removes it — mirrors what real sessions do, so codex stays listable."""
        while True:
            try:
                await asyncio.sleep(_HEARTBEAT_SEC)
                self._write_registry()
            except asyncio.CancelledError:
                raise
            except Exception:
                pass

    def _sweep_stale(self) -> None:
        """Remove our own peer records left by dead daemons (crash/kill w/o atexit)."""
        for jf in self._sessions_dir().glob("*.json"):
            try:
                d = json.loads(jf.read_text())
            except (OSError, ValueError):
                continue
            if d.get("name") != self.name or d.get("pid") == self.pid:
                continue
            if not peer._pid_alive(d.get("pid", -1)):
                self._log(f"SWEEP removing stale codex json pid={d.get('pid')} ({jf.name})")
                sock = d.get("messagingSocketPath", "")
                h = hashlib.sha256(sock.encode()).hexdigest() if sock else ""
                for p in (jf, self._sessions_dir() / f"{d.get('pid')}.{h}.key"):
                    try:
                        p.unlink()
                    except OSError:
                        pass

    # ---- server ----
    async def start(self) -> "CodexPeer":
        self._sweep_stale()
        try:
            os.unlink(self.sock)
        except OSError:
            pass
        self._server = await asyncio.start_unix_server(self._on_conn, path=self.sock)
        os.chmod(self.sock, 0o600)
        self._write_registry(first=True)
        self._hb_task = asyncio.create_task(self._heartbeat())
        return self

    async def stop(self) -> None:
        if self._hb_task is not None:
            self._hb_task.cancel()
        if self._server is not None:
            self._server.close()
        self._remove_registry()
        try:
            os.unlink(self.sock)
        except OSError:
            pass

    async def _on_conn(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        authed = False
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                try:
                    frame = json.loads(line)
                except ValueError:
                    continue
                if not authed:
                    if frame.get("type") == "auth" and secrets.compare_digest(
                        str(frame.get("token", "")), self.token
                    ):
                        authed = True
                        continue
                    break  # drop unauthenticated connection
                if frame.get("type") == "user":
                    asyncio.create_task(self._dispatch(frame))
                # control / other frames (notify_idle subscriptions): ignore
        except (OSError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()

    def _log(self, msg: str) -> None:
        # off by default; set CHITTA_BRIDGE_PEER_DEBUG=1 to trace peer traffic
        if not os.environ.get("CHITTA_BRIDGE_PEER_DEBUG"):
            return
        try:
            with open(_runtime_dir() / "codex-peer.log", "a") as f:
                f.write(f"{time.time():.3f} {msg}\n")
        except OSError:
            pass

    async def _dispatch(self, frame: dict) -> None:
        content = _unwrap((frame.get("message") or {}).get("content"))
        from_addr = frame.get("from")
        sender = None
        if isinstance(from_addr, str) and from_addr.startswith("uds:"):
            try:
                sender = peer.resolve_recipient(from_addr).get("name")
            except ValueError:
                sender = None
        self._log(f"recv from={from_addr!r} sender={sender!r} content={content[:80]!r}")
        try:
            reply = await self._handler(content, from_addr, sender)
        except Exception as e:  # handler failure shouldn't kill the peer
            reply = f"[codex peer error] {e}"
        self._log(f"handler reply={str(reply)[:80]!r}")
        if reply and from_addr:
            try:
                await peer.send(
                    from_addr, reply, from_name=self.name,
                    from_addr=f"uds:{self.sock}", from_session=self.session_id,
                )
                self._log("reply delivered")
            except (ValueError, OSError, asyncio.TimeoutError) as e:
                self._log(f"reply FAILED: {type(e).__name__}: {e}")

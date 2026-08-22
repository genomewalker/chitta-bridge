"""Receive-side test for chitta_bridge.peer_server (Claude→codex→reply loop).

Registers a CodexPeer in a temp HOME, delivers a claude-style message to it with
peer.send (unwrapping the channel tag), and asserts the handler runs and the
reply is routed back to the sender's inbox — the full symmetric loop.
"""
import asyncio
import hashlib
import json
import os
import shutil
import tempfile

from chitta_bridge import peer
from chitta_bridge.peer_server import CodexPeer, _unwrap


def test_unwrap_channel_tag():
    wrapped = '<cross-session-message from="uds:/x">\nhello world\n</cross-session-message>'
    assert _unwrap(wrapped) == "hello world"
    assert _unwrap("plain") == "plain"
    assert _unwrap([{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]) == "a\nb"


async def _loop(tmp_path):
    sd = tmp_path / ".claude" / "sessions"
    sd.mkdir(parents=True)
    os.environ["HOME"] = str(tmp_path)
    # unix socket paths cap at ~108 chars, so keep sockets in a short dir
    short = tempfile.mkdtemp(prefix="cbp-")
    os.environ["XDG_RUNTIME_DIR"] = short

    # fake "sender" Claude session with an inbox the reply must reach
    sender_sock = os.path.join(short, "sender.sock")
    (sd / "100001.json").write_text(json.dumps({
        "pid": 100001, "sessionId": "s", "name": "sender-1",
        "messagingSocketPath": sender_sock, "cwd": "/tmp",
    }))
    sh = hashlib.sha256(sender_sock.encode()).hexdigest()
    (sd / f"100001.{sh}.key").write_text(json.dumps({"peerToken": "cafe" * 8}))

    reply_frames: list = []

    async def sender_srv(reader, writer):
        data = await reader.read()
        for ln in data.decode().splitlines():
            reply_frames.append(json.loads(ln))
        writer.close()

    srv = await asyncio.start_unix_server(sender_srv, path=sender_sock)

    seen = {}

    async def handler(content, from_addr, sender):
        seen["content"] = content
        seen["sender"] = sender
        return "codex says hi"

    p = CodexPeer("codex", handler, cwd="/tmp")
    p.pid = 100002  # distinct from the fake sender pid
    await p.start()

    # peer.send wraps in the channel frame (as real claude does); the server
    # then unwraps it before handing content to the handler.
    await peer.send(
        f"uds:{p.sock}", "review this",
        from_name="sender-1", from_addr=f"uds:{sender_sock}",
    )
    await asyncio.sleep(0.3)  # handler + reply round-trip

    srv.close()
    await p.stop()
    shutil.rmtree(short, ignore_errors=True)
    return seen, reply_frames


def test_receive_and_reply(tmp_path, monkeypatch):
    monkeypatch.setattr(peer, "_pid_alive", lambda pid: True)
    home = os.environ.get("HOME")
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    try:
        seen, replies = asyncio.run(_loop(tmp_path))
    finally:
        if home:
            os.environ["HOME"] = home
        if xdg is not None:
            os.environ["XDG_RUNTIME_DIR"] = xdg
        else:
            os.environ.pop("XDG_RUNTIME_DIR", None)

    assert seen.get("content") == "review this"          # channel tag unwrapped
    assert seen.get("sender") == "sender-1"              # from_addr resolved to name
    user = [f for f in replies if f.get("type") == "user"]
    assert user
    content = user[0]["message"]["content"]
    assert 'from-name="codex"' in content and 'from-mode="prompting"' in content
    assert "\ncodex says hi\n</cross-session-message>" in content

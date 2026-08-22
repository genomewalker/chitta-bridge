"""Wire-protocol test for chitta_bridge.peer (codex→Claude messaging).

Spins up a fake Claude messaging socket + session registry in a temp HOME and
asserts peer.send() emits the exact two newline-delimited frames claude-code
expects: an auth frame carrying the peerToken, then the user-message frame.
"""
import asyncio
import hashlib
import json
import os

import pytest

from chitta_bridge import peer


async def _run(tmp_path):
    sessions = tmp_path / ".claude" / "sessions"
    sessions.mkdir(parents=True)
    sock = str(tmp_path / "peer.sock")
    pid = os.getpid()  # a live pid so list_sessions() keeps it
    token = "deadbeef" * 4

    (sessions / f"{pid}.json").write_text(json.dumps({
        "pid": pid, "sessionId": "sess-xyz", "name": "target-1",
        "messagingSocketPath": sock, "cwd": "/tmp",
    }))
    h = hashlib.sha256(sock.encode()).hexdigest()
    (sessions / f"{pid}.{h}.key").write_text(json.dumps({"peerToken": token}))

    received: list[bytes] = []
    server = await asyncio.start_unix_server(
        lambda r, w: _collect(r, w, received), path=sock,
    )
    try:
        os.environ["HOME"] = str(tmp_path)
        # resolve by name
        assert peer.resolve_recipient("target-1")["sock"] == sock
        res = await peer.send("target-1", "hello there", from_name="codex-x")
        await asyncio.sleep(0.05)
    finally:
        server.close()

    lines = b"".join(received).decode().splitlines()
    auth = json.loads(lines[0])
    frame = json.loads(lines[1])
    assert auth == {"type": "auth", "token": token}
    assert frame["type"] == "user" and frame["msgV"] == 1
    content = frame["message"]["content"]
    assert content.startswith('<cross-session-message ')
    assert 'from-name="codex-x"' in content
    assert 'from-mode="prompting"' in content
    assert "\nhello there\n</cross-session-message>" in content
    assert frame["priority"] == "next"
    assert res["name"] == "target-1"


async def _collect(reader, writer, sink):
    sink.append(await reader.read())
    writer.close()


def test_peer_wire_frames(tmp_path, monkeypatch):
    home = os.environ.get("HOME")
    try:
        asyncio.run(_run(tmp_path))
    finally:
        if home:
            os.environ["HOME"] = home


def test_resolve_unknown_lists_reachable(monkeypatch):
    monkeypatch.setattr(peer, "list_sessions", lambda **_: [{"name": "a", "sessionId": "s", "pid": 1, "sock": "/x"}])
    with pytest.raises(ValueError, match="Reachable: a"):
        peer.resolve_recipient("nope")

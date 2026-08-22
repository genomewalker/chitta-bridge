"""Per-TUI mailbox worker: exposes one codex TUI as a Claude messaging peer.

A codex TUI the user runs is independent — the bridge can't push turns into its
input loop. So instead this worker gives the TUI a first-class *inbox*:

  * runs as its own process (own pid) → registers a real session record and
    appears in every Claude session's ListAgents as `<name>`;
  * inbound cross-session messages are appended to a mailbox file;
  * the TUI drains them with the check_messages tool (pull model) and replies
    with message_claude (stamped from this peer, so replies come back here).

Spawned/stopped by the codex_peer_register / codex_peer_stop bridge tools.

Usage: python -m chitta_bridge.peer_worker --name codex-geodesic --cwd /path
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from pathlib import Path

from chitta_bridge.peer_server import CodexPeer


def mailbox_dir() -> Path:
    d = Path.home() / ".chitta-bridge" / "mailboxes"
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    return d


def mailbox_path(name: str) -> Path:
    return mailbox_dir() / f"{name}.jsonl"


async def _amain() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True)
    ap.add_argument("--cwd", default=os.getcwd())
    args = ap.parse_args()

    mp = mailbox_path(args.name)

    async def handler(content, from_addr, sender):
        rec = {"at": time.time(), "from": from_addr, "sender": sender, "content": content}
        with open(mp, "a") as f:
            f.write(json.dumps(rec) + "\n")
        return None  # pull model: queue only, the TUI drains via check_messages

    peer = CodexPeer(args.name, handler, cwd=args.cwd)
    await peer.start()
    try:
        await asyncio.Event().wait()
    finally:
        await peer.stop()


if __name__ == "__main__":
    asyncio.run(_amain())

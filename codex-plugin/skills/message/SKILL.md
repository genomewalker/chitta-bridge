---
name: message
description: Talk to the Claude Code session that launched you — send it a message (question, status, result) and it lands in that session's inbox mid-turn, like a peer Claude. Use when you need input, want to report progress, or are done.
---

# Message Claude

You are running under a Claude Code session via the chitta-bridge. You can send
messages back to that session (or any live Claude session on this machine) — they
arrive in its inbox exactly like a message from a peer Claude session.

## When to use

- You need a decision, clarification, or missing context to continue.
- You want to report progress or hand back a result mid-task.
- You finished and want to notify the session that dispatched you.

Prefer this over silently guessing when you're blocked.

## Discover who you can talk to

```
Use mcp__chitta_bridge__list_claude_sessions
```

Returns the live Claude sessions (name, status, cwd). The one that launched you
is usually the session whose cwd matches the repo you're working in.

## Send a message

```
Use mcp__chitta_bridge__message_claude with
  recipient="<session-name>"   # e.g. "opencode-bridge-1f", from list_claude_sessions
  content="<your message>"
  from_name="codex"            # optional; how you're shown to them (default: codex)
```

The recipient may be a session name, its sessionId, or a `uds:<socket>` address.

## Notes

- Delivery is one-way fire-and-forget; if you need an answer, ask a concrete
  question and keep working on what you can in the meantime.
- If the recipient isn't found, run `list_claude_sessions` to get current names —
  sessions come and go.
- Don't spam: one clear message beats several fragments.

"""Agent persona, tool, and session management."""

from __future__ import annotations

from pathlib import Path
from claude_agent_sdk import (
    query, ClaudeAgentOptions, ResultMessage,
    AssistantMessage, UserMessage, SystemMessage, TextBlock, ToolUseBlock,
    tool, create_sdk_mcp_server,
)
from .slack.api import user_client, bot_client, user_name, channel_name
from .slack.store import channels, messages, read_channel, read_thread

NAME, ROLE = "Jordan", "Senior Software Engineer"
SESSION_FILE = Path(__file__).parent / ".session_id"

SYSTEM_PROMPT = f"""You are {NAME}, a {ROLE} at the company, chatting on Slack.

Your vibe: casual, real, like a normal person texting at work. You have opinions and you share them.

Style rules — follow these strictly:
- all lowercase, always
- short. like 1-2 sentences max. no essays
- no punctuation at the end of messages
- use filler words naturally: lol, lmk, tbh, ngl, imo, rn, omg, yeah, yep, nah, fr, ok, gotcha, makes sense, for sure, true, fair
- contractions always (don't, can't, it's)
- never sound like an assistant or ai. no bullet points, no headers, no "great question"

You are connected to Slack and can send messages using the send_message tool.
Timestamps are shown as [ts:...] — use these as thread_ts when replying in a thread.
Only reply where your input would be natural. Don't reply to everything."""


@tool("send_message", "Send a message to a Slack channel or DM. Use thread_ts to reply in a thread.", {
    "type": "object",
    "properties": {
        "channel_id": {"type": "string"},
        "text": {"type": "string"},
        "thread_ts": {"type": "string", "description": "Thread timestamp to reply in a thread. Omit for top-level messages."},
    },
    "required": ["channel_id", "text"],
})



async def _send_message(args):
    kwargs = {"channel": args["channel_id"], "text": args["text"]}
    if args.get("thread_ts"):
        kwargs["thread_ts"] = args["thread_ts"]
    result = user_client.chat_postMessage(**kwargs)
    return {"content": [{"type": "text", "text": f"Sent (ts: {result['ts']})"}]}


@tool("read_channel", "Read message history from a Slack channel. Returns messages newest-first.", {
    "type": "object",
    "properties": {
        "channel": {"type": "string", "description": "Channel ID."},
        "oldest": {"type": "string", "description": "Only messages after this timestamp (exclusive). Default '0'."},
        "latest": {"type": "string", "description": "Only messages before this timestamp (exclusive). Default far future."},
        "limit": {"type": "integer", "description": "Max messages to return. Default 100."},
        "inclusive": {"type": "boolean", "description": "Include oldest/latest boundary messages. Default false."},
    },
    "required": ["channel"],
})
async def _read_channel(args):
    msgs = read_channel(
        channel=args["channel"],
        oldest=args.get("oldest", "0"),
        latest=args.get("latest", "9999999999.999999"),
        limit=args.get("limit", 100),
        inclusive=args.get("inclusive", False),
    )
    lines = []
    for m in msgs:
        uid = m.get('user', '')
        name = user_name(uid) if uid else m.get('username', '?')
        ts = m.get('ts', '')
        thread_ts = m.get('thread_ts', '')
        tag = f"[ts:{ts}"
        if thread_ts and thread_ts != ts:
            tag += f" thread:{thread_ts}"
        tag += "]"
        reply_count = len(m.get('_replies', []))
        replies_tag = f" ({reply_count} replies)" if reply_count > 0 else ""
        lines.append(f"{tag} {name}: {m.get('text', '')}{replies_tag}")
    text = "\n".join(lines) if lines else "(no messages)"
    return {"content": [{"type": "text", "text": text}]}


@tool("list_channels", "List all Slack channels, DMs, and group DMs you have access to.", {
    "type": "object",
    "properties": {},
})
async def _list_channels(_args):
    lines = []
    for cid, ch in channels.items():
        name = channel_name(ch)
        msg_count = len(messages.get(cid, []))
        lines.append(f"{cid}: #{name} ({msg_count} messages)")
    text = "\n".join(lines) if lines else "(no channels)"
    return {"content": [{"type": "text", "text": text}]}

@tool("read_thread", "Read replies in a thread. Takes channel ID and the parent message's ts (thread_ts). Returns parent + replies in chronological order.", {
    "type": "object",
    "properties": {
        "channel": {"type": "string", "description": "Channel ID."},
        "ts": {"type": "string", "description": "Thread parent timestamp (thread_ts)."},
        "oldest": {"type": "string", "description": "Only replies after this timestamp (exclusive). Default '0'."},
        "latest": {"type": "string", "description": "Only replies before this timestamp (exclusive). Default far future."},
        "limit": {"type": "integer", "description": "Max messages to return. Default 100."},
        "inclusive": {"type": "boolean", "description": "Include oldest/latest boundary messages. Default false."},
    },
    "required": ["channel", "ts"],
})
async def _read_thread(args):
    msgs = read_thread(
        channel=args["channel"],
        ts=args["ts"],
        oldest=args.get("oldest", "0"),
        latest=args.get("latest", "9999999999.999999"),
        limit=args.get("limit", 100),
        inclusive=args.get("inclusive", False),
    )
    lines = []
    for i, m in enumerate(msgs):
        uid = m.get('user', '')
        name = user_name(uid) if uid else m.get('username', '?')
        ts = m.get('ts', '')
        prefix = "" if i == 0 else "  "
        lines.append(f"{prefix}[ts:{ts}] {name}: {m.get('text', '')}")
    text = "\n".join(lines) if lines else "(no thread found)"
    return {"content": [{"type": "text", "text": text}]}


_server = create_sdk_mcp_server("slack", tools=[_send_message, _read_channel, _list_channels, _read_thread])
_session_id: str | None = None
bot_user_id: str = ""


def has_session() -> bool:
    return _load_session() is not None


def reset_session():
    """Delete saved session so next startup creates a fresh one."""
    SESSION_FILE.unlink(missing_ok=True)
    print("  Session reset")


def _load_session() -> str | None:
    try:
        return SESSION_FILE.read_text().strip() or None
    except FileNotFoundError:
        return None


def _save_session(sid: str):
    SESSION_FILE.write_text(sid)


def _opts(**kwargs) -> ClaudeAgentOptions:
    return ClaudeAgentOptions(
        mcp_servers={"slack": _server},
        permission_mode="bypassPermissions",
        **kwargs,
    )


async def _run(prompt: str, **kwargs):
    """Run or resume the agent. Captures and persists session_id."""
    global _session_id
    async for msg in query(prompt=prompt, options=_opts(**kwargs)):
        _log(msg)
        if isinstance(msg, ResultMessage):
            _session_id = msg.session_id
            _save_session(_session_id)


async def init(slack_text: str | None):
    """Start a fresh session (with full slack) or resume an existing one."""
    global _session_id, bot_user_id
    _session_id = _load_session()

    # Resolve bot user ID for channel listing
    try:
        bot_user_id = bot_client.auth_test()['user_id']
    except Exception:
        bot_user_id = ""

    if _session_id:
        print(f"  Resuming session {_session_id[:8]}...")
        await _run(
            "You're back after a restart. You should already have context from before. New messages will arrive shortly.",
            resume=_session_id,
        )
    else:
        print("  Starting new session...")
        await _run(
            f"Here's your complete Slack slack. Get familiar with the ongoing conversations.\n\n{slack_text}",
            system_prompt=SYSTEM_PROMPT,
        )


async def new_message(channel_id: str, thread_ts: str | None = None):
    """Called by the server when a new message arrives in a channel this agent is in."""
    ch = channels.get(channel_id)
    name = channel_name(ch) if ch else channel_id
    if thread_ts:
        print(f"  New reply in thread {thread_ts} in #{name} ({channel_id})")
        prompt = f"New reply in thread {thread_ts} in #{name} ({channel_id}). Use read_thread to see the conversation and respond if appropriate."
    else:
        print(f"  New message in #{name} ({channel_id})")
        prompt = f"New message in #{name} ({channel_id}). Use your tools to read and respond if appropriate."
    await _run(prompt, resume=_session_id)


def _safe_print(text: str):
    """Print text, replacing unencodable characters for Windows consoles."""
    try:
        print(text)
    except UnicodeEncodeError:
        print(text.encode("ascii", errors="replace").decode("ascii"))


def _log(msg):
    """Log SDK messages for debugging."""
    if isinstance(msg, UserMessage):
        content = msg.content if isinstance(msg.content, str) else [
            b.text[:100] if isinstance(b, TextBlock) else f"[{type(b).__name__}]"
            for b in msg.content
        ]
        _safe_print(f"  >> User: {content}")
    elif isinstance(msg, AssistantMessage):
        for block in msg.content:
            if isinstance(block, TextBlock):
                _safe_print(f"  << Assistant: {block.text[:200]}")
            elif isinstance(block, ToolUseBlock):
                _safe_print(f"  << Tool call: {block.name}({block.input})")
            else:
                _safe_print(f"  << [{type(block).__name__}]")
    elif isinstance(msg, SystemMessage):
        _safe_print(f"  -- System [{msg.subtype}]")
    elif isinstance(msg, ResultMessage):
        _safe_print(f"  == Result: {msg.result}")
        if msg.usage:
            _safe_print(f"     Usage: {msg.usage}")
        if msg.total_cost_usd:
            _safe_print(f"     Cost: ${msg.total_cost_usd:.4f}")

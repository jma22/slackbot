"""Agent persona, tool, and session management."""

from pathlib import Path
from claude_agent_sdk import (
    query, ClaudeAgentOptions, ResultMessage,
    AssistantMessage, UserMessage, SystemMessage, TextBlock, ToolUseBlock,
    tool, create_sdk_mcp_server,
)
from .slack.api import user_client, bot_client
from . import slack

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


_server = create_sdk_mcp_server("slack", tools=[_send_message])
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


async def new_message(channel_id: str):
    """Called by the server when a new message arrives in a channel this agent is in."""
    text = slack.render_channel(channel_id)
    if not text:
        return
    # slack.drain_channel(channel_id)
    await _run(
        text + "\n\nRespond to anything that makes sense for you. Don't reply to everything.",
        resume=_session_id,
    )


def _log(msg):
    """Log SDK messages for debugging."""
    if isinstance(msg, UserMessage):
        content = msg.content if isinstance(msg.content, str) else [
            b.text[:100] if isinstance(b, TextBlock) else f"[{type(b).__name__}]"
            for b in msg.content
        ]
        print(f"  >> User: {content}")
    elif isinstance(msg, AssistantMessage):
        for block in msg.content:
            if isinstance(block, TextBlock):
                print(f"  << Assistant: {block.text[:200]}")
            elif isinstance(block, ToolUseBlock):
                print(f"  << Tool call: {block.name}({block.input})")
            else:
                print(f"  << [{type(block).__name__}]")
    elif isinstance(msg, SystemMessage):
        print(f"  -- System [{msg.subtype}]")
    elif isinstance(msg, ResultMessage):
        print(f"  == Result: {msg.result}")
        if msg.usage:
            print(f"     Usage: {msg.usage}")
        if msg.total_cost_usd:
            print(f"     Cost: ${msg.total_cost_usd:.4f}")

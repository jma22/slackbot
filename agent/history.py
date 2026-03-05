"""Full workspace history: channels, messages, threads, and new-message queue.

Persisted to disk. Absorbs the old messages.py functionality: Socket Mode
ingestion, DM polling, dedup, async notification, and drain.
"""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from .slack import (
    my_user_id, user_name, channel_name,
    list_channels as slack_list_channels, fetch_messages, fetch_thread_replies,
)

_FILE = Path(__file__).parent / ".history.json"
_CURSOR_FILE = Path(__file__).parent / ".last_ts"

# --- State ---

# channel_id -> raw channel dict from Slack API
channels: dict[str, dict] = {}
# channel_id -> list of raw message dicts (chronological, threads in _replies)
messages: dict[str, list[dict]] = {}

# --- New-message queue ---

_new: list[tuple[str, dict]] = []  # (channel_id, msg_dict)
_seen: set[str] = set()            # ts values already queued (dedup)
_lock = threading.Lock()
_dm_cursors: dict[str, str] = {}   # channel_id -> latest_ts for DM polling

# Async notification
_loop: asyncio.AbstractEventLoop | None = None
_notify: asyncio.Event | None = None


# ───────────────────── Persistence ─────────────────────

def save():
    """Persist current state to disk."""
    data = {"channels": channels, "messages": messages}
    _FILE.write_text(json.dumps(data))


def _load_from_disk() -> bool:
    global channels, messages
    try:
        data = json.loads(_FILE.read_text())
        channels = data.get("channels", {})
        messages = data.get("messages", {})
        return bool(channels)
    except (FileNotFoundError, json.JSONDecodeError):
        return False


def _fetch_all():
    for ch in slack_list_channels():
        cid = ch['id']
        channels[cid] = ch
        try:
            msgs = fetch_messages(cid)
        except Exception as e:
            print(f"  Skipping #{channel_name(ch)}: {e}")
            continue
        for m in msgs:
            if m.get('reply_count', 0) > 0:
                try:
                    m['_replies'] = fetch_thread_replies(cid, m['ts'])
                except Exception:
                    m['_replies'] = []
            else:
                m['_replies'] = []
        messages[cid] = msgs
    print(f"  Fetched history from {len(channels)} channels")


def load():
    """Load history: from disk if available, otherwise full fetch from Slack."""
    if _load_from_disk():
        print(f"  Loaded history from disk ({len(channels)} channels)")
    else:
        print("  Fetching full history from Slack...")
        _fetch_all()
        save()


# ───────────────────── Update / catchup ─────────────────────

def update(channel_id: str, msg: dict):
    """Insert or update a message. Thread replies go under parent's _replies."""
    if channel_id not in messages:
        messages[channel_id] = []

    thread_ts = msg.get('thread_ts')
    if thread_ts and thread_ts != msg.get('ts'):
        for parent in messages[channel_id]:
            if parent.get('ts') == thread_ts:
                replies = parent.setdefault('_replies', [])
                if not any(r.get('ts') == msg.get('ts') for r in replies):
                    replies.append(msg)
                return

    if not any(m.get('ts') == msg.get('ts') for m in messages[channel_id]):
        messages[channel_id].append(msg)


def catchup(oldest: str) -> list[tuple[str, dict]]:
    """Fetch missed messages since oldest. Returns list of (channel_id, msg)."""
    new = []
    for ch in slack_list_channels():
        cid = ch['id']
        channels[cid] = ch
        try:
            all_msgs = fetch_messages(cid)
        except Exception:
            continue
        for m in all_msgs:
            ts = m.get('ts', '0')
            if ts > oldest and m.get('user') != my_user_id and m.get('text'):
                update(cid, m)
                new.append((cid, m))
            latest_reply = m.get('latest_reply', '0')
            if latest_reply > oldest:
                try:
                    replies = fetch_thread_replies(cid, ts)
                except Exception:
                    replies = []
                m['_replies'] = replies
                update(cid, m)
                for r in replies:
                    if r.get('ts', '0') > oldest and r.get('user') != my_user_id and r.get('text'):
                        new.append((cid, r))
        if cid not in messages:
            messages[cid] = []
    if new:
        print(f"  Caught up on {len(new)} missed message(s)")
    save()
    return new


# ───────────────────── Render ─────────────────────

def render() -> str:
    """Render full history as text for agent init."""
    lines = []
    for cid, ch in channels.items():
        name = channel_name(ch)
        msgs = messages.get(cid, [])
        lines.append(f"=== #{name} (id: {cid}) ===")
        for m in msgs:
            text = m.get('text', '')
            if not text:
                continue
            uid = m.get('user', '')
            uname = user_name(uid) if uid else m.get('username', '?')
            lines.append(f"  [ts:{m.get('ts')}] {uname}: {text}")
            for r in m.get('_replies', []):
                rtext = r.get('text', '')
                if not rtext:
                    continue
                ruid = r.get('user', '')
                rname = user_name(ruid) if ruid else r.get('username', '?')
                lines.append(f"    └ [ts:{r.get('ts')}] {rname}: {rtext}")
        lines.append("")
    print(f"  Rendered history from {len(channels)} channels")
    return "\n".join(lines)


def render_channel(channel_id: str) -> str | None:
    """Render new (pending) messages for a single channel as text."""
    with _lock:
        pending = [(cid, m) for cid, m in _new if cid == channel_id]
    if not pending:
        return None

    ch = channels.get(channel_id)
    label = channel_name(ch) if ch else channel_id

    lines = [f"New messages in #{label} ({channel_id}):"]
    for _, m in pending:
        uid = m.get('user', '')
        name = user_name(uid) if uid else m.get('username', '?')
        ts = m.get('ts', '')
        thread_ts = m.get('thread_ts', '')
        tag = f"[ts:{ts}"
        if thread_ts and thread_ts != ts:
            tag += f" thread:{thread_ts}"
        tag += "]"
        lines.append(f"  {tag} {name}: {m.get('text', '')}")
    return "\n".join(lines)


# ───────────────────── Async notification ─────────────────────

def init_notify(loop: asyncio.AbstractEventLoop):
    """Bind to the running event loop. Call once from async main."""
    global _loop, _notify
    _loop = loop
    _notify = asyncio.Event()


def _wake():
    """Thread-safe: wake the async main loop."""
    if _loop and _notify:
        _loop.call_soon_threadsafe(_notify.set)


# ───────────────────── Enqueue / ingest ─────────────────────

def _enqueue(channel_id: str, msg: dict):
    """Add a message to the queue if not already seen. Updates history."""
    ts = msg.get('ts', '')
    with _lock:
        if ts in _seen:
            return
        _seen.add(ts)
        _new.append((channel_id, msg))
    update(channel_id, msg)
    _wake()


def on_message(event: dict):
    """Process a Slack message event (from Socket Mode)."""
    subtype = event.get('subtype')
    if (subtype and subtype != 'bot_message') or not event.get('text') or not event.get('channel'):
        return
    msg = {k: v for k, v in event.items() if k in ('user', 'text', 'ts', 'thread_ts', 'bot_id', 'username', 'subtype')}
    if event.get('user') != my_user_id:
        _enqueue(event['channel'], msg)


def do_catchup():
    """Fetch missed messages since last drain, via catchup."""
    oldest = _load_last_ts()
    if oldest == '0':
        return
    for cid, msg in catchup(oldest):
        _enqueue(cid, msg)


def init_dm_cursors():
    """Set DM cursors to latest so we don't replay old DMs."""
    for ch in slack_list_channels(types="im,mpim"):
        cid = ch['id']
        try:
            msgs = fetch_messages(cid)
        except Exception:
            continue
        if msgs:
            _dm_cursors[cid] = msgs[-1]['ts']
    print(f"  {len(_dm_cursors)} DM channels tracked")


def poll_dms():
    """Poll user's DMs/group DMs for new messages."""
    for ch in slack_list_channels(types="im,mpim"):
        cid = ch['id']
        oldest = _dm_cursors.get(cid, '0')
        try:
            msgs = fetch_messages(cid, oldest=oldest)
        except Exception:
            continue
        new_msgs = [m for m in msgs if m['ts'] != oldest]
        if not new_msgs:
            continue
        for m in new_msgs:
            if m.get('user') != my_user_id:
                _enqueue(cid, m)
        _dm_cursors[cid] = new_msgs[-1]['ts']


# ───────────────────── on_new_msg / drain ─────────────────────

async def on_new_msg() -> list[dict]:
    """Block until new messages arrive, then return info about each new message.

    Returns a list of dicts, each with:
        - channel: str (channel ID)
        - thread_ts: str | None (set if this is a thread reply)
    Deduplicated by (channel, thread_ts) pair.
    """
    if _notify:
        await _notify.wait()
        _notify.clear()

    # Brief batch delay to collect rapid-fire messages
    await asyncio.sleep(1)

    with _lock:
        if not _new:
            return []
        # Build deduplicated list of (channel, thread_ts) notifications
        seen_keys = set()
        result = []
        for cid, m in _new:
            thread_ts = m.get('thread_ts') if m.get('thread_ts') != m.get('ts') else None
            key = (cid, thread_ts)
            if key not in seen_keys:
                seen_keys.add(key)
                result.append({"channel": cid, "thread_ts": thread_ts})

    # Persist cursor
    with _lock:
        if _new:
            latest_ts = max(m.get('ts', '0') for _, m in _new)
            _save_last_ts(latest_ts)
    save()

    return result


def drain_channel(channel_id: str):
    """Remove drained messages for a channel from the pending queue."""
    with _lock:
        _new[:] = [(cid, m) for cid, m in _new if cid != channel_id]
        # If queue is fully empty, clear seen set
        if not _new:
            _seen.clear()



# ───────────────────── Cursor persistence ─────────────────────

def _load_last_ts() -> str:
    try:
        return _CURSOR_FILE.read_text().strip() or '0'
    except FileNotFoundError:
        return '0'


def _save_last_ts(ts: str):
    _CURSOR_FILE.write_text(ts)


#------------------------- exposed API -----------------#


def list_channels(agent) -> list[str]:
    """Return channel IDs that the given agent is in (queries Slack API)."""
    from .slack import bot_client
    try:
        result = []
        cursor = None
        while True:
            r = bot_client.users_conversations(
                user=agent.bot_user_id,
                types="public_channel,private_channel,mpim,im",
                cursor=cursor,
                limit=200,
            )
            result.extend(ch['id'] for ch in r['channels'])
            cursor = r.get('response_metadata', {}).get('next_cursor')
            if not cursor:
                break
        return result
    except Exception as e:
        print(f"  Error listing channels for agent: {e}")
        return list(channels.keys())

def read_channel(
    channel: str,
    oldest: str = "0",
    latest: str = "9999999999.999999",
    limit: int = 100,
    inclusive: bool = False,
) -> list[dict]:
    """Read messages from a channel. Mirrors Slack conversations.history API.

    Args:
        channel: Channel ID.
        oldest: Only messages after this timestamp (exclusive, unless inclusive=True).
        latest: Only messages before this timestamp (exclusive, unless inclusive=True).
        limit: Maximum number of messages to return.
        inclusive: Include messages with oldest/latest timestamps.

    Returns:
        List of message dicts in reverse chronological order (newest first),
        matching Slack's conversations.history response format.
    """
    all_msgs = messages.get(channel, [])
    result = []
    for m in all_msgs:
        ts = m.get('ts', '0')
        if inclusive:
            if ts < oldest or ts > latest:
                continue
        else:
            if ts <= oldest or ts >= latest:
                continue
        result.append(m)
    # Slack returns newest first
    result.reverse()
    return result[:limit]


def read_thread(
    channel: str,
    ts: str,
    oldest: str = "0",
    latest: str = "9999999999.999999",
    limit: int = 100,
    inclusive: bool = False,
) -> list[dict]:
    """Read replies in a thread. Mirrors Slack conversations.replies API.

    Args:
        channel: Channel ID.
        ts: Thread parent timestamp.
        oldest: Only messages after this timestamp (exclusive, unless inclusive=True).
        latest: Only messages before this timestamp (exclusive, unless inclusive=True).
        limit: Maximum number of messages to return.
        inclusive: Include messages with oldest/latest timestamps.

    Returns:
        List of message dicts starting with the parent message, then replies
        in chronological order, matching Slack's conversations.replies response format.
    """
    all_msgs = messages.get(channel, [])

    # Find the parent message
    parent = None
    for m in all_msgs:
        if m.get('ts') == ts:
            parent = m
            break
    if parent is None:
        return []

    # Start with parent, then filter replies
    result = [parent]
    for r in parent.get('_replies', []):
        r_ts = r.get('ts', '0')
        if inclusive:
            if r_ts < oldest or r_ts > latest:
                continue
        else:
            if r_ts <= oldest or r_ts >= latest:
                continue
        result.append(r)

    return result[:limit]

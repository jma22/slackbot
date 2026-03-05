"""New message queue: Socket Mode events, DM polling, and drain for the agent."""

import asyncio
import threading
from pathlib import Path
from . import history
from .slack import my_user_id, user_name, channel_name, list_channels, fetch_messages

_CURSOR_FILE = Path(__file__).parent / ".last_ts"

_new: list[tuple[str, dict]] = []  # (channel_id, msg_dict)
_seen: set[str] = set()  # ts values already queued (dedup)
_lock = threading.Lock()
_dm_cursors: dict[str, str] = {}  # channel_id -> latest_ts for DM polling

# Async notification: wakes the main loop when messages arrive
_loop: asyncio.AbstractEventLoop | None = None
_notify: asyncio.Event | None = None


def init_notify(loop: asyncio.AbstractEventLoop):
    """Bind to the running event loop. Call once from async main."""
    global _loop, _notify
    _loop = loop
    _notify = asyncio.Event()


async def wait_for_new():
    """Await until new messages arrive."""
    if _notify:
        await _notify.wait()
        _notify.clear()


def _wake():
    """Thread-safe: wake the async main loop."""
    if _loop and _notify:
        _loop.call_soon_threadsafe(_notify.set)


def _enqueue(channel_id: str, msg: dict):
    """Add a message to the queue if not already seen. Updates history."""
    ts = msg.get('ts', '')
    with _lock:
        if ts in _seen:
            return
        _seen.add(ts)
        _new.append((channel_id, msg))
    history.update(channel_id, msg)
    _wake()


def _channel_label(channel_id: str) -> str:
    """Resolve channel display name from history, falling back to API."""
    ch = history.channels.get(channel_id)
    if ch:
        return channel_name(ch)
    return channel_id


# --- Ingest ---

def on_message(event: dict):
    """Process a Slack message event (from Socket Mode)."""
    subtype = event.get('subtype')
    if (subtype and subtype != 'bot_message') or not event.get('text') or not event.get('channel'):
        return
    msg = {k: v for k, v in event.items() if k in ('user', 'text', 'ts', 'thread_ts', 'bot_id', 'username', 'subtype')}
    if event.get('user') != my_user_id:
        _enqueue(event['channel'], msg)


def catchup():
    """Fetch missed messages since last drain, via history module."""
    oldest = _load_last_ts()
    if oldest == '0':
        return
    for cid, msg in history.catchup(oldest):
        _enqueue(cid, msg)


def init_dm_cursors():
    """Set DM cursors to latest so we don't replay old DMs."""
    for ch in list_channels(types="im,mpim"):
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
    for ch in list_channels(types="im,mpim"):
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


# --- Drain ---

def drain_new() -> str | None:
    """Drain pending messages as rendered text. Returns None if empty."""
    with _lock:
        if not _new:
            return None
        pending = list(_new)
        _new.clear()
        _seen.clear()

    latest_ts = max(m.get('ts', '0') for _, m in pending)
    _save_last_ts(latest_ts)
    history.save()

    by_channel: dict[str, list[dict]] = {}
    for cid, msg in pending:
        by_channel.setdefault(f"#{_channel_label(cid)} ({cid})", []).append(msg)

    lines = ["New messages:"]
    for ch_label, msgs in by_channel.items():
        lines.append(f"\n{ch_label}:")
        for m in msgs:
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


# --- Cursor persistence ---

def _load_last_ts() -> str:
    try:
        return _CURSOR_FILE.read_text().strip() or '0'
    except FileNotFoundError:
        return '0'


def _save_last_ts(ts: str):
    _CURSOR_FILE.write_text(ts)

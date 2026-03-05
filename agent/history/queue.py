"""New-message queue, async notification, and drain."""

import asyncio
import threading
from .store import channels, update, save, save_last_ts
from ..slack import my_user_id, user_name, channel_name

_new: list[tuple[str, dict]] = []
_seen: set[str] = set()
_lock = threading.Lock()

_loop: asyncio.AbstractEventLoop | None = None
_notify: asyncio.Event | None = None


def init_notify():
    global _loop, _notify
    _loop = asyncio.get_running_loop()
    _notify = asyncio.Event()


def _wake():
    if _loop and _notify:
        _loop.call_soon_threadsafe(_notify.set)


def enqueue(channel_id: str, msg: dict):
    """Add a message to the queue if not already seen. Updates history."""
    ts = msg.get('ts', '')
    with _lock:
        if ts in _seen:
            return
        _seen.add(ts)
        _new.append((channel_id, msg))
    update(channel_id, msg)
    _wake()


async def on_new_msg() -> list[str]:
    """Block until new messages arrive, then return list of channel IDs."""
    if _notify:
        await _notify.wait()
        _notify.clear()

    await asyncio.sleep(1)

    with _lock:
        if not _new:
            return []
        channel_ids = list(dict.fromkeys(cid for cid, _ in _new))

    with _lock:
        if _new:
            latest_ts = max(m.get('ts', '0') for _, m in _new)
            save_last_ts(latest_ts)
    save()

    return channel_ids


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


def drain_channel(channel_id: str):
    """Remove drained messages for a channel from the pending queue."""
    with _lock:
        _new[:] = [(cid, m) for cid, m in _new if cid != channel_id]
        if not _new:
            _seen.clear()

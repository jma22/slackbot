"""Persistent storage: channels, messages, load/save, update, catchup."""

import json
from pathlib import Path
from .api import (
    my_user_id, user_name, channel_name,
    list_channels as slack_list_channels, fetch_messages, fetch_thread_replies,
)

_FILE = Path(__file__).resolve().parent.parent / ".history.json"
_CURSOR_FILE = Path(__file__).resolve().parent.parent / ".last_ts"

# channel_id -> raw channel dict from Slack API
channels: dict[str, dict] = {}
# channel_id -> list of raw message dicts (chronological, threads in _replies)
messages: dict[str, list[dict]] = {}


def save():
    _FILE.write_text(json.dumps({"channels": channels, "messages": messages}))


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
    if _load_from_disk():
        print(f"  Loaded history from disk ({len(channels)} channels)")
    else:
        print("  Fetching full history from Slack...")
        _fetch_all()
        save()


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


def load_last_ts() -> str:
    try:
        return _CURSOR_FILE.read_text().strip() or '0'
    except FileNotFoundError:
        return '0'


def save_last_ts(ts: str):
    _CURSOR_FILE.write_text(ts)


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

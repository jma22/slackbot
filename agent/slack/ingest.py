"""Message ingestion: Socket Mode handler, DM polling, catchup."""

from .api import (
    my_user_id, list_channels as slack_list_channels, fetch_messages,
)
from .store import catchup, load_last_ts
from .queue import enqueue

_dm_cursors: dict[str, str] = {}


def on_message(event: dict):
    """Process a Slack message event (from Socket Mode)."""
    subtype = event.get('subtype')
    if (subtype and subtype != 'bot_message') or not event.get('text') or not event.get('channel'):
        return
    msg = {k: v for k, v in event.items() if k in ('user', 'text', 'ts', 'thread_ts', 'bot_id', 'username', 'subtype')}
    if event.get('user') != my_user_id:
        enqueue(event['channel'], msg)


def do_catchup():
    """Fetch missed messages since last drain, via catchup."""
    oldest = load_last_ts()
    if oldest == '0':
        return
    for cid, msg in catchup(oldest):
        enqueue(cid, msg)


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
                enqueue(cid, m)
        _dm_cursors[cid] = new_msgs[-1]['ts']

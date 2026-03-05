"""Slack clients, name resolution, and API helpers."""

import os
import time
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

user_client = WebClient(token=os.environ['SLACK_USER_TOKEN'])
bot_client = WebClient(token=os.environ['SLACK_BOT_TOKEN'])
my_user_id: str = ""  # set on startup

_name_cache: dict[str, str] = {}


def _paginate(method, key: str, **kwargs) -> list[dict]:
    """Generic Slack API pagination with rate-limit handling."""
    result, cursor, page = [], None, 0
    while True:
        page += 1
        try:
            t0 = time.time()
            r = method(cursor=cursor, limit=200, **kwargs)
            elapsed = time.time() - t0
            # print(f"  [slack] {method.__name__} page={page} got={len(r[key])} {elapsed:.1f}s")
        except SlackApiError as e:
            if e.response.status_code == 429:
                retry_after = int(e.response.headers.get('Retry-After', 5))
                print(f"  [slack] RATE LIMITED on {method.__name__} — waiting {retry_after}s")
                time.sleep(retry_after)
                continue
            raise
        result.extend(r[key])
        cursor = r.get('response_metadata', {}).get('next_cursor')
        if not cursor:
            return result


def init():
    """Resolve and cache our own user ID."""
    global my_user_id
    my_user_id = user_client.auth_test()['user_id']


def user_name(uid: str) -> str:
    if uid not in _name_cache:
        try:
            p = user_client.users_info(user=uid)['user']['profile']
            _name_cache[uid] = (
                p.get('display_name_normalized')
                or p.get('real_name_normalized')
                or p.get('real_name')
                or uid
            )
        except Exception:
            _name_cache[uid] = uid
    return _name_cache[uid]


def channel_name(ch: dict) -> str:
    return f"DM-{user_name(ch['user'])}" if ch.get('is_im') else ch.get('name', ch['id'])


def list_channels(types: str = "public_channel,private_channel,mpim,im") -> list[dict]:
    return _paginate(user_client.users_conversations, 'channels', types=types)


def fetch_messages(channel_id: str, oldest: str = "0") -> list[dict]:
    """Fetch top-level messages in chronological order."""
    # print(f"  [slack] fetch_messages({channel_id}, oldest={oldest})")
    msgs = _paginate(user_client.conversations_history, 'messages', channel=channel_id, oldest=oldest)
    # print(f"  [slack] fetch_messages({channel_id}) -> {len(msgs)} messages")
    msgs.reverse()
    return msgs


def fetch_thread_replies(channel_id: str, thread_ts: str) -> list[dict]:
    """Fetch thread replies (excludes parent)."""
    # print(f"  [slack] fetch_thread_replies({channel_id}, {thread_ts})")
    try:
        msgs = user_client.conversations_replies(channel=channel_id, ts=thread_ts, limit=200).get('messages', [])
    except SlackApiError as e:
        if e.response.status_code == 429:
            retry_after = int(e.response.headers.get('Retry-After', 5))
            print(f"  [slack] RATE LIMITED on conversations_replies — waiting {retry_after}s")
            time.sleep(retry_after)
            msgs = user_client.conversations_replies(channel=channel_id, ts=thread_ts, limit=200).get('messages', [])
        else:
            raise
    print(f"  [slack] fetch_thread_replies -> {len(msgs) - 1} replies")
    return msgs[1:]


def join_all_public_channels():
    """Have the bot join every public channel it's not already in."""
    member_ids = {ch['id'] for ch in _paginate(bot_client.users_conversations, 'channels', types="public_channel")}
    all_channels = _paginate(bot_client.conversations_list, 'channels', types="public_channel")

    joined = 0
    for ch in all_channels:
        if ch['id'] not in member_ids and not ch.get('is_archived'):
            try:
                bot_client.conversations_join(channel=ch['id'])
                joined += 1
            except Exception as e:
                print(f"  Could not join #{ch.get('name')}: {e}")
    print(f"  Joined {joined} new channel(s)")

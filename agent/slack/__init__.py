"""History subpackage — public API.

Call start() once to initialize Slack, load history, and begin ingesting
messages via Socket Mode + DM polling. Then await on_new_msg() in your main loop.
"""

import os
import time
import threading
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from .store import load, render
from .queue import on_new_msg, render_channel
from .ingest import on_message, do_catchup, init_dm_cursors, poll_dms

DM_POLL_INTERVAL = 5


def start():
    """Initialize Slack, load history, start Socket Mode + DM polling."""
    from .api import init as init_api, join_all_public_channels, bot_client

    print("Initializing Slack...")
    init_api()
    print("Joining public channels...")
    join_all_public_channels()
    print("Initializing DM cursors...")
    init_dm_cursors()
    print("Loading history...")
    load()
    print("Catching up on missed messages...")
    do_catchup()

    # Socket Mode
    app = App(token=os.environ['SLACK_BOT_TOKEN'])

    @app.event("message")
    def _on_message(event, **_):
        on_message(event)

    @app.event("channel_created")
    def _on_channel_created(event, **_):
        cid = event['channel']['id']
        name = event['channel']['name']
        try:
            bot_client.conversations_join(channel=cid)
            print(f"Auto-joined #{name}")
        except Exception as e:
            print(f"Could not auto-join #{name}: {e}")

    handler = SocketModeHandler(app, os.environ['SLACK_APP_TOKEN'])
    threading.Thread(target=handler.start, daemon=True).start()
    print("Socket Mode connected")

    # DM polling
    def dm_poll_loop():
        while True:
            try:
                poll_dms()
            except Exception as e:
                print(f"DM poll error: {e}")
            time.sleep(DM_POLL_INTERVAL)

    threading.Thread(target=dm_poll_loop, daemon=True).start()


def list_channels(agent) -> list[str]:
    """Return channel IDs that the given agent is in (queries Slack API)."""
    from .api import bot_client
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
        from .store import channels
        return list(channels.keys())

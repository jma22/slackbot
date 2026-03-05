"""Entry point: wires Socket Mode, DM polling, and agent session."""

import asyncio
import os
import signal
import sys
import time
import threading
from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

load_dotenv()

from .slack import init as init_slack, join_all_public_channels
from . import history
from .messages import (
    catchup, init_dm_cursors, init_notify, poll_dms,
    drain_new, on_message, wait_for_new,
)
from .bot import NAME, ROLE, init as init_agent, respond, has_session, reset_session

DM_POLL_INTERVAL = 5
BATCH_DELAY = 1  # seconds to wait for more messages before responding

app = App(token=os.environ['SLACK_BOT_TOKEN'])


@app.event("channel_created")
def on_channel_created(event, **_):
    cid = event['channel']['id']
    name = event['channel']['name']
    try:
        app.client.conversations_join(channel=cid)
        print(f"Auto-joined #{name}")
    except Exception as e:
        print(f"Could not auto-join #{name}: {e}")


@app.event("message")
def on_message_event(event, **_):
    on_message(event)


async def main():
    if "--reset" in sys.argv:
        reset_session()

    print(f"Agent: {NAME} ({ROLE})")
    init_slack()
    print("Joining public channels...")
    join_all_public_channels()
    print("Initializing DM cursors...")
    init_dm_cursors()
    print("Loading history...")
    history.load()
    print("Catching up on missed messages...")
    catchup()

    handler = SocketModeHandler(app, os.environ['SLACK_APP_TOKEN'])
    threading.Thread(target=handler.start, daemon=True).start()
    print("Socket Mode connected")

    # Bind message notification to this event loop
    init_notify(asyncio.get_running_loop())

    print("Initializing agent session...")
    await init_agent(None if has_session() else history.render())
    print(f"Agent ready\n")

    shutting_down = False

    def request_shutdown(*_):
        nonlocal shutting_down
        if shutting_down:
            print("\nForce quit")
            sys.exit(1)
        shutting_down = True
        print("\nShutting down after current cycle...")

    signal.signal(signal.SIGINT, request_shutdown)
    signal.signal(signal.SIGTERM, request_shutdown)

    # DM polling in background (Socket Mode doesn't cover user DMs)
    def dm_poll_loop():
        while not shutting_down:
            try:
                poll_dms()
            except Exception as e:
                print(f"DM poll error: {e}")
            time.sleep(DM_POLL_INTERVAL)

    threading.Thread(target=dm_poll_loop, daemon=True).start()

    # Main loop: wakes instantly when any thread enqueues a message
    while not shutting_down:
        await wait_for_new()

        # Brief pause to batch rapid-fire messages
        await asyncio.sleep(BATCH_DELAY)

        new_text = drain_new()
        if not new_text:
            continue

        print(f"[{time.strftime('%H:%M:%S')}] New messages")
        try:
            await respond(new_text)
        except Exception as e:
            print(f"Agent error: {e}")
        print()

    print("Goodbye")


asyncio.run(main())

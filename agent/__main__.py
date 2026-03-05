"""Server: owns History and Agents, runs the main event loop."""

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
from . import bot

DM_POLL_INTERVAL = 5

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
    history.on_message(event)


async def main():
    if "--reset" in sys.argv:
        bot.reset_session()

    agents = [bot]

    print(f"Agent: {bot.NAME} ({bot.ROLE})")
    init_slack()
    print("Joining public channels...")
    join_all_public_channels()
    print("Initializing DM cursors...")
    history.init_dm_cursors()
    print("Loading history...")
    history.load()
    print("Catching up on missed messages...")
    history.do_catchup()

    handler = SocketModeHandler(app, os.environ['SLACK_APP_TOKEN'])
    threading.Thread(target=handler.start, daemon=True).start()
    print("Socket Mode connected")

    # Bind message notification to this event loop
    history.init_notify(asyncio.get_running_loop())

    print("Initializing agent session...")
    await bot.init(None if bot.has_session() else history.render())
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

    # DM polling in background
    def dm_poll_loop():
        while not shutting_down:
            try:
                history.poll_dms()
            except Exception as e:
                print(f"DM poll error: {e}")
            time.sleep(DM_POLL_INTERVAL)

    threading.Thread(target=dm_poll_loop, daemon=True).start()

    # Main loop
    while not shutting_down:
        channels_with_new = await history.on_new_msg()
        if not channels_with_new:
            continue

        print(f"[{time.strftime('%H:%M:%S')}] New messages in {len(channels_with_new)} channel(s)")

        for agent in agents:
            agent_channels = set(history.list_channels(agent))
            for ch in channels_with_new:
                if ch in agent_channels:
                    try:
                        await agent.new_message(ch)
                    except Exception as e:
                        print(f"Agent error in {ch}: {e}")
        print()

    print("Goodbye")


asyncio.run(main())

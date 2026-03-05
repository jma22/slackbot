"""Main entry point: starts slack, initializes agents, runs the event loop."""

import asyncio
import signal
import sys
import time
from dotenv import load_dotenv

load_dotenv()

from . import slack
from . import bot


async def main():
    if "--reset" in sys.argv:
        bot.reset_session()

    agents = [bot]

    print(f"Agent: {bot.NAME} ({bot.ROLE})")
    slack.start()

    print("Initializing agent session...")
    await bot.init(None if bot.has_session() else slack.render())
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

    while not shutting_down:
        channels_with_new = await slack.on_new_msg()
        if not channels_with_new:
            continue

        print(f"[{time.strftime('%H:%M:%S')}] New messages in {len(channels_with_new)} channel(s)")

        for agent in agents:
            agent_channels = set(slack.list_channels(agent))
            for ch in channels_with_new:
                if ch in agent_channels:
                    try:
                        await agent.new_message(ch)
                    except Exception as e:
                        print(f"Agent error in {ch}: {e}")
        print()

    print("Goodbye")


asyncio.run(main())

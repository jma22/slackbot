import time
import anthropic
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from pathlib import Path
from dotenv import load_dotenv
import os
import sys

load_dotenv(dotenv_path=Path('.') / '.env')
sys.stdout.reconfigure(encoding='utf-8', line_buffering=True)

# ─────────────────────────────────────────────
# CONFIG — fill these in
# ───────────────────────────────────────────
SLACK_USER_TOKEN = os.getenv("SLACK_USER_TOKEN")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
# CHANNELS_TO_WATCH = ["C1234567890"]  # Add specific channel IDs to watch (leave empty [] to skip)
CHANNELS_TO_WATCH = []  # Watch no channels, only DMs
POLL_INTERVAL = 5  # seconds between checks

# ─────────────────────────────────────────────
# SETUP
# ─────────────────────────────────────────────
slack = WebClient(token=SLACK_USER_TOKEN)
ai = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# Get your own user ID so we don't reply to ourselves
MY_USER_ID = slack.auth_test()["user_id"]
print(f"Logged in as user: {MY_USER_ID}")

# Track the last seen timestamp per conversation to avoid re-processing old messages
last_seen = {}


def get_ai_reply(message_text: str, context: str = "") -> str:
    """Send message to Claude and get a casual, friendly reply."""
    prompt = f"""You are replying on behalf of a person on Slack. 
Keep replies casual, friendly, and concise — like a real person texting.
Don't be overly formal. Use natural language. Keep it short (1-3 sentences max).

{f'Context from the conversation: {context}' if context else ''}

Message to reply to: {message_text}

Write just the reply, nothing else."""

    response = ai.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=300,
        messages=[{"role": "user", "content": prompt}]
    )
    return response.content[0].text.strip()


def handle_new_message(channel_id: str, message: dict, channel_type: str):
    """Process a new message and send an AI reply."""
    user = message.get("user", "")
    text = message.get("text", "")
    ts = message.get("ts", "")

    # Skip our own messages
    if user == MY_USER_ID:
        return

    # Skip bot messages
    # if message.get("bot_id") or message.get("subtype"):
    #     return

    print(f"\nNew message in {channel_type} ({channel_id}): {text}")

    # Fetch a bit of conversation history for context
    try:
        history = slack.conversations_history(channel=channel_id, limit=5, latest=ts)
        context_msgs = [
            m["text"] for m in reversed(history["messages"])
            if m.get("text") and m.get("ts") != ts
        ]
        context = " | ".join(context_msgs[-3:])  # last 3 messages as context
    except Exception:
        context = ""

    # Generate reply
    reply = get_ai_reply(text, context)
    print(f"Replying: {reply}")

    # Send reply (in thread if it's a channel message)
    try:
        if channel_type == "channel":
            slack.chat_postMessage(
                channel=channel_id,
                text=reply,
                thread_ts=ts  # reply in thread to avoid spamming the channel
            )
        else:
            slack.chat_postMessage(
                channel=channel_id,
                text=reply
            )
        print("Reply sent!")
    except SlackApiError as e:
        print(f"Failed to send reply: {e.response['error']}")


def get_dm_channels() -> list:
    """Get all open DM channel IDs."""
    try:
        result = slack.conversations_list(types="im", limit=100)
        return [c["id"] for c in result["channels"] if not c.get("is_user_deleted")]
    except SlackApiError as e:
        print(f"Could not fetch DMs: {e.response['error']}")
        return []


def poll_conversations(channel_ids: list, channel_type: str):
    """Check for new messages in a list of channels."""
    for channel_id in channel_ids:
        try:
            oldest = last_seen.get(channel_id)
            kwargs = {"channel": channel_id, "limit": 10}
            if oldest:
                kwargs["oldest"] = oldest

            result = slack.conversations_history(**kwargs)
            messages = result.get("messages", [])

            # Process oldest first
            for msg in reversed(messages):
                ts = msg.get("ts", "")
                if ts == oldest:
                    continue  # skip the boundary message we already saw
                handle_new_message(channel_id, msg, channel_type)

            if messages:
                last_seen[channel_id] = messages[0]["ts"]  # most recent ts

        except SlackApiError as e:
            print(e.response)
            print(f"Error polling {channel_id}: {e.response['error']}")


# ─────────────────────────────────────────────
# MAIN LOOP
# ─────────────────────────────────────────────
print(f"\nStarting Slack auto-reply...")
print(f"   Watching DMs: Yes")
print(f"   Watching channels: {CHANNELS_TO_WATCH if CHANNELS_TO_WATCH else 'None'}")
print(f"   Poll interval: every {POLL_INTERVAL}s")
print(f"   Tone: Casual & friendly\n")

# Seed last_seen with current latest timestamps so we only reply to NEW messages
dm_channels = get_dm_channels()
all_channels = dm_channels + CHANNELS_TO_WATCH

for ch in all_channels:
    try:
        result = slack.conversations_history(channel=ch, limit=1)
        msgs = result.get("messages", [])
        if msgs:
            last_seen[ch] = msgs[0]["ts"]
    except Exception:
        pass

print(f"Monitoring {len(dm_channels)} DMs and {len(CHANNELS_TO_WATCH)} channels...\n")

while True:
    dm_channels = get_dm_channels()  # refresh DM list in case new ones open
    poll_conversations(dm_channels, "dm")
    poll_conversations(CHANNELS_TO_WATCH, "channel")
    time.sleep(POLL_INTERVAL)
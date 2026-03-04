from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from pathlib import Path
from dotenv import load_dotenv
import anthropic
import os

load_dotenv(dotenv_path=Path('.') / '.env')

app = App(token=os.environ['SLACK_BOT_TOKEN'])
claude = anthropic.Anthropic()

# channel_id -> list of {"role": ..., "content": ...} messages for Claude context
history = {}
# user_id -> display name cache
user_names = {}


def get_user_name(user_id):
    """Get a user's display name, caching the result."""
    if user_id not in user_names:
        try:
            result = app.client.users_info(user=user_id)
            profile = result['user']['profile']
            user_names[user_id] = profile.get('display_name') or profile.get('real_name') or user_id
        except Exception:
            user_names[user_id] = user_id
    return user_names[user_id]


def load_all_history():
    """Load message history from all channels/DMs the bot is in."""
    bot_user_id = app.client.auth_test()['user_id']
    cursor = None
    while True:
        result = app.client.users_conversations(
            types="public_channel,private_channel,mpim,im",
            cursor=cursor,
            limit=200,
        )
        for channel in result['channels']:
            ch_id = channel['id']
            load_channel_history(ch_id, bot_user_id)
        cursor = result.get('response_metadata', {}).get('next_cursor')
        if not cursor:
            break
    print(f"Loaded history for {len(history)} conversations")


def load_channel_history(channel_id, bot_user_id):
    """Load recent messages from a single channel into the history dict."""
    msgs = []
    try:
        result = app.client.conversations_history(channel=channel_id, limit=50)
    except Exception as e:
        print(f"Could not load history for {channel_id}: {e}")
        return
    # messages come newest-first, reverse for chronological order
    for msg in reversed(result.get('messages', [])):
        if msg.get('subtype'):
            continue
        text = msg.get('text', '')
        if not text:
            continue
        user_id = msg.get('user', '')
        is_bot = user_id == bot_user_id
        role = "assistant" if is_bot else "user"
        content = text if is_bot else f"{get_user_name(user_id)}: {text}"
        # Merge consecutive same-role messages
        if msgs and msgs[-1]['role'] == role:
            msgs[-1]['content'] += "\n" + content
        else:
            msgs.append({"role": role, "content": content})
    if msgs:
        history[channel_id] = msgs


@app.event("message")
def reply_to_message(message, say):
    if message.get('subtype') == 'bot_message' or message.get('bot_id'):
        return
    ch_id = message['channel']
    user_text = message['text']
    user_name = get_user_name(message.get('user', ''))
    print(f"{user_name}: {user_text}")

    if ch_id not in history:
        history[ch_id] = []

    history[ch_id].append({"role": "user", "content": f"{user_name}: {user_text}"})

    response = claude.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1024,
        messages=history[ch_id],
    )
    reply = response.content[0].text
    print(f"Claude: {reply}")

    history[ch_id].append({"role": "assistant", "content": reply})
    say(reply)


if __name__ == "__main__":
    load_all_history()
    print(history)
    app.client.chat_postMessage(channel='#general', text='Bot is online!')
    SocketModeHandler(app, os.environ['SLACK_APP_TOKEN']).start()

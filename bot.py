from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from pathlib import Path
from dotenv import load_dotenv
import anthropic
import sys
import os
from pydantic import BaseModel


sys.stdout.reconfigure(line_buffering=True)

load_dotenv(dotenv_path=Path('.') / '.env')

app = App(token=os.environ['SLACK_BOT_TOKEN'])
claude = anthropic.Anthropic()

# channel_name -> list of {"role": ..., "content": ...} messages for Claude context
history = {}
# user_id -> display name cache
user_names = {}
# channel_id -> channel name cache
channel_names = {}


def get_user_name(user_id):
    """Get a user's display name, caching the result."""
    if user_id not in user_names:
        try:
            result = app.client.users_info(user=user_id)
            profile = result['user']['profile']
            name = (
                profile.get('display_name_normalized')
                or profile.get('real_name_normalized')
                or profile.get('real_name')
                or user_id
            )
            user_names[user_id] = name
            print(f"Resolved user {user_id} -> {name}")
        except Exception as e:
            print(f"Failed to resolve user {user_id}: {e}")
            user_names[user_id] = user_id
    return user_names[user_id]


def get_channel_name(channel_id):
    """Get a channel/DM name, caching the result."""
    if channel_id not in channel_names:
        try:
            result = app.client.conversations_info(channel=channel_id)
            ch = result['channel']
            if ch.get('is_im'):
                # For DMs, use the other user's name
                channel_names[channel_id] = f"DM-{get_user_name(ch['user'])}"
            else:
                channel_names[channel_id] = ch.get('name', channel_id)
        except Exception:
            channel_names[channel_id] = channel_id
    return channel_names[channel_id]


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
            # Cache channel name from the listing
            ch = channel
            if ch.get('is_im'):
                channel_names[ch_id] = f"DM-{get_user_name(ch['user'])}"
            else:
                channel_names[ch_id] = ch.get('name', ch_id)
            load_channel_history(ch_id, bot_user_id)
        cursor = result.get('response_metadata', {}).get('next_cursor')
        if not cursor:
            break
    print(f"Loaded history for {len(history)} conversations")


def load_channel_history(channel_id, bot_user_id):
    """Load recent messages from a single channel into the history dict."""
    ch_name = get_channel_name(channel_id)
    msgs = []
    try:
        result = app.client.conversations_history(channel=channel_id, limit=50)
    except Exception as e:
        print(f"Could not load history for {ch_name}: {e}")
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
        history[ch_name] = msgs


def history_to_string():
    lines = []
    for ch_name, msgs in history.items():
        lines.append(f"--- {ch_name} ---")
        for msg in msgs:
            speaker = "weewoo" if msg['role'] == "assistant" else msg['content'].split(":")[0]
            text = msg['content'] if msg['role'] == "assistant" else ":".join(msg['content'].split(":")[1:]).strip()
            lines.append(f"{speaker}: {text}")
        lines.append("")
    return "\n".join(lines)


class ReplyDecision(BaseModel):
    need_reply: bool
    reason: str
    channel_to_reply: str = None
    reply_content: str = None

class ReplyRequest(BaseModel):
    replies: list[ReplyDecision]


def get_channel_id(channel_name):
    """Reverse lookup: channel name -> channel ID."""
    for ch_id, ch_name in channel_names.items():
        if ch_name == channel_name:
            return ch_id
    return None


def check_if_need_reply():
    history_string = history_to_string()
    prompt = f"""You are a Slack bot named weewoo. Given the conversation history below, determine if you need to reply in any channel.

You should reply if:
- Someone directly asked you a question or mentioned you
- A conversation is directed at you or waiting for your response
- The last message in a channel is from a user (not you) and seems to expect a response

You should NOT reply if:
- You already replied and no new user messages came after
- The conversation doesn't involve you
- Messages are between other users and don't need your input

Conversation history:
{history_string}

Return a list of replies you need to send. For each, include the exact channel name from the history and your reply content."""

    print(prompt)
    response = claude.messages.parse(
        model="claude-sonnet-4-20250514",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
        response_model=ReplyRequest,
    )
    print(response.replies)

    for decision in response.replies:
        if not decision.need_reply:
            continue
        ch_id = get_channel_id(decision.channel_to_reply)
        if not ch_id:
            print(f"Could not find channel: {decision.channel_to_reply}")
            continue
        print(f"[{decision.channel_to_reply}] weewoo: {decision.reply_content}")
        app.client.chat_postMessage(channel=ch_id, text=decision.reply_content)
        # Update history
        ch_name = decision.channel_to_reply
        if ch_name not in history:
            history[ch_name] = []
        history[ch_name].append({"role": "assistant", "content": decision.reply_content})



@app.event("message")
def reply_to_message(message, say):
    if message.get('subtype') == 'bot_message' or message.get('bot_id'):
        return
    ch_name = get_channel_name(message['channel'])
    user_text = message['text']
    user_name = get_user_name(message.get('user', ''))
    print(f"[{ch_name}] {user_name}: {user_text}")

    if ch_name not in history:
        history[ch_name] = []

    history[ch_name].append({"role": "user", "content": f"{user_name}: {user_text}"})

    response = claude.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1024,
        messages=history[ch_name],
    )
    reply = response.content[0].text
    print(f"[{ch_name}] Claude: {reply}")

    history[ch_name].append({"role": "assistant", "content": reply})
    say(reply)

if __name__ == "__main__":
    load_all_history()
    print(history_to_string())
    
    # app.client.chat_postMessage(channel='#general', text='Bot is online!')
    SocketModeHandler(app, os.environ['SLACK_APP_TOKEN']).start()

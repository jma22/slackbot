from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from pathlib import Path
from dotenv import load_dotenv
import anthropic
import json
import sys
import os
import time
from pydantic import BaseModel
from typing import List


sys.stdout.reconfigure(encoding='utf-8', line_buffering=True)

load_dotenv(dotenv_path=Path('.') / '.env')

app = App(token=os.environ['SLACK_BOT_TOKEN'])
claude = anthropic.Anthropic()

class HistoryObject(BaseModel):
    role: str
    content: str
    user_id: str = None
    channel_id: str = None
    is_new: bool = False



# channel_name -> list of {"role": ..., "content": ...} messages for Claude context
history : dict[str, List[HistoryObject]] = {}
# user_id -> display name cache
user_names = {}
# channel_id -> channel name cache
channel_names = {}
PERSONAS_FILE = Path('personas.json')

# persona_name -> {"name": ..., "role": ...}
def load_personas():
    if PERSONAS_FILE.exists():
        return json.loads(PERSONAS_FILE.read_text())
    return {}

def save_personas():
    PERSONAS_FILE.write_text(json.dumps(personas, indent=2))

personas = load_personas()
print(personas)



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
    msgs: List[HistoryObject] = []
    try:
        result = app.client.conversations_history(channel=channel_id, limit=50)
    except Exception as e:
        print(f"Could not load history for {ch_name}: {e}")
        return
    # messages come newest-first, reverse for chronological order
    for msg in reversed(result.get('messages', [])):
        subtype = msg.get('subtype')
        if subtype and subtype != 'bot_message':
            continue
        text = msg.get('text', '')
        if not text:
            continue
        user_id = msg.get('user', '')
        is_bot = user_id == bot_user_id or msg.get('bot_id')
        role = "assistant" if is_bot else "user"
        if is_bot:
            bot_name = msg.get('username', 'bot')
            content = f"{bot_name}: {text}"
        else:
            content = f"{get_user_name(user_id)}: {text}"
        # Merge consecutive same-role messages
        if msgs and msgs[-1].role == role:
            msgs[-1].content += "\n" + content
        else:
            msgs.append(HistoryObject(role=role, content=content, user_id=user_id, channel_id=channel_id, is_new=False))
    if msgs:
        history[ch_name] = msgs


def history_to_string():
    lines = []
    for ch_name, msgs in history.items():
        lines.append(f"--- {ch_name} ---")
        for msg in msgs:
            prefix = "[NEW] " if msg.is_new else ""
            lines.append(f"{prefix}{msg.content}")
        lines.append("")
    return "\n".join(lines)



@app.command("/persona")
def create_persona(ack, respond, command):
    ack()
    parts = command['text'].strip().split(None, 1)
    if len(parts) < 2:
        respond("Usage: `/persona <name> <role>` — e.g. `/persona Alex software engineer`")
        return
    p_name, p_role = parts
    personas[p_name] = {"name": p_name, "role": p_role}
    save_personas()
    print(f"Created persona: {p_name} ({p_role})")
    respond(f"Created persona *{p_name}* — {p_role}.")
    
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


def check_if_need_reply(persona):
    print("Checking if need to reply for persona:", persona['name'])
    history_string = history_to_string()
    prompt = f"""you are {persona['name']}, a {persona['role']} at a company, chatting in slack with coworkers.
your vibe: casual, real, like a normal person texting at work. you have opinions and you share them.

style rules — follow these strictly:
- all lowercase, always
- short. like 1-2 sentences max. no essays
- no punctuation at the end of messages
- use filler words naturally: lol, lmk, tbh, ngl, imo, rn, omg, yeah, yep, nah, fr, ok, gotcha, makes sense, for sure, true, fair
- contractions always (don't, can't, it's)
- never sound like an assistant or ai. no bullet points, no headers, no 'great question'

messages marked with [NEW] are new and unread. only respond to [NEW] messages.

reply if:
- a [NEW] message is asking something relevant to your work as a {persona['role']} or something you'd naturally have a take on
- a [NEW] message is directed at you or waiting for your response

do NOT reply if:
- there are no [NEW] messages in a channel
- you already replied and no new messages came after
- the [NEW] messages have nothing to do with you or your expertise
- the [NEW] messages are between other users and don't need your input

conversation history:
{history_string}

return a list of replies to send. only reply in channels where [NEW] messages exist and it makes sense for a {persona['role']} to chime in."""

    print(prompt)
    response = claude.messages.parse(
        model="claude-opus-4-6",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
        output_format=ReplyRequest,
    )

    parsed = response.content[0].parsed_output
    print(f"{persona['name']} response: {parsed}")

    logs_dir = Path('logs')
    logs_dir.mkdir(exist_ok=True)
    log_path = logs_dir / f"{persona['name']}_{int(time.time())}.log"
    log_path.write_text(f"=== PROMPT ===\n{prompt}\n\n=== RESPONSE ===\n{parsed}\n")

    for reply in parsed.replies:
        if reply.need_reply and reply.channel_to_reply and reply.reply_content:
            ch_id = get_channel_id(reply.channel_to_reply)
            if ch_id:
                app.client.chat_postMessage(
                    channel=ch_id,
                    text=reply.reply_content,
                    username=persona['name'],
                    icon_emoji=persona.get('icon_emoji', ':bust_in_silhouette:'),
                )
                update_history(reply.channel_to_reply, "assistant", f"{persona['name']}: {reply.reply_content}", is_new=False)
            else:
                print(f"Could not find channel ID for {reply.channel_to_reply}")




def update_history(ch_name, role, content, is_new=True):
    if ch_name not in history:
        history[ch_name] = []
    history[ch_name].append(HistoryObject(role=role, content=content, is_new=is_new))


@app.event("message")
def reply_to_message(message):
    # Capture bot messages in history but don't trigger replies
    if message.get('subtype') == 'bot_message' or message.get('bot_id'):
        ch_name = get_channel_name(message['channel'])
        bot_name = message.get('username', 'bot')
        text = message.get('text', '')
        if text:
            update_history(ch_name, "assistant", f"{bot_name}: {text}", is_new=False)
        return
    ch_name = get_channel_name(message['channel'])
    user_text = message['text']
    user_name = get_user_name(message.get('user', ''))
    print(f"[{ch_name}] {user_name}: {user_text}")

    update_history(ch_name, "user", f"{user_name}: {user_text}")
    for persona in personas.values():
        check_if_need_reply(persona)
    # Mark all messages as no longer new
    for msgs in history.values():
        for msg in msgs:
            msg.is_new = False

    

if __name__ == "__main__":
    load_all_history()
    print(history_to_string())
    SocketModeHandler(app, os.environ['SLACK_APP_TOKEN']).start()

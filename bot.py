from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from pathlib import Path
from dotenv import load_dotenv
import anthropic
import json
import sys
import os

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
            speaker = "Bot" if msg['role'] == "assistant" else msg['content'].split(":")[0]
            text = msg['content'] if msg['role'] == "assistant" else ":".join(msg['content'].split(":")[1:]).strip()
            lines.append(f"{speaker}: {text}")
        lines.append("")
    return "\n".join(lines)


def persona_reply(persona, messages):
    """Ask a persona to respond. Returns reply text, or None if it chose not to respond."""
    system = (
        f"you are {persona['name']}, a {persona['role']} at a company, chatting in slack with coworkers.\n"
        f"your vibe: casual, real, like a normal person texting at work. you have opinions and you share them.\n"
        f"style rules — follow these strictly:\n"
        f"- all lowercase, always\n"
        f"- short. like 1-2 sentences max. no essays\n"
        f"- no punctuation at the end of messages\n"
        f"- use filler words naturally: lol, lmk, tbh, ngl, imo, rn, omg, yeah, yep, nah, fr, ok, gotcha, makes sense, for sure, true, fair\n"
        f"- contractions always (don't, can't, it's)\n"
        f"- never sound like an assistant or ai. no bullet points, no headers, no 'great question'\n"
        f"only chime in if the message is relevant to your work as a {persona['role']} or if someone's asking something you'd naturally have a take on.\n"
        f"if it's not relevant to you at all, reply with exactly: NO_RESPONSE"
    )
    response = claude.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1024,
        system=system,
        messages=messages,
    )
    text = response.content[0].text.strip()
    return None if text == "NO_RESPONSE" else text


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

    print(f"personas: {list(personas.keys())}")
    for persona in personas.values():
        try:
            reply = persona_reply(persona, history[ch_name])
        except Exception as e:
            print(f"Error from persona {persona['name']}: {e}")
            continue
        print(f"[{ch_name}] persona_reply {persona['name']} -> {reply!r}")
        if reply:
            print(f"[{ch_name}] {persona['name']}: {reply}")
            history[ch_name].append({"role": "assistant", "content": reply})
            app.client.chat_postMessage(
                channel=message['channel'],
                text=reply,
                username=persona['name'],
                icon_emoji=persona.get('icon_emoji', ':bust_in_silhouette:'),
            )

if __name__ == "__main__":
    load_all_history()
    app.client.chat_postMessage(channel='#general', text='Bot is online!')
    SocketModeHandler(app, os.environ['SLACK_APP_TOKEN']).start()

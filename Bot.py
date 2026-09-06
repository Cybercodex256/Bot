import os
import time
import random
import threading
from collections import deque
from flask import Flask, jsonify
from neonize.client import NewClient
from neonize.events import MessageEv
from openai import OpenAI

# 1. Initialize Flask App for Render Keep-Alive
app = Flask(__name__)

@app.route('/ping', methods=['GET'])
def ping():
    """Endpoint that you will ping with your cron job."""
    return jsonify({"status": "healthy", "timestamp": time.time()}), 200

@app.route('/', methods=['GET'])
def home():
    """Default route to check if the app layer is up."""
    return "WhatsApp Bot & Flask Server are running!", 200

def run_flask():
    """Runs the Flask server. Render automatically assigns a PORT variable."""
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, use_reloader=False)


# 2. Initialize OpenAI client & Neonize Client
openai_client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY", "YOUR_API_KEY_HERE"))
client = NewClient("session.db")

# 3. In-Memory Stores
BOT_STATUS = {}
CHAT_MEMORY = {}

def load_ai_instructions(filename="instructions.md") -> str:
    """Reads the custom markdown instruction file safely from the disk."""
    try:
        with open(filename, "r", encoding="utf-8") as file:
            return file.read()
    except FileNotFoundError:
        print(f"Warning: '{filename}' not found. Falling back to default prompt.")
        return "You are a casual and helpful personal assistant running inside WhatsApp."

SYSTEM_INSTRUCTIONS = load_ai_instructions()


def get_llm_response(sender_id: str, new_user_message: str) -> str:
    """Appends the new message to memory, queries the LLM with markdown context, and stores the answer."""
    if sender_id not in CHAT_MEMORY:
        CHAT_MEMORY[sender_id] = deque(maxlen=10)
        
    CHAT_MEMORY[sender_id].append({"role": "user", "content": new_user_message})
    
    system_prompt = {
        "role": "system", 
        "content": SYSTEM_INSTRUCTIONS
    }
    messages_payload = [system_prompt] + list(CHAT_MEMORY[sender_id])
    
    try:
        response = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages_payload,
            max_tokens=250
        )
        bot_reply = response.choices.message.content.strip()
        CHAT_MEMORY[sender_id].append({"role": "assistant", "content": bot_reply})
        return bot_reply
        
    except Exception as e:
        print(f"LLM Processing Error: {e}")
        return "Sorry, my brain stumbled. Try messaging me again in a moment! 🤖"


# Register the event decorator correctly with Neonize
@client.event(MessageEv)
def on_message(client: NewClient, event: MessageEv):
    """Event listener that intercepts every incoming WhatsApp notification."""
    text_message = event.Message.conversation or event.Message.extendedTextMessage.text
    if not text_message:
        return
        
    sender_id = event.Info.Sender.String()
    is_from_me = event.Info.IsFromMe
    clean_msg = text_message.strip().lower()

    # --- COMMANDS ---
    if "!bot off" in clean_msg:
        BOT_STATUS[sender_id] = False
        if sender_id in CHAT_MEMORY:
            CHAT_MEMORY[sender_id].clear()
        client.reply_message(event, "🤖 Assistant paused.")
        return
        
    if "!bot on" in clean_msg:
        BOT_STATUS[sender_id] = True
        client.reply_message(event, "🤖 Assistant activated!")
        return

    if is_from_me and "!bot reload" in clean_msg:
        global SYSTEM_INSTRUCTIONS
        SYSTEM_INSTRUCTIONS = load_ai_instructions()
        client.reply_message(event, "🔄 Successfully reloaded 'instructions.md' changes live!")
        return

    if sender_id not in BOT_STATUS:
        BOT_STATUS[sender_id] = True

    # --- AUTO-RESPONSE ---
    if BOT_STATUS[sender_id] and not is_from_me:
        print(f"Processing chat from {sender_id}: {text_message}")
        bot_reply = get_llm_response(sender_id, text_message)
        time.sleep(random.randint(2, 5))
        client.reply_message(event, bot_reply)


def main():
    # 1. Start the Flask server on a background thread
    print("Starting background Flask server for Render keep-alive...")
    flask_thread = threading.Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()

    # 2. Start the core WhatsApp Client connection
    print("Launching Python WhatsApp Bot engine...")
    client.connect()
    print("Tracking this shit")


if __name__ == "__main__":
    main()


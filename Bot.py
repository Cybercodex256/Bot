import os
import time
import random
from collections import deque
from neonize.client import NewClient
from neonize.events import MessageEvent
from openai import OpenAI

# 1. Initialize OpenAI client
# Ensure your environment variable is set: export OPENAI_API_KEY="your-key"
openai_client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY", "YOUR_API_KEY_HERE"))

# 2. In-Memory Stores
# Tracks whether the bot is enabled/disabled per chat window
BOT_STATUS = {}

# Tracks the rolling chat history per contact (Max 10 messages to save context and tokens)
CHAT_MEMORY = {}


def load_ai_instructions(filename="instructions.md") -> str:
    """Reads the custom markdown instruction file safely from the disk."""
    try:
        with open(filename, "r", encoding="utf-8") as file:
            return file.read()
    except FileNotFoundError:
        print(f"Warning: '{filename}' not found. Falling back to default prompt.")
        return "You are a casual and helpful personal assistant running inside WhatsApp."


# Load the Markdown instructions into a global variable at startup
SYSTEM_INSTRUCTIONS = load_ai_instructions()


def get_llm_response(sender_id: str, new_user_message: str) -> str:
    """Appends the new message to memory, queries the LLM with markdown context, and stores the answer."""
    # Initialize memory deque for this contact if it doesn't exist yet
    if sender_id not in CHAT_MEMORY:
        CHAT_MEMORY[sender_id] = deque(maxlen=10)
        
    # Append the incoming message from your contact to their specific history queue
    CHAT_MEMORY[sender_id].append({"role": "user", "content": new_user_message})
    
    # Construct the full payload combining the system file rules with rolling history
    system_prompt = {
        "role": "system", 
        "content": SYSTEM_INSTRUCTIONS
    }
    messages_payload = [system_prompt] + list(CHAT_MEMORY[sender_id])
    
    try:
        response = openai_client.chat.completions.create(
            model="gpt-4o-mini",  # Highly optimized, fast, and cost-efficient for text chat
            messages=messages_payload,
            max_tokens=250
        )
        
        bot_reply = response.choices.message.content.strip()
        
        # Save the AI's response to memory so it remembers what it said in the next turn
        CHAT_MEMORY[sender_id].append({"role": "assistant", "content": bot_reply})
        return bot_reply
        
    except Exception as e:
        print(f"LLM Processing Error: {e}")
        return "Sorry, my brain stumbled. Try messaging me again in a moment! 🤖"


def on_message(client: NewClient, event: MessageEvent):
    """Event listener that intercepts every incoming WhatsApp notification."""
    # Extract message safely, bypassing empty media triggers (images/audio/stickers)
    text_message = event.Message.conversation or event.Message.extendedTextMessage.text
    if not text_message:
        return
        
    sender_id = event.Info.Sender.String()
    is_from_me = event.Info.IsFromMe
    clean_msg = text_message.strip().lower()

    # --- COMMAND 1: TURN BOT OFF ---
    if "!bot off" in clean_msg:
        BOT_STATUS[sender_id] = False
        if sender_id in CHAT_MEMORY:
            CHAT_MEMORY[sender_id].clear()  # Clear context to start clean next time
        client.reply_message(event, "🤖 Assistant paused. I will remain quiet until you type !bot on.")
        return
        
    # --- COMMAND 2: TURN BOT ON ---
    if "!bot on" in clean_msg:
        BOT_STATUS[sender_id] = True
        client.reply_message(event, "🤖 Assistant activated! I will now manage incoming chats using context memory.")
        return

    # --- COMMAND 3: LIVE RELOAD MARKDOWN SETTINGS (Admin Only) ---
    # Only you can trigger this command from your own phone typing
    if is_from_me and "!bot reload" in clean_msg:
        global SYSTEM_INSTRUCTIONS
        SYSTEM_INSTRUCTIONS = load_ai_instructions()
        client.reply_message(event, "🔄 Successfully reloaded 'instructions.md' changes live!")
        return

    # Default State: If a chat has never specified, assume the bot is ON
    if sender_id not in BOT_STATUS:
        BOT_STATUS[sender_id] = True

    # --- AUTO-RESPONSE TRIGGER PIPELINE ---
    # Only execute if the bot is active for this contact, and you didn't write the message yourself
    if BOT_STATUS[sender_id] and not is_from_me:
        print(f"Processing chat from {sender_id}: {text_message}")
        
        # 1. Fetch response from LLM using memory context
        bot_reply = get_llm_response(sender_id, text_message)
        
        # 2. Anti-Ban Humanizing Delay (Waits between 2 to 5 seconds before replying)
        time.sleep(random.randint(2, 5))
        
        # 3. Fire the response packet back into the WhatsApp thread
        client.reply_message(event, bot_reply)


def main():
    # 'session.db' caches credentials locally so you don't scan the QR code on every single restart
    client = NewClient("session.db")
    
    # Bind our incoming message handler function to the client event loop
    client.event_handlers.append(on_message)
    
    print("Launching Python WhatsApp Bot with Markdown context instructions...")
    print("If this is your first run, please scan the QR code generated in the terminal.")
    client.connect()


if __name__ == "__main__":
    main()


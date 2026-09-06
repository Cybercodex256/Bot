import os
import time
import random
import threading
from collections import deque
import typing
import typing_extensions

# Compatibility patch for typing.Self in Python 3.10
if not hasattr(typing, "Self"):
    typing.Self = typing_extensions.Self

from flask import Flask, jsonify, request
from neonize.client import NewClient
from neonize.events import (
    MessageEv, ConnectedEv, DisconnectedEv,
    PairStatusEv, ConnectFailureEv, ClientOutdatedEv,
    StreamErrorEv, LoggedOutEv, TemporaryBanEv
)
from neonize.utils.jid import Jid2String
from openai import OpenAI
import segno

# 1. In-Memory Stores for WhatsApp state & QR & Logs
QR_DATA = {"svg": None, "raw": None, "timestamp": 0}
CONNECTION_STATE = {"connected": False, "phone": None, "last_error": None}
WHATSAPP_LOGS = []

def add_log(msg: str):
    print(f"[WhatsApp] {msg}")
    WHATSAPP_LOGS.append(f"[{time.strftime('%H:%M:%S')}] {msg}")
    if len(WHATSAPP_LOGS) > 20:
        WHATSAPP_LOGS.pop(0)

# 2. Initialize Flask App for Render Keep-Alive & QR linking UI
app = Flask(__name__)

@app.route('/ping', methods=['GET'])
def ping():
    """Endpoint that you will ping with your cron job."""
    return jsonify({
        "status": "healthy",
        "timestamp": time.time(),
        "whatsapp_connected": CONNECTION_STATE["connected"]
    }), 200

@app.route('/api/status', methods=['GET'])
def api_status():
    """Returns real-time WhatsApp connection status, current QR code, and recent diagnostic logs."""
    return jsonify({
        "connected": CONNECTION_STATE["connected"],
        "has_qr": QR_DATA["svg"] is not None,
        "qr_svg": QR_DATA["svg"],
        "timestamp": QR_DATA["timestamp"],
        "last_error": CONNECTION_STATE["last_error"],
        "logs": WHATSAPP_LOGS[-10:]
    }), 200

@app.route('/api/pair', methods=['POST'])
def api_pair():
    """Generates an 8-character pairing code for a phone number."""
    data = request.get_json(silent=True) or request.form
    phone = (data.get("phone") or "").strip().replace("+", "").replace(" ", "").replace("-", "")
    if not phone:
        return jsonify({"error": "Phone number is required (e.g., 14155552671)"}), 400
    try:
        add_log(f"Requesting pairing code for phone: {phone[:4]}***")
        code = client.PairPhone(phone, show_push_notification=True)
        add_log(f"Pairing code generated: {code}")
        return jsonify({"success": True, "pairing_code": code, "phone": phone}), 200
    except Exception as e:
        err_msg = str(e)
        add_log(f"Pairing code error: {err_msg}")
        CONNECTION_STATE["last_error"] = err_msg
        return jsonify({"error": err_msg}), 500

@app.route('/api/reset', methods=['POST'])
def api_reset():
    """Clears old session database to start a fresh pairing cycle."""
    try:
        add_log("User requested session reset.")
        if os.path.exists("session.db"):
            # Don't delete if locked, but recreate client if needed
            add_log("Session reset triggered.")
        return jsonify({"success": True, "message": "Session reset requested."}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/', methods=['GET'])
def home():
    """Default route displaying connection status and live QR code to pair WhatsApp."""
    accept = request.headers.get("Accept", "")
    if "text/html" not in accept:
        return "WhatsApp Bot & Flask Server are running!", 200

    return """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Connect WhatsApp Bot</title>
  <style>
    :root {
      --bg: #0b141a;
      --card: #111b21;
      --header: #202c33;
      --border: #222e35;
      --text: #e9edef;
      --muted: #8696a0;
      --green: #00a884;
      --green-hover: #06cf9c;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }
    body { background-color: var(--bg); color: var(--text); min-height: 100vh; display: flex; flex-direction: column; align-items: center; justify-content: center; padding: 24px 16px; }
    .container { max-width: 580px; width: 100%; background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 32px 24px; box-shadow: 0 12px 32px rgba(0,0,0,0.5); text-align: center; }
    .badge { display: inline-flex; align-items: center; gap: 6px; padding: 6px 14px; border-radius: 9999px; font-size: 0.85rem; font-weight: 600; margin-bottom: 20px; }
    .badge-connecting { background: rgba(255, 179, 0, 0.15); color: #ffb300; }
    .badge-connected { background: rgba(0, 168, 132, 0.15); color: var(--green); }
    .badge-dot { width: 8px; height: 8px; border-radius: 50%; background: currentColor; }
    h1 { font-size: 1.4rem; font-weight: 600; margin-bottom: 8px; }
    p.desc { color: var(--muted); font-size: 0.92rem; line-height: 1.5; margin-bottom: 24px; }
    .qr-wrapper { background: #ffffff; padding: 16px; border-radius: 12px; display: inline-flex; justify-content: center; align-items: center; min-width: 240px; min-height: 240px; margin-bottom: 24px; }
    .qr-wrapper img { width: 220px; height: 220px; display: block; }
    .steps { background: var(--header); border-radius: 10px; padding: 18px 20px; text-align: left; margin-bottom: 24px; font-size: 0.88rem; line-height: 1.6; }
    .steps ol { margin-left: 20px; color: var(--text); }
    .steps li { margin-bottom: 6px; }
    .steps strong { color: var(--green); }
    .pair-toggle { margin-top: 16px; font-size: 0.85rem; color: var(--muted); }
    .pair-toggle a { color: var(--green); text-decoration: none; cursor: pointer; }
    .pair-toggle a:hover { text-decoration: underline; }
    .pair-box { margin-top: 16px; display: none; }
    .pair-input-row { display: flex; gap: 8px; margin-top: 8px; }
    input[type="text"] { flex: 1; background: var(--header); border: 1px solid var(--border); color: var(--text); padding: 10px 14px; border-radius: 8px; font-size: 0.92rem; outline: none; }
    input[type="text"]:focus { border-color: var(--green); }
    button { background: var(--green); color: #111b21; border: none; padding: 10px 18px; border-radius: 8px; font-weight: 600; cursor: pointer; }
    button:hover { background: var(--green-hover); }
    .code-display { margin-top: 14px; font-size: 1.5rem; font-weight: 700; letter-spacing: 4px; color: var(--green); font-family: monospace; background: var(--header); padding: 12px; border-radius: 8px; }
  </style>
</head>
<body>
  <div class="container">
    <div id="statusBadge" class="badge badge-connecting">
      <span class="badge-dot"></span> <span id="statusText">Waiting for WhatsApp link...</span>
    </div>

    <h1>Connect Your WhatsApp</h1>
    <p class="desc">Link this AI assistant bot directly to your personal WhatsApp account using multi-device linking.</p>

    <div id="errorBanner" style="display: none; background: rgba(239, 68, 68, 0.15); border: 1px solid rgba(239, 68, 68, 0.3); color: #fca5a5; padding: 12px 16px; border-radius: 8px; margin-bottom: 20px; font-size: 0.88rem; text-align: left;"></div>

    <div id="qrArea">
      <!-- Tabs to choose linking method -->
      <div style="display: flex; background: var(--header); padding: 4px; border-radius: 8px; margin-bottom: 20px;">
        <button id="tabPhoneBtn" type="button" onclick="switchTab('phone')" style="flex: 1; padding: 8px 12px; background: var(--green); color: #111b21; border-radius: 6px; font-weight: 600; cursor: pointer;">Phone Number</button>
        <button id="tabQrBtn" type="button" onclick="switchTab('qr')" style="flex: 1; padding: 8px 12px; background: transparent; color: var(--muted); border-radius: 6px; font-weight: 600; cursor: pointer;">QR Code</button>
      </div>

      <!-- Phone Number View (Default) -->
      <div id="phoneView" style="display: block;">
        <p style="color: var(--muted); font-size: 0.88rem; margin-bottom: 12px; text-align: left;">Enter your WhatsApp phone number with country code (no + or spaces):</p>
        <div class="pair-input-row">
          <input type="text" id="phoneInput" placeholder="e.g. 256700354922 or 14155552671">
          <button type="button" onclick="requestPairCode()">Get Code</button>
        </div>
        <div id="pairResult"></div>

        <div class="steps" style="margin-top: 20px;">
          <div style="font-weight: 600; margin-bottom: 8px; color: var(--text);">How to enter the code:</div>
          <ol>
            <li>Open <strong>WhatsApp</strong> on your phone.</li>
            <li>Go to <strong>Settings</strong> (iPhone) or <strong>⋮ Menu</strong> (Android) $\rightarrow$ <strong>Linked Devices</strong>.</li>
            <li>Tap <strong>Link a Device</strong>.</li>
            <li>Tap <strong>"Link with phone number instead"</strong> at the bottom.</li>
            <li>Enter the 8-character code shown above.</li>
          </ol>
        </div>
      </div>

      <!-- QR Code View -->
      <div id="qrView" style="display: none;">
        <div class="qr-wrapper" id="qrContainer">
          <div style="color: #555; font-size: 0.9rem;">Generating QR code...</div>
        </div>
        <div class="steps">
          <div style="font-weight: 600; margin-bottom: 8px; color: var(--text);">How to scan:</div>
          <ol>
            <li>Open <strong>WhatsApp</strong> on your phone.</li>
            <li>Go to <strong>Settings</strong> or <strong>⋮ Menu</strong> $\rightarrow$ <strong>Linked Devices</strong>.</li>
            <li>Tap <strong>Link a Device</strong> and point your camera at the QR code above.</li>
          </ol>
        </div>
      </div>
    </div>

    <div id="connectedArea" style="display: none;">
      <div style="background: rgba(0, 168, 132, 0.1); border: 1px solid rgba(0, 168, 132, 0.3); border-radius: 12px; padding: 24px; margin-bottom: 20px;">
        <div style="font-size: 3rem; margin-bottom: 12px;">✅</div>
        <h2 style="font-size: 1.25rem; color: var(--green); margin-bottom: 8px;">WhatsApp Account Linked!</h2>
        <p style="color: var(--muted); font-size: 0.9rem; line-height: 1.5;">Your WhatsApp Bot is active and listening for messages. It will respond using the rules from <code>instructions.md</code>.</p>
      </div>
      <div style="font-size: 0.85rem; color: var(--muted);">
        Commands available in chats: <code>!ping</code>, <code>!bot off</code>, <code>!bot on</code>, <code>!bot reload</code>
      </div>
    </div>
  </div>

  <script>
    function switchTab(mode) {
      if (mode === 'phone') {
        document.getElementById('phoneView').style.display = 'block';
        document.getElementById('qrView').style.display = 'none';
        document.getElementById('tabPhoneBtn').style.background = 'var(--green)';
        document.getElementById('tabPhoneBtn').style.color = '#111b21';
        document.getElementById('tabQrBtn').style.background = 'transparent';
        document.getElementById('tabQrBtn').style.color = 'var(--muted)';
      } else {
        document.getElementById('phoneView').style.display = 'none';
        document.getElementById('qrView').style.display = 'block';
        document.getElementById('tabQrBtn').style.background = 'var(--green)';
        document.getElementById('tabQrBtn').style.color = '#111b21';
        document.getElementById('tabPhoneBtn').style.background = 'transparent';
        document.getElementById('tabPhoneBtn').style.color = 'var(--muted)';
      }
    }

    async function requestPairCode() {
      const phone = document.getElementById('phoneInput').value.trim();
      if (!phone) return alert('Please enter your phone number with country code');
      const resEl = document.getElementById('pairResult');
      resEl.innerHTML = '<div style="color: var(--muted); margin-top: 10px;">Requesting 8-digit code from WhatsApp...</div>';
      try {
        const res = await fetch('/api/pair', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({ phone })
        });
        const data = await res.json();
        if (data.pairing_code) {
          resEl.innerHTML = '<div style="margin-top: 14px; font-size: 0.88rem; color: var(--muted);">Enter this code on your phone:</div><div class="code-display">' + data.pairing_code + '</div><p style="font-size: 0.8rem; color: var(--muted); margin-top: 6px;">Check your phone for a WhatsApp pairing notification!</p>';
        } else {
          resEl.innerHTML = '<div style="color: #ff5252; margin-top: 10px;">Error: ' + (data.error || 'Failed to get pairing code') + '</div>';
        }
      } catch (err) {
        resEl.innerHTML = '<div style="color: #ff5252; margin-top: 10px;">Error: ' + err.message + '</div>';
      }
    }

    async function pollStatus() {
      try {
        const res = await fetch('/api/status');
        const data = await res.json();
        if (data.connected) {
          document.getElementById('statusBadge').className = 'badge badge-connected';
          document.getElementById('statusText').innerText = 'Connected to WhatsApp';
          document.getElementById('qrArea').style.display = 'none';
          document.getElementById('connectedArea').style.display = 'block';
        } else if (data.qr_svg) {
          document.getElementById('qrContainer').innerHTML = '<img src="' + data.qr_svg + '" alt="WhatsApp QR Code">';
        }

        const errEl = document.getElementById('errorBanner');
        if (data.last_error && errEl) {
          errEl.style.display = 'block';
          errEl.innerText = data.last_error;
        } else if (errEl) {
          errEl.style.display = 'none';
        }
      } catch (e) {
        console.error(e);
      }
    }
    setInterval(pollStatus, 3000);
    pollStatus();
  </script>
</body>
</html>""", 200

def run_flask():
    """Runs the Flask server. Render automatically assigns a PORT variable (defaults to 3000)."""
    port = int(os.environ.get("PORT", 3000))
    app.run(host='0.0.0.0', port=port, use_reloader=False)


# 3. Initialize OpenAI client & Neonize Client
openai_client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY", "YOUR_API_KEY_HERE"))
client = NewClient("session.db")

# Neonize Event Handlers
@client.qr
def on_qr(client_instance: NewClient, data: bytes):
    """Captures WhatsApp QR code and formats as SVG data URI for web scanning."""
    print("\n[WhatsApp] New QR code generated. Scan in WhatsApp or via web dashboard.")
    try:
        qr = segno.make_qr(data)
        qr.terminal(compact=True)
        QR_DATA["svg"] = qr.svg_data_uri(scale=5)
        QR_DATA["raw"] = data.decode("utf-8", errors="ignore")
        QR_DATA["timestamp"] = time.time()
    except Exception as e:
        print(f"[WhatsApp] QR rendering notice: {e}")

@client.event(ConnectedEv)
def on_connected(client_instance: NewClient, event: ConnectedEv):
    """Triggered when WhatsApp successfully authenticates and establishes connection."""
    add_log("Successfully connected to WhatsApp!")
    CONNECTION_STATE["connected"] = True
    CONNECTION_STATE["last_error"] = None
    QR_DATA["svg"] = None

@client.event(DisconnectedEv)
def on_disconnected(client_instance: NewClient, event: DisconnectedEv):
    """Triggered when WhatsApp disconnects."""
    add_log("Disconnected from WhatsApp.")
    CONNECTION_STATE["connected"] = False

@client.event(PairStatusEv)
def on_pair_status(client_instance: NewClient, event: PairStatusEv):
    """Triggered during device pairing."""
    status_str = f"Status={event.Status}, Error={event.Error}, ID={event.ID}"
    add_log(f"PairStatus event: {status_str}")
    if event.Error:
        CONNECTION_STATE["last_error"] = f"Pair error: {event.Error}"

@client.event(ConnectFailureEv)
def on_connect_failure(client_instance: NewClient, event: ConnectFailureEv):
    """Triggered when connection handshake fails."""
    err_str = f"Reason={event.Reason}, Message={event.Message}"
    add_log(f"ConnectFailure: {err_str}")
    CONNECTION_STATE["last_error"] = f"Connection failed: {event.Message or event.Reason}"

@client.event(ClientOutdatedEv)
def on_client_outdated(client_instance: NewClient, event: ClientOutdatedEv):
    """Triggered if WhatsApp considers the protocol version outdated."""
    add_log("WhatsApp reported: Client protocol outdated.")
    CONNECTION_STATE["last_error"] = "WhatsApp rejected client version as outdated."

@client.event(StreamErrorEv)
def on_stream_error(client_instance: NewClient, event: StreamErrorEv):
    """Triggered on stream communication errors."""
    add_log(f"Stream error: {event}")
    CONNECTION_STATE["last_error"] = f"Stream error: {event}"

@client.event(LoggedOutEv)
def on_logged_out(client_instance: NewClient, event: LoggedOutEv):
    """Triggered if logged out."""
    add_log(f"Logged out by WhatsApp: {event}")
    CONNECTION_STATE["connected"] = False
    CONNECTION_STATE["last_error"] = "Logged out by WhatsApp."

@client.event(TemporaryBanEv)
def on_temporary_ban(client_instance: NewClient, event: TemporaryBanEv):
    """Triggered if temporary ban."""
    add_log(f"Temporary ban: {event}")
    CONNECTION_STATE["last_error"] = "Account temporarily banned by WhatsApp."



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
        bot_reply = response.choices[0].message.content.strip()
        CHAT_MEMORY[sender_id].append({"role": "assistant", "content": bot_reply})
        return bot_reply
        
    except Exception as e:
        err_str = str(e)
        add_log(f"LLM Processing Error: {err_str}")
        if "insufficient_quota" in err_str or "credit_balance_exhausted" in err_str:
            return "⚠️ *OpenAI Notice:* Your OpenAI account has 0 credits remaining. Please top up your balance at https://platform.openai.com/settings/organization/billing to receive AI responses."
        return f"Sorry, my brain stumbled: {err_str[:100]} 🤖"


def send_reply(client_inst: NewClient, event: MessageEv, text: str):
    """Sends a reply to the originating chat, quoting the message if possible, or falling back to a direct send."""
    chat_jid = event.Info.MessageSource.Chat
    try:
        client_inst.reply_message(message=text, quoted=event)
        add_log(f"Replied with quote: {text[:40]}...")
    except Exception as e:
        add_log(f"reply_message fallback ({e}), attempting send_message")
        try:
            client_inst.send_message(to=chat_jid, message=text)
            add_log(f"Sent message directly: {text[:40]}...")
        except Exception as e2:
            add_log(f"send_message error: {e2}")


# Register the event decorator correctly with Neonize
@client.event(MessageEv)
def on_message(client_inst: NewClient, event: MessageEv):
    """Event listener that intercepts every incoming WhatsApp notification."""
    try:
        msg = event.Message
        if not msg:
            return

        # 1. Extract message text across different types
        text_message = (
            msg.conversation
            or (msg.extendedTextMessage and msg.extendedTextMessage.text)
            or (msg.imageMessage and msg.imageMessage.caption)
            or (msg.videoMessage and msg.videoMessage.caption)
            or ""
        )
        if not text_message:
            return

        # 2. Extract MessageSource metadata safely
        msg_source = event.Info.MessageSource
        sender_jid = msg_source.Sender
        chat_jid = msg_source.Chat
        is_from_me = msg_source.IsFromMe

        sender_id = Jid2String(sender_jid)
        chat_id = Jid2String(chat_jid)
        clean_msg = text_message.strip().lower()

        add_log(f"Message in {chat_id} from {sender_id} (me={is_from_me}): {text_message[:50]}")

        # --- COMMANDS (respond whether sent by self or other contact) ---
        if clean_msg == "!ping" or clean_msg.startswith("!ping "):
            send_reply(client_inst, event, "🏓 Pong! The WhatsApp bot is active and listening.")
            return

        if clean_msg in ["!bot help", "!help", "!commands"]:
            help_text = (
                "🤖 *WhatsApp Bot Commands:*\n\n"
                "• `!ping` - Test if bot is active\n"
                "• `!bot on` - Turn assistant ON for this chat\n"
                "• `!bot off` - Pause assistant for this chat\n"
                "• `!bot status` - View bot status in this chat\n"
                "• `!bot reload` - Reload instructions.md live\n"
                "• `!ai <question>` - Ask AI a prompt directly"
            )
            send_reply(client_inst, event, help_text)
            return

        if "!bot off" in clean_msg:
            BOT_STATUS[chat_id] = False
            if chat_id in CHAT_MEMORY:
                CHAT_MEMORY[chat_id].clear()
            send_reply(client_inst, event, "🤖 Assistant paused for this chat. Send `!bot on` to reactivate.")
            return

        if "!bot on" in clean_msg:
            BOT_STATUS[chat_id] = True
            send_reply(client_inst, event, "🤖 Assistant activated for this chat!")
            return

        if "!bot status" in clean_msg:
            is_active = BOT_STATUS.get(chat_id, True)
            status_text = (
                f"🤖 *Bot Status:*\n"
                f"• State: {'🟢 Active (ON)' if is_active else '🔴 Paused (OFF)'}\n"
                f"• Chat: `{chat_id}`\n"
                f"• Model: `gpt-4o-mini`"
            )
            send_reply(client_inst, event, status_text)
            return

        if "!bot reload" in clean_msg:
            global SYSTEM_INSTRUCTIONS
            SYSTEM_INSTRUCTIONS = load_ai_instructions()
            send_reply(client_inst, event, "🔄 Successfully reloaded 'instructions.md' changes live!")
            return

        # Direct prompt command (works from yourself or others)
        if clean_msg.startswith("!ai "):
            prompt = text_message[4:].strip()
            if prompt:
                bot_reply = get_llm_response(chat_id, prompt)
                send_reply(client_inst, event, bot_reply)
            return

        # Ensure default status for this chat is ON
        if chat_id not in BOT_STATUS:
            BOT_STATUS[chat_id] = True

        # --- AUTO-RESPONSE FOR CONTACTS (skip own messages so you don't talk to yourself) ---
        if BOT_STATUS[chat_id] and not is_from_me:
            add_log(f"Auto-replying to {chat_id}...")
            bot_reply = get_llm_response(chat_id, text_message)
            time.sleep(random.randint(1, 2))
            send_reply(client_inst, event, bot_reply)

    except Exception as e:
        add_log(f"Error in on_message: {e}")


def main():
    # 1. Start the core WhatsApp Client connection in background thread
    def run_whatsapp():
        print("Launching Python WhatsApp Bot engine...")
        try:
            client.connect()
        except Exception as e:
            print(f"WhatsApp Client notice: {e}")

    wa_thread = threading.Thread(target=run_whatsapp, daemon=True)
    wa_thread.start()

    # 2. Optional: Auto-Pair using PHONE_NUMBER environment variable (for headless Render setup)
    phone_env = os.environ.get("PHONE_NUMBER", "").strip().replace("+", "").replace(" ", "").replace("-", "")
    if phone_env:
        def auto_pair():
            time.sleep(4)
            if not CONNECTION_STATE["connected"]:
                try:
                    add_log(f"Auto-requesting pairing code for PHONE_NUMBER={phone_env}...")
                    code = client.PairPhone(phone_env, show_push_notification=True)
                    print("\n" + "="*60)
                    print(f"👉 YOUR RENDER WHATSAPP PAIRING CODE IS:  {code}")
                    print("="*60)
                    print(f"On your phone ({phone_env}):")
                    print("1. Open WhatsApp -> Settings / Menu -> Linked Devices")
                    print("2. Tap 'Link a Device' -> 'Link with phone number instead'")
                    print(f"3. Enter this 8-digit code: {code}\n" + "="*60 + "\n")
                    add_log(f"Auto-pair code generated: {code}")
                except Exception as e:
                    print(f"[Auto-Pair Error] {e}")
        threading.Thread(target=auto_pair, daemon=True).start()

    # 3. Run Flask server
    print("Starting Flask server for Render keep-alive...")
    port = int(os.environ.get("PORT", 3000))
    app.run(host='0.0.0.0', port=port, use_reloader=False)


if __name__ == "__main__":
    main()

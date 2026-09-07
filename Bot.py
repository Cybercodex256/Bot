import os
import json
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
from neonize.utils.jid import Jid2String, build_jid
from google import genai
from google.genai import types
import segno

# 1. In-Memory Stores for WhatsApp state & QR & Logs
QR_DATA = {"svg": None, "raw": None, "timestamp": 0}
CONNECTION_STATE = {"connected": False, "phone": None, "last_error": None}
ACTIVE_PAIRING_CODE = {
    "code": None,
    "phone": None,
    "generated_at": 0
}
WHATSAPP_LOGS = []

def add_log(msg: str):
    print(f"[WhatsApp] {msg}")
    WHATSAPP_LOGS.append(f"[{time.strftime('%H:%M:%S')}] {msg}")
    if len(WHATSAPP_LOGS) > 20:
        WHATSAPP_LOGS.pop(0)

# 2. Initialize Flask App for Render Keep-Alive & QR linking UI
app = Flask(__name__)

@app.after_request
def add_cors_headers(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type,Authorization'
    response.headers['Access-Control-Allow-Methods'] = 'GET,POST,OPTIONS'
    return response

@app.route('/ping', methods=['GET'])
def ping():
    """Endpoint that you will ping with your cron job."""
    return jsonify({
        "status": "healthy",
        "timestamp": time.time(),
        "whatsapp_connected": CONNECTION_STATE["connected"]
    }), 200

def ensure_whatsapp_connected(timeout: float = 6.0) -> bool:
    """Waits for Neonize client or QR code readiness with WhatsApp servers."""
    start_time = time.time()
    while time.time() - start_time < timeout:
        if getattr(client, "is_connected", False) or QR_DATA.get("svg") is not None:
            return True
        time.sleep(0.3)
    return getattr(client, "is_connected", False) or QR_DATA.get("svg") is not None

@app.route('/api/status', methods=['GET'])
def api_status():
    """Returns real-time WhatsApp connection status, current QR code, and recent diagnostic logs."""
    return jsonify({
        "connected": CONNECTION_STATE["connected"],
        "has_qr": QR_DATA["svg"] is not None,
        "qr_svg": QR_DATA["svg"],
        "timestamp": QR_DATA["timestamp"],
        "last_error": CONNECTION_STATE["last_error"],
        "global_enabled": BOT_CONFIG.get("global_enabled", True),
        "filter_mode": BOT_CONFIG.get("mode", "all"),
        "muted_count": len(BOT_CONFIG.get("muted_chats", [])),
        "allowed_count": len(BOT_CONFIG.get("allowed_chats", [])),
        "logs": WHATSAPP_LOGS[-10:]
    }), 200

@app.route('/api/pair', methods=['POST'])
def api_pair():
    """Generates an 8-character pairing code for a phone number with automatic handshake waiting."""
    data = request.get_json(silent=True) or request.form
    phone = (data.get("phone") or "").strip().replace("+", "").replace(" ", "").replace("-", "")
    force = bool(data.get("force", False))
    if not phone:
        return jsonify({"error": "Phone number is required (e.g., 256700354922)"}), 400

    # If the bot is already authenticated and connected
    if CONNECTION_STATE["connected"] or getattr(client, "is_logged_in", False):
        return jsonify({
            "error": "This WhatsApp bot is ALREADY linked and connected! If you want to link a new number, click 'Unlink Device' below first."
        }), 400

    # If force is not requested, and we recently generated a valid pairing code (< 10 min), return it
    if not force and ACTIVE_PAIRING_CODE["code"] and ACTIVE_PAIRING_CODE["phone"] == phone:
        if time.time() - ACTIVE_PAIRING_CODE["generated_at"] < 600:
            add_log(f"Returning active cached pairing code for {phone[:4]}***: {ACTIVE_PAIRING_CODE['code']}")
            return jsonify({
                "success": True,
                "pairing_code": ACTIVE_PAIRING_CODE["code"],
                "phone": phone,
                "cached": True
            }), 200

    # Ensure the client or QR engine has initialized
    if not (getattr(client, "is_connected", False) or QR_DATA.get("svg") is not None):
        add_log("PairPhone requested while WhatsApp connection is initializing; waiting...")
        if not ensure_whatsapp_connected(timeout=6.0):
            return jsonify({
                "error": "WhatsApp server connection is currently establishing. Please wait a few seconds and try again."
            }), 503

    # Attempt pairing with retries if the websocket was briefly renegotiating
    last_err = None
    for attempt in range(3):
        try:
            add_log(f"Requesting new pairing code for {phone[:4]}*** (attempt {attempt + 1})...")
            # PairPhone signature: (phone: str, show_push_notification: bool, ...)
            code = client.PairPhone(phone, True)
            if code:
                cleaned_code = str(code).strip()
                ACTIVE_PAIRING_CODE["code"] = cleaned_code
                ACTIVE_PAIRING_CODE["phone"] = phone
                ACTIVE_PAIRING_CODE["generated_at"] = time.time()
                add_log(f"New pairing code generated successfully: {cleaned_code}")
                return jsonify({"success": True, "pairing_code": cleaned_code, "phone": phone, "fresh": True}), 200
            else:
                add_log("PairPhone returned empty code, retrying...")
                time.sleep(1.0)
                continue
        except Exception as e:
            last_err = str(e)
            add_log(f"PairPhone attempt {attempt + 1} notice: {last_err}")
            time.sleep(1.2)

    err_msg = last_err or "Failed to generate pairing code"
    CONNECTION_STATE["last_error"] = err_msg

    # If fresh request hit rate limit but we have a recent active code for this phone, return it as fallback
    if ACTIVE_PAIRING_CODE["code"] and ACTIVE_PAIRING_CODE["phone"] == phone:
        return jsonify({
            "success": True,
            "pairing_code": ACTIVE_PAIRING_CODE["code"],
            "phone": phone,
            "cached": True,
            "notice": "Reusing your existing active pairing code because WhatsApp limits repeated requests."
        }), 200

    if "rate-overlimit" in err_msg.lower() or "429" in err_msg:
        err_msg = "WhatsApp Rate Limit: WhatsApp allows only a limited number of pairing code requests per phone number within a short period. Please switch to the 'QR Code' tab to link immediately without any waiting period, or wait a few minutes."
    elif "websocket not connected" in err_msg.lower():
        err_msg = "WhatsApp connection is still initializing. Please wait a few seconds and try again."
    return jsonify({"success": False, "error": err_msg}), 500

@app.route('/api/unlink', methods=['POST'])
@app.route('/api/reset', methods=['POST'])
def api_reset():
    """Logs out and resets local session so you can pair a different phone number."""
    try:
        add_log("User requested session reset / unlink.")
        try:
            client.logout()
        except Exception as e:
            add_log(f"Logout notice: {e}")
        try:
            client.disconnect()
        except Exception as e:
            add_log(f"Disconnect notice: {e}")

        CONNECTION_STATE["connected"] = False
        QR_DATA["svg"] = None
        QR_DATA["timestamp"] = 0

        # Remove local sqlite session files so next pairing is completely fresh
        for fname in ["session.db", "session.db-shm", "session.db-wal"]:
            if os.path.exists(fname):
                try:
                    os.remove(fname)
                    add_log(f"Removed {fname}")
                except Exception as fe:
                    add_log(f"Notice deleting {fname}: {fe}")

        return jsonify({"success": True, "message": "Logged out successfully. You can now pair a new phone number."}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/', methods=['GET'])
def home():
    """Default route displaying connection status and live QR code to pair WhatsApp."""
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
          <input type="text" id="phoneInput" value='""" + os.environ.get("PHONE_NUMBER", "").strip().replace("+", "").replace(" ", "").replace("-", "") + """' placeholder="e.g. 256700354922 or 14155552671">
          <button type="button" id="getCodeBtn" onclick="requestPairCode(false)">Get Code</button>
          <button type="button" id="getNewCodeBtn" onclick="requestPairCode(true)" style="background: rgba(255,255,255,0.08); color: var(--text); border: 1px solid var(--border);">New Code</button>
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
      <div style="background: rgba(0, 168, 132, 0.1); border: 1px solid rgba(0, 168, 132, 0.3); border-radius: 12px; padding: 20px; margin-bottom: 20px; text-align: left;">
        <div style="display: flex; align-items: center; gap: 10px; margin-bottom: 10px;">
          <span style="font-size: 1.5rem;">🔒</span>
          <h2 style="font-size: 1.15rem; color: var(--green); margin: 0;">WhatsApp Connected & Stealth Hub Ready</h2>
        </div>
        <p style="color: var(--muted); font-size: 0.88rem; line-height: 1.5; margin-bottom: 12px;">
          Your WhatsApp Bot is online. To keep your commands and AI inquiries 100% private from other participants, use your personal <strong>"Message Yourself"</strong> chat in WhatsApp as your private command center.
        </p>

        <div style="background: var(--header); padding: 12px; border-radius: 8px; font-size: 0.82rem; color: var(--text); line-height: 1.6;">
          <div style="color: var(--green); font-weight: 600; margin-bottom: 4px;">⚡ Quick WhatsApp Commands:</div>
          • <code>!help</code> — Open complete guide & command directory<br>
          • <code>!chats</code> — View active filter mode, whitelist & muted numbers<br>
          • <code>!mode all</code> / <code>!mode whitelist</code> — Toggle chat filtering<br>
          • <code>!mute &lt;phone&gt;</code> / <code>!allow &lt;phone&gt;</code> — Manage specific chats<br>
          • <code>!research &lt;topic&gt;</code> — In-depth research report delivered privately<br>
          • <code>!ask &lt;question&gt;</code> — Ghost mode (auto-deletes in shared chats)
        </div>
      </div>
      <div>
        <button type="button" onclick="unlinkAccount()" style="background: transparent; border: 1px solid rgba(239, 68, 68, 0.4); color: #f87171; padding: 8px 16px; border-radius: 8px; font-size: 0.82rem; cursor: pointer;">
          Unlink Device / Pair Different Number
        </button>
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

    async function unlinkAccount() {
      if (!confirm('Are you sure you want to unlink this WhatsApp account and reset the session?')) return;
      try {
        const res = await fetch('/api/unlink', { method: 'POST' });
        const data = await res.json();
        alert(data.message || 'Logged out successfully');
        window.location.reload();
      } catch (err) {
        alert('Notice during unlink: ' + err.message);
        window.location.reload();
      }
    }

    async function requestPairCode(force = false) {
      const phoneInput = document.getElementById('phoneInput');
      const getCodeBtn = document.getElementById('getCodeBtn');
      const getNewCodeBtn = document.getElementById('getNewCodeBtn');
      const phone = phoneInput ? phoneInput.value.trim() : '';
      if (!phone) return alert('Please enter your phone number with country code');
      const resEl = document.getElementById('pairResult');
      if (resEl) resEl.innerHTML = '<div style="color: var(--muted); margin-top: 10px;">Connecting to WhatsApp server & requesting ' + (force ? 'new ' : '') + '8-digit code...</div>';
      if (getCodeBtn) getCodeBtn.disabled = true;
      if (getNewCodeBtn) getNewCodeBtn.disabled = true;
      try {
        const res = await fetch('/api/pair', {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'Accept': 'application/json'
          },
          body: JSON.stringify({ phone: phone, force: !!force })
        });
        let data = null;
        try {
          data = await res.json();
        } catch (_) {}

        if (data && data.pairing_code) {
          if (resEl) {
            const cachedNote = data.cached ? '<div style="font-size: 0.78rem; color: #8696a0; margin-top: 4px;">' + (data.notice || '(Using active code — click "New Code" to regenerate)') + '</div>' : '<div style="font-size: 0.78rem; color: var(--green); margin-top: 4px;">(Freshly generated pairing code)</div>';
            resEl.innerHTML = '<div style="margin-top: 14px; font-size: 0.88rem; color: var(--muted);">Enter this code on your phone:</div><div class="code-display" id="generatedPairingCode">' + data.pairing_code + '</div>' + cachedNote + '<p style="font-size: 0.82rem; color: var(--muted); margin-top: 6px;">Check WhatsApp on your phone for a pairing notification, or enter the code manually.</p>';
          }
        } else {
          let errMsg = (data && data.error) ? data.error : ((data && data.message) ? data.message : 'WhatsApp server connection is busy. Please wait a few seconds and try again.');
          if (errMsg.toLowerCase().includes('rate') || errMsg.includes('429')) {
            errMsg = '<div style="background: rgba(245, 158, 11, 0.12); border: 1px solid rgba(245, 158, 11, 0.3); color: #fbbf24; padding: 12px; border-radius: 8px; margin-top: 10px; text-align: left; font-size: 0.86rem; line-height: 1.5;">' +
              '<strong>⏱️ WhatsApp Rate-Limit:</strong> WhatsApp temporarily limits how frequently new pairing codes can be generated for this phone number.<br><br>' +
              '• <strong>Instant Solution:</strong> Click the <strong>"QR Code"</strong> tab above and scan the QR code in WhatsApp for immediate connection (no waiting required).<br>' +
              '• Or wait 2–5 minutes before requesting another code.' +
              '</div>';
            if (resEl) resEl.innerHTML = errMsg;
          } else {
            if (resEl) resEl.innerHTML = '<div style="color: #ff5252; margin-top: 10px; font-size: 0.88rem; line-height: 1.4;">' + errMsg + '</div>';
          }
        }
      } catch (err) {
        if (resEl) resEl.innerHTML = '<div style="color: #ff5252; margin-top: 10px; font-size: 0.88rem;">Network error: ' + (err.message || 'Please retry in a moment') + '</div>';
      } finally {
        if (getCodeBtn) {
          getCodeBtn.disabled = false;
          getCodeBtn.innerText = 'Get Code';
        }
        if (getNewCodeBtn) {
          getNewCodeBtn.disabled = false;
          getNewCodeBtn.innerText = 'New Code';
        }
      }
    }

    let isConnected = false;
    async function pollStatus() {
      try {
        const res = await fetch('/api/status');
        if (!res.ok) return;
        const contentType = res.headers.get('content-type') || '';
        if (!contentType.includes('application/json')) return;
        const data = await res.json();
        if (!data) return;

        if (data.connected) {
          isConnected = true;
          const badge = document.getElementById('statusBadge');
          if (badge) badge.className = 'badge badge-connected';
          const txt = document.getElementById('statusText');
          if (txt) txt.innerText = 'Connected to WhatsApp';
          const qrArea = document.getElementById('qrArea');
          if (qrArea) qrArea.style.display = 'none';
          const connArea = document.getElementById('connectedArea');
          if (connArea) connArea.style.display = 'block';
        } else if (data.qr_svg) {
          isConnected = false;
          const badge = document.getElementById('statusBadge');
          if (badge) badge.className = 'badge badge-connecting';
          const txt = document.getElementById('statusText');
          if (txt) txt.innerText = 'Waiting for WhatsApp link...';
          const qrArea = document.getElementById('qrArea');
          if (qrArea) qrArea.style.display = 'block';
          const connArea = document.getElementById('connectedArea');
          if (connArea) connArea.style.display = 'none';
          const qrCont = document.getElementById('qrContainer');
          if (qrCont) qrCont.innerHTML = '<img src="' + data.qr_svg + '" alt="WhatsApp QR Code">';
        }

        const errEl = document.getElementById('errorBanner');
        if (errEl) {
          if (data.last_error) {
            errEl.style.display = 'block';
            errEl.innerText = data.last_error;
          } else {
            errEl.style.display = 'none';
          }
        }
      } catch (_) {
        // Silently retry on next interval during transient server reloads
      }
    }
    setInterval(pollStatus, 4000);
    pollStatus();
  </script>
</body>
</html>""", 200

def run_flask():
    """Runs the Flask server. Render automatically assigns a PORT variable (defaults to 3000)."""
    port = int(os.environ.get("PORT", 3000))
    app.run(host='0.0.0.0', port=port, use_reloader=False)


# 3. Initialize Google Gemini AI client & Neonize Client
def get_gemini_client():
    """Lazily initializes the Google Gemini API client with GEMINI_API_KEY."""
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("GEMINI_API_KEY environment variable is missing.")
    return genai.Client(api_key=api_key)

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



# 3. Persistent Configuration & Stores
BOT_CONFIG_FILE = "bot_config.json"
DEFAULT_BOT_CONFIG = {
    "global_enabled": True,
    "mode": "all",  # "all" (blacklist mode) or "whitelist" (only whitelisted)
    "muted_chats": [],
    "allowed_chats": []
}

def load_bot_config():
    if os.path.exists(BOT_CONFIG_FILE):
        try:
            with open(BOT_CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return {**DEFAULT_BOT_CONFIG, **data}
        except Exception as e:
            print(f"[Config] Notice loading {BOT_CONFIG_FILE}: {e}")
    return dict(DEFAULT_BOT_CONFIG)

def save_bot_config(cfg):
    try:
        with open(BOT_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception as e:
        print(f"[Config] Notice saving {BOT_CONFIG_FILE}: {e}")

BOT_CONFIG = load_bot_config()
OWNER_JID = None
CHAT_MEMORY = {}

def clean_phone_str(raw: str) -> str:
    """Extracts clean digits from a JID or user input string."""
    return (
        raw.strip()
        .replace("+", "")
        .replace(" ", "")
        .replace("-", "")
        .replace("@s.whatsapp.net", "")
        .replace("@g.us", "")
    )

def is_chat_muted(chat_str: str) -> bool:
    """Checks if a chat is explicitly muted."""
    chat_clean = clean_phone_str(chat_str)
    for m in BOT_CONFIG.get("muted_chats", []):
        if clean_phone_str(m) == chat_clean or m == chat_str:
            return True
    return False

def is_chat_allowed(chat_str: str) -> bool:
    """Determines if the bot is permitted to reply to this chat."""
    if not BOT_CONFIG.get("global_enabled", True):
        return False
    if is_chat_muted(chat_str):
        return False
    mode = BOT_CONFIG.get("mode", "all")
    if mode == "whitelist":
        chat_clean = clean_phone_str(chat_str)
        for a in BOT_CONFIG.get("allowed_chats", []):
            if clean_phone_str(a) == chat_clean or a == chat_str:
                return True
        return False
    return True

def safe_revoke_message(client_inst: NewClient, event: MessageEv):
    """Ghost Mode: Deletes/revokes the sender's command message so other users never see it."""
    try:
        msg_source = event.Info.MessageSource
        client_inst.revoke_message(
            chat=msg_source.Chat,
            sender=msg_source.Sender,
            message_id=event.Info.ID
        )
        add_log(f"Ghost Mode: Revoked command message {event.Info.ID}")
    except Exception as e:
        add_log(f"Notice during revoke_message: {e}")

def get_owner_target(client_inst: NewClient, event: MessageEv = None):
    """Resolves the owner's WhatsApp JID for private routing."""
    global OWNER_JID
    if event and event.Info.MessageSource.IsFromMe:
        OWNER_JID = event.Info.MessageSource.Sender
    if OWNER_JID:
        return OWNER_JID
    phone_env = os.environ.get("PHONE_NUMBER", "").strip().replace("+", "").replace(" ", "").replace("-", "")
    if phone_env:
        return build_jid(phone_env)
    return event.Info.MessageSource.Sender if event else None

def send_to_owner(client_inst: NewClient, event: MessageEv, text: str):
    """Sends a private message strictly to the owner's Message Yourself chat."""
    target = get_owner_target(client_inst, event)
    if target:
        try:
            client_inst.send_message(to=target, message=text)
            add_log(f"Private to owner: {text[:40]}...")
            return True
        except Exception as e:
            add_log(f"Error sending private message to owner: {e}")
    try:
        send_reply(client_inst, event, text)
        return True
    except Exception as e2:
        add_log(f"Fallback send error: {e2}")
        return False

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
    """Standard conversational auto-reply for authorized contacts based on instructions.md using Gemini."""
    if sender_id not in CHAT_MEMORY:
        CHAT_MEMORY[sender_id] = deque(maxlen=10)
        
    CHAT_MEMORY[sender_id].append({"role": "user", "content": new_user_message})
    
    # Map stored history to Gemini contents structure
    gemini_contents = []
    for item in CHAT_MEMORY[sender_id]:
        role_name = "user" if item["role"] == "user" else "model"
        gemini_contents.append({
            "role": role_name,
            "parts": [{"text": item["content"]}]
        })

    try:
        gemini_client = get_gemini_client()
        config = types.GenerateContentConfig(
            system_instruction=SYSTEM_INSTRUCTIONS,
            max_output_tokens=300,
            temperature=0.7
        )
        response = gemini_client.models.generate_content(
            model="gemini-2.5-flash",
            contents=gemini_contents,
            config=config
        )
        bot_reply = (response.text or "").strip()
        if not bot_reply:
            bot_reply = "👍 Received!"
        CHAT_MEMORY[sender_id].append({"role": "assistant", "content": bot_reply})
        return bot_reply
    except Exception as e:
        err_str = str(e)
        add_log(f"Gemini LLM Processing Error: {err_str}")
        if "API_KEY_INVALID" in err_str or "API key not valid" in err_str:
            return "⚠️ *Gemini AI Notice:* Invalid GEMINI_API_KEY provided. Please check your environment variables."
        if "RESOURCE_EXHAUSTED" in err_str or "429" in err_str:
            return "⚠️ *Gemini AI Notice:* Rate limit reached. Please retry in a few moments."
        return f"Sorry, my brain stumbled: {err_str[:100]} 🤖"

def get_private_ai_answer(prompt: str, is_research: bool = False) -> str:
    """Dedicated AI response generator for private owner questions and comprehensive research using Gemini."""
    if is_research:
        system_content = (
            "You are an elite research analyst and private copilot. "
            "Deliver an in-depth, structured, highly factual, and clear research report on the topic requested. "
            "Format neatly using WhatsApp markdown (*bold* for headings, bullet points). "
            "Include: 1) Executive Summary, 2) Key Insights & Facts, 3) Analysis / Nuances / Comparisons, 4) Conclusion / Takeaways."
        )
        max_tokens = 1200
    else:
        system_content = (
            "You are a brilliant, concise, and helpful personal AI assistant. "
            "Provide insightful, accurate, and direct answers using WhatsApp markdown (*bold*, bullet points)."
        )
        max_tokens = 600

    try:
        gemini_client = get_gemini_client()
        config = types.GenerateContentConfig(
            system_instruction=system_content,
            max_output_tokens=max_tokens,
            temperature=0.4 if is_research else 0.7
        )
        response = gemini_client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=config
        )
        answer = (response.text or "").strip()
        return answer if answer else "⚠️ Gemini generated an empty response. Please try rephrasing."
    except Exception as e:
        err_str = str(e)
        add_log(f"Gemini AI Error: {err_str}")
        if "API_KEY_INVALID" in err_str or "API key not valid" in err_str:
            return "⚠️ *Gemini AI Notice:* Invalid GEMINI_API_KEY. Please verify your Gemini API key."
        if "RESOURCE_EXHAUSTED" in err_str or "429" in err_str:
            return "⚠️ *Gemini AI Notice:* Rate limit reached. Please wait a minute and retry."
        return f"⚠️ Could not process request: {err_str[:120]}"

def get_help_menu_text() -> str:
    """Returns formatted private help and command manual."""
    return (
        "🛠️ *WhatsApp Bot Admin & Research Menu*\n\n"
        "🔒 *Privacy & Stealth Mode:*\n"
        "• All bot commands and AI research are 100% private to you.\n"
        "• If you type `!ask`, `!bot mute`, or any admin command in a shared chat with another person, "
        "the bot automatically deletes your message from that chat and delivers the answer privately here!\n\n"
        "📋 *Chat Filtering & Controls:*\n"
        "• `!bot on` / `!bot off` — Turn the bot ON or OFF globally.\n"
        "• `!bot mute` (or `!mute`) — Stop bot from responding in the current chat.\n"
        "• `!bot unmute` (or `!allow`) — Re-enable bot for the current chat.\n"
        "• `!mute <phone>` — Mute a specific number (e.g. `!mute 256700...`).\n"
        "• `!allow <phone>` — Allow a specific number (e.g. `!allow 256700...`).\n"
        "• `!mode whitelist` — Only reply to numbers you have allowed with `!allow`.\n"
        "• `!mode all` — Reply to all incoming chats (except muted ones).\n"
        "• `!chats` — View current mode, active whitelist, and muted list.\n\n"
        "🧠 *Private AI & Deep Research:*\n"
        "• `!ai <question>` — Ask quick questions, draft replies, or brainstorm.\n"
        "• `!research <topic>` — Perform in-depth research with key facts and summary.\n"
        "• *(In any other chat)* `!ask <query>` — Ghost mode: deletes your message in that chat and delivers the AI answer here privately.\n\n"
        "⚙️ *System Diagnostics:*\n"
        "• `!status` — View uptime, memory, and WhatsApp connection state.\n"
        "• `!reload` — Reload your `instructions.md` prompt live without restarting.\n"
        "• `!ping` — Test if the bot is responsive."
    )

def get_chats_status_text() -> str:
    """Returns formatted summary of allowed and muted chats."""
    mode = BOT_CONFIG.get("mode", "all")
    global_status = "🟢 Enabled (ON)" if BOT_CONFIG.get("global_enabled", True) else "🔴 Disabled (OFF)"
    muted = BOT_CONFIG.get("muted_chats", [])
    allowed = BOT_CONFIG.get("allowed_chats", [])

    muted_str = "\n".join([f"  • `{m}`" for m in muted]) if muted else "  _(None)_"
    allowed_str = "\n".join([f"  • `{a}`" for a in allowed]) if allowed else "  _(None)_"

    return (
        f"📋 *Bot Chat Filter Configuration:*\n\n"
        f"• *Global Status:* {global_status}\n"
        f"• *Filter Mode:* `{mode.upper()}` "
        f"({'Only replies to approved whitelist' if mode == 'whitelist' else 'Replies to all chats except muted'})\n\n"
        f"🔇 *Muted Chats ({len(muted)}):*\n{muted_str}\n\n"
        f"✅ *Allowed / Whitelist Chats ({len(allowed)}):*\n{allowed_str}\n\n"
        f"_Commands to manage:_\n"
        f"• `!allow <phone>` — Add to whitelist\n"
        f"• `!mute <phone>` — Add to muted list\n"
        f"• `!mode whitelist` or `!mode all`"
    )

def get_system_status_text() -> str:
    """Returns high-level system diagnostics."""
    is_active = BOT_CONFIG.get("global_enabled", True)
    mode = BOT_CONFIG.get("mode", "all")
    muted_count = len(BOT_CONFIG.get("muted_chats", []))
    allowed_count = len(BOT_CONFIG.get("allowed_chats", []))
    return (
        f"🤖 *WhatsApp Bot System Status:*\n\n"
        f"• Connection: 🟢 Connected to WhatsApp\n"
        f"• Bot State: {'🟢 Active (ON)' if is_active else '🔴 Paused (OFF)'}\n"
        f"• Filter Mode: `{mode.upper()}`\n"
        f"• Muted Chats: {muted_count}\n"
        f"• Allowed / Whitelist Chats: {allowed_count}\n"
        f"• Active Context Memory: {len(CHAT_MEMORY)} chats\n"
        f"• AI Model: `Gemini 2.5 Flash (Google GenAI)`"
    )

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
        clean_msg = text_message.strip()
        lower_msg = clean_msg.lower()

        # Keep track of owner's JID
        if is_from_me:
            get_owner_target(client_inst, event)

        # Detect if this is in the user's "Message Yourself" private chat
        is_self_chat = is_from_me and (
            chat_jid.User == sender_jid.User
            or chat_id == sender_id
            or (chat_jid.Server == "s.whatsapp.net" and clean_phone_str(chat_id) == clean_phone_str(sender_id))
        )

        add_log(f"Message in {chat_id} from {sender_id} (me={is_from_me}, self_chat={is_self_chat}): {text_message[:50]}")

        # =========================================================================
        # 1. MESSAGES FROM OTHER CONTACTS (not from owner)
        # =========================================================================
        if not is_from_me:
            # Contacts cannot run admin commands!
            # Check if this chat is allowed to receive auto-replies
            if not is_chat_allowed(chat_id):
                add_log(f"Ignored message from {chat_id} (chat not allowed or muted)")
                return

            add_log(f"Auto-replying to {chat_id}...")
            bot_reply = get_llm_response(chat_id, text_message)
            time.sleep(random.randint(1, 2))
            send_reply(client_inst, event, bot_reply)
            return

        # =========================================================================
        # 2. MESSAGES FROM OWNER IN A SHARED CHAT (Ghost Mode / Stealth Protection)
        # =========================================================================
        if is_from_me and not is_self_chat:
            # If it's a normal message to a contact/group, ignore so owner can chat naturally
            if not clean_msg.startswith("!"):
                return

            # It's an owner command in a shared chat!
            # IMMEDIATELY delete/revoke message from shared chat so other user never sees it
            safe_revoke_message(client_inst, event)

            # --- Stealth Command: Private AI Inquiry in Shared Chat ---
            if lower_msg.startswith("!ask ") or lower_msg.startswith("!ai "):
                query = clean_msg[5:].strip() if lower_msg.startswith("!ask ") else clean_msg[4:].strip()
                if query:
                    ai_answer = get_private_ai_answer(query, is_research=False)
                    send_to_owner(client_inst, event, f"💡 *Private AI Answer (Inquiry from `{chat_id}`):*\n\n{ai_answer}")
                return

            # --- Stealth Command: Deep Research in Shared Chat ---
            if lower_msg.startswith("!research "):
                topic = clean_msg[10:].strip()
                if topic:
                    report = get_private_ai_answer(topic, is_research=True)
                    send_to_owner(client_inst, event, f"📊 *Private Research Report (Requested from `{chat_id}`):*\n\n{report}")
                return

            # --- Stealth Command: Mute Current Shared Chat ---
            if lower_msg in ["!bot mute", "!mute", "!bot here off", "!bot off"]:
                target_clean = clean_phone_str(chat_id)
                if target_clean not in BOT_CONFIG.get("muted_chats", []):
                    BOT_CONFIG.setdefault("muted_chats", []).append(target_clean)
                if target_clean in BOT_CONFIG.get("allowed_chats", []):
                    BOT_CONFIG["allowed_chats"].remove(target_clean)
                save_bot_config(BOT_CONFIG)
                send_to_owner(client_inst, event, f"🔇 *Chat Muted:* Bot will no longer respond to messages in `{chat_id}`.\n_Tip: Type `!bot unmute` or `!allow {target_clean}` to re-enable._")
                return

            # --- Stealth Command: Unmute / Allow Current Shared Chat ---
            if lower_msg in ["!bot unmute", "!unmute", "!allow", "!bot here on", "!bot on"]:
                target_clean = clean_phone_str(chat_id)
                if target_clean in BOT_CONFIG.get("muted_chats", []):
                    BOT_CONFIG["muted_chats"].remove(target_clean)
                if target_clean not in BOT_CONFIG.get("allowed_chats", []):
                    BOT_CONFIG.setdefault("allowed_chats", []).append(target_clean)
                save_bot_config(BOT_CONFIG)
                send_to_owner(client_inst, event, f"🔊 *Chat Unmuted:* Bot is now active and will respond to `{chat_id}`.")
                return

            # --- Stealth Commands: Help / Chats / Status / Ping ---
            if lower_msg in ["!help", "!commands"]:
                send_to_owner(client_inst, event, get_help_menu_text())
                return

            if lower_msg == "!chats":
                send_to_owner(client_inst, event, get_chats_status_text())
                return

            if lower_msg in ["!status", "!bot status"]:
                send_to_owner(client_inst, event, get_system_status_text())
                return

            if lower_msg in ["!ping"]:
                send_to_owner(client_inst, event, f"🏓 Pong! The WhatsApp bot is active and listening.")
                return

            if lower_msg in ["!reload", "!bot reload"]:
                global SYSTEM_INSTRUCTIONS
                SYSTEM_INSTRUCTIONS = load_ai_instructions()
                send_to_owner(client_inst, event, "🔄 Successfully reloaded 'instructions.md' changes live!")
                return

            return

        # =========================================================================
        # 3. MESSAGES IN "MESSAGE YOURSELF" (Owner's Private Control Hub)
        # =========================================================================
        if is_self_chat:
            # Help Menu
            if lower_msg in ["!help", "!commands", "!bot help"]:
                send_reply(client_inst, event, get_help_menu_text())
                return

            # Global Bot ON / OFF
            if lower_msg in ["!bot on", "!on"]:
                BOT_CONFIG["global_enabled"] = True
                save_bot_config(BOT_CONFIG)
                send_reply(client_inst, event, "🟢 *WhatsApp Bot Activated:* The assistant is ON and will respond to allowed incoming chats.")
                return

            if lower_msg in ["!bot off", "!off"]:
                BOT_CONFIG["global_enabled"] = False
                save_bot_config(BOT_CONFIG)
                send_reply(client_inst, event, "🔴 *WhatsApp Bot Paused:* The assistant is OFF globally. No auto-replies will be sent to any contacts.")
                return

            # Mode switching: Whitelist vs All
            if lower_msg in ["!mode whitelist", "!whitelist"]:
                BOT_CONFIG["mode"] = "whitelist"
                save_bot_config(BOT_CONFIG)
                send_reply(client_inst, event, "🔒 *Mode Changed to WHITELIST:*\nThe bot will ONLY respond to numbers in your allowed list. Use `!allow <phone>` to add numbers.")
                return

            if lower_msg in ["!mode all", "!all"]:
                BOT_CONFIG["mode"] = "all"
                save_bot_config(BOT_CONFIG)
                send_reply(client_inst, event, "🌐 *Mode Changed to ALL:*\nThe bot will respond to all incoming contacts, EXCEPT those in your muted list.")
                return

            # View Chats Config
            if lower_msg in ["!chats", "!list"]:
                send_reply(client_inst, event, get_chats_status_text())
                return

            # Allow / Whitelist a specific number
            if lower_msg.startswith("!allow ") or lower_msg.startswith("!unmute "):
                arg = clean_msg.split(maxsplit=1)[1].strip() if " " in clean_msg else ""
                clean_target = clean_phone_str(arg)
                if not clean_target:
                    send_reply(client_inst, event, "ℹ️ *Usage:* `!allow <phone_number>` (e.g. `!allow 256700354922`).")
                    return
                if clean_target in BOT_CONFIG.get("muted_chats", []):
                    BOT_CONFIG["muted_chats"].remove(clean_target)
                if clean_target not in BOT_CONFIG.get("allowed_chats", []):
                    BOT_CONFIG.setdefault("allowed_chats", []).append(clean_target)
                save_bot_config(BOT_CONFIG)
                send_reply(client_inst, event, f"✅ *Allowed Contact:* `{clean_target}` added to allowed list and unmuted.")
                return

            # Mute a specific number
            if lower_msg.startswith("!mute ") or lower_msg.startswith("!block "):
                arg = clean_msg.split(maxsplit=1)[1].strip() if " " in clean_msg else ""
                clean_target = clean_phone_str(arg)
                if not clean_target:
                    send_reply(client_inst, event, "ℹ️ *Usage:* `!mute <phone_number>` (e.g. `!mute 256700354922`).")
                    return
                if clean_target in BOT_CONFIG.get("allowed_chats", []):
                    BOT_CONFIG["allowed_chats"].remove(clean_target)
                if clean_target not in BOT_CONFIG.get("muted_chats", []):
                    BOT_CONFIG.setdefault("muted_chats", []).append(clean_target)
                save_bot_config(BOT_CONFIG)
                send_reply(client_inst, event, f"🔇 *Muted Contact:* `{clean_target}` is now muted and will not receive bot replies.")
                return

            # Deep Research Mode
            if lower_msg.startswith("!research "):
                topic = clean_msg[10:].strip()
                if not topic:
                    send_reply(client_inst, event, "ℹ️ *Usage:* `!research <topic>` (e.g. `!research quantum computing trends 2026`).")
                    return
                add_log(f"Deep Research requested: {topic}")
                send_reply(client_inst, event, f"🔎 *Researching '{topic}'...* Preparing in-depth report.")
                report = get_private_ai_answer(topic, is_research=True)
                send_reply(client_inst, event, f"📊 *Research Report: {topic}*\n\n{report}")
                return

            # Private AI Prompt
            if lower_msg.startswith("!ai "):
                query = clean_msg[4:].strip()
                if query:
                    answer = get_private_ai_answer(query, is_research=False)
                    send_reply(client_inst, event, answer)
                return

            # Status & Diagnostics
            if lower_msg in ["!status", "!bot status"]:
                send_reply(client_inst, event, get_system_status_text())
                return

            if lower_msg in ["!reload", "!bot reload"]:
                SYSTEM_INSTRUCTIONS = load_ai_instructions()
                send_reply(client_inst, event, "🔄 Successfully reloaded 'instructions.md' changes live!")
                return

            if lower_msg == "!ping":
                send_reply(client_inst, event, "🏓 Pong! The WhatsApp bot is active and listening.")
                return

            # Natural Conversation in Message Yourself: Treat as private AI copilot query!
            add_log(f"Private copilot query in Message Yourself: {clean_msg[:40]}")
            copilot_reply = get_private_ai_answer(clean_msg, is_research=False)
            send_reply(client_inst, event, copilot_reply)
            return

    except Exception as e:
        add_log(f"Error in on_message: {e}")


def main():
    # 1. Start the core WhatsApp Client connection in background thread with auto-reconnect
    def run_whatsapp():
        while True:
            print("Launching Python WhatsApp Bot engine...")
            try:
                client.connect()
            except Exception as e:
                print(f"WhatsApp Client notice: {e}")
                add_log(f"WhatsApp client notice: {e}")
            time.sleep(3)

    wa_thread = threading.Thread(target=run_whatsapp, daemon=True)
    wa_thread.start()

    # 2. Optional: Auto-Pair using PHONE_NUMBER environment variable (for headless Render setup)
    phone_env = os.environ.get("PHONE_NUMBER", "").strip().replace("+", "").replace(" ", "").replace("-", "")
    if phone_env:
        def auto_pair():
            add_log(f"Waiting for WhatsApp connection before auto-pairing PHONE_NUMBER={phone_env}...")
            # Wait up to 30s for the websocket handshake to establish
            for _ in range(60):
                time.sleep(0.5)
                if getattr(client, "is_connected", False):
                    break

            if getattr(client, "is_logged_in", False) or CONNECTION_STATE["connected"]:
                add_log("WhatsApp is already linked and connected, skipping auto-pair.")
                return

            for attempt in range(3):
                try:
                    add_log(f"Auto-requesting pairing code for PHONE_NUMBER={phone_env} (attempt {attempt + 1})...")
                    code = client.PairPhone(phone_env, True)
                    if code:
                        cleaned_code = str(code).strip()
                        ACTIVE_PAIRING_CODE["code"] = cleaned_code
                        ACTIVE_PAIRING_CODE["phone"] = phone_env
                        ACTIVE_PAIRING_CODE["generated_at"] = time.time()
                        print("\n" + "="*60)
                        print(f"👉 YOUR RENDER WHATSAPP PAIRING CODE IS:  {cleaned_code}")
                        print("="*60)
                        print(f"On your phone ({phone_env}):")
                        print("1. Open WhatsApp -> Settings / Menu -> Linked Devices")
                        print("2. Tap 'Link a Device' -> 'Link with phone number instead'")
                        print(f"3. Enter this 8-digit code: {cleaned_code}\n" + "="*60 + "\n")
                        add_log(f"Auto-pair code generated: {cleaned_code}")
                        break
                except Exception as e:
                    print(f"[Auto-Pair Attempt {attempt + 1}] {e}")
                    time.sleep(3)

        threading.Thread(target=auto_pair, daemon=True).start()

    # 3. Run Flask server
    print("Starting Flask server for Render keep-alive...")
    port = int(os.environ.get("PORT", 3000))
    app.run(host='0.0.0.0', port=port, use_reloader=False)


if __name__ == "__main__":
    main()

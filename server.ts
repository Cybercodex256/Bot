import express, { Request, Response } from "express";
import fs from "fs";
import path from "path";
import dotenv from "dotenv";
import OpenAI from "openai";

dotenv.config();

const app = express();
const PORT = parseInt(process.env.PORT || "3000", 10);

app.use(express.json());
app.use(express.urlencoded({ extended: true }));

// 1. Initialize OpenAI client
const openai = new OpenAI({
  apiKey: process.env.OPENAI_API_KEY || "YOUR_API_KEY_HERE",
});

// 2. In-Memory Stores (matching Bot.py)
interface ChatMessage {
  role: "system" | "user" | "assistant";
  content: string;
}

const BOT_STATUS: Record<string, boolean> = {};
const CHAT_MEMORY: Record<string, ChatMessage[]> = {};

function loadAiInstructions(filename = "instructions.md"): string {
  try {
    const fullPath = path.resolve(process.cwd(), filename);
    if (fs.existsSync(fullPath)) {
      return fs.readFileSync(fullPath, "utf-8");
    }
    console.warn(`Warning: '${filename}' not found. Falling back to default prompt.`);
    return "You are a casual and helpful personal assistant running inside WhatsApp.";
  } catch (err) {
    console.error("Error reading instructions:", err);
    return "You are a casual and helpful personal assistant running inside WhatsApp.";
  }
}

let SYSTEM_INSTRUCTIONS = loadAiInstructions();

// 3. LLM Response generator
async function getLlmResponse(senderId: string, newUserMessage: string): Promise<string> {
  if (!CHAT_MEMORY[senderId]) {
    CHAT_MEMORY[senderId] = [];
  }

  // Keep max 10 messages
  CHAT_MEMORY[senderId].push({ role: "user", content: newUserMessage });
  if (CHAT_MEMORY[senderId].length > 10) {
    CHAT_MEMORY[senderId].shift();
  }

  const systemPrompt: ChatMessage = {
    role: "system",
    content: SYSTEM_INSTRUCTIONS,
  };

  const messagesPayload = [systemPrompt, ...CHAT_MEMORY[senderId]];

  try {
    if (!process.env.OPENAI_API_KEY || process.env.OPENAI_API_KEY === "YOUR_API_KEY_HERE") {
      console.warn("OPENAI_API_KEY not configured. Providing local conversational response.");
      const fallbackReply = generateOfflineResponse(newUserMessage);
      CHAT_MEMORY[senderId].push({ role: "assistant", content: fallbackReply });
      return fallbackReply;
    }

    const response = await openai.chat.completions.create({
      model: "gpt-4o-mini",
      messages: messagesPayload,
      max_tokens: 250,
    });

    const botReply = response.choices[0]?.message?.content?.trim() || "";
    CHAT_MEMORY[senderId].push({ role: "assistant", content: botReply });
    return botReply;
  } catch (error) {
    console.error("LLM Processing Error:", error);
    return "Sorry, my brain stumbled. Try messaging me again in a moment! 🤖";
  }
}

function generateOfflineResponse(message: string): string {
  const lower = message.toLowerCase();
  if (lower.includes("hello") || lower.includes("hi") || lower.includes("hey")) {
    return "Hey there! How can I help you today? 😊";
  }
  if (lower.includes("meet") || lower.includes("call") || lower.includes("schedule")) {
    return "I'll pass that along right away and have them follow up with you! 👍";
  }
  if (lower.includes("ai") || lower.includes("bot") || lower.includes("robot")) {
    return "Yes, I'm an automated personal assistant managing messages here! 🤖";
  }
  return "Thanks for your message! I've noted that down and will pass it along shortly. 👍 (Set OPENAI_API_KEY for dynamic GPT-4o-mini responses)";
}

// 4. Handle incoming message event logic (matching on_message in Bot.py)
export function processIncomingMessage(
  senderId: string,
  textMessage: string,
  isFromMe: boolean = false
): { reply: string | null; botStatus: boolean; action?: string } {
  const cleanMsg = textMessage.trim().toLowerCase();

  // --- COMMANDS ---
  if (cleanMsg.includes("!bot off")) {
    BOT_STATUS[senderId] = false;
    if (CHAT_MEMORY[senderId]) {
      delete CHAT_MEMORY[senderId];
    }
    return { reply: "🤖 Assistant paused.", botStatus: false, action: "pause" };
  }

  if (cleanMsg.includes("!bot on")) {
    BOT_STATUS[senderId] = true;
    return { reply: "🤖 Assistant activated!", botStatus: true, action: "activate" };
  }

  if (isFromMe && cleanMsg.includes("!bot reload")) {
    SYSTEM_INSTRUCTIONS = loadAiInstructions();
    return {
      reply: "🔄 Successfully reloaded 'instructions.md' changes live!",
      botStatus: BOT_STATUS[senderId] ?? true,
      action: "reload",
    };
  }

  if (BOT_STATUS[senderId] === undefined) {
    BOT_STATUS[senderId] = true;
  }

  return { reply: null, botStatus: BOT_STATUS[senderId] };
}

// --- ROUTES ---

// Keep-alive ping endpoint (original Bot.py)
app.get("/ping", (req: Request, res: Response) => {
  res.json({
    status: "healthy",
    timestamp: Date.now() / 1000,
  });
});

// Bot status API
app.get("/api/status", (req: Request, res: Response) => {
  res.json({
    status: "running",
    activeSessions: Object.keys(CHAT_MEMORY).length,
    botStatus: BOT_STATUS,
    hasApiKey: Boolean(process.env.OPENAI_API_KEY && process.env.OPENAI_API_KEY !== "YOUR_API_KEY_HERE"),
    instructionsPreview: SYSTEM_INSTRUCTIONS.slice(0, 150) + "...",
  });
});

// Instructions GET / POST
app.get("/api/instructions", (req: Request, res: Response) => {
  res.json({ instructions: SYSTEM_INSTRUCTIONS });
});

app.post("/api/instructions", (req: Request, res: Response) => {
  const { instructions } = req.body;
  if (typeof instructions === "string") {
    try {
      const fullPath = path.resolve(process.cwd(), "instructions.md");
      fs.writeFileSync(fullPath, instructions, "utf-8");
      SYSTEM_INSTRUCTIONS = instructions;
      res.json({ success: true, message: "Instructions updated and reloaded." });
    } catch (e: any) {
      res.status(500).json({ error: e.message });
    }
  } else {
    res.status(400).json({ error: "instructions string required" });
  }
});

// Incoming message API / simulator
app.post("/api/message", async (req: Request, res: Response) => {
  const { sender_id = "default_user", message = "", is_from_me = false } = req.body;

  if (!message || typeof message !== "string") {
    res.status(400).json({ error: "message is required" });
    return;
  }

  const result = processIncomingMessage(sender_id, message, is_from_me);

  // If command was triggered
  if (result.reply) {
    res.json({
      sender_id,
      user_message: message,
      reply: result.reply,
      bot_status: result.botStatus,
      action: result.action,
    });
    return;
  }

  // Auto-response if active and not from me
  if (result.botStatus && !is_from_me) {
    console.log(`Processing chat from ${sender_id}: ${message}`);
    const botReply = await getLlmResponse(sender_id, message);
    res.json({
      sender_id,
      user_message: message,
      reply: botReply,
      bot_status: result.botStatus,
    });
    return;
  }

  res.json({
    sender_id,
    user_message: message,
    reply: null,
    bot_status: result.botStatus,
    note: "Bot is paused for this sender",
  });
});

// Root Route: Returns text for API/curl or full interactive web UI for browser
app.get("/", (req: Request, res: Response) => {
  const acceptHeader = req.headers["accept"] || "";
  if (acceptHeader.includes("text/plain")) {
    res.send("WhatsApp Bot & Flask Server are running!");
    return;
  }

  res.send(`<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>WhatsApp Bot & Server</title>
  <style>
    :root {
      --bg: #0b141a;
      --card-bg: #111b21;
      --header-bg: #202c33;
      --text: #e9edef;
      --text-muted: #8696a0;
      --accent: #00a884;
      --accent-hover: #06cf9c;
      --chat-in: #202c33;
      --chat-out: #005c4b;
      --border: #222e35;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }
    body { background-color: var(--bg); color: var(--text); min-height: 100vh; display: flex; flex-direction: column; }
    header { background: var(--header-bg); padding: 14px 24px; border-bottom: 1px solid var(--border); display: flex; align-items: center; justify-content: space-between; }
    .brand { display: flex; align-items: center; gap: 12px; font-weight: 600; font-size: 1.15rem; }
    .badge-live { display: inline-flex; align-items: center; gap: 6px; background: rgba(0, 168, 132, 0.15); color: var(--accent); padding: 4px 10px; border-radius: 9999px; font-size: 0.8rem; font-weight: 500; }
    .badge-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--accent); }
    main { max-width: 900px; width: 100%; margin: 24px auto; padding: 0 16px; display: grid; grid-template-columns: 1fr; gap: 20px; }
    .card { background: var(--card-bg); border-radius: 10px; border: 1px solid var(--border); overflow: hidden; }
    .card-header { padding: 14px 20px; background: var(--header-bg); border-bottom: 1px solid var(--border); font-size: 0.95rem; font-weight: 600; display: flex; justify-content: space-between; align-items: center; }
    .card-body { padding: 20px; }
    .chat-box { height: 320px; overflow-y: auto; display: flex; flex-direction: column; gap: 10px; padding: 10px; background: #0c1317; border-radius: 8px; margin-bottom: 14px; }
    .msg { max-width: 75%; padding: 8px 12px; border-radius: 8px; font-size: 0.92rem; line-height: 1.4; word-break: break-word; }
    .msg-bot { background: var(--chat-in); align-self: flex-start; color: var(--text); }
    .msg-user { background: var(--chat-out); align-self: flex-end; color: #fff; }
    .input-row { display: flex; gap: 8px; }
    input[type="text"] { flex: 1; background: var(--header-bg); border: 1px solid var(--border); color: var(--text); padding: 10px 14px; border-radius: 8px; font-size: 0.92rem; outline: none; }
    input[type="text"]:focus { border-color: var(--accent); }
    button { background: var(--accent); color: #111b21; border: none; padding: 10px 18px; border-radius: 8px; font-weight: 600; cursor: pointer; transition: background 0.15s; }
    button:hover { background: var(--accent-hover); }
    .button-secondary { background: var(--header-bg); color: var(--text); border: 1px solid var(--border); }
    .button-secondary:hover { background: #2a3942; }
    .chips { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 10px; }
    .chip { background: var(--header-bg); color: var(--text-muted); padding: 4px 10px; border-radius: 6px; font-size: 0.8rem; cursor: pointer; border: 1px solid var(--border); }
    .chip:hover { color: var(--text); border-color: var(--accent); }
    pre { background: #0c1317; padding: 12px; border-radius: 8px; font-size: 0.82rem; overflow-x: auto; color: #aebac1; font-family: monospace; }
    .status-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 12px; margin-bottom: 16px; }
    .status-item { background: var(--header-bg); padding: 12px 16px; border-radius: 8px; }
    .status-label { font-size: 0.75rem; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.5px; }
    .status-val { font-size: 1.1rem; font-weight: 600; margin-top: 4px; color: var(--accent); }
  </style>
</head>
<body>
  <header>
    <div class="brand">
      <span>WhatsApp Assistant Bot</span>
      <span class="badge-live"><span class="badge-dot"></span> Online</span>
    </div>
    <div>
      <a href="/ping" target="_blank" style="color: var(--accent); text-decoration: none; font-size: 0.85rem; font-weight: 500;">Health: /ping ↗</a>
    </div>
  </header>

  <main>
    <div class="card">
      <div class="card-header">
        <span>Live Bot Simulator & Chat</span>
        <span id="botStateLabel" style="font-size: 0.82rem; color: var(--accent);">Status: Active</span>
      </div>
      <div class="card-body">
        <div class="chat-box" id="chatBox">
          <div class="msg msg-bot">👋 Hello! WhatsApp Bot & Server are running! Test commands like <code>!bot off</code>, <code>!bot on</code>, <code>!bot reload</code> or chat directly.</div>
        </div>
        <form class="input-row" id="chatForm">
          <input type="text" id="msgInput" placeholder="Type a message or command (e.g., !bot off, hey what do you do?)" required autocomplete="off" />
          <button type="submit" id="sendBtn">Send</button>
        </form>
        <div class="chips">
          <span class="chip" onclick="quickSend('!bot off')">!bot off</span>
          <span class="chip" onclick="quickSend('!bot on')">!bot on</span>
          <span class="chip" onclick="quickSend('!bot reload')">!bot reload</span>
          <span class="chip" onclick="quickSend('Are you an AI?')">Are you an AI?</span>
          <span class="chip" onclick="quickSend('Can we meet tomorrow at 3pm?')">Can we meet tomorrow?</span>
        </div>
      </div>
    </div>

    <div class="card">
      <div class="card-header">
        <span>Server & Bot Runtime Status</span>
        <button class="button-secondary" onclick="checkStatus()" style="font-size: 0.78rem; padding: 4px 10px;">Refresh</button>
      </div>
      <div class="card-body">
        <div class="status-grid">
          <div class="status-item">
            <div class="status-label">Engine</div>
            <div class="status-val">Node.js Express</div>
          </div>
          <div class="status-item">
            <div class="status-label">Port</div>
            <div class="status-val">${PORT}</div>
          </div>
          <div class="status-item">
            <div class="status-label">LLM Provider</div>
            <div class="status-val" id="llmStatus">OpenAI GPT-4o-mini</div>
          </div>
          <div class="status-item">
            <div class="status-label">Memory</div>
            <div class="status-val">Last 10 msgs (In-Memory)</div>
          </div>
        </div>
        <div style="font-size: 0.85rem; color: var(--text-muted); margin-bottom: 6px;">Active System Prompt (from instructions.md):</div>
        <pre id="promptPreview">Loading instructions...</pre>
      </div>
    </div>
  </main>

  <script>
    const chatBox = document.getElementById('chatBox');
    const chatForm = document.getElementById('chatForm');
    const msgInput = document.getElementById('msgInput');
    const sendBtn = document.getElementById('sendBtn');
    const senderId = "test_user_1";

    async function checkStatus() {
      try {
        const res = await fetch('/api/status');
        const data = await res.json();
        const instRes = await fetch('/api/instructions');
        const instData = await instRes.json();
        document.getElementById('promptPreview').innerText = instData.instructions || "None";
        document.getElementById('llmStatus').innerText = data.hasApiKey ? "OpenAI GPT-4o-mini" : "Offline / Simulation Mode";
      } catch (e) {
        console.error(e);
      }
    }
    checkStatus();

    function appendMsg(text, type) {
      const el = document.createElement('div');
      el.className = 'msg ' + (type === 'user' ? 'msg-user' : 'msg-bot');
      el.innerText = text;
      chatBox.appendChild(el);
      chatBox.scrollTop = chatBox.scrollHeight;
    }

    async function sendMessage(text) {
      if (!text.trim()) return;
      appendMsg(text, 'user');
      msgInput.value = '';
      sendBtn.disabled = true;

      try {
        const res = await fetch('/api/message', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ sender_id: senderId, message: text, is_from_me: text.includes('!bot reload') })
        });
        const data = await res.json();
        if (data.reply) {
          appendMsg(data.reply, 'bot');
        } else if (data.note) {
          appendMsg("⏸ " + data.note, 'bot');
        }
        if (data.bot_status !== undefined) {
          document.getElementById('botStateLabel').innerText = "Status: " + (data.bot_status ? "Active" : "Paused");
          document.getElementById('botStateLabel').style.color = data.bot_status ? "var(--accent)" : "#f15c6d";
        }
      } catch (err) {
        appendMsg("Error communicating with bot server", 'bot');
      } finally {
        sendBtn.disabled = false;
        msgInput.focus();
      }
    }

    chatForm.addEventListener('submit', (e) => {
      e.preventDefault();
      sendMessage(msgInput.value);
    });

    function quickSend(txt) {
      sendMessage(txt);
    }
  </script>
</body>
</html>`);
});

// Launch server
app.listen(PORT, "0.0.0.0", () => {
  console.log(`WhatsApp Bot & Express Server running on port ${PORT}`);
});

import os
import time
import threading
import requests
from flask import Flask

# --- config ---
KICK_USERNAME = os.getenv("KICK_USERNAME")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "60"))
PORT = int(os.getenv("PORT", "10000"))

if not KICK_USERNAME or not DISCORD_WEBHOOK_URL:
    raise SystemExit("Missing env vars: KICK_USERNAME and/or DISCORD_WEBHOOK_URL")

# --- tiny web server (keeps Render Web Service happy) ---
app = Flask(__name__)

@app.get("/")
def home():
    return "kick-live-bot is running", 200

# --- bot logic ---
was_live = False

def is_live() -> bool:
    url = f"https://kick.com/api/v2/channels/{KICK_USERNAME}"
    r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
    r.raise_for_status()
    data = r.json()
    return data.get("livestream") is not None

def send_message():
    payload = {"content": f"🔴 **LIVE NOW!**\nhttps://kick.com/{KICK_USERNAME}"}
    r = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=20)
    r.raise_for_status()

def bot_loop():
    global was_live
    while True:
        try:
            live = is_live()

            if live and not was_live:
                send_message()
                was_live = True

            if not live and was_live:
                was_live = False

        except Exception as e:
            print("Bot error:", repr(e))

        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    # run bot in background thread
    threading.Thread(target=bot_loop, daemon=True).start()

    # run web server in main thread (binds PORT for Render)
    app.run(host="0.0.0.0", port=PORT)

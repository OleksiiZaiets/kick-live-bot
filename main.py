import os
import time
import threading
import requests
from flask import Flask

KICK_USERNAME = os.getenv("KICK_USERNAME")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")
ROLE_ID = os.getenv("ROLE_ID", "").strip()
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "60"))
PORT = int(os.getenv("PORT", "10000"))

if not KICK_USERNAME or not DISCORD_WEBHOOK_URL:
    raise SystemExit("Missing env vars: KICK_USERNAME and/or DISCORD_WEBHOOK_URL")

app = Flask(__name__)

@app.get("/")
def home():
    return "kick-live-bot is running", 200

was_live = False
last_live_id = None

def fetch_channel():
    url = f"https://kick.com/api/v2/channels/{KICK_USERNAME}"
    r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
    r.raise_for_status()
    return r.json()

def build_message(data: dict):
    livestream = data.get("livestream") or {}
    live_id = livestream.get("id")

    title = (livestream.get("session_title") or livestream.get("title") or "").strip()

    category = ""
    cats = livestream.get("categories")
    if isinstance(cats, list) and len(cats) > 0 and isinstance(cats[0], dict):
        category = (cats[0].get("name") or "").strip()

    kick_url = f"https://kick.com/{KICK_USERNAME}"
    ping = f"<@&{ROLE_ID}> " if ROLE_ID else ""
    content = f"{ping}🔴 **LIVE NOW**\n{kick_url}"

    desc_lines = []
    if title:
        desc_lines.append(f"**Title:** {title}")
    if category:
        desc_lines.append(f"**Category:** {category}")

    embed = {
        "title": f"{KICK_USERNAME} is live on Kick!",
        "url": kick_url,
        "description": "\n".join(desc_lines) if desc_lines else None,
    }
    embed = {k: v for k, v in embed.items() if v is not None}

    return live_id, content, embed

def send_discord(content: str, embed: dict):
    payload = {"content": content, "embeds": [embed]}
    r = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=20)
    r.raise_for_status()

def bot_loop():
    global was_live, last_live_id

    while True:
        try:
            data = fetch_channel()
            livestream = data.get("livestream")
            is_live = isinstance(livestream, dict) and livestream.get("id") is not None

            if is_live:
                live_id, content, embed = build_message(data)

                # announce only once per live session
                if (not was_live) or (live_id and live_id != last_live_id):
                    send_discord(content, embed)
                    was_live = True
                    last_live_id = live_id
            else:
                was_live = False
                last_live_id = None

        except Exception as e:
            print("Bot error:", repr(e))

        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    threading.Thread(target=bot_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT)

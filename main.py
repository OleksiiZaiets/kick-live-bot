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
OFFLINE_RESET_SECONDS = int(os.getenv("OFFLINE_RESET_SECONDS", "30"))

if not KICK_USERNAME or not DISCORD_WEBHOOK_URL:
    raise SystemExit("Missing env vars: KICK_USERNAME and/or DISCORD_WEBHOOK_URL")

app = Flask(__name__)


@app.get("/")
def home():
    return "kick-live-bot is running", 200


# ✅ TEST ENDPOINT (checks Discord webhook without needing a stream)
@app.get("/test")
def test():
    kick_url = f"https://kick.com/{KICK_USERNAME}"
    ping = f"<@&{ROLE_ID}> " if ROLE_ID else ""
    payload = {"content": f"{ping}🧪 TEST ALERT\n{kick_url}"}
    r = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=20)
    return f"sent: {r.status_code}", 200


# ✅ DEBUG ENDPOINT (checks what Kick API currently returns)
@app.get("/debug")
def debug():
    try:
        data = fetch_channel()
        livestream = data.get("livestream")
        is_live = isinstance(livestream, dict) and livestream.get("id") is not None

        return {
            "kick_username": KICK_USERNAME,
            "is_live": is_live,
            "live_id": (livestream or {}).get("id"),
            "session_title": (livestream or {}).get("session_title") or (livestream or {}).get("title"),
            "categories": (livestream or {}).get("categories"),
        }, 200
    except Exception as e:
        return {"error": repr(e)}, 500


announced_this_session = False
offline_since = None


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
    global announced_this_session, offline_since

    while True:
        try:
            data = fetch_channel()
            livestream = data.get("livestream")
            is_live = isinstance(livestream, dict) and livestream.get("id") is not None

            # ✅ DEBUG LOG (you'll see this in Render Logs)
            print("DEBUG is_live:", is_live, "live_id:", (livestream or {}).get("id"))

            now = time.time()

            if is_live:
                live_id, content, embed = build_message(data)

                # If offline long enough -> treat as a new session
                if offline_since and (now - offline_since >= OFFLINE_RESET_SECONDS):
                    announced_this_session = False

                offline_since = None

                # Announce once per detected live session (even if bot starts after stream began)
                if not announced_this_session:
                    print("DEBUG sending discord alert...")
                    send_discord(content, embed)
                    announced_this_session = True

            else:
                # Start offline timer
                if offline_since is None:
                    offline_since = now
                announced_this_session = False

        except Exception as e:
            print("Bot error:", repr(e))

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    threading.Thread(target=bot_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT)

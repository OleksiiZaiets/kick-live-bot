import os
import time
import requests

KICK_USERNAME = os.getenv("KICK_USERNAME")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")

if not KICK_USERNAME or not DISCORD_WEBHOOK_URL:
    raise SystemExit("Missing env vars: KICK_USERNAME and/or DISCORD_WEBHOOK_URL")
    
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "60"))

was_live = False

def is_live():
    url = f"https://kick.com/api/v2/channels/{KICK_USERNAME}"
    r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"})
    data = r.json()
    return data.get("livestream") is not None

def send_message():
    payload = {
        "content": f"🔴 **LIVE NOW!**\nhttps://kick.com/{KICK_USERNAME}"
    }
    requests.post(DISCORD_WEBHOOK_URL, json=payload)

while True:
    try:
        live = is_live()

        if live and not was_live:
            send_message()
            was_live = True

        if not live and was_live:
            was_live = False

    except Exception as e:
        print("Error:", e)

    time.sleep(CHECK_INTERVAL)

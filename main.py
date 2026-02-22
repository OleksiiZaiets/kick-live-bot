import os
import time
import threading
import requests
from flask import Flask, request

# ------------------- ENV -------------------
KICK_USERNAME = os.getenv("KICK_USERNAME", "").strip()
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
ROLE_ID = os.getenv("ROLE_ID", "").strip()

KICK_CLIENT_ID = os.getenv("KICK_CLIENT_ID", "").strip()
KICK_CLIENT_SECRET = os.getenv("KICK_CLIENT_SECRET", "").strip()

CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "60"))
PORT = int(os.getenv("PORT", "10000"))
OFFLINE_RESET_SECONDS = int(os.getenv("OFFLINE_RESET_SECONDS", "300"))  # 5 min default

if not KICK_USERNAME or not DISCORD_WEBHOOK_URL:
    raise SystemExit("Missing env vars: KICK_USERNAME and/or DISCORD_WEBHOOK_URL")

if not KICK_CLIENT_ID or not KICK_CLIENT_SECRET:
    raise SystemExit("Missing env vars: KICK_CLIENT_ID and/or KICK_CLIENT_SECRET")

app = Flask(__name__)

# ------------------- KICK TOKEN CACHE -------------------
_kick_token = None
_kick_token_exp = 0  # epoch seconds

# ------------------- /test cooldown -------------------
_last_test_at = 0.0


def get_app_token() -> str:
    """
    App Access Token (client_credentials)
    """
    global _kick_token, _kick_token_exp

    now = time.time()
    if _kick_token and now < (_kick_token_exp - 30):
        return _kick_token

    url = "https://id.kick.com/oauth/token"
    data = {
        "grant_type": "client_credentials",
        "client_id": KICK_CLIENT_ID,
        "client_secret": KICK_CLIENT_SECRET,
    }

    r = requests.post(url, data=data, timeout=20)
    if r.status_code >= 400:
        raise RuntimeError(f"Kick token error {r.status_code}: {r.text[:300]}")

    js = r.json()
    token = js.get("access_token")
    expires_in = int(js.get("expires_in", 3600))

    if not token:
        raise RuntimeError(f"Kick token missing in response: {js}")

    _kick_token = token
    _kick_token_exp = now + expires_in
    return _kick_token


def fetch_channel_official():
    """
    Official Kick Public API: GET /public/v1/channels?slug=...
    """
    token = get_app_token()
    url = "https://api.kick.com/public/v1/channels"
    params = {"slug": KICK_USERNAME}
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": "kick-live-bot/1.0",
    }

    r = requests.get(url, params=params, headers=headers, timeout=20)
    if r.status_code >= 400:
        raise RuntimeError(f"Kick API error {r.status_code}: {r.text[:300]}")

    js = r.json()
    data = js.get("data") or []
    if not data:
        raise RuntimeError(f"No channel data returned for slug={KICK_USERNAME}: {js}")

    return data[0]


def ping_prefix() -> str:
    return f"<@&{ROLE_ID}> " if ROLE_ID else ""


def send_discord(content: str, embed: dict | None = None) -> int:
    """
    Sends a Discord webhook message.
    - Adds headers
    - Retries once on 429 with backoff
    """
    payload = {"content": content}
    if embed:
        payload["embeds"] = [embed]

    headers = {
        "User-Agent": "kick-live-bot/1.0 (+https://kick-live-bot.onrender.com)",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }

    r = requests.post(DISCORD_WEBHOOK_URL, json=payload, headers=headers, timeout=20)

    # Rate limited
    if r.status_code == 429:
        retry_after = 10.0
        # Discord normally returns JSON with retry_after;
        # Cloudflare pages are HTML, so json() may fail.
        try:
            js = r.json()
            retry_after = float(js.get("retry_after", retry_after))
        except Exception:
            pass

        print(f"DEBUG Discord 429. Sleeping {retry_after}s then retrying once...")
        time.sleep(retry_after)
        r = requests.post(DISCORD_WEBHOOK_URL, json=payload, headers=headers, timeout=20)

    if r.status_code >= 400:
        raise RuntimeError(f"Discord webhook error {r.status_code}: {r.text[:300]}")

    return r.status_code


def build_message(channel: dict):
    stream = channel.get("stream") or {}
    is_live = bool(stream.get("is_live"))

    # ✅ IMPORTANT: session_key only if live (so no "0001-01-01...")
    session_key = (stream.get("start_time") or "").strip() if is_live else None

    title = (channel.get("stream_title") or "").strip()
    category = ((channel.get("category") or {}).get("name") or "").strip()

    kick_url = f"https://kick.com/{KICK_USERNAME}"
    content = f"{ping_prefix()}🔴 **LIVE NOW**\n{kick_url}"

    lines = []
    if title:
        lines.append(f"**Title:** {title}")
    if category:
        lines.append(f"**Category:** {category}")

    embed = {
        "title": f"{KICK_USERNAME} is live on Kick!",
        "url": kick_url,
        "description": "\n".join(lines) if lines else None,
    }
    embed = {k: v for k, v in embed.items() if v is not None}

    return is_live, session_key, content, embed


# ------------------- ROUTES -------------------
@app.get("/")
def home():
    return "kick-live-bot is running", 200


@app.get("/health")
def health():
    return {"status": "ok", "kick_username": KICK_USERNAME}, 200


@app.get("/test")
def test():
    """
    Test webhook with cooldown to avoid accidental spamming / extending rate limits.
    """
    global _last_test_at
    now = time.time()

    if now - _last_test_at < 60:
        return {"ok": False, "error": "Test cooldown 60s. Try again later."}, 429

    _last_test_at = now

    try:
        kick_url = f"https://kick.com/{KICK_USERNAME}"
        status = send_discord(f"{ping_prefix()}🧪 TEST ALERT\n{kick_url}")
        return {"ok": True, "sent_status": status}, 200
    except Exception as e:
        return {"ok": False, "error": repr(e)}, 500


@app.get("/debug")
def debug():
    try:
        ch = fetch_channel_official()
        stream = ch.get("stream") or {}
        is_live = bool(stream.get("is_live"))

        return {
            "kick_username": KICK_USERNAME,
            "is_live": is_live,
            "viewer_count": stream.get("viewer_count", 0) or 0,
            "stream_title": ch.get("stream_title") or "",
            "category": (ch.get("category") or {}).get("name") or "",
            # ✅ show start_time only if live (avoids "0001-01-01..." confusion)
            "start_time": stream.get("start_time") if is_live else None,
        }, 200
    except Exception as e:
        return {"error": repr(e)}, 500


# Optional: so your Redirect URL doesn't 404 (not needed for client_credentials, but harmless)
@app.get("/callback")
def callback():
    return {"ok": True, "note": "callback endpoint exists (not used in client_credentials)"}, 200


# ------------------- LOOP STATE -------------------
announced_this_session = False
offline_since = None
last_session_key = None


def bot_loop():
    global announced_this_session, offline_since, last_session_key

    while True:
        try:
            ch = fetch_channel_official()
            is_live, session_key, content, embed = build_message(ch)

            print("DEBUG is_live:", is_live, "session_key:", session_key)

            now = time.time()

            if is_live:
                # if it was offline for long enough -> allow new announce
                if offline_since and (now - offline_since >= OFFLINE_RESET_SECONDS):
                    announced_this_session = False
                    last_session_key = None

                offline_since = None

                # announce once per live session (or if session_key changes)
                if (not announced_this_session) or (session_key and session_key != last_session_key):
                    print("DEBUG sending discord alert...")
                    send_discord(content, embed)
                    announced_this_session = True
                    last_session_key = session_key
            else:
                if offline_since is None:
                    offline_since = now
                announced_this_session = False
                last_session_key = None

        except Exception as e:
            print("Bot error:", repr(e))

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    threading.Thread(target=bot_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT)

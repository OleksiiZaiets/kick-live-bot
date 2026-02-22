import os
import time
import threading
import requests
from urllib.parse import urlencode
from flask import Flask, request

# =========================
# ENV
# =========================
KICK_USERNAME = os.getenv("KICK_USERNAME", "").strip()
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
ROLE_ID = os.getenv("ROLE_ID", "").strip()

CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "60"))
PORT = int(os.getenv("PORT", "10000"))

# "Cooldown" після офлайну (5 хв = 300)
OFFLINE_RESET_SECONDS = int(os.getenv("OFFLINE_RESET_SECONDS", "300"))

# Kick OAuth
KICK_CLIENT_ID = os.getenv("KICK_CLIENT_ID", "").strip()
KICK_CLIENT_SECRET = os.getenv("KICK_CLIENT_SECRET", "").strip()
KICK_REDIRECT_URI = os.getenv("KICK_REDIRECT_URI", "").strip()
KICK_REFRESH_TOKEN = os.getenv("KICK_REFRESH_TOKEN", "").strip()

if not KICK_USERNAME or not DISCORD_WEBHOOK_URL:
    raise SystemExit("Missing env vars: KICK_USERNAME and/or DISCORD_WEBHOOK_URL")

app = Flask(__name__)

# =========================
# OAuth token cache (RAM)
# =========================
_access_token = None
_access_token_expires_at = 0  # unix seconds

# Session state (RAM)
announced_this_session = False
offline_since = None
last_session_key = None  # helps avoid double announce while live

# =========================
# Helpers
# =========================
def discord_ping_prefix() -> str:
    return f"<@&{ROLE_ID}> " if ROLE_ID else ""

def send_discord(content: str, embed: dict | None = None):
    payload = {"content": content}
    if embed:
        payload["embeds"] = [embed]
    r = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=20)
    r.raise_for_status()
    return r.status_code

def token_is_valid() -> bool:
    # refresh a bit earlier than expiry
    return _access_token is not None and time.time() < (_access_token_expires_at - 30)

def refresh_access_token():
    """
    Uses refresh_token grant to get a new access token.
    Kick docs: token URL is https://id.kick.com/oauth/token :contentReference[oaicite:1]{index=1}
    """
    global _access_token, _access_token_expires_at

    if not KICK_CLIENT_ID or not KICK_CLIENT_SECRET:
        raise RuntimeError("Missing KICK_CLIENT_ID / KICK_CLIENT_SECRET env vars")
    if not KICK_REFRESH_TOKEN:
        raise RuntimeError("Missing KICK_REFRESH_TOKEN env var. Visit /auth first to generate it.")

    token_url = "https://id.kick.com/oauth/token"
    data = {
        "grant_type": "refresh_token",
        "refresh_token": KICK_REFRESH_TOKEN,
        "client_id": KICK_CLIENT_ID,
        "client_secret": KICK_CLIENT_SECRET,
        "redirect_uri": KICK_REDIRECT_URI,
    }

    r = requests.post(token_url, data=data, timeout=20)
    r.raise_for_status()
    js = r.json()

    _access_token = js.get("access_token")
    expires_in = int(js.get("expires_in", 3600))
    _access_token_expires_at = int(time.time()) + expires_in

    if not _access_token:
        raise RuntimeError(f"Token response missing access_token: {js}")

def get_access_token() -> str:
    if token_is_valid():
        return _access_token
    refresh_access_token()
    return _access_token

def fetch_channel_official():
    """
    Official endpoint:
    GET https://api.kick.com/public/v1/channels?slug=...
    Requires scope: channel:read :contentReference[oaicite:2]{index=2}
    """
    token = get_access_token()
    url = "https://api.kick.com/public/v1/channels"
    params = {"slug": KICK_USERNAME}
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }

    r = requests.get(url, params=params, headers=headers, timeout=20)
    r.raise_for_status()
    js = r.json()

    data = (js.get("data") or [])
    if not data:
        raise RuntimeError(f"No channel data returned for slug={KICK_USERNAME}. Response: {js}")

    return data[0]  # channel object

def build_message_from_channel(channel: dict):
    """
    Channel response contains:
    - channel['stream']['is_live']
    - channel['stream_title']
    - channel['category']['name']
    - channel['stream']['start_time']
    """
    kick_url = f"https://kick.com/{KICK_USERNAME}"

    title = (channel.get("stream_title") or "").strip()
    category = ""
    cat = channel.get("category") or {}
    if isinstance(cat, dict):
        category = (cat.get("name") or "").strip()

    stream = channel.get("stream") or {}
    start_time = (stream.get("start_time") or "").strip()  # used as session key when available

    content = f"{discord_ping_prefix()}🔴 **LIVE NOW**\n{kick_url}"

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

    # session key: prefer start_time; fallback to title+category
    session_key = start_time or f"{title}|{category}"

    return session_key, content, embed

# =========================
# Routes
# =========================
@app.get("/")
def home():
    return "kick-live-bot is running", 200

@app.get("/test")
def test():
    kick_url = f"https://kick.com/{KICK_USERNAME}"
    status = send_discord(f"{discord_ping_prefix()}🧪 TEST ALERT\n{kick_url}")
    return f"sent: {status}", 200

@app.get("/debug")
def debug():
    try:
        channel = fetch_channel_official()
        stream = channel.get("stream") or {}
        is_live = bool(stream.get("is_live"))
        return {
            "kick_username": KICK_USERNAME,
            "is_live": is_live,
            "start_time": stream.get("start_time"),
            "viewer_count": stream.get("viewer_count"),
            "stream_title": channel.get("stream_title"),
            "category": (channel.get("category") or {}).get("name"),
        }, 200
    except Exception as e:
        return {"error": repr(e)}, 500

@app.get("/auth")
def auth():
    """
    Starts OAuth authorization_code flow.
    Docs: Authorization URL is https://id.kick.com/oauth/authorize :contentReference[oaicite:3]{index=3}
    """
    if not KICK_CLIENT_ID or not KICK_REDIRECT_URI:
        return {"error": "Missing KICK_CLIENT_ID or KICK_REDIRECT_URI env vars"}, 500

    params = {
        "client_id": KICK_CLIENT_ID,
        "redirect_uri": KICK_REDIRECT_URI,
        "response_type": "code",
        "scope": "channel:read",
    }
    return (f"Open this URL to authorize:\n\nhttps://id.kick.com/oauth/authorize?{urlencode(params)}\n", 200)

@app.get("/callback")
def callback():
    """
    Receives ?code=..., exchanges it for tokens.
    Token URL: https://id.kick.com/oauth/token :contentReference[oaicite:4]{index=4}
    """
    code = (request.args.get("code") or "").strip()
    if not code:
        return {"error": "Missing code param"}, 400

    if not KICK_CLIENT_ID or not KICK_CLIENT_SECRET or not KICK_REDIRECT_URI:
        return {"error": "Missing KICK_CLIENT_ID / KICK_CLIENT_SECRET / KICK_REDIRECT_URI env vars"}, 500

    token_url = "https://id.kick.com/oauth/token"
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": KICK_CLIENT_ID,
        "client_secret": KICK_CLIENT_SECRET,
        "redirect_uri": KICK_REDIRECT_URI,
    }

    r = requests.post(token_url, data=data, timeout=20)
    r.raise_for_status()
    js = r.json()

    refresh_token = js.get("refresh_token")
    access_token = js.get("access_token")

    if not refresh_token:
        return {"error": "No refresh_token in response", "response": js}, 500

    # cache access token immediately
    global _access_token, _access_token_expires_at
    _access_token = access_token
    _access_token_expires_at = int(time.time()) + int(js.get("expires_in", 3600))

    # IMPORTANT: user must copy refresh token to Render env var
    return (
        "✅ Authorized!\n\n"
        "Now copy this refresh token into Render Environment variable KICK_REFRESH_TOKEN:\n\n"
        f"{refresh_token}\n\n"
        "Then redeploy the service.\n",
        200,
    )

# =========================
# Bot loop
# =========================
def bot_loop():
    global announced_this_session, offline_since, last_session_key

    while True:
        try:
            channel = fetch_channel_official()
            stream = channel.get("stream") or {}
            is_live = bool(stream.get("is_live"))

            # DEBUG logs
            print("DEBUG is_live:", is_live, "start_time:", stream.get("start_time"), "viewer_count:", stream.get("viewer_count"))

            now = time.time()

            if is_live:
                session_key, content, embed = build_message_from_channel(channel)

                # If we were offline for long enough, allow announcing again
                if offline_since and (now - offline_since >= OFFLINE_RESET_SECONDS):
                    announced_this_session = False
                    last_session_key = None

                offline_since = None

                # Announce once per session (best-effort)
                if (not announced_this_session) or (last_session_key and session_key != last_session_key):
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

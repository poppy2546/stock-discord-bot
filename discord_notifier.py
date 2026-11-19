import os
import sys
import time
import urllib.request
import urllib.error
import urllib.parse
import json

def _load_env():
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    if os.path.exists(env_path):
        try:
            with open(env_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if "=" in line:
                        k, v = line.split("=", 1)
                        k = k.strip()
                        v = v.strip().strip("'\"")
                        if k and k not in os.environ:
                            os.environ[k] = v
        except Exception:
            pass

_load_env()
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")

def _chunk_text(text, max_len=1900):
    chunks = []
    lines = text.split("\n")
    current_chunk = []
    current_len = 0
    for line in lines:
        if current_len + len(line) + 1 > max_len:
            chunks.append("\n".join(current_chunk))
            current_chunk = [line]
            current_len = len(line)
        else:
            current_chunk.append(line)
            current_len += len(line) + 1
    if current_chunk:
        chunks.append("\n".join(current_chunk))
    return chunks

def _safe_print(msg):
    try:
        print(msg)
    except UnicodeEncodeError:
        try:
            print(msg.encode(sys.stdout.encoding or 'utf-8', errors='replace').decode(sys.stdout.encoding or 'utf-8'))
        except Exception:
            pass

_PRIVATE_MARKERS = (
    "💼 ข้อมูลต้นทุนและพอร์ตของคุณ",   # รอบ 23:00
    "💼 สรุปภาพรวมพอร์ตโฟลิโอของคุณ",  # รอบ 07:00
    "🧮 จำลองผลหลังถัวเฉลี่ย",          # DCA Simulator (รอบ 20:00 ไม่มีหัวพอร์ต)
)

def _strip_private_portfolio(text):
    """ตัดส่วนข้อมูลพอร์ตส่วนตัวออก (เก็บส่วนหมายเหตุ 📌 ท้ายรายงานไว้)"""
    idxs = [text.find(m) for m in _PRIVATE_MARKERS if m in text]
    if not idxs:
        return text
    start = min(idxs)
    tail_idx = text.find("\n📌", start)
    tail = text[tail_idx:] if tail_idx != -1 else ""
    return (text[:start].rstrip() + "\n" + tail).strip()

_ALLOWED_WEBHOOK_HOSTS = {
    "discord.com",
    "discordapp.com",
    "canary.discord.com",
    "ptb.discord.com",
}


def _is_valid_discord_webhook_url(url: str) -> bool:
    if not url or not isinstance(url, str):
        return False
    try:
        parsed = urllib.parse.urlparse(url.strip())
        return (
            parsed.scheme == "https"
            and (parsed.hostname or "").lower() in _ALLOWED_WEBHOOK_HOSTS
            and parsed.path.startswith("/api/webhooks/")
        )
    except Exception:
        return False


def _post_discord_json(url, payload, headers, max_retries=3):
    data = json.dumps(payload).encode("utf-8")
    for attempt in range(max_retries):
        try:
            req = urllib.request.Request(url, data=data, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=10) as res:
                return json.loads(res.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < max_retries - 1:
                retry_after = 1.5
                try:
                    body = json.loads(e.read().decode("utf-8") or "{}")
                    retry_after = float(body.get("retry_after", 1.5)) + 0.2
                except Exception:
                    pass
                finally:
                    try:
                        e.close()
                    except Exception:
                        pass
                time.sleep(min(retry_after, 10.0))
                continue
            try:
                e.close()
            except Exception:
                pass
            raise
    return None

def send_discord_dm(recipient_id, message_text, bot_token=None):
    token = bot_token or os.getenv("DISCORD_BOT_TOKEN")
    if not token or not recipient_id:
        _safe_print("⚠️ ขาด DISCORD_BOT_TOKEN หรือ DISCORD_OWNER_ID สำหรับส่ง DM ส่วนตัว")
        return False

    headers = {
        "Authorization": f"Bot {token}",
        "Content-Type": "application/json",
        "User-Agent": "StockAlertBot/1.0"
    }

    channel_id = None
    try:
        res_data = _post_discord_json(
            "https://discord.com/api/v10/users/@me/channels",
            {"recipient_id": str(recipient_id)},
            headers
        )
        channel_id = (res_data or {}).get("id")
    except Exception as e:
        _safe_print(f"❌ Failed to create Discord DM channel for {recipient_id}: {e}")
        return False

    if not channel_id:
        return False

    chunks = _chunk_text(message_text)
    success = True
    for idx, chunk in enumerate(chunks):
        try:
            if idx > 0:
                time.sleep(0.35)
            _post_discord_json(
                f"https://discord.com/api/v10/channels/{channel_id}/messages",
                {"content": chunk},
                headers
            )
        except Exception as e:
            _safe_print(f"❌ Failed to send Discord DM chunk: {e}")
            success = False

    return success

def send_discord_webhook(report_text, webhook_url=None):
    url = (webhook_url or os.getenv("DISCORD_WEBHOOK_URL") or "").strip()
    if not url:
        return False
    if not _is_valid_discord_webhook_url(url):
        _safe_print("⚠️ Invalid DISCORD_WEBHOOK_URL: must be an https://discord.com/api/webhooks/... URL")
        return False

    headers = {
        "Content-Type": "application/json",
        "User-Agent": "StockAlertBot/1.0"
    }

    chunks = _chunk_text(report_text)
    success = True
    for idx, chunk in enumerate(chunks):
        try:
            if idx > 0:
                time.sleep(0.35)
            _post_discord_json(url, {"content": chunk}, headers)
        except Exception as e:
            _safe_print(f"Failed to send Discord webhook chunk: {e}")
            success = False
    return success

def send_discord_notification(report_text, buys=None, macro=None):
    _load_env()
    bot_token = os.getenv("DISCORD_BOT_TOKEN")
    owner_id = os.getenv("DISCORD_OWNER_ID")
    webhook_url = os.getenv("DISCORD_WEBHOOK_URL")

    notify_private = os.getenv("DISCORD_NOTIFY_PRIVATE", "true").lower() in ("true", "1", "yes")
    send_public_webhook = os.getenv("DISCORD_SEND_PUBLIC_WEBHOOK", "false").lower() in ("true", "1", "yes")

    sent_dm = False
    if notify_private and bot_token and owner_id:
        sent_dm = send_discord_dm(owner_id, report_text, bot_token=bot_token)
        if sent_dm:
            _safe_print(f"✅ ส่งรายงานหุ้นแบบส่วนตัวเข้า Discord DM (User: {owner_id}) สำเร็จ")

    if webhook_url:
        if not notify_private or not sent_dm:
            # Fallback or private mode disabled: strip portfolio unless explicitly allowed
            include_port = os.getenv("DISCORD_WEBHOOK_INCLUDE_PORTFOLIO", "false").lower() in ("true", "1", "yes")
            text = report_text if include_port else _strip_private_portfolio(report_text)
            send_discord_webhook(text, webhook_url)
        elif send_public_webhook:
            # Also send public scan summary to webhook (without private portfolio)
            public_text = _strip_private_portfolio(report_text)
            send_discord_webhook(public_text, webhook_url)
        else:
            _safe_print("ℹ️ โหมดส่วนตัวเปิดใช้งาน: ส่งเข้า DM เรียบร้อย (ไม่โพสต์ข้อมูลพอร์ตลงห้องสาธารณะ)")

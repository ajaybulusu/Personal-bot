"""
Executive Assistant Telegram Bot — v5 (Railway)
================================================
- Runs 24/7 on Railway
- Handles all Telegram Q&A, voice notes, calendar queries
- Morning briefing is sent by Manus scheduled task (8am SGT daily)
- Simple health-check HTTP server on PORT so Railway stays happy
"""

import os, sys, json, time, logging, threading, traceback, queue, tempfile, re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler

from google.oauth2 import service_account
from googleapiclient.discovery import build as gcal_build
import base64, subprocess
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
from openai import OpenAI

# ── Config ────────────────────────────────────────────────────────────────────
BOT_TOKEN   = os.environ.get("BOT_TOKEN", "")
CHAT_ID     = os.environ.get("CHAT_ID", "")
OPENAI_KEYS = [k for k in [
    os.environ.get("OPENAI_KEY_PRIMARY", ""),
    os.environ.get("OPENAI_KEY_BACKUP", ""),
] if k]
if not OPENAI_KEYS:
    raise RuntimeError("No OpenAI keys set")
OPENAI_MODEL = "gpt-4o-mini"

BASE_DIR       = Path(__file__).parent
CALENDAR_CACHE = BASE_DIR / "calendar_cache.json"
STATE_FILE     = BASE_DIR / "bot_state.json"
LOG_FILE       = BASE_DIR / "bot.log"
SA_FILE        = BASE_DIR / "service_account.json"
HEARTBEAT_FILE = BASE_DIR / "bot_heartbeat"
GCAL_ID        = os.environ.get("GCAL_ID", "bulusu.ajay@gmail.com")

TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

# Write service_account.json from env var on Railway
_sa_env = os.environ.get("SERVICE_ACCOUNT_JSON", "")
if _sa_env and not SA_FILE.exists():
    SA_FILE.write_text(_sa_env)

if not CALENDAR_CACHE.exists():
    CALENDAR_CACHE.write_text("[]")

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler(sys.stdout)]
)
log = logging.getLogger(__name__)

# ── OpenAI dual-key failover ──────────────────────────────────────────────────
_key_idx = 0

def _ai():
    return OpenAI(api_key=OPENAI_KEYS[_key_idx], base_url="https://api.openai.com/v1")

# ── Serial message queue ──────────────────────────────────────────────────────
msg_queue = queue.Queue()

# ── State ─────────────────────────────────────────────────────────────────────
def load_state():
    try:    return json.loads(STATE_FILE.read_text())
    except: return {"last_update_id": 0}

def save_state(s):
    STATE_FILE.write_text(json.dumps(s))

def touch_heartbeat():
    HEARTBEAT_FILE.write_text(str(time.time()))

# ── Telegram ──────────────────────────────────────────────────────────────────
def _curl(endpoint, data=None, params_str="", timeout=15):
    url = f"{TG_API}/{endpoint}{params_str}"
    cmd = ["curl", "-s", "--max-time", str(timeout), "-X", "POST", url]
    if data:
        cmd += ["-H", "Content-Type: application/json", "-d", json.dumps(data)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout+5)
        return json.loads(r.stdout) if r.stdout else {}
    except Exception as e:
        log.warning(f"curl error: {e}")
        return {}

def tg_send(text: str):
    _curl("sendMessage", data={"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown"})

def tg_typing():
    _curl("sendChatAction", data={"chat_id": CHAT_ID, "action": "typing"})

def tg_get_updates(offset: int, timeout: int = 25):
    cmd = ["curl", "-s", "--max-time", str(timeout+5),
           f"{TG_API}/getUpdates?offset={offset}&timeout={timeout}&allowed_updates=[\"message\"]"]
    for attempt in range(3):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout+10)
            d = json.loads(r.stdout)
            if d.get("ok"):
                return d.get("result", [])
        except Exception as e:
            log.warning(f"getUpdates attempt {attempt+1}: {e}")
            time.sleep(2)
    return []

# ── Voice transcription ───────────────────────────────────────────────────────
def transcribe_voice(file_id: str):
    global _key_idx
    try:
        info = _curl("getFile", data={"file_id": file_id})
        file_path = info.get("result", {}).get("file_path", "")
        if not file_path:
            return None
        url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
        tmp = tempfile.mktemp(suffix=".ogg")
        subprocess.run(["curl", "-s", "-o", tmp, url], timeout=30)
        client = OpenAI(api_key=OPENAI_KEYS[_key_idx], base_url="https://api.openai.com/v1")
        with open(tmp, "rb") as f:
            t = client.audio.transcriptions.create(model="whisper-1", file=f, response_format="text")
        try: os.unlink(tmp)
        except: pass
        return str(t)
    except Exception as e:
        log.error(f"transcribe_voice: {e}")
        return None

# ── Google Calendar ───────────────────────────────────────────────────────────
def get_gcal():
    creds = service_account.Credentials.from_service_account_file(
        str(SA_FILE), scopes=["https://www.googleapis.com/auth/calendar"])
    return gcal_build("calendar", "v3", credentials=creds)

def gcal_list_upcoming(days=60):
    try:
        svc = get_gcal()
        now = datetime.now(timezone(timedelta(hours=8)))
        res = svc.events().list(
            calendarId=GCAL_ID,
            timeMin=now.isoformat(),
            timeMax=(now + timedelta(days=days)).isoformat(),
            maxResults=100, singleEvents=True, orderBy="startTime"
        ).execute()
        events = res.get("items", [])
        if events:
            CALENDAR_CACHE.write_text(json.dumps(events, indent=2))
        return events
    except Exception as e:
        log.warning(f"gcal_list error: {e}")
        try:    return json.loads(CALENDAR_CACHE.read_text())
        except: return []

def gcal_create(summary, date, start_time=None, end_time=None, description="", location=""):
    svc = get_gcal()
    if start_time:
        s_dt = f"{date}T{start_time}:00+08:00"
        if end_time:
            e_dt = f"{date}T{end_time}:00+08:00"
        else:
            from datetime import datetime as dt
            e_dt = (dt.fromisoformat(s_dt) + timedelta(hours=1)).isoformat()
        start = {"dateTime": s_dt, "timeZone": "Asia/Singapore"}
        end   = {"dateTime": e_dt, "timeZone": "Asia/Singapore"}
    else:
        start = end = {"date": date}

    body = {"summary": summary, "start": start, "end": end,
            "reminders": {"useDefault": False, "overrides": [
                {"method": "popup", "minutes": 30},
                {"method": "popup", "minutes": 1440}]}}
    if description: body["description"] = description
    if location:    body["location"]    = location
    return svc.events().insert(calendarId=GCAL_ID, body=body).execute()

def load_calendar():
    try:    return json.loads(CALENDAR_CACHE.read_text())
    except: return []

def fmt_calendar(events):
    if not events: return "No upcoming events."
    lines = []
    for e in events:
        s = e.get("start", {})
        dt = s.get("dateTime", s.get("date", ""))
        loc = e.get("location", "")
        lines.append(f"• {e.get('summary','')}{'  @ '+loc[:40] if loc else ''} — {dt}")
    return "\n".join(lines)

# ── GPT ───────────────────────────────────────────────────────────────────────
SYSTEM = """You are Ajay Bulusu's personal executive assistant (Singapore, SGT=UTC+8). Today: {today}.

For CALENDAR ADD requests respond with:
<calendar>
{{"summary":"...","date":"YYYY-MM-DD","start_time":"HH:MM","end_time":"HH:MM","location":"","description":""}}
</calendar>
Then add a brief confirmation.

For all other questions answer directly and concisely.
Use *bold* for emphasis, • for bullets. Flag urgent items with ⚠️.

=== CALENDAR (next 14 days) ===
{calendar}"""

def gpt(user: str, calendar_ctx: str) -> str:
    global _key_idx
    today = datetime.now(timezone(timedelta(hours=8))).strftime("%d %B %Y, %A")
    system = SYSTEM.format(today=today, calendar=calendar_ctx)
    for attempt in range(6):
        try:
            r = _ai().chat.completions.create(
                model=OPENAI_MODEL,
                messages=[{"role":"system","content":system},{"role":"user","content":user}],
                max_tokens=900, temperature=0.2
            )
            return r.choices[0].message.content.strip()
        except Exception as e:
            err = str(e)
            if "429" in err or "rate_limit" in err.lower():
                other = 1 - _key_idx
                log.warning(f"Key {_key_idx} rate-limited → switching to key {other}")
                _key_idx = other
                time.sleep(3)
            elif "401" in err or "invalid" in err.lower():
                _key_idx = 1 - _key_idx
                time.sleep(2)
            else:
                log.error(f"gpt error: {e}")
                return f"⚠️ AI error: {err[:120]}"
    return "⚠️ Both OpenAI keys are rate-limited — please try again in a minute."

# ── Message handler ───────────────────────────────────────────────────────────
def handle_message(text: str):
    try:
        tg_typing()
        tl = text.strip().lower()

        if tl in ["/start", "/help", "help"]:
            tg_send(HELP_TEXT)
            return

        # Always fetch fresh calendar
        calendar = gcal_list_upcoming(days=14)
        now_sgt  = datetime.now(timezone(timedelta(hours=8)))
        cal_14   = []
        for ev in calendar:
            try:
                s = ev.get("start", {})
                dt_str = s.get("dateTime", s.get("date", ""))
                dt = datetime.fromisoformat(dt_str)
                if not dt.tzinfo:
                    dt = dt.replace(tzinfo=timezone(timedelta(hours=8)))
                if dt <= now_sgt + timedelta(days=14):
                    cal_14.append(ev)
            except:
                cal_14.append(ev)

        cal_ctx  = fmt_calendar(cal_14)
        response = gpt(text.strip(), cal_ctx)

        if "<calendar>" in response and "</calendar>" in response:
            m = re.search(r"<calendar>(.*?)</calendar>", response, re.DOTALL)
            reply = re.sub(r"<calendar>.*?</calendar>", "", response, flags=re.DOTALL).strip()
            if m:
                try:
                    d = json.loads(m.group(1).strip())
                    gcal_create(
                        d.get("summary", text[:50]),
                        d["date"],
                        d.get("start_time"), d.get("end_time"),
                        d.get("description",""), d.get("location","")
                    )
                    t_str = f" at {d['start_time']}" if d.get("start_time") else ""
                    e_str = f"–{d['end_time']}" if d.get("end_time") else ""
                    l_str = f"\n📍 {d['location']}" if d.get("location") else ""
                    tg_send(f"✅ *Added to Google Calendar!*\n\n*{d.get('summary','')}*\n🗓 {d['date']}{t_str}{e_str}{l_str}\n\n_Event is live in your calendar._")
                    return
                except Exception as e:
                    log.error(f"Calendar create error: {e}")
                    tg_send(f"⚠️ Could not add to calendar: {str(e)[:120]}")
                    return

        tg_send(response)

    except Exception as e:
        log.error(f"handle_message: {e}\n{traceback.format_exc()}")
        tg_send(f"⚠️ Error: {str(e)[:100]}")

# ── Queue worker ──────────────────────────────────────────────────────────────
def queue_worker():
    while True:
        try:
            text = msg_queue.get(timeout=60)
            if text is None: break
            handle_message(text)
            msg_queue.task_done()
        except queue.Empty:
            pass
        except Exception as e:
            log.error(f"Queue worker: {e}")

# ── Help text ─────────────────────────────────────────────────────────────────
HELP_TEXT = """🤖 *Your Executive Assistant*

Ask me anything naturally:

💳 *Payments & Bills*
• "When is my next StarHub bill?"
• "Did my Sobha installment go through?"
• "Any failed payments?"

✈️ *Travel*
• "What are my upcoming flights?"

📊 *Investments*
• "Any new IBKR alerts?"

📅 *Calendar*
• "Add pickleball Monday 4–5pm"
• "Schedule dentist Friday 3pm"
• "What's on my calendar this week?"

🎙️ *Voice Notes*
Just send a voice message — I'll transcribe and answer!

*Commands:*
/help — This message"""

# ── Minimal HTTP health-check (Railway requires a bound port) ─────────────────
class HealthHandler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"status":"ok"}')

def start_health_server():
    port = int(os.environ.get("PORT", 8080))
    HTTPServer(("0.0.0.0", port), HealthHandler).serve_forever()

# ── Main ──────────────────────────────────────────────────────────────────────
def run():
    state  = load_state()
    offset = state.get("last_update_id", 0) + 1
    cal    = load_calendar()
    log.info(f"Bot v5 starting. Calendar events: {len(cal)}, Offset: {offset}")

    threading.Thread(target=queue_worker, daemon=True).start()
    threading.Thread(target=start_health_server, daemon=True).start()

    tg_send(
        f"🤖 *Executive Assistant v5 is online!*\n\n"
        f"✅ {len(cal)} calendar events loaded\n"
        f"✅ Direct OpenAI (dual-key failover)\n"
        f"✅ 24/7 on Railway cloud\n"
        f"✅ Daily 8am briefing via Manus\n\n"
        f"Ask me anything or type /help"
    )

    touch_heartbeat()
    consecutive_errors = 0

    while True:
        try:
            updates = tg_get_updates(offset)
            touch_heartbeat()
            consecutive_errors = 0

            for upd in updates:
                uid     = upd["update_id"]
                offset  = uid + 1
                state["last_update_id"] = uid
                save_state(state)

                msg     = upd.get("message", {})
                chat_id = str(msg.get("chat", {}).get("id", ""))
                text    = msg.get("text", "").strip()

                if chat_id != CHAT_ID:
                    continue

                voice = msg.get("voice") or msg.get("audio")
                if voice and not text:
                    fid = voice.get("file_id")
                    log.info(f"VOICE [{uid}]: {fid}")
                    def _do(fid=fid, uid=uid):
                        tg_send("🎙️ _Transcribing your voice note..._")
                        t = transcribe_voice(fid)
                        if t:
                            log.info(f"VOICE [{uid}]: {t[:80]}")
                            tg_send(f"🎙️ *You said:* _{t}_")
                            msg_queue.put(t)
                        else:
                            tg_send("⚠️ Could not transcribe — please try again or type your question.")
                    threading.Thread(target=_do, daemon=True).start()
                    continue

                if not text:
                    continue

                log.info(f"MSG [{uid}]: {text[:80]}")
                msg_queue.put(text)

        except KeyboardInterrupt:
            log.info("Bot stopped.")
            msg_queue.put(None)
            break
        except Exception as e:
            consecutive_errors += 1
            log.error(f"Poll error #{consecutive_errors}: {e}")
            time.sleep(min(60, 5 * consecutive_errors))

if __name__ == "__main__":
    run()

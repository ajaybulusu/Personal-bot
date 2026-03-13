"""
Executive Assistant Telegram Bot — v5
======================================
Architecture:
  - Runs 24/7 on Railway cloud
  - Handles all Telegram messages, voice notes, calendar queries
  - Email cache is pushed daily by Manus scheduled task (8am SGT)
  - Exposes /sync endpoint so Manus can push fresh email data
"""

import os, sys, json, time, logging, threading, traceback, queue, tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler

# Google Calendar direct API
from google.oauth2 import service_account
from googleapiclient.discovery import build as gcal_build
import base64

import subprocess
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
from openai import OpenAI

# ── Config ────────────────────────────────────────────────────────────────────
BOT_TOKEN    = os.environ.get("BOT_TOKEN", "")
CHAT_ID      = os.environ.get("CHAT_ID", "")
OPENAI_KEYS  = [
    k for k in [
        os.environ.get("OPENAI_KEY_PRIMARY", ""),
        os.environ.get("OPENAI_KEY_BACKUP", ""),
    ] if k
]
if not OPENAI_KEYS:
    raise RuntimeError("No OpenAI keys set — check OPENAI_KEY_PRIMARY env var")
OPENAI_MODEL = "gpt-4o-mini"
SYNC_SECRET  = os.environ.get("SYNC_SECRET", "ajay-bot-sync-2026")

BASE_DIR       = Path(__file__).parent
EMAIL_CACHE    = BASE_DIR / "email_cache.json"
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

# Create empty caches on first boot
for _cache in [EMAIL_CACHE, CALENDAR_CACHE]:
    if not _cache.exists():
        _cache.write_text("[]")

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger(__name__)

# ── OpenAI — dual-key failover ────────────────────────────────────────────────
_active_key_idx = 0

def _get_ai_client(key_idx=None):
    global _active_key_idx
    idx = key_idx if key_idx is not None else _active_key_idx
    return OpenAI(api_key=OPENAI_KEYS[idx], base_url="https://api.openai.com/v1")

ai = _get_ai_client(0)

# ── Serial message queue ──────────────────────────────────────────────────────
msg_queue = queue.Queue()

# ── State ─────────────────────────────────────────────────────────────────────
def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except:
        return {"last_update_id": 0}

def save_state(s):
    STATE_FILE.write_text(json.dumps(s))

def touch_heartbeat():
    HEARTBEAT_FILE.write_text(str(time.time()))

# ── Telegram via curl ─────────────────────────────────────────────────────────
def _curl_tg(endpoint, data=None, params=None, timeout=15):
    url = f"{TG_API}/{endpoint}"
    cmd = ["curl", "-s", "--max-time", str(timeout), "-X", "POST", url]
    if data:
        cmd += ["-H", "Content-Type: application/json", "-d", json.dumps(data)]
    if params:
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        url = f"{url}?{qs}"
        cmd = ["curl", "-s", "--max-time", str(timeout), url]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 5)
        return json.loads(result.stdout) if result.stdout else {}
    except Exception as e:
        log.warning(f"curl_tg error: {e}")
        return {}

def tg_send(text: str):
    _curl_tg("sendMessage", data={
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "Markdown"
    })

def tg_typing():
    _curl_tg("sendChatAction", data={"chat_id": CHAT_ID, "action": "typing"})

def tg_get_updates(offset: int, timeout: int = 25):
    cmd = [
        "curl", "-s", "--max-time", str(timeout + 5),
        f"{TG_API}/getUpdates?offset={offset}&timeout={timeout}&allowed_updates=[\"message\"]"
    ]
    for attempt in range(3):
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 10)
            data = json.loads(result.stdout)
            if data.get("ok"):
                return data.get("result", [])
        except Exception as e:
            log.warning(f"getUpdates attempt {attempt+1} error: {e}")
            time.sleep(2)
    return []

# ── Voice transcription ───────────────────────────────────────────────────────
def transcribe_voice(file_id: str) -> str | None:
    try:
        info = _curl_tg("getFile", data={"file_id": file_id})
        file_path = info.get("result", {}).get("file_path", "")
        if not file_path:
            return None
        download_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
        tmp_path = tempfile.mktemp(suffix=".ogg")
        subprocess.run(["curl", "-s", "-o", tmp_path, download_url], timeout=30)
        whisper_client = OpenAI(api_key=OPENAI_KEYS[_active_key_idx], base_url="https://api.openai.com/v1")
        with open(tmp_path, "rb") as f:
            transcript = whisper_client.audio.transcriptions.create(
                model="whisper-1", file=f, response_format="text"
            )
        try:
            os.unlink(tmp_path)
        except:
            pass
        return str(transcript)
    except Exception as e:
        log.error(f"transcribe_voice error: {e}")
        return None

# ── Google Calendar ───────────────────────────────────────────────────────────
def get_gcal_service():
    creds = service_account.Credentials.from_service_account_file(
        str(SA_FILE),
        scopes=["https://www.googleapis.com/auth/calendar"]
    )
    return gcal_build("calendar", "v3", credentials=creds)

def gcal_create_event(summary, date, start_time=None, end_time=None,
                      description="", location="") -> dict:
    service = get_gcal_service()
    if start_time:
        start_dt = f"{date}T{start_time}:00+08:00"
        if end_time:
            end_dt = f"{date}T{end_time}:00+08:00"
        else:
            from datetime import datetime as dt
            st = dt.fromisoformat(start_dt)
            end_dt = (st + timedelta(hours=1)).isoformat()
        start = {"dateTime": start_dt, "timeZone": "Asia/Singapore"}
        end   = {"dateTime": end_dt,   "timeZone": "Asia/Singapore"}
    else:
        start = {"date": date}
        end   = {"date": date}

    body = {
        "summary": summary,
        "start": start,
        "end": end,
        "reminders": {"useDefault": False, "overrides": [
            {"method": "popup", "minutes": 30},
            {"method": "popup", "minutes": 1440},
        ]},
    }
    if description:
        body["description"] = description
    if location:
        body["location"] = location

    event = service.events().insert(calendarId=GCAL_ID, body=body).execute()
    log.info(f"Calendar event created: {summary} on {date}")
    return event

def gcal_list_upcoming(days=60) -> list:
    try:
        service = get_gcal_service()
        now = datetime.now(timezone(timedelta(hours=8)))
        result = service.events().list(
            calendarId=GCAL_ID,
            timeMin=now.isoformat(),
            timeMax=(now + timedelta(days=days)).isoformat(),
            maxResults=100,
            singleEvents=True,
            orderBy="startTime"
        ).execute()
        events = result.get("items", [])
        if events:
            CALENDAR_CACHE.write_text(json.dumps(events, indent=2))
        return events
    except Exception as e:
        log.warning(f"gcal_list_upcoming error: {e}")
        try:
            return json.loads(CALENDAR_CACHE.read_text())
        except:
            return []

# ── Data loaders ──────────────────────────────────────────────────────────────
def load_emails(days=7) -> list:
    try:
        data = json.loads(EMAIL_CACHE.read_text())
        emails = data if isinstance(data, list) else list(data.values())
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        filtered = []
        for e in emails:
            try:
                date_str = e.get("date", "")
                if date_str:
                    from email.utils import parsedate_to_datetime
                    dt = parsedate_to_datetime(date_str)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    if dt >= cutoff:
                        filtered.append(e)
                else:
                    filtered.append(e)
            except:
                filtered.append(e)
        log.info(f"Email filter: {len(filtered)}/{len(emails)} emails in past {days} days")
        return filtered
    except:
        return []

def load_calendar() -> list:
    try:
        return json.loads(CALENDAR_CACHE.read_text())
    except:
        return []

# ── OpenAI — dual-key failover, rate limit retry ──────────────────────────────
def gpt(system: str, user: str, max_tokens=900) -> str:
    global _active_key_idx, ai
    for attempt in range(6):
        try:
            r = ai.chat.completions.create(
                model=OPENAI_MODEL,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user",   "content": user},
                ],
                max_tokens=max_tokens,
                temperature=0.2,
            )
            return r.choices[0].message.content.strip()
        except Exception as e:
            err = str(e)
            if "429" in err or "rate_limit" in err.lower():
                other_idx = 1 - _active_key_idx
                log.warning(f"Key {_active_key_idx} rate-limited — switching to key {other_idx}")
                _active_key_idx = other_idx
                ai = _get_ai_client(_active_key_idx)
                time.sleep(3)
            elif "401" in err or "invalid" in err.lower() or "incorrect" in err.lower():
                log.error(f"Key {_active_key_idx} auth error — switching to other key")
                _active_key_idx = 1 - _active_key_idx
                ai = _get_ai_client(_active_key_idx)
                time.sleep(2)
            else:
                log.error(f"gpt error: {e}")
                return f"⚠️ AI error: {err[:120]}"
    return "⚠️ Both OpenAI keys are rate-limited — please try again in a minute."

# ── Single unified GPT call per message ──────────────────────────────────────
MASTER_PROMPT = """You are Ajay Bulusu's personal executive assistant (Singapore-based, SGT = UTC+8).
You have access to his Gmail and Google Calendar. Today is {today}.

Your capabilities:
1. Answer questions about payments, bills, insurance, flights, reservations, IBKR, DBS Wealth, StarHub, SP Group, Sobha
2. Add events to Google Calendar when asked

For CALENDAR ADD requests, respond with a JSON block like:
<calendar>
{{"summary": "event name", "date": "YYYY-MM-DD", "start_time": "HH:MM", "end_time": "HH:MM", "location": "", "description": ""}}
</calendar>
Then add a brief confirmation message after the JSON block.

For all other questions, answer directly and concisely. Use *bold* for emphasis, • for bullets.
Flag urgent/overdue items with ⚠️. Extract specific amounts, dates, policy numbers where relevant.

=== CALENDAR EVENTS (next 14 days) ===
{calendar}

=== RELEVANT EMAILS ===
{emails}"""

def score_emails(question: str, emails: list, top_n=15) -> list:
    keywords = question.lower().split()
    kw_map = {
        "flight": ["sq", "ek", "tr", "ai ", "airline", "booking", "pnr", "e-ticket"],
        "insurance": ["premium", "policy", "absli", "max life", "icici pru", "care health", "nach"],
        "payment": ["bill", "invoice", "due", "debit", "transfer", "paid"],
        "sobha": ["installment", "property", "demand letter"],
        "starhub": ["bill", "invoice", "mobile", "broadband"],
        "ibkr": ["interactive brokers", "trade", "margin", "fyi"],
        "dbs": ["wealth", "treasures", "eadvice", "cio"],
        "calendar": ["schedule", "meeting", "appointment", "event"],
    }
    expanded = list(keywords)
    for kw in keywords:
        for key, extras in kw_map.items():
            if key in kw:
                expanded.extend(extras)

    scored = []
    for e in emails:
        text = " ".join([
            e.get("subject", ""), e.get("from", ""),
            e.get("snippet", ""), e.get("body", "")
        ]).lower()
        score = sum(1 for kw in expanded if str(kw).lower() in text)
        if score > 0:
            scored.append((score, e))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [e for _, e in scored[:top_n]]

def format_calendar(events: list) -> str:
    if not events:
        return "No upcoming events."
    lines = []
    for e in events:
        start = e.get("start", {})
        dt = start.get("dateTime", start.get("date", ""))
        summary = e.get("summary", "")
        location = e.get("location", "")
        loc_str = f" @ {location}" if location else ""
        lines.append(f"• {summary}{loc_str} — {dt}")
    return "\n".join(lines)

def format_emails(emails: list) -> str:
    if not emails:
        return "No relevant emails found."
    parts = []
    for i, e in enumerate(emails, 1):
        body = (e.get("body") or e.get("snippet", ""))[:400]
        parts.append(
            f"[{i}] From: {e.get('from','')}\n"
            f"Subject: {e.get('subject','')}\n"
            f"Date: {e.get('date','')[:16]}\n"
            f"{body}"
        )
    return "\n\n---\n\n".join(parts)

def handle_message(text: str):
    try:
        tg_typing()
        t = text.strip()
        tl = t.lower()

        if tl in ["/start", "/help", "help"]:
            tg_send(HELP_TEXT)
            return

        if tl in ["/briefing", "/brief", "briefing"]:
            send_briefing()
            return

        emails   = load_emails(days=7)
        calendar = load_calendar()
        today    = datetime.now(timezone(timedelta(hours=8))).strftime("%d %B %Y, %A")

        now_sgt = datetime.now(timezone(timedelta(hours=8)))
        cal_14d = []
        for ev in calendar:
            try:
                start = ev.get("start", {})
                dt_str = start.get("dateTime", start.get("date", ""))
                dt = datetime.fromisoformat(dt_str)
                if not dt.tzinfo:
                    dt = dt.replace(tzinfo=timezone(timedelta(hours=8)))
                if dt <= now_sgt + timedelta(days=14):
                    cal_14d.append(ev)
            except:
                cal_14d.append(ev)

        relevant  = score_emails(t, emails)
        email_ctx = format_emails(relevant)
        cal_ctx   = format_calendar(cal_14d)

        response = gpt(
            MASTER_PROMPT.format(today=today, calendar=cal_ctx, emails=email_ctx),
            t,
            max_tokens=900
        )

        if "<calendar>" in response and "</calendar>" in response:
            import re
            cal_json_str = re.search(r"<calendar>(.*?)</calendar>", response, re.DOTALL)
            reply_text = re.sub(r"<calendar>.*?</calendar>", "", response, flags=re.DOTALL).strip()

            if cal_json_str:
                try:
                    cal_data = json.loads(cal_json_str.group(1).strip())
                    summary    = cal_data.get("summary", t[:50])
                    date       = cal_data.get("date")
                    start_time = cal_data.get("start_time")
                    end_time   = cal_data.get("end_time")
                    location   = cal_data.get("location") or ""
                    description = cal_data.get("description") or ""

                    if date:
                        gcal_create_event(summary, date, start_time, end_time, description, location)
                        time_str = f" at {start_time}" if start_time else ""
                        end_str  = f"–{end_time}" if end_time else ""
                        loc_str  = f"\n📍 {location}" if location else ""
                        tg_send(
                            f"✅ *Added to Google Calendar!*\n\n"
                            f"*{summary}*\n"
                            f"🗓 {date}{time_str}{end_str}{loc_str}\n\n"
                            f"_Event is live in your calendar now._"
                        )
                        return
                except Exception as e:
                    log.error(f"Calendar create error: {e}")
                    tg_send(f"⚠️ Could not add to calendar: {str(e)[:120]}")
                    return

        tg_send(response)

    except Exception as e:
        log.error(f"handle_message error: {e}\n{traceback.format_exc()}")
        tg_send(f"⚠️ Error: {str(e)[:100]}")


# ── Queue worker ──────────────────────────────────────────────────────────────
def queue_worker():
    while True:
        try:
            text = msg_queue.get(timeout=60)
            if text is None:
                break
            handle_message(text)
            msg_queue.task_done()
        except queue.Empty:
            pass
        except Exception as e:
            log.error(f"Queue worker error: {e}")


# ── Morning briefing ──────────────────────────────────────────────────────────
def send_briefing():
    emails   = load_emails(days=7)
    calendar = gcal_list_upcoming(days=60)
    today    = datetime.now(timezone(timedelta(hours=8))).strftime("%A, %d %B %Y")

    tg_send(f"☀️ *Good morning, Ajay!*\nExecutive briefing for *{today}*\n")

    now = datetime.now(timezone(timedelta(hours=8)))
    week_end = now + timedelta(days=7)
    week_events = []
    for e in calendar:
        start = e.get("start", {})
        dt_str = start.get("dateTime", start.get("date", ""))
        try:
            dt = datetime.fromisoformat(dt_str)
            if not dt.tzinfo:
                dt = dt.replace(tzinfo=timezone(timedelta(hours=8)))
            if now <= dt <= week_end:
                week_events.append((dt, e))
        except:
            pass
    week_events.sort(key=lambda x: x[0])
    if week_events:
        lines = ["📅 *This Week*"]
        for dt, e in week_events:
            fmt = dt.strftime("%a %d %b, %I:%M %p") if "T" in e.get("start",{}).get("dateTime","") else dt.strftime("%a %d %b")
            loc = e.get("location","")
            loc_str = f" @ {loc[:40]}" if loc else ""
            lines.append(f"• {e.get('summary','')}{loc_str} — _{fmt}_")
        tg_send("\n".join(lines))

    month_end = now + timedelta(days=30)
    flight_events = []
    for e in calendar:
        if any(w in e.get("summary","").lower() for w in ["sq ", "ek ", "tr ", "ai ", "flight"]):
            start = e.get("start", {})
            dt_str = start.get("dateTime", start.get("date", ""))
            try:
                dt = datetime.fromisoformat(dt_str)
                if not dt.tzinfo:
                    dt = dt.replace(tzinfo=timezone(timedelta(hours=8)))
                if now <= dt <= month_end:
                    flight_events.append((dt, e))
            except:
                pass
    flight_events.sort(key=lambda x: x[0])
    if flight_events:
        lines = ["✈️ *Upcoming Flights*"]
        for dt, e in flight_events:
            fmt = dt.strftime("%a %d %b, %I:%M %p")
            lines.append(f"• {e.get('summary','')} — _{fmt}_")
        tg_send("\n".join(lines))

    categories = [
        ("🚨 *Urgent — Failed/Declined Payments*",
         ["failed", "declined", "bounce", "insufficient", "nach", "dishonour", "unsuccessful", "returned"]),
        ("⏰ *Upcoming Payment Dues*",
         ["due", "reminder", "premium", "installment", "invoice", "bill", "pay by"]),
        ("🏠 *Sobha Property*",
         ["sobha", "installment", "property payment", "demand"]),
        ("📊 *Interactive Brokers*",
         ["interactive brokers", "ibkr", "trade confirmation", "margin"]),
        ("💼 *DBS Wealth / Treasures*",
         ["dbs wealth", "treasures", "eadvice", "cio", "portfolio"]),
        ("🛡️ *Insurance*",
         ["insurance", "premium", "policy", "absli", "max life", "icici pru", "care health"]),
        ("📡 *StarHub / SP Group*",
         ["starhub", "sp group", "spgroup", "electricity", "utilities"]),
    ]

    found_any = bool(week_events or flight_events)
    for title, keywords in categories:
        scored = []
        for e in emails:
            text = " ".join([e.get("subject",""), e.get("from",""),
                             e.get("snippet",""), e.get("body","")]).lower()
            score = sum(1 for kw in keywords if kw.lower() in text)
            if score > 0:
                scored.append((score, e))
        scored.sort(key=lambda x: x[0], reverse=True)
        top = [e for _, e in scored[:3]]
        if top:
            found_any = True
            lines = [title]
            for e in top:
                date_str = e.get("date","")[:16]
                lines.append(f"• {e.get('subject','')[:65]} _{date_str}_")
            tg_send("\n".join(lines))
            time.sleep(0.5)

    if not found_any:
        tg_send("✅ All clear — nothing urgent today!")

    tg_send("_Briefing complete. Ask me anything! Type /help for examples._")


# ── Help text ─────────────────────────────────────────────────────────────────
HELP_TEXT = """🤖 *Your Executive Assistant*

Ask me anything naturally:

💳 *Payments & Bills*
• "When is my next StarHub bill?"
• "Did my Sobha installment go through?"
• "What insurance premiums are overdue?"
• "Any failed payments?"

✈️ *Travel*
• "What are my upcoming flights?"
• "When is my next trip?"

🍽️ *Reservations*
• "Any restaurant reservations this week?"

📊 *Investments*
• "Any new IBKR alerts?"
• "What did DBS Wealth send recently?"

🛡️ *Insurance*
• "Status of my ABSLI policy?"
• "When is my Max Life premium due?"

📅 *Calendar*
• "Add pickleball Monday 4–5pm"
• "Schedule dentist Friday 3pm"
• "What's on my calendar this week?"

🎙️ *Voice Notes*
Just send a voice message — I'll transcribe and answer it!

*Commands:*
/briefing — Send your morning briefing now
/help — This message"""


# ── HTTP sync endpoint — receives email cache from Manus daily task ───────────
class SyncHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # suppress default HTTP logs

    def do_POST(self):
        if self.path != "/sync":
            self.send_response(404)
            self.end_headers()
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            data = json.loads(body)

            if data.get("secret") != SYNC_SECRET:
                self.send_response(403)
                self.end_headers()
                self.wfile.write(b'{"error":"forbidden"}')
                return

            emails = data.get("emails", [])
            EMAIL_CACHE.write_text(json.dumps(emails, indent=2))
            log.info(f"Sync: received {len(emails)} emails from Manus")

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": True, "count": len(emails)}).encode())
        except Exception as e:
            log.error(f"Sync handler error: {e}")
            self.send_response(500)
            self.end_headers()

    def do_GET(self):
        # Health check endpoint
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        email_count = len(load_emails(days=30))
        self.wfile.write(json.dumps({"status": "ok", "emails": email_count}).encode())


def start_sync_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), SyncHandler)
    log.info(f"Sync server listening on port {port}")
    server.serve_forever()


# ── Main polling loop ─────────────────────────────────────────────────────────
def run():
    state  = load_state()
    offset = state.get("last_update_id", 0) + 1

    email_count = len(load_emails(days=30))
    cal_count   = len(load_calendar())
    log.info(f"Bot v5 starting. Emails in cache: {email_count}, Calendar: {cal_count}, Offset: {offset}")

    # Start serial queue worker
    worker = threading.Thread(target=queue_worker, daemon=True)
    worker.start()

    # Start HTTP sync server (Railway requires a bound port)
    sync_thread = threading.Thread(target=start_sync_server, daemon=True)
    sync_thread.start()

    tg_send(
        f"🤖 *Executive Assistant v5 is online!*\n\n"
        f"✅ {email_count} emails in cache\n"
        f"✅ {cal_count} calendar events loaded\n"
        f"✅ Direct OpenAI (dual-key failover)\n"
        f"✅ 24/7 on Railway cloud\n\n"
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
                uid = upd["update_id"]
                offset = uid + 1
                state["last_update_id"] = uid
                save_state(state)

                msg     = upd.get("message", {})
                chat_id = str(msg.get("chat", {}).get("id", ""))
                text    = msg.get("text", "").strip()

                if chat_id != CHAT_ID:
                    continue

                # Handle voice messages
                voice = msg.get("voice") or msg.get("audio")
                if voice and not text:
                    file_id = voice.get("file_id")
                    log.info(f"VOICE [{uid}]: file_id={file_id}")
                    def _transcribe_and_queue(fid=file_id, uid=uid):
                        tg_send("🎙️ _Transcribing your voice note..._")
                        transcript = transcribe_voice(fid)
                        if transcript:
                            log.info(f"VOICE [{uid}] transcript: {transcript[:80]}")
                            tg_send(f"🎙️ *You said:* _{transcript}_")
                            msg_queue.put(transcript)
                        else:
                            tg_send("⚠️ Sorry, I couldn't transcribe that. Please try again or type your question.")
                    threading.Thread(target=_transcribe_and_queue, daemon=True).start()
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
            log.error(f"Poll loop error #{consecutive_errors}: {e}")
            sleep_time = min(60, 5 * consecutive_errors)
            time.sleep(sleep_time)


if __name__ == "__main__":
    run()

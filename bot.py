"""
Executive Assistant Telegram Bot — v4 STABLE
=============================================
Key fixes in v4:
  - Serial message queue (no parallel threads hitting OpenAI)
  - Single GPT call per message (was 3 calls before = rate limit hell)
  - Rate limit backoff: waits 25s and retries on 429
  - Robust curl polling with retry on network errors
  - Heartbeat updated every poll cycle
  - Zero Manus credits per query
"""

import os, sys, json, time, logging, threading, traceback, queue, tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Google Calendar direct API
from google.oauth2 import service_account
from googleapiclient.discovery import build as gcal_build

import subprocess
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
from openai import OpenAI

# ── Config — loaded from environment variables (set in Railway dashboard) ────
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
OPENAI_KEY   = OPENAI_KEYS[0]
OPENAI_MODEL = "gpt-4o-mini"

BASE_DIR         = Path(__file__).parent
EMAIL_CACHE      = BASE_DIR / "email_cache.json"
CALENDAR_CACHE   = BASE_DIR / "calendar_cache.json"
STATE_FILE       = BASE_DIR / "bot_state.json"
LOG_FILE         = BASE_DIR / "bot.log"
SA_FILE          = BASE_DIR / "service_account.json"
HEARTBEAT_FILE   = BASE_DIR / "bot_heartbeat"
GCAL_ID          = os.environ.get("GCAL_ID", "bulusu.ajay@gmail.com")

TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

# On Railway, write service_account.json from env var if not present
_sa_env = os.environ.get("SERVICE_ACCOUNT_JSON", "")
if _sa_env and not SA_FILE.exists():
    SA_FILE.write_text(_sa_env)

# Create empty caches on first boot (Railway has no persistent files)
for _cache in [EMAIL_CACHE, CALENDAR_CACHE]:
    if not _cache.exists():
        _cache.write_text("[]")

# ── Logging ─────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger(__name__)

# ── OpenAI client — ALWAYS direct to api.openai.com ─────────────────────────
# Dual-key setup: primary + backup with automatic failover
_active_key_idx = 0

def _get_ai_client(key_idx=None):
    global _active_key_idx
    idx = key_idx if key_idx is not None else _active_key_idx
    return OpenAI(api_key=OPENAI_KEYS[idx], base_url="https://api.openai.com/v1")

ai = _get_ai_client(0)

# ── Serial message queue — prevents parallel OpenAI calls ───────────────────
msg_queue = queue.Queue()

# ── State ────────────────────────────────────────────────────────────────────
def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except:
        return {"last_update_id": 0}

def save_state(s):
    STATE_FILE.write_text(json.dumps(s))

def touch_heartbeat():
    HEARTBEAT_FILE.write_text(str(time.time()))

# ── Telegram via curl (SSL-reliable) ─────────────────────────────────────────
def _curl_tg(endpoint, data=None, params=None, timeout=15):
    url = f"{TG_API}/{endpoint}"
    if params:
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        url = f"{url}?{qs}"
    cmd = ["curl", "-s", "--max-time", str(timeout), "-X"]
    if data:
        cmd += ["POST", url, "-H", "Content-Type: application/json", "-d", json.dumps(data)]
    else:
        cmd += ["GET", url]
    for attempt in range(5):
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 5)
            if result.returncode == 0 and result.stdout.strip():
                return json.loads(result.stdout)
        except Exception as e:
            log.warning(f"curl attempt {attempt+1} failed: {e}")
        time.sleep(2 * (attempt + 1))
    return None

def tg_send(text, parse_mode="Markdown"):
    MAX = 4000
    for chunk in [text[i:i+MAX] for i in range(0, max(len(text), 1), MAX)]:
        r = _curl_tg("sendMessage", data={
            "chat_id": CHAT_ID, "text": chunk, "parse_mode": parse_mode
        })
        if r and not r.get("ok"):
            _curl_tg("sendMessage", data={"chat_id": CHAT_ID, "text": chunk})
        time.sleep(0.3)

def tg_typing():
    try:
        _curl_tg("sendChatAction", data={"chat_id": CHAT_ID, "action": "typing"}, timeout=5)
    except:
        pass

def tg_get_updates(offset, timeout=8):
    touch_heartbeat()
    r = _curl_tg("getUpdates", params={
        "offset": offset, "timeout": timeout, "allowed_updates": "message"
    }, timeout=timeout + 7)
    touch_heartbeat()
    if r and r.get("ok"):
        return r.get("result", [])
    return []

def tg_get_file(file_id: str) -> str | None:
    """Get the file path on Telegram servers for a given file_id."""
    r = _curl_tg("getFile", params={"file_id": file_id})
    if r and r.get("ok"):
        return r["result"].get("file_path")
    return None

def tg_download_file(file_path: str, dest: str) -> bool:
    """Download a file from Telegram to a local path using curl."""
    url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
    cmd = ["curl", "-s", "--max-time", "30", "-o", dest, url]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=35)
        return result.returncode == 0
    except Exception as e:
        log.warning(f"tg_download_file error: {e}")
        return False

def transcribe_voice(file_id: str) -> str | None:
    """Download a Telegram voice/audio file and transcribe with Whisper."""
    try:
        file_path = tg_get_file(file_id)
        if not file_path:
            log.warning("transcribe_voice: could not get file path")
            return None

        ext = ".ogg"
        if file_path.endswith(".mp3"):
            ext = ".mp3"
        elif file_path.endswith(".m4a"):
            ext = ".m4a"

        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
            tmp_path = tmp.name

        ok = tg_download_file(file_path, tmp_path)
        if not ok:
            log.warning("transcribe_voice: download failed")
            return None

        with open(tmp_path, "rb") as audio_file:
            whisper_client = _get_ai_client(_active_key_idx)
            transcript = whisper_client.audio.transcriptions.create(
                model="whisper-1",
                file=audio_file,
                response_format="text"
            )
        os.unlink(tmp_path)
        text = transcript.strip() if isinstance(transcript, str) else transcript
        log.info(f"Voice transcribed: {str(text)[:100]}")
        return str(text)
    except Exception as e:
        log.error(f"transcribe_voice error: {e}")
        try:
            os.unlink(tmp_path)
        except:
            pass
        return None

# ── Google Calendar ──────────────────────────────────────────────────────────
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

# ── Data loaders ─────────────────────────────────────────────────────────────
def load_emails(days=7) -> list:
    """Load emails from the past N days only (default 7 days)."""
    try:
        data = json.loads(EMAIL_CACHE.read_text())
        emails = data if isinstance(data, list) else list(data.values())
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        filtered = []
        for e in emails:
            try:
                date_str = e.get("date", "")
                if date_str:
                    # Try parsing various date formats
                    from email.utils import parsedate_to_datetime
                    dt = parsedate_to_datetime(date_str)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    if dt >= cutoff:
                        filtered.append(e)
                else:
                    filtered.append(e)  # keep if no date
            except:
                filtered.append(e)  # keep if date unparseable
        log.info(f"Email filter: {len(filtered)}/{len(emails)} emails in past {days} days")
        return filtered
    except:
        return []

def load_calendar() -> list:
    try:
        return json.loads(CALENDAR_CACHE.read_text())
    except:
        return []

# ── OpenAI — dual-key failover, rate limit retry ────────────────────────────
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
                # Switch to the other key immediately
                other_idx = 1 - _active_key_idx
                log.warning(f"Key {_active_key_idx} rate-limited — switching to key {other_idx} (attempt {attempt+1})")
                _active_key_idx = other_idx
                ai = _get_ai_client(_active_key_idx)
                time.sleep(3)  # brief pause before retry with new key
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
    """Fast keyword scoring — no extra GPT call needed."""
    keywords = question.lower().split()
    # Add domain-specific expansions
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
    """Process a message and reply. Called serially from the queue worker."""
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

        emails   = load_emails(days=7)  # only past 7 days
        calendar = load_calendar()
        today    = datetime.now(timezone(timedelta(hours=8))).strftime("%d %B %Y, %A")

        # Filter calendar to next 14 days for Q&A context
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

        relevant = score_emails(t, emails)
        email_ctx = format_emails(relevant)
        cal_ctx   = format_calendar(cal_14d)

        # Single GPT call handles both calendar intent AND Q&A
        response = gpt(
            MASTER_PROMPT.format(today=today, calendar=cal_ctx, emails=email_ctx),
            t,
            max_tokens=900
        )

        # Check if GPT wants to create a calendar event
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
                        event = gcal_create_event(summary, date, start_time, end_time, description, location)
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


# ── Queue worker — processes messages one at a time ──────────────────────────
def queue_worker():
    """Runs in a background thread, processes messages serially."""
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
    emails   = load_emails()
    calendar = gcal_list_upcoming(days=60)
    today    = datetime.now(timezone(timedelta(hours=8))).strftime("%A, %d %B %Y")

    tg_send(f"☀️ *Good morning, Ajay!*\nExecutive briefing for *{today}*\n")

    # Calendar: this week
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

    # Upcoming flights
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

    # Email categories
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


# ── Main polling loop ─────────────────────────────────────────────────────────
def run():
    state  = load_state()
    offset = state.get("last_update_id", 0) + 1

    email_count = len(load_emails())
    cal_count   = len(load_calendar())
    log.info(f"Bot v4 starting. Emails: {email_count}, Calendar: {cal_count}, Offset: {offset}")

    # Start serial queue worker
    worker = threading.Thread(target=queue_worker, daemon=True)
    worker.start()

    # Send startup message
    tg_send(
        f"🤖 *Executive Assistant v4 is online!*\n\n"
        f"✅ {email_count} emails loaded\n"
        f"✅ {cal_count} calendar events loaded\n"
        f"✅ Direct OpenAI (zero Manus credits)\n"
        f"✅ Serial queue (no rate limit crashes)\n\n"
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

                # Handle voice messages (audio notes)
                voice = msg.get("voice") or msg.get("audio")
                if voice and not text:
                    file_id = voice.get("file_id")
                    log.info(f"VOICE [{uid}]: file_id={file_id}")
                    # Transcribe in a thread so polling isn't blocked
                    def _transcribe_and_queue(fid=file_id, uid=uid):
                        tg_send("🎙️ _Transcribing your voice note..._")
                        transcript = transcribe_voice(fid)
                        if transcript:
                            log.info(f"VOICE [{uid}] transcript: {transcript[:80]}")
                            tg_send(f"🎙️ *You said:* _{transcript}_")
                            msg_queue.put(transcript)
                        else:
                            tg_send("⚠️ Sorry, I couldn't transcribe that audio. Please try again or type your question.")
                    threading.Thread(target=_transcribe_and_queue, daemon=True).start()
                    continue

                if not text:
                    continue

                log.info(f"MSG [{uid}]: {text[:80]}")
                msg_queue.put(text)  # Queue it — worker processes serially

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

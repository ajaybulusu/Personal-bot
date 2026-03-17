"""
Personal Assistant Telegram Bot — v8
- Always replies, never silently fails
- Google Calendar via service account (shared calendar)
- Gmail via OAuth2 refresh token
- OpenAI GPT-4o-mini with dual key failover
- Voice transcription via Whisper
- Daily 8am SGT briefing
"""

import os, re, json, logging, asyncio
from datetime import datetime, timezone, timedelta

import requests
import openai
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes
from apscheduler.schedulers.asyncio import AsyncIOScheduler

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot")

# ── Environment variables ─────────────────────────────────────────────────────
BOT_TOKEN         = os.environ["BOT_TOKEN"]
CHAT_ID           = os.environ["CHAT_ID"]
SA_JSON           = os.environ.get("SERVICE_ACCOUNT_JSON", "")
GCAL_ID           = os.environ.get("GCAL_ID", "primary")
GMAIL_CLIENT_ID   = os.environ.get("GMAIL_CLIENT_ID", "")
GMAIL_CLIENT_SEC  = os.environ.get("GMAIL_CLIENT_SECRET", "")
GMAIL_REFRESH_TOK = os.environ.get("GMAIL_REFRESH_TOKEN", "")
OPENAI_KEYS       = [k for k in [
    os.environ.get("OPENAI_KEY_PRIMARY", ""),
    os.environ.get("OPENAI_KEY_BACKUP", ""),
] if k]
SGT = timezone(timedelta(hours=8))

if not OPENAI_KEYS:
    raise RuntimeError("Set OPENAI_KEY_PRIMARY in Railway env vars")

_key_idx = 0

# ── OpenAI ────────────────────────────────────────────────────────────────────
def ask_openai(messages: list) -> str:
    global _key_idx
    for attempt in range(len(OPENAI_KEYS) * 2):
        try:
            client = openai.OpenAI(api_key=OPENAI_KEYS[_key_idx])
            resp = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=messages,
                max_tokens=1024,
                timeout=30
            )
            return resp.choices[0].message.content.strip()
        except Exception as e:
            err = str(e)
            log.error(f"OpenAI key[{_key_idx}] error: {err[:120]}")
            if "429" in err or "rate_limit" in err.lower() or "401" in err:
                _key_idx = 1 - _key_idx
                import time; time.sleep(3)
            else:
                return f"AI error: {err[:100]}"
    return "Both OpenAI keys failed. Try again in a moment."

# ── Google Calendar ───────────────────────────────────────────────────────────
def get_calendar_events(days: int = 14) -> str:
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build as gbuild
        sa = json.loads(SA_JSON)
        creds = service_account.Credentials.from_service_account_info(
            sa, scopes=["https://www.googleapis.com/auth/calendar"]
        )
        svc = gbuild("calendar", "v3", credentials=creds)
        now = datetime.now(SGT)
        result = svc.events().list(
            calendarId=GCAL_ID,
            timeMin=now.isoformat(),
            timeMax=(now + timedelta(days=days)).isoformat(),
            maxResults=50, singleEvents=True, orderBy="startTime"
        ).execute()
        events = result.get("items", [])
        if not events:
            return "No upcoming events in the next 14 days."
        lines = []
        for e in events:
            s = e.get("start", {})
            dt = s.get("dateTime", s.get("date", ""))
            try:
                if "T" in dt:
                    parsed = datetime.fromisoformat(dt)
                    dt_fmt = parsed.strftime("%a %d %b %I:%M%p")
                else:
                    parsed = datetime.strptime(dt, "%Y-%m-%d")
                    dt_fmt = parsed.strftime("%a %d %b (all day)")
            except:
                dt_fmt = dt
            loc = e.get("location", "")
            loc_str = f" @ {loc[:40]}" if loc else ""
            lines.append(f"• {e.get('summary','(no title)')} — {dt_fmt}{loc_str}")
        return "\n".join(lines)
    except Exception as e:
        log.error(f"Calendar error: {e}")
        return f"[Calendar unavailable: {str(e)[:80]}]"

def add_calendar_event(summary, date, start_time=None, end_time=None, location="", description="") -> str:
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build as gbuild
        sa = json.loads(SA_JSON)
        creds = service_account.Credentials.from_service_account_info(
            sa, scopes=["https://www.googleapis.com/auth/calendar"]
        )
        svc = gbuild("calendar", "v3", credentials=creds)
        if start_time:
            s_dt = f"{date}T{start_time}:00+08:00"
            e_dt = f"{date}T{end_time}:00+08:00" if end_time else \
                   (datetime.fromisoformat(s_dt) + timedelta(hours=1)).isoformat()
            start = {"dateTime": s_dt, "timeZone": "Asia/Singapore"}
            end   = {"dateTime": e_dt, "timeZone": "Asia/Singapore"}
        else:
            start = end = {"date": date}
        body = {
            "summary": summary, "start": start, "end": end,
            "location": location, "description": description,
            "reminders": {"useDefault": False, "overrides": [
                {"method": "popup", "minutes": 30},
                {"method": "popup", "minutes": 1440}
            ]}
        }
        event = svc.events().insert(calendarId=GCAL_ID, body=body).execute()
        return event.get("htmlLink", "added")
    except Exception as e:
        log.error(f"Calendar add error: {e}")
        return f"error: {str(e)[:80]}"

# ── Gmail ─────────────────────────────────────────────────────────────────────
def get_gmail_token() -> str:
    if not all([GMAIL_CLIENT_ID, GMAIL_CLIENT_SEC, GMAIL_REFRESH_TOK]):
        return ""
    try:
        r = requests.post("https://oauth2.googleapis.com/token", data={
            "client_id": GMAIL_CLIENT_ID,
            "client_secret": GMAIL_CLIENT_SEC,
            "refresh_token": GMAIL_REFRESH_TOK,
            "grant_type": "refresh_token"
        }, timeout=10)
        return r.json().get("access_token", "")
    except Exception as e:
        log.error(f"Gmail token error: {e}")
        return ""

def search_gmail(query: str, max_results: int = 15) -> str:
    token = get_gmail_token()
    if not token:
        return "[Gmail not configured]"
    try:
        headers = {"Authorization": f"Bearer {token}"}
        r = requests.get(
            "https://gmail.googleapis.com/gmail/v1/users/me/messages",
            headers=headers, params={"maxResults": max_results, "q": query}, timeout=10
        )
        msg_ids = [m["id"] for m in r.json().get("messages", [])]
        if not msg_ids:
            return f"No emails found for: {query}"
        lines = []
        for mid in msg_ids[:max_results]:
            mr = requests.get(
                f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{mid}"
                "?format=metadata&metadataHeaders=Subject&metadataHeaders=From&metadataHeaders=Date",
                headers=headers, timeout=10
            )
            p = mr.json()
            hdrs = {h["name"]: h["value"] for h in p.get("payload", {}).get("headers", [])}
            lines.append(
                f"From: {hdrs.get('From','')[:50]}\n"
                f"Subject: {hdrs.get('Subject','')[:80]}\n"
                f"Date: {hdrs.get('Date','')[:30]}\n"
                f"Preview: {p.get('snippet','')[:150]}\n---"
            )
        return "\n".join(lines)
    except Exception as e:
        log.error(f"Gmail search error: {e}")
        return f"[Gmail search failed: {str(e)[:80]}]"

def get_email_context(query: str) -> str:
    q = query.lower()
    gmail_query = ""
    if any(w in q for w in ["starhub", "bill", "invoice", "payment due", "amount due"]):
        gmail_query = "starhub OR invoice OR bill"
    elif any(w in q for w in ["sobha", "property", "rent", "mortgage"]):
        gmail_query = "sobha OR property rent"
    elif any(w in q for w in ["insurance", "aia", "prudential", "great eastern", "policy"]):
        gmail_query = "insurance premium OR policy renewal"
    elif any(w in q for w in ["ibkr", "interactive brokers", "stock", "portfolio"]):
        gmail_query = "IBKR OR interactive brokers"
    elif any(w in q for w in ["flight", "booking", "hotel", "travel", "airbnb"]):
        gmail_query = "booking confirmation OR flight OR hotel"
    elif any(w in q for w in ["dbs", "bank", "transaction", "transfer"]):
        gmail_query = "DBS bank OR transaction"
    elif any(w in q for w in ["email", "mail", "message", "inbox"]):
        gmail_query = ""  # fetch recent
    
    if gmail_query is not None and (gmail_query or any(w in q for w in ["email", "mail", "inbox"])):
        return search_gmail(gmail_query or "is:unread", max_results=10)
    return ""

# ── Main AI handler ───────────────────────────────────────────────────────────
async def process_message(text: str) -> str:
    today = datetime.now(SGT).strftime("%A, %d %B %Y %H:%M SGT")
    
    # Fetch calendar and email context in parallel
    loop = asyncio.get_event_loop()
    cal_task   = loop.run_in_executor(None, get_calendar_events, 14)
    email_task = loop.run_in_executor(None, get_email_context, text)
    cal_ctx, email_ctx = await asyncio.gather(cal_task, email_task)

    context_parts = [f"=== CALENDAR (next 14 days) ===\n{cal_ctx}"]
    if email_ctx:
        context_parts.append(f"=== RELEVANT EMAILS ===\n{email_ctx}")

    system = f"""You are Ajay's personal executive assistant in Singapore (SGT = UTC+8).
Today is {today}.

You have access to Ajay's real Google Calendar and Gmail data below.
Answer questions directly using this data. Be concise and executive-level.
For bills/payments, extract the amount and due date from the email previews.
For calendar, list events with times clearly.
Do NOT say you cannot access data — the data is provided below.
Use plain text only (no markdown asterisks).

{chr(10).join(context_parts)}"""

    # Check if this is a calendar add request
    add_keywords = ["add", "schedule", "book", "create", "set up", "remind me", "put"]
    cal_keywords = ["meeting", "appointment", "event", "call", "dinner", "lunch", "gym",
                    "flight", "dentist", "doctor", "birthday", "catch-up", "catchup"]
    is_add = any(w in text.lower() for w in add_keywords) and \
             any(w in text.lower() for w in cal_keywords)

    if is_add:
        system += """

When adding a calendar event, respond ONLY with this JSON block followed by a confirmation:
<calendar_add>
{"summary": "...", "date": "YYYY-MM-DD", "start_time": "HH:MM", "end_time": "HH:MM", "location": "", "description": ""}
</calendar_add>
Then write a brief confirmation line."""

    messages = [
        {"role": "system", "content": system},
        {"role": "user",   "content": text}
    ]

    response = await loop.run_in_executor(None, ask_openai, messages)

    # Handle calendar add
    if "<calendar_add>" in response and "</calendar_add>" in response:
        match = re.search(r"<calendar_add>(.*?)</calendar_add>", response, re.DOTALL)
        clean = re.sub(r"<calendar_add>.*?</calendar_add>", "", response, flags=re.DOTALL).strip()
        if match:
            try:
                d = json.loads(match.group(1).strip())
                link = await loop.run_in_executor(None, lambda: add_calendar_event(
                    d.get("summary", "New Event"), d["date"],
                    d.get("start_time"), d.get("end_time"),
                    d.get("location", ""), d.get("description", "")
                ))
                t = f" at {d['start_time']}" if d.get("start_time") else ""
                return f"Added to calendar: {d.get('summary','')} on {d['date']}{t}\n\n{clean}".strip()
            except Exception as e:
                log.error(f"Calendar add parse error: {e}")
                return clean or response

    return response

# ── Telegram handlers ─────────────────────────────────────────────────────────
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_chat.id) != CHAT_ID:
        return
    text = update.message.text.strip()
    log.info(f"MSG: {text[:80]}")
    await update.message.chat.send_action(ChatAction.TYPING)
    try:
        reply = await process_message(text)
        await update.message.reply_text(reply[:4096])
    except Exception as e:
        log.error(f"handle_text error: {e}")
        await update.message.reply_text(f"Error: {str(e)[:200]}")

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_chat.id) != CHAT_ID:
        return
    voice = update.message.voice or update.message.audio
    if not voice:
        return
    await update.message.chat.send_action(ChatAction.TYPING)
    try:
        await update.message.reply_text("Transcribing voice note...")
        file = await context.bot.get_file(voice.file_id)
        import tempfile
        tmp = tempfile.mktemp(suffix=".ogg")
        await file.download_to_drive(tmp)
        client = openai.OpenAI(api_key=OPENAI_KEYS[_key_idx])
        with open(tmp, "rb") as f:
            transcript = client.audio.transcriptions.create(
                model="whisper-1", file=f, response_format="text"
            )
        try: os.unlink(tmp)
        except: pass
        text = str(transcript).strip()
        log.info(f"VOICE: {text[:80]}")
        await update.message.reply_text(f"You said: {text}")
        reply = await process_message(text)
        await update.message.reply_text(reply[:4096])
    except Exception as e:
        log.error(f"handle_voice error: {e}")
        await update.message.reply_text(f"Voice error: {str(e)[:150]}")

async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_chat.id) != CHAT_ID:
        return
    await update.message.reply_text(
        "Personal Assistant online.\n\n"
        "Ask me anything:\n"
        "- What's on my calendar this week?\n"
        "- What's my StarHub bill amount?\n"
        "- Add dentist Friday 3pm\n"
        "- Any upcoming payments due?\n"
        "- Morning briefing\n\n"
        "Or send a voice note."
    )

async def send_daily_briefing(app: Application):
    log.info("Sending daily briefing...")
    try:
        reply = await process_message(
            "Give me my full morning briefing: today's calendar events with times, "
            "any bills or payments due this week from emails, and what I should focus on today."
        )
        header = f"Good morning! Daily Briefing — {datetime.now(SGT).strftime('%A %d %B %Y')}\n\n"
        await app.bot.send_message(chat_id=CHAT_ID, text=(header + reply)[:4096])
    except Exception as e:
        log.error(f"Briefing error: {e}")
        await app.bot.send_message(chat_id=CHAT_ID, text=f"Briefing failed: {e}")

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    log.info("=== Bot v8 starting ===")
    log.info(f"OpenAI keys: {len(OPENAI_KEYS)}")
    log.info(f"Calendar ID: {GCAL_ID}")
    log.info(f"Gmail: {'configured' if GMAIL_REFRESH_TOK else 'NOT SET'}")
    log.info(f"Service account: {'set' if SA_JSON else 'MISSING'}")

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", handle_start))
    app.add_handler(CommandHandler("help",  handle_start))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    scheduler = AsyncIOScheduler(timezone="Asia/Singapore")
    scheduler.add_job(send_daily_briefing, "cron", hour=8, minute=0, args=[app])
    scheduler.start()
    log.info("Scheduler started — daily briefing at 08:00 SGT")

    log.info("Polling...")
    app.run_polling(allowed_updates=["message"], drop_pending_updates=False)

if __name__ == "__main__":
    main()

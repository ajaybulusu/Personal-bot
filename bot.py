"""
Personal Assistant Telegram Bot — v9
- Conversation memory (last 10 exchanges)
- Google Calendar via service account (shared calendar)
- Gmail via OAuth2 refresh token
- OpenAI GPT-4o-mini with dual key failover
- Voice transcription via Whisper
- Daily 8am SGT briefing
- /debug command to verify env vars
"""

import os, re, json, logging, asyncio, tempfile
from datetime import datetime, timezone, timedelta
from collections import deque

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
OPENAI_KEYS       = [k.strip() for k in [
    os.environ.get("OPENAI_KEY_PRIMARY", ""),
    os.environ.get("OPENAI_KEY_BACKUP", ""),
] if k.strip()]
SGT = timezone(timedelta(hours=8))

if not OPENAI_KEYS:
    raise RuntimeError("Set OPENAI_KEY_PRIMARY in Railway env vars")

_key_idx = 0

# ── Conversation memory ───────────────────────────────────────────────────────
# Stores last 10 exchanges per chat_id
_conv: dict = {}
MAX_TURNS = 10

def get_history(chat_id: str) -> list:
    return list(_conv.get(chat_id, []))

def save_turn(chat_id: str, user_text: str, assistant_text: str):
    if chat_id not in _conv:
        _conv[chat_id] = deque(maxlen=MAX_TURNS * 2)
    _conv[chat_id].append({"role": "user", "content": user_text})
    _conv[chat_id].append({"role": "assistant", "content": assistant_text})

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
    if not SA_JSON:
        return "Calendar unavailable: SERVICE_ACCOUNT_JSON not set."
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
            singleEvents=True,
            orderBy="startTime",
            maxResults=20
        ).execute()
        events = result.get("items", [])
        if not events:
            return f"No upcoming events in the next {days} days."
        lines = []
        for e in events:
            start = e["start"].get("dateTime", e["start"].get("date", ""))
            try:
                dt = datetime.fromisoformat(start.replace("Z", "+00:00")).astimezone(SGT)
                start_str = dt.strftime("%a %d %b %Y %H:%M SGT")
            except Exception:
                start_str = start
            lines.append(f"- {e.get('summary','(no title)')} | {start_str}")
        return "\n".join(lines)
    except Exception as e:
        log.error(f"Calendar error: {e}")
        return f"Calendar error: {str(e)[:150]}"

def add_calendar_event(summary, date, start_time=None, end_time=None, location="", description="") -> str:
    if not SA_JSON:
        return "Calendar unavailable."
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build as gbuild
        sa = json.loads(SA_JSON)
        creds = service_account.Credentials.from_service_account_info(
            sa, scopes=["https://www.googleapis.com/auth/calendar"]
        )
        svc = gbuild("calendar", "v3", credentials=creds)
        if start_time:
            start = {"dateTime": f"{date}T{start_time}:00+08:00", "timeZone": "Asia/Singapore"}
            end_t = end_time or (datetime.strptime(start_time, "%H:%M") + timedelta(hours=1)).strftime("%H:%M")
            end = {"dateTime": f"{date}T{end_t}:00+08:00", "timeZone": "Asia/Singapore"}
        else:
            start = {"date": date}
            end = {"date": date}
        event = {"summary": summary, "location": location, "description": description,
                 "start": start, "end": end}
        created = svc.events().insert(calendarId=GCAL_ID, body=event).execute()
        return created.get("htmlLink", "Event created")
    except Exception as e:
        log.error(f"Calendar add error: {e}")
        return f"Failed to add event: {str(e)[:100]}"

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

def search_gmail(query: str, max_results: int = 10) -> str:
    token = get_gmail_token()
    if not token:
        return "Gmail unavailable: OAuth token missing or failed."
    try:
        headers = {"Authorization": f"Bearer {token}"}
        r = requests.get(
            "https://gmail.googleapis.com/gmail/v1/users/me/messages",
            headers=headers,
            params={"maxResults": max_results, "q": query},
            timeout=10
        )
        msgs = r.json().get("messages", [])
        if not msgs:
            return f"No emails found for: {query}"
        results = []
        for m in msgs[:8]:
            mr = requests.get(
                f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{m['id']}?format=metadata"
                "&metadataHeaders=Subject&metadataHeaders=From&metadataHeaders=Date",
                headers=headers, timeout=10
            )
            p = mr.json()
            hdrs = {h["name"]: h["value"] for h in p.get("payload", {}).get("headers", [])}
            snippet = p.get("snippet", "")[:200]
            results.append(
                f"From: {hdrs.get('From','?')}\n"
                f"Subject: {hdrs.get('Subject','?')}\n"
                f"Date: {hdrs.get('Date','?')}\n"
                f"Preview: {snippet}\n"
            )
        return "\n---\n".join(results)
    except Exception as e:
        log.error(f"Gmail search error: {e}")
        return f"Gmail error: {str(e)[:100]}"

def get_email_context(question: str) -> str:
    q = question.lower()
    gmail_query = None
    if any(w in q for w in ["starhub", "star hub", "telco", "mobile bill", "broadband"]):
        gmail_query = "StarHub bill OR invoice OR payment"
    elif any(w in q for w in ["sobha", "property", "condo", "maintenance"]):
        gmail_query = "Sobha OR property maintenance"
    elif any(w in q for w in ["insurance", "aia", "prudential", "great eastern", "income"]):
        gmail_query = "insurance premium OR policy"
    elif any(w in q for w in ["ibkr", "interactive brokers", "stock", "portfolio"]):
        gmail_query = "IBKR OR interactive brokers"
    elif any(w in q for w in ["flight", "booking", "hotel", "travel", "airbnb"]):
        gmail_query = "booking confirmation OR flight OR hotel"
    elif any(w in q for w in ["dbs", "bank", "transaction", "transfer", "payment"]):
        gmail_query = "DBS bank OR transaction OR payment"
    elif any(w in q for w in ["bill", "invoice", "due", "overdue", "amount"]):
        gmail_query = "bill OR invoice OR payment due"
    elif any(w in q for w in ["email", "mail", "message", "inbox"]):
        gmail_query = "is:unread"
    if gmail_query is not None:
        return search_gmail(gmail_query, max_results=10)
    return ""

# ── Main AI handler ───────────────────────────────────────────────────────────
async def process_message(text: str, chat_id: str) -> str:
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
Answer questions directly and concisely using this data.
For bills/payments, extract the exact amount and due date from the email previews.
For calendar, list events with times clearly.
Do NOT say you cannot access data — the data is provided below.
Use plain text only (no markdown asterisks or bullet symbols).
Remember the conversation context and refer back to it when relevant.

{chr(10).join(context_parts)}"""

    # Check if this is a calendar add request
    add_keywords = ["add", "schedule", "book", "create", "set up", "remind me", "put"]
    cal_keywords = ["meeting", "appointment", "event", "call", "dinner", "lunch", "gym",
                    "flight", "dentist", "doctor", "birthday", "catch-up", "catchup", "session"]
    is_add = any(w in text.lower() for w in add_keywords) and \
             any(w in text.lower() for w in cal_keywords)

    if is_add:
        system += """

When adding a calendar event, respond ONLY with this JSON block followed by a confirmation:
<calendar_add>
{"summary": "...", "date": "YYYY-MM-DD", "start_time": "HH:MM", "end_time": "HH:MM", "location": "", "description": ""}
</calendar_add>
Then write a brief confirmation line."""

    # Build messages with conversation history for context
    history = get_history(chat_id)
    messages = [{"role": "system", "content": system}] + history + [{"role": "user", "content": text}]

    response = await loop.run_in_executor(None, ask_openai, messages)

    # Save this exchange to conversation memory
    save_turn(chat_id, text, response)

    # Handle calendar add response
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
    chat_id = str(update.effective_chat.id)
    log.info(f"MSG: {text[:80]}")
    await update.message.chat.send_action(ChatAction.TYPING)
    try:
        reply = await process_message(text, chat_id)
        await update.message.reply_text(reply[:4096])
    except Exception as e:
        log.error(f"handle_text error: {e}")
        await update.message.reply_text(f"Error: {str(e)[:200]}")

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_chat.id) != CHAT_ID:
        return
    chat_id = str(update.effective_chat.id)
    await update.message.reply_text("Transcribing your voice note...")
    try:
        voice = update.message.voice or update.message.audio
        file = await context.bot.get_file(voice.file_id)
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            await file.download_to_drive(tmp.name)
            tmp_path = tmp.name
        client = openai.OpenAI(api_key=OPENAI_KEYS[_key_idx])
        with open(tmp_path, "rb") as af:
            transcription = client.audio.transcriptions.create(
                model="whisper-1", file=af
            ).text
        os.unlink(tmp_path)
        await update.message.reply_text(f"You said: {transcription}")
        await update.message.chat.send_action(ChatAction.TYPING)
        reply = await process_message(transcription, chat_id)
        await update.message.reply_text(reply[:4096])
    except Exception as e:
        log.error(f"handle_voice error: {e}")
        await update.message.reply_text(f"Voice error: {str(e)[:200]}")

async def handle_debug(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_chat.id) != CHAT_ID:
        return
    lines = [
        f"BOT_TOKEN: set",
        f"CHAT_ID: {CHAT_ID}",
        f"GCAL_ID: {GCAL_ID}",
        f"SERVICE_ACCOUNT_JSON: {'set (' + str(len(SA_JSON)) + ' chars)' if SA_JSON else 'MISSING'}",
        f"GMAIL_CLIENT_ID: {'set' if GMAIL_CLIENT_ID else 'MISSING'}",
        f"GMAIL_CLIENT_SECRET: {'set' if GMAIL_CLIENT_SEC else 'MISSING'}",
        f"GMAIL_REFRESH_TOKEN: {'set (' + str(len(GMAIL_REFRESH_TOK)) + ' chars)' if GMAIL_REFRESH_TOK else 'MISSING'}",
        f"OPENAI_KEYS: {len(OPENAI_KEYS)} loaded",
        "",
    ]
    token = get_gmail_token()
    if token:
        try:
            r = requests.get(
                "https://gmail.googleapis.com/gmail/v1/users/me/messages",
                headers={"Authorization": f"Bearer {token}"},
                params={"maxResults": 1, "q": "is:inbox"},
                timeout=10
            )
            msgs = r.json().get("messages", [])
            lines.append(f"Gmail: OK - {len(msgs)} message(s) found")
        except Exception as e:
            lines.append(f"Gmail: ERROR - {str(e)[:80]}")
    else:
        lines.append("Gmail: FAILED to get token")
    cal = get_calendar_events(7)
    if "unavailable" in cal.lower() or "error" in cal.lower():
        lines.append(f"Calendar: ERROR - {cal[:80]}")
    else:
        lines.append(f"Calendar: OK - {cal[:120]}")
    await update.message.reply_text("\n".join(lines))

async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_chat.id) != CHAT_ID:
        return
    await update.message.reply_text(
        "Hello Ajay! Your executive assistant is online.\n\n"
        "I can help with:\n"
        "- Calendar: view events, add meetings\n"
        "- Bills: StarHub, DBS, insurance, Sobha\n"
        "- Emails: payments, bookings, alerts\n"
        "- Voice notes: just hold the mic\n\n"
        "Type /debug to check system status."
    )

# ── Daily briefing ────────────────────────────────────────────────────────────
async def send_daily_briefing(app):
    try:
        log.info("Sending daily briefing...")
        today = datetime.now(SGT).strftime("%A, %d %B %Y")
        loop = asyncio.get_event_loop()
        cal_ctx   = await loop.run_in_executor(None, get_calendar_events, 7)
        email_ctx = await loop.run_in_executor(None, search_gmail,
            "bill OR invoice OR payment OR statement OR booking", 15)

        system = f"""You are Ajay's executive assistant. Today is {today} (SGT).
Generate a concise morning briefing covering:
1. Today's calendar events
2. This week's upcoming events
3. Any bills or payments due soon (from emails)
4. Any important emails or alerts

Use plain text. Be direct and executive-level. No markdown."""

        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": f"Calendar:\n{cal_ctx}\n\nEmails:\n{email_ctx}"}
        ]
        reply = await loop.run_in_executor(None, ask_openai, messages)
        header = f"Good morning! Daily Briefing — {today}\n\n"
        await app.bot.send_message(chat_id=CHAT_ID, text=(header + reply)[:4096])
    except Exception as e:
        log.error(f"Briefing error: {e}")
        await app.bot.send_message(chat_id=CHAT_ID, text=f"Briefing failed: {e}")

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    log.info("=== Bot v9 starting ===")
    log.info(f"OpenAI keys: {len(OPENAI_KEYS)}")
    log.info(f"Calendar ID: {GCAL_ID}")
    log.info(f"Gmail: {'configured' if GMAIL_REFRESH_TOK else 'NOT SET'}")
    log.info(f"Service account: {'set' if SA_JSON else 'MISSING'}")

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", handle_start))
    app.add_handler(CommandHandler("help",  handle_start))
    app.add_handler(CommandHandler("debug", handle_debug))
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

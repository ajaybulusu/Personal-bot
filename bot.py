"""
Personal Assistant Telegram Bot — v6 (Railway, fixed)
======================================================
- python-telegram-bot async
- OpenAI GPT-4o-mini (dual key failover, no Anthropic needed)
- Google Calendar via service account (works)
- Gmail gracefully skipped (personal Gmail needs OAuth — handled by Manus 8am briefing)
- APScheduler for daily 8am SGT briefing
- All secrets from Railway environment variables
"""

import os, re, json, logging, asyncio
from datetime import datetime, timezone, timedelta

import openai
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes
from google.oauth2 import service_account
from googleapiclient.discovery import build as google_build

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s"
)
log = logging.getLogger("bot")

# ── Config from environment ───────────────────────────────────────────────────
BOT_TOKEN  = os.environ["BOT_TOKEN"]
CHAT_ID    = os.environ["CHAT_ID"]
SA_JSON    = os.environ.get("SERVICE_ACCOUNT_JSON", "")
GCAL_ID    = os.environ.get("GCAL_ID", "primary")
SGT        = timezone(timedelta(hours=8))

# Dual OpenAI key support
OPENAI_KEYS = [k for k in [
    os.environ.get("OPENAI_KEY_PRIMARY", os.environ.get("OPENAI_API_KEY", "")),
    os.environ.get("OPENAI_KEY_BACKUP", ""),
] if k]

if not OPENAI_KEYS:
    raise RuntimeError("No OpenAI keys configured. Set OPENAI_KEY_PRIMARY in Railway environment variables.")

_key_idx = 0

def get_openai_client():
    return openai.OpenAI(api_key=OPENAI_KEYS[_key_idx])

# ── Google Calendar ───────────────────────────────────────────────────────────
def get_calendar_service():
    if not SA_JSON:
        raise RuntimeError("SERVICE_ACCOUNT_JSON not set")
    sa_info = json.loads(SA_JSON)
    creds = service_account.Credentials.from_service_account_info(
        sa_info, scopes=["https://www.googleapis.com/auth/calendar"]
    )
    return google_build("calendar", "v3", credentials=creds)

def fetch_upcoming_events(days: int = 14) -> list:
    try:
        svc = get_calendar_service()
        now = datetime.now(SGT)
        result = svc.events().list(
            calendarId=GCAL_ID,
            timeMin=now.isoformat(),
            timeMax=(now + timedelta(days=days)).isoformat(),
            maxResults=50,
            singleEvents=True,
            orderBy="startTime"
        ).execute()
        return result.get("items", [])
    except Exception as e:
        log.error(f"fetch_upcoming_events: {e}")
        return []

def format_events(events: list) -> str:
    if not events:
        return "No upcoming events."
    lines = []
    for e in events:
        s = e.get("start", {})
        dt = s.get("dateTime", s.get("date", ""))
        try:
            if "T" in dt:
                parsed = datetime.fromisoformat(dt)
                dt_fmt = parsed.strftime("%a %d %b, %I:%M%p")
            else:
                parsed = datetime.strptime(dt, "%Y-%m-%d")
                dt_fmt = parsed.strftime("%a %d %b (all day)")
        except:
            dt_fmt = dt
        loc = e.get("location", "")
        loc_str = f"  📍 {loc[:40]}" if loc else ""
        lines.append(f"• {e.get('summary', '(no title)')} — {dt_fmt}{loc_str}")
    return "\n".join(lines)

def create_calendar_event(summary, date, start_time=None, end_time=None, description="", location="") -> str:
    svc = get_calendar_service()
    if start_time:
        s_dt = f"{date}T{start_time}:00+08:00"
        if end_time:
            e_dt = f"{date}T{end_time}:00+08:00"
        else:
            from datetime import datetime as dt_cls
            e_dt = (dt_cls.fromisoformat(s_dt) + timedelta(hours=1)).isoformat()
        start = {"dateTime": s_dt, "timeZone": "Asia/Singapore"}
        end   = {"dateTime": e_dt, "timeZone": "Asia/Singapore"}
    else:
        start = end = {"date": date}

    body = {
        "summary": summary,
        "start": start,
        "end": end,
        "description": description,
        "location": location,
        "reminders": {
            "useDefault": False,
            "overrides": [
                {"method": "popup", "minutes": 30},
                {"method": "popup", "minutes": 1440}
            ]
        }
    }
    event = svc.events().insert(calendarId=GCAL_ID, body=body).execute()
    return event.get("htmlLink", "")

# ── AI layer: OpenAI with dual-key failover ───────────────────────────────────
SYSTEM_PROMPT = """You are Ajay's personal executive assistant based in Singapore (SGT = UTC+8).
Today is {today}.

CAPABILITIES:
1. Answer questions about calendar, schedule, reminders, travel, bills, investments
2. Add calendar events — when Ajay asks to schedule/add something, respond with:
   <calendar_add>
   {{"summary": "...", "date": "YYYY-MM-DD", "start_time": "HH:MM", "end_time": "HH:MM", "location": "", "description": ""}}
   </calendar_add>
   Then confirm in plain text.
3. Give reminders, briefings, and proactive suggestions

STYLE: Concise, direct, executive-level. Use bullet points for lists.
Flag urgent/overdue items with ⚠️. Keep responses under 300 words unless a full briefing is requested.
Do NOT use Markdown asterisks or underscores — use plain text only.

=== CALENDAR (next 14 days) ===
{calendar}"""

def ask_ai(user_message: str, calendar_ctx: str) -> str:
    global _key_idx
    today = datetime.now(SGT).strftime("%A, %d %B %Y")
    system = SYSTEM_PROMPT.format(today=today, calendar=calendar_ctx)

    for attempt in range(len(OPENAI_KEYS) * 2):
        try:
            client = get_openai_client()
            response = client.chat.completions.create(
                model="gpt-4o-mini",
                max_tokens=1024,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user",   "content": user_message}
                ]
            )
            return response.choices[0].message.content
        except Exception as e:
            err = str(e)
            log.error(f"OpenAI key {_key_idx} error: {err[:100]}")
            if "429" in err or "rate_limit" in err.lower() or "401" in err:
                _key_idx = 1 - _key_idx
                import time; time.sleep(2)
            else:
                return f"⚠️ AI error: {err[:150]}"

    return "⚠️ Both OpenAI keys failed — please try again in a moment."

# ── Core message processing ───────────────────────────────────────────────────
async def process_message(text: str) -> str:
    loop = asyncio.get_event_loop()
    events = await loop.run_in_executor(None, fetch_upcoming_events, 14)
    cal_ctx = format_events(events)
    response = ask_ai(text, cal_ctx)

    if "<calendar_add>" in response and "</calendar_add>" in response:
        match = re.search(r"<calendar_add>(.*?)</calendar_add>", response, re.DOTALL)
        clean_reply = re.sub(r"<calendar_add>.*?</calendar_add>", "", response, flags=re.DOTALL).strip()
        if match:
            try:
                d = json.loads(match.group(1).strip())
                await loop.run_in_executor(None, lambda: create_calendar_event(
                    d.get("summary", "New Event"),
                    d["date"],
                    d.get("start_time"),
                    d.get("end_time"),
                    d.get("description", ""),
                    d.get("location", "")
                ))
                t_str = f" at {d['start_time']}" if d.get("start_time") else ""
                e_str = f"–{d['end_time']}" if d.get("end_time") else ""
                l_str = f"\n📍 {d['location']}" if d.get("location") else ""
                return (
                    f"✅ Added to Google Calendar\n\n"
                    f"{d.get('summary', '')}\n"
                    f"🗓 {d['date']}{t_str}{e_str}{l_str}\n\n"
                    f"{clean_reply}"
                ).strip()
            except Exception as e:
                log.error(f"Calendar create failed: {e}")
                return f"⚠️ Couldn't add to calendar: {str(e)[:100]}\n\n{clean_reply}"

    return response

# ── Daily briefing ────────────────────────────────────────────────────────────
async def send_daily_briefing(app: Application):
    log.info("Sending daily briefing...")
    try:
        briefing_prompt = (
            "Give me my morning briefing. Include: "
            "1) Today's and this week's calendar events with times, "
            "2) Any reminders or things I might be forgetting, "
            "3) A brief note on what to focus on today. "
            "Be concise but complete. Note: email briefing is handled separately."
        )
        reply = await process_message(briefing_prompt)
        header = f"☀️ Good morning! Daily Briefing — {datetime.now(SGT).strftime('%A %d %B')}\n\n"
        await app.bot.send_message(chat_id=CHAT_ID, text=header + reply)
    except Exception as e:
        log.error(f"Daily briefing error: {e}")
        try:
            await app.bot.send_message(chat_id=CHAT_ID, text=f"⚠️ Briefing failed: {e}")
        except:
            pass

# ── Telegram handlers ─────────────────────────────────────────────────────────
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_chat.id) != CHAT_ID:
        return
    text = update.message.text.strip()
    log.info(f"MSG: {text[:80]}")
    await update.message.chat.send_action(ChatAction.TYPING)
    try:
        reply = await process_message(text)
        # Truncate if too long for Telegram
        if len(reply) > 4096:
            reply = reply[:4090] + "..."
        await update.message.reply_text(reply)
    except Exception as e:
        log.error(f"handle_text error: {e}")
        try:
            await update.message.reply_text(f"⚠️ Something went wrong: {str(e)[:150]}")
        except:
            pass

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_chat.id) != CHAT_ID:
        return
    voice = update.message.voice or update.message.audio
    if not voice:
        return
    await update.message.chat.send_action(ChatAction.TYPING)
    try:
        await update.message.reply_text("🎙 Transcribing your voice note...")
        file = await context.bot.get_file(voice.file_id)
        import tempfile, subprocess
        tmp = tempfile.mktemp(suffix=".ogg")
        await file.download_to_drive(tmp)
        client = get_openai_client()
        with open(tmp, "rb") as f:
            transcript = client.audio.transcriptions.create(
                model="whisper-1", file=f, response_format="text"
            )
        try:
            os.unlink(tmp)
        except:
            pass
        text = str(transcript).strip()
        log.info(f"VOICE: {text[:80]}")
        await update.message.reply_text(f"🎙 You said: {text}")
        reply = await process_message(text)
        if len(reply) > 4096:
            reply = reply[:4090] + "..."
        await update.message.reply_text(reply)
    except Exception as e:
        log.error(f"handle_voice error: {e}")
        await update.message.reply_text(f"⚠️ Voice transcription failed: {str(e)[:100]}")

async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_chat.id) != CHAT_ID:
        return
    await update.message.reply_text(
        "🤖 Personal Assistant is online!\n\n"
        "I can help with:\n"
        "• 📅 Calendar — 'What's on this week?' / 'Add dentist Friday 3pm'\n"
        "• ☀️ Briefing — 'Give me my morning briefing'\n"
        "• 🎙 Voice notes — just send a voice message\n"
        "• 💳 Bills & payments — ask anything\n"
        "• ✈️ Travel — flights, bookings\n"
        "• 📊 Investments — IBKR, DBS\n\n"
        "Talk to me like you would a human assistant."
    )

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    log.info("Starting bot v6...")
    log.info(f"OpenAI keys configured: {len(OPENAI_KEYS)}")
    log.info(f"Service account: {'set' if SA_JSON else 'MISSING'}")
    log.info(f"Calendar ID: {GCAL_ID}")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", handle_start))
    app.add_handler(CommandHandler("help",  handle_start))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    # Schedule daily briefing at 8:00 AM SGT
    scheduler = AsyncIOScheduler(timezone="Asia/Singapore")
    scheduler.add_job(
        send_daily_briefing,
        trigger="cron",
        hour=8, minute=0,
        args=[app],
        id="daily_briefing"
    )
    scheduler.start()
    log.info("Scheduler started — daily briefing at 08:00 SGT")

    log.info("Bot polling...")
    app.run_polling(allowed_updates=["message"], drop_pending_updates=False)

if __name__ == "__main__":
    main()

"""
Personal Assistant Telegram Bot
================================
Simple, reliable, async. Reads Gmail + Google Calendar, answers questions,
adds calendar events, sends a daily 8am SGT briefing.

Architecture:
  - python-telegram-bot (async, no subprocess curl nonsense)
  - Anthropic Claude API for AI responses
  - Google APIs via OAuth service account (Calendar) + Gmail API
  - APScheduler for daily briefing
  - All state in memory — restarts cleanly with no stale file issues
"""

import os, re, json, logging, asyncio
from datetime import datetime, timezone, timedelta
from base64 import urlsafe_b64decode
from email import message_from_bytes

import anthropic
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
BOT_TOKEN        = os.environ["BOT_TOKEN"]
CHAT_ID          = os.environ["CHAT_ID"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
GCAL_ID          = os.environ.get("GCAL_ID", "primary")
SA_JSON          = os.environ["SERVICE_ACCOUNT_JSON"]   # full JSON string

SGT = timezone(timedelta(hours=8))

# ── Google credentials (from env var — no file writing needed) ────────────────
import json as _json, tempfile, os as _os

def _get_google_creds(scopes):
    sa_info = _json.loads(SA_JSON)
    return service_account.Credentials.from_service_account_info(sa_info, scopes=scopes)

def get_calendar_service():
    creds = _get_google_creds(["https://www.googleapis.com/auth/calendar"])
    return google_build("calendar", "v3", credentials=creds)

def get_gmail_service():
    creds = _get_google_creds([
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://mail.google.com/"
    ])
    return google_build("gmail", "v1", credentials=creds)

# ── Calendar helpers ───────────────────────────────────────────────────────────
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
        # Parse and reformat nicely
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
    try:
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
    except Exception as e:
        log.error(f"create_calendar_event: {e}")
        raise

# ── Gmail helpers ──────────────────────────────────────────────────────────────
def fetch_recent_emails(max_results: int = 15) -> list[dict]:
    """Returns list of {from, subject, date, snippet, body_preview}"""
    try:
        svc = get_gmail_service()
        msgs = svc.users().messages().list(
            userId="me",
            maxResults=max_results,
            labelIds=["INBOX"],
            q="is:unread OR newer_than:2d"
        ).execute().get("messages", [])

        emails = []
        for m in msgs[:max_results]:
            try:
                full = svc.users().messages().get(
                    userId="me", id=m["id"], format="metadata",
                    metadataHeaders=["From", "Subject", "Date"]
                ).execute()
                headers = {h["name"]: h["value"] for h in full.get("payload", {}).get("headers", [])}
                emails.append({
                    "from":    headers.get("From", ""),
                    "subject": headers.get("Subject", "(no subject)"),
                    "date":    headers.get("Date", ""),
                    "snippet": full.get("snippet", ""),
                    "id":      m["id"]
                })
            except Exception as e:
                log.warning(f"Email fetch partial error: {e}")
        return emails
    except Exception as e:
        log.error(f"fetch_recent_emails: {e}")
        return []

def format_emails(emails: list) -> str:
    if not emails:
        return "No recent emails."
    lines = []
    for e in emails:
        lines.append(f"• *{e['subject']}*\n  From: {e['from'][:50]}\n  {e['snippet'][:100]}")
    return "\n\n".join(lines)

# ── Claude AI ─────────────────────────────────────────────────────────────────
client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

SYSTEM_PROMPT = """You are Ajay's personal executive assistant based in Singapore (SGT = UTC+8).
Today is {today}.

You have access to Ajay's Gmail and Google Calendar context below.

CAPABILITIES:
1. Answer questions about emails, calendar, schedule, reminders
2. Add calendar events — when Ajay asks to schedule/add something, respond with a JSON block:
   <calendar_add>
   {{"summary": "...", "date": "YYYY-MM-DD", "start_time": "HH:MM", "end_time": "HH:MM", "location": "", "description": ""}}
   </calendar_add>
   Then confirm in plain text.
3. Give reminders, briefings, and proactive suggestions

STYLE: Concise, direct, executive-level. Use *bold* for key info, bullet points for lists.
Flag urgent/overdue items with ⚠️. Keep responses under 300 words unless a full briefing is requested.

=== CALENDAR (next 14 days) ===
{calendar}

=== RECENT EMAILS (last 2 days / unread) ===
{emails}"""

def ask_claude(user_message: str, calendar_ctx: str, email_ctx: str) -> str:
    today = datetime.now(SGT).strftime("%A, %d %B %Y")
    system = SYSTEM_PROMPT.format(today=today, calendar=calendar_ctx, emails=email_ctx)
    try:
        response = client.messages.create(
            model="claude-opus-4-5",
            max_tokens=1024,
            system=system,
            messages=[{"role": "user", "content": user_message}]
        )
        return response.content[0].text
    except Exception as e:
        log.error(f"Claude API error: {e}")
        return f"⚠️ AI error: {str(e)[:120]}"

# ── Core message processing ───────────────────────────────────────────────────
async def process_message(text: str, app: Application) -> str:
    """Fetch context, call Claude, handle calendar adds. Returns reply string."""
    # Fetch fresh context in parallel
    loop = asyncio.get_event_loop()
    events_fut = loop.run_in_executor(None, fetch_upcoming_events, 14)
    emails_fut = loop.run_in_executor(None, fetch_recent_emails, 15)
    events, emails = await asyncio.gather(events_fut, emails_fut)

    cal_ctx   = format_events(events)
    email_ctx = format_emails(emails)

    response = ask_claude(text, cal_ctx, email_ctx)

    # Handle calendar add if Claude returned one
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
                    f"✅ *Added to Google Calendar*\n\n"
                    f"*{d.get('summary', '')}*\n"
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
            "1) Today's calendar events with times, "
            "2) Any important/unread emails that need attention, "
            "3) Key reminders or anything I might be forgetting. "
            "Be concise but complete."
        )
        reply = await process_message(briefing_prompt, app)
        header = f"☀️ *Good morning! Daily Briefing — {datetime.now(SGT).strftime('%A %d %B')}*\n\n"
        await app.bot.send_message(
            chat_id=CHAT_ID,
            text=header + reply,
            parse_mode="Markdown"
        )
    except Exception as e:
        log.error(f"Daily briefing error: {e}")
        await app.bot.send_message(chat_id=CHAT_ID, text=f"⚠️ Briefing failed: {e}")

# ── Telegram handlers ─────────────────────────────────────────────────────────
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_chat.id) != CHAT_ID:
        return

    text = update.message.text.strip()
    log.info(f"MSG: {text[:80]}")

    await update.message.chat.send_action(ChatAction.TYPING)
    reply = await process_message(text, context.application)
    await update.message.reply_text(reply, parse_mode="Markdown")

async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_chat.id) != CHAT_ID:
        return
    await update.message.reply_text(
        "🤖 *Personal Assistant is online!*\n\n"
        "I can help with:\n"
        "• 📅 Calendar — 'What's on this week?' / 'Add dentist Friday 3pm'\n"
        "• 📧 Email — 'Any urgent emails?' / 'What did X send me?'\n"
        "• ☀️ Briefing — 'Give me my morning briefing'\n"
        "• 🔔 Reminders — just ask naturally\n\n"
        "Talk to me like you would a human assistant.",
        parse_mode="Markdown"
    )

# ── App startup ───────────────────────────────────────────────────────────────
def main():
    log.info("Starting bot...")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", handle_start))
    app.add_handler(CommandHandler("help",  handle_start))
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
    app.run_polling(allowed_updates=["message"], drop_pending_updates=True)

if __name__ == "__main__":
    main()

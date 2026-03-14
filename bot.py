"""
Personal Assistant Telegram Bot
================================
Simple, reliable, async. Reads Gmail + Google Calendar, answers questions,
adds calendar events, sends a daily 8am SGT briefing.

Architecture:
  - python-telegram-bot (async)
  - Anthropic Claude API (primary) + OpenAI GPT (fallback)
  - Google APIs via service account (Calendar + Gmail)
  - APScheduler for daily briefing
  - All state in memory — restarts cleanly
"""

import os, re, json, logging, asyncio
from datetime import datetime, timezone, timedelta

import anthropic
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
BOT_TOKEN         = os.environ["BOT_TOKEN"]
CHAT_ID           = os.environ["CHAT_ID"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
OPENAI_API_KEY    = os.environ.get("OPENAI_API_KEY", "")
GCAL_ID           = os.environ.get("GCAL_ID", "primary")
SA_JSON           = os.environ["SERVICE_ACCOUNT_JSON"]

SGT = timezone(timedelta(hours=8))

# ── Google credentials ──────────────────────────────────────────────────────
def _get_google_creds(scopes):
    sa_info = json.loads(SA_JSON)
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
        loc_str = f"  \ud83d\udccd {loc[:40]}" if loc else ""
        lines.append(f"\u2022 {e.get('summary', '(no title)')} \u2014 {dt_fmt}{loc_str}")
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
        subj = e['subject'].replace('*', '').replace('_', '')
        sender = e['from'][:50].replace('*', '').replace('_', '')
        snippet = e['snippet'][:100].replace('*', '').replace('_', '')
        lines.append(f"\u2022 {subj}\n  From: {sender}\n  {snippet}")
    return "\n\n".join(lines)

# ── AI layer: Claude primary, OpenAI fallback ─────────────────────────
claude_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
openai_client = openai.OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

SYSTEM_PROMPT = """You are Ajay's personal executive assistant based in Singapore (SGT = UTC+8).
Today is {today}.

You have access to Ajay's Gmail and Google Calendar context below.

CAPABILITIES:
1. Answer questions about emails, calendar, schedule, reminders
2. Add calendar events \u2014 when Ajay asks to schedule/add something, respond with a JSON block:
   <calendar_add>
   {{"summary": "...", "date": "YYYY-MM-DD", "start_time": "HH:MM", "end_time": "HH:MM", "location": "", "description": ""}}
   </calendar_add>
   Then confirm in plain text.
3. Give reminders, briefings, and proactive suggestions

STYLE: Concise, direct, executive-level. Use bullet points for lists.
Flag urgent/overdue items with \u26a0\ufe0f. Keep responses under 300 words unless a full briefing is requested.

IMPORTANT FORMATTING RULES \u2014 you MUST follow these:
- Do NOT use Markdown. No asterisks, underscores, or square brackets.
- Use CAPS, emoji, or plain text for emphasis instead.
- Keep formatting simple and plain-text friendly.

=== CALENDAR (next 14 days) ===
{calendar}

=== RECENT EMAILS (last 2 days / unread) ===
{emails}"""

def ask_ai(user_message: str, calendar_ctx: str, email_ctx: str) -> str:
    """Try Claude first; if it fails, fall back to OpenAI GPT."""
    today = datetime.now(SGT).strftime("%A, %d %B %Y")
    system = SYSTEM_PROMPT.format(today=today, calendar=calendar_ctx, emails=email_ctx)

    # \u2500\u2500 Try Claude first \u2500\u2500
    try:
        log.info("Calling Claude...")
        response = claude_client.messages.create(
            model="claude-opus-4-5-20251101",
            max_tokens=1024,
            system=system,
            messages=[{"role": "user", "content": user_message}]
        )
        return response.content[0].text
    except Exception as e:
        log.error(f"Claude API error: {e}")

    # \u2500\u2500 Fall back to OpenAI \u2500\u2500
    if openai_client:
        try:
            log.info("Falling back to OpenAI...")
            response = openai_client.chat.completions.create(
                model="gpt-4o",
                max_tokens=1024,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_message}
                ]
            )
            return response.choices[0].message.content
        except Exception as e:
            log.error(f"OpenAI API error: {e}")
            return f"\u26a0\ufe0f Both AI providers failed.\nClaude error + OpenAI error: {str(e)[:150]}"

    return "\u26a0\ufe0f Claude API failed and no OpenAI fallback configured."

# \u2500\u2500 Safe Telegram send \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
def clean_for_telegram(text: str) -> str:
    """Strip characters that break Telegram's Markdown parser."""
    text = re.sub(r"<[^>]+>", "", text)       # strip XML/HTML tags
    text = text.replace("**", "").replace("__", "")  # strip bold/italic markdown
    return text

async def safe_reply(target, text, chat_id=None):
    """Send a message. Falls back to stripped plain text if anything fails."""
    text = clean_for_telegram(text)
    try:
        if chat_id:
            await target.send_message(chat_id=chat_id, text=text)
        else:
            await target.reply_text(text)
    except Exception as e:
        log.error(f"Telegram send failed: {e}")
        plain = re.sub(r'[*_`\\[\\]()]', '', text)
        try:
            if chat_id:
                await target.send_message(chat_id=chat_id, text=plain[:4096])
            else:
                await target.reply_text(plain[:4096])
        except Exception as e2:
            log.error(f"Even plain send failed: {e2}")

# \u2500\u2500 Core message processing \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
async def process_message(text: str, app: Application) -> str:
    """Fetch context, call AI, handle calendar adds."""
    loop = asyncio.get_event_loop()
    events_fut = loop.run_in_executor(None, fetch_upcoming_events, 14)
    emails_fut = loop.run_in_executor(None, fetch_recent_emails, 15)
    events, emails = await asyncio.gather(events_fut, emails_fut)

    cal_ctx   = format_events(events)
    email_ctx = format_emails(emails)

    response = ask_ai(text, cal_ctx, email_ctx)

    # Handle calendar add if AI returned one
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
                e_str = f"\u2013{d['end_time']}" if d.get("end_time") else ""
                l_str = f"\n\ud83d\udccd {d['location']}" if d.get("location") else ""
                return (
                    f"\u2705 Added to Google Calendar\n\n"
                    f"{d.get('summary', '')}\n"
                    f"\ud83d\uddd3 {d['date']}{t_str}{e_str}{l_str}\n\n"
                    f"{clean_reply}"
                ).strip()
            except Exception as e:
                log.error(f"Calendar create failed: {e}")
                return f"\u26a0\ufe0f Couldn't add to calendar: {str(e)[:100]}\n\n{clean_reply}"

    return response

# \u2500\u2500 Daily briefing \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
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
        header = f"\u2600\ufe0f Good morning! Daily Briefing \u2014 {datetime.now(SGT).strftime('%A %d %B')}\n\n"
        await safe_reply(app.bot, header + reply, chat_id=CHAT_ID)
    except Exception as e:
        log.error(f"Daily briefing error: {e}")
        try:
            await app.bot.send_message(chat_id=CHAT_ID, text=f"\u26a0\ufe0f Briefing failed: {e}")
        except:
            pass

# \u2500\u2500 Telegram handlers \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_chat.id) != CHAT_ID:
        return

    text = update.message.text.strip()
    log.info(f"MSG: {text[:80]}")

    await update.message.chat.send_action(ChatAction.TYPING)
    try:
        reply = await process_message(text, context.application)
        await safe_reply(update.message, reply)
    except Exception as e:
        log.error(f"handle_text error: {e}")
        try:
            await update.message.reply_text(f"\u26a0\ufe0f Something went wrong: {str(e)[:150]}")
        except:
            pass

async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_chat.id) != CHAT_ID:
        return
    await update.message.reply_text(
        "\ud83e\udd16 Personal Assistant is online!\n\n"
        "I can help with:\n"
        "\u2022 \ud83d\udcc5 Calendar \u2014 'What's on this week?' / 'Add dentist Friday 3pm'\n"
        "\u2022 \ud83d\udce7 Email \u2014 'Any urgent emails?' / 'What did X send me?'\n"
        "\u2022 \u2600\ufe0f Briefing \u2014 'Give me my morning briefing'\n"
        "\u2022 \ud83d\udd14 Reminders \u2014 just ask naturally\n\n"
        "Talk to me like you would a human assistant."
    )

# \u2500\u2500 App startup \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
def main():
    log.info("Starting bot...")
    log.info(f"Claude API key: {'set' if ANTHROPIC_API_KEY else 'MISSING'}")
    log.info(f"OpenAI API key: {'set' if OPENAI_API_KEY else 'not set (no fallback)'}")

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
    log.info("Scheduler started \u2014 daily briefing at 08:00 SGT")

    log.info("Bot polling...")
    app.run_polling(allowed_updates=["message"], drop_pending_updates=True)

if __name__ == "__main__":
    main()

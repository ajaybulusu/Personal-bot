# Personal Assistant Bot — Setup Guide

## What's in this bot
- Reads Gmail (recent + unread emails)
- Reads & writes Google Calendar
- Answers questions via Claude AI (with OpenAI GPT fallback)
- Daily 8:00 AM SGT briefing
- Runs 24/7 on Railway

---

## Step 1 — Get your Telegram Bot Token + Chat ID

1. Open Telegram → search **@BotFather** → `/newbot` → follow prompts
2. Copy the **BOT_TOKEN** it gives you
3. Start a chat with your new bot, send any message
4. Visit: `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates`
5. Find `"chat":{"id":XXXXXXX}` — that number is your **CHAT_ID**

---

## Step 2 — Get your API Keys

### Anthropic (primary)
1. Go to https://console.anthropic.com
2. API Keys → Create Key
3. Copy it — this is your **ANTHROPIC_API_KEY**

### OpenAI (fallback)
1. Go to https://platform.openai.com/api-keys
2. Create Key
3. Copy it — this is your **OPENAI_API_KEY**

---

## Step 3 — Set up Google Service Account (for Gmail + Calendar)

1. Go to https://console.cloud.google.com
2. Create a new project (or use existing)
3. Enable these APIs:
   - **Google Calendar API**
   - **Gmail API**
4. Go to **IAM & Admin → Service Accounts → Create Service Account**
   - Name it anything (e.g. "personal-bot")
   - No roles needed at project level
5. Click the service account → **Keys → Add Key → JSON**
   - Download the JSON file
6. Open the JSON file in a text editor → copy the ENTIRE contents
   - This is your **SERVICE_ACCOUNT_JSON** env var

### Share your Calendar with the service account:
1. Open Google Calendar → Settings → your calendar → Share with specific people
2. Add the service account email (looks like `something@project.iam.gserviceaccount.com`)
3. Give it **"Make changes to events"** permission

### Grant Gmail access via Domain-Wide Delegation:
1. In Google Cloud Console → your Service Account → **Edit → Show Advanced**
2. Enable **Domain-wide delegation** → save
3. Go to https://admin.google.com → Security → API Controls → Domain-wide delegation
4. Add the Client ID of your service account
5. Scopes to add:
   ```
   https://www.googleapis.com/auth/gmail.readonly,https://mail.google.com/
   ```

> **If you're on personal Gmail (not Google Workspace):**
> Gmail domain-wide delegation requires Workspace. For personal accounts,
> use OAuth2 instead — or skip Gmail and the bot will work with Calendar only.
> Just remove the `fetch_recent_emails` calls in bot.py.

---

## Step 4 — Deploy to Railway

1. Go to https://railway.app → New Project → Deploy from GitHub repo
   (Push this code to a GitHub repo first, or use Railway CLI)

2. Set these **Environment Variables** in Railway dashboard:

   | Variable | Value |
   |---|---|
   | `BOT_TOKEN` | Your Telegram bot token |
   | `CHAT_ID` | Your Telegram chat ID (number) |
   | `ANTHROPIC_API_KEY` | Your Anthropic API key |
   | `OPENAI_API_KEY` | Your OpenAI API key (fallback) |
   | `SERVICE_ACCOUNT_JSON` | The full contents of your service account JSON |
   | `GCAL_ID` | Your calendar email (e.g. `yourname@gmail.com`) or `primary` |

3. Deploy → Railway will build and run the bot

4. Check **Logs** tab — you should see:
   ```
   Bot polling...
   Scheduler started — daily briefing at 08:00 SGT
   ```

---

## Usage

Talk to your bot naturally:

- `What's on my calendar this week?`
- `Any urgent emails?`
- `Add dentist appointment Friday 3pm`
- `Schedule team lunch next Tuesday 12:30-2pm at Marina Bay Sands`
- `Give me my morning briefing`
- `What did [person] email me about?`

---

## Troubleshooting

**Bot not responding:**
- Check Railway logs for errors
- Verify BOT_TOKEN and CHAT_ID are correct
- Make sure you've started the bot in Telegram first (/start)

**Calendar not working:**
- Make sure the Calendar API is enabled in Google Cloud
- Make sure you've shared your calendar with the service account email

**Gmail not working:**
- Domain-wide delegation only works with Google Workspace
- For personal Gmail, consider removing email features or setting up OAuth2

**Railway keeps restarting:**
- Check logs for Python errors
- The `railway.toml` has auto-restart configured — check the error before it restarts

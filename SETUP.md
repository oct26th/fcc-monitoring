# FCC Monitoring Crawler - Setup Instructions

## Overview

This crawler monitors FCC equipment approvals for specified grantee codes. Two implementations are available:

1. **SpiderCloud API** (`crawler.py`) - Uses SpiderCloud API (default, reliable)
2. **Browser Automation** (`crawler_browser.py`) - Uses Playwright to scrape FCC.gov directly

## Monitored Grantee Codes

```
UZ7, HD5, U4F, U4G, V2X, SS4, HLE, 2AOJL, 2AC6A, 2AR9L
```

## Prerequisites

```bash
# Install dependencies
cd /home/node/.openclaw/workspace-shinji/fcc-monitor
pip install -r requirements.txt

# Install Playwright browsers (for browser-based crawler)
playwright install chromium
```

## Environment Variables

Create a `.env` file or set these environment variables:

```bash
# Required: SpiderCloud API Key (for fallback)
export SPIDERCLOUD_API_KEY="sk-ac0423ad-1897-4094-bc2c-5e5a92777375"

# Optional: Browser to use (chromium, firefox, webkit)
export PLAYWRIGHT_BROWSER="chromium"

# Optional: Run browser in visible mode (for debugging)
export HEADLESS="false"

# Optional: Telegram notifications
export TELEGRAM_BOT_TOKEN="your-bot-token"
export TELEGRAM_CHAT_ID="your-chat-id"

# Optional: Discord webhook notifications
export DISCORD_WEBHOOK_URL="your-webhook-url"

# Optional: Logging
export LOG_LEVEL="INFO"
```

## Usage

### SpiderCloud API Crawler (Default)

```bash
cd /home/node/.openclaw/workspace-shinji/fcc-monitor
python crawler.py
```

### Browser-Based Crawler (Playwright)

```bash
cd /home/node/.openclaw/workspace-shinji/fcc-monitor
python crawler_browser.py
```

The browser-based crawler:
- Uses Playwright for browser automation
- Bypasses anti-bot protection on FCC.gov
- Falls back to SpiderCloud API if browser fails
- Takes screenshots on errors for debugging

### Run as Daemon

```bash
# SpiderCloud version
python crawler.py --daemon

# Browser version (recommended for better coverage)
python crawler_browser.py --daemon --interval 3600
```

### Run via Cron (Recommended)

```bash
# Edit crontab
crontab -e

# Add this line for daily execution at 14:00 UTC
0 14 * * * /usr/bin/python3 /home/node/.openclaw/workspace-shinji/fcc-monitor/crawler_browser.py >> /home/node/.openclaw/workspace-shinji/fcc-monitor/logs/cron.log 2>&1
```

## Database & Data Storage

- **SQLite Database**: `data/fcc_monitor.db`
- **State File**: `data/last_state.json`
- **Logs**: `logs/fcc_monitor.log`

## Telegram Setup

1. Create a bot via @BotFather on Telegram
2. Get your bot token
3. Start a chat with your bot
4. Get chat ID using: `https://api.telegram.org/bot<TOKEN>/getUpdates`

## Cron Schedule Examples

```bash
# Daily at 14:00 UTC (recommended)
0 14 * * *

# Twice daily
0 8,20 * * *

# Every 6 hours
0 */6 * * *
```

## Testing

```bash
# Test with verbose logging
LOG_LEVEL=DEBUG python crawler.py

# Test Telegram notification (requires token)
python -c "
from crawler import TelegramNotifier
n = TelegramNotifier('TOKEN', 'CHAT_ID')
n.send_new_records_notification([])
"
```

## Monitoring

```bash
# Check last run status
cat /home/node/.openclaw/workspace-shinji/fcc-monitor/data/last_state.json

# View recent logs
tail -50 /home/node/.openclaw/workspace-shinji/fcc-monitor/logs/fcc_monitor.log
```

## Troubleshooting

1. **No data fetched**: Check API key validity
2. **Telegram not working**: Verify bot token and chat ID
3. **Database errors**: Check write permissions on data directory

---

**NERV System Status**: Operational  
**Last Updated**: 2026-03-02

"""Notification management for FCC Monitor."""
import json
import logging
from typing import List, Optional
from urllib import request, parse

from .models import FCCRecord

logger = logging.getLogger("fcc_monitor.notifier")

class Notifier:
    """Sends notifications via Telegram and Discord."""
    
    def __init__(self, telegram_token: str = "", telegram_chat_id: str = "", discord_webhook: str = ""):
        self.telegram_token = telegram_token
        self.telegram_chat_id = telegram_chat_id
        self.discord_webhook = discord_webhook
        
    def send_telegram(self, message: str):
        """Send message via Telegram."""
        if not self.telegram_token or not self.telegram_chat_id:
            logger.debug("Telegram not configured, skipping")
            return False
            
        url = f"https://api.telegram.org/bot{self.telegram_token}/sendMessage"
        data = parse.urlencode({
            "chat_id": self.telegram_chat_id,
            "text": message,
            "parse_mode": "HTML"
        }).encode()
        
        try:
            req = request.Request(url, data=data)
            with request.urlopen(req, timeout=10) as response:
                if response.status == 200:
                    logger.info("Telegram notification sent")
                    return True
        except Exception as e:
            logger.error(f"Telegram notification failed: {e}")
            
        return False

    def send_discord(self, message: str):
        """Send message via Discord Webhook."""
        if not self.discord_webhook:
            logger.debug("Discord webhook not configured, skipping")
            return False
            
        data = json.dumps({
            "content": message,
            "username": "NERV Monitor"
        }).encode('utf-8')
        
        try:
            req = request.Request(
                self.discord_webhook, 
                data=data, 
                headers={'Content-Type': 'application/json'}
            )
            with request.urlopen(req, timeout=10) as response:
                if response.status in [200, 204]:
                    logger.info("Discord notification sent")
                    return True
        except Exception as e:
            logger.error(f"Discord notification failed: {e}")
            
        return False

    def notify_new_records(self, new_records: List[FCCRecord]):
        """Format and send notification for new FCC records."""
        if not new_records:
            return
            
        count = len(new_records)
        msg = f"📡 <b>NERV FCC Monitor - 新型號授權回報</b>\n\n抓取到 {count} 筆新紀錄：\n"
        
        # List up to 10 records in detail
        for r in new_records[:10]:
            msg += f"• <code>{r.fcc_id}</code>: {r.applicant_name[:40]}\n"
            
        if count > 10:
            msg += f"\n... 以及另外 {count - 10} 筆新紀錄。"
            
        self.send_telegram(msg)
        self.send_discord(msg)

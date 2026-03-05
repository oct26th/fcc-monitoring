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
        """Format and send notification for new FCC records, grouped by grantee."""
        if not new_records:
            return
            
        count = len(new_records)
        msg = f"📡 <b>NERV FCC Monitor - 新型號授權回報</b>\n"
        msg += f"偵測到 {count} 筆新紀錄，摘要如下：\n\n"
        
        # Group records by grantee code
        grouped: dict[str, List[FCCRecord]] = {}
        for r in new_records:
            code = r.grantee_code
            if code not in grouped:
                grouped[code] = []
            grouped[code].append(r)
            
        # Build the message grouped by brand
        for code, records in grouped.items():
            # Get clean brand name from first record
            brand_name = records[0].applicant_name.split('(')[0].strip()
            # Ultimate format: 📱 CODE (Brand Name)
            msg += f"📱 <b>{code} ({brand_name})</b>\n"
            
            for r in records[:20]: # Show up to 20 per brand
                # Use application type or description
                type_info = r.application_type if r.application_type else r.product_description
                if not type_info: type_info = "New Filing"
                
                # Truncate if too long
                if len(type_info) > 30:
                    type_info = type_info[:27] + ".."
                
                # Try to normalize date to YYYY/MM/DD if it's MM/DD/YYYY
                date_str = r.grant_date
                if '/' in date_str and len(date_str) == 10:
                    parts = date_str.split('/')
                    if len(parts[2]) == 4: # MM/DD/YYYY -> YYYY/MM/DD
                        date_str = f"{parts[2]}/{parts[0]}/{parts[1]}"
                
                msg += f"- {r.fcc_id} ({type_info}) - {date_str}\n"
            msg += "\n"
            
        if count > 20:
            msg += f"<i>... 總計 {count} 筆新機情報已入庫。</i>"
            
        self.send_telegram(msg)
        self.send_discord(msg)

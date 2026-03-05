"""Notification management for FCC Monitor."""
import json
from typing import List, Optional
from urllib import request, parse

from loguru import logger

from .models import FCCRecord

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
            
        brand_map = {
            "Datalogic S.r.l.": "Datalogic",
            "Unitech Electronics Co., Ltd.": "Unitech",
            "Honeywell International Inc": "Honeywell",
            "Honeywell International Inc.": "Honeywell",
            "Zebra Technologies Corporation": "Zebra",
            "Symbol Technologies Inc": "Symbol",
            "Motorola Solutions, Inc.": "Motorola",
            "CipherLab Co., Ltd.": "CipherLab",
            "Honeywell Safety and Productivity Solutions": "Honeywell",
            "CipherLab Co Ltd": "CipherLab",
            "Point Mobile Co., LTD.": "Point Mobile",
        }

        # Build the message grouped by brand
        for code, records in grouped.items():
            # Get clean brand name from first record
            raw_brand = records[0].applicant_name.split('(')[0].strip()
            brand_name = brand_map.get(raw_brand, raw_brand.split(' ')[0])
            
            # Ultimate format: 🏢 CODE (Brand Name)
            msg += f"🏢 {code} ({brand_name})\n"
            
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
            msg += f"... 總計 {count} 筆新機情報已入庫。"
            
        self.send_telegram(msg)
        self.send_discord(msg)

    # =========================================================================
    # Brand Crawl Notifications
    # =========================================================================
    
    def notify_brand_crawl_complete(
        self, 
        brand: str, 
        fcc_id: str, 
        product_name: str = None,
        success: bool = True,
        error: str = None,
        specs: dict = None,
        source_url: str = None
    ):
        """
        Notify when brand crawl operation completes.
        
        Args:
            brand: Brand name (e.g., "Honeywell", "Zebra")
            fcc_id: FCC ID
            product_name: Product name if found
            success: Whether crawl was successful
            error: Error message if failed
            specs: Full specification dictionary for detailed notification
            source_url: Product page URL on brand website (if available)
        """
        brand_emoji = {
            "honeywell": "🐝",
            "zebra": "🦓"
        }
        
        emoji = brand_emoji.get(brand.lower(), "📡")
        
        if success:
            msg = f"{emoji} <b>{brand.title()} 官網資料庫已更新</b>\n"
            msg += f"FCC ID: <code>{fcc_id}</code>\n"
            if product_name:
                msg += f"產品: {product_name}\n"
            
            # Add source URL or FCC link for direct access
            if source_url:
                msg += f"🔗 官網: <a href=\"{source_url}\">查看產品頁面</a>\n"
            else:
                # Fallback to FCC search link
                fcc_search_url = f"https://apps.fcc.gov/oetcf/eas/reports/GenericSearchResult.cfm?SearchType=All&FCCID={fcc_id}"
                msg += f"🔗 FCC: <a href=\"{fcc_search_url}\">查看FCC資料</a>\n"
            
            # FIX: Include specific specs in the notification
            if specs:
                # Extract key specs to display
                key_fields = []
                if specs.get("product_name"):
                    key_fields.append(f"📱 名稱: {specs['product_name']}")
                if specs.get("model_number"):
                    key_fields.append(f"🔢 型號: {specs['model_number']}")
                if specs.get("frequency"):
                    key_fields.append(f"📶 頻率: {specs['frequency']}")
                if specs.get("output_power"):
                    key_fields.append(f"⚡ 功率: {specs['output_power']}")
                if specs.get("antenna_type"):
                    key_fields.append(f"📡 天線: {specs['antenna_type']}")
                
                if key_fields:
                    msg += "\n" + "\n".join(key_fields[:3])  # Show max 3 key specs
            
            msg += f"\n狀態: ✅ 成功抓取規格資料"
        else:
            msg = f"{emoji} <b>{brand.title()} 官網資料抓取失敗</b>\n"
            msg += f"FCC ID: <code>{fcc_id}</code>\n"
            # Always show FCC link even on failure
            fcc_search_url = f"https://apps.fcc.gov/oetcf/eas/reports/GenericSearchResult.cfm?SearchType=All&FCCID={fcc_id}"
            msg += f"🔗 FCC: <a href=\"{fcc_search_url}\">查看FCC資料</a>\n"
            msg += f"錯誤: {error or 'Unknown error'}\n"
            msg += f"狀態: ⏳ 待重試"
        
        self.send_telegram(msg)
        self.send_discord(msg)

    def notify_c2pc_alert(self, alert_message: str):
        """
        Send C2PC hardware silent upgrade alert.
        
        Args:
            alert_message: Pre-formatted C2PC alert message
        """
        self.send_telegram(alert_message)
        self.send_discord(alert_message)

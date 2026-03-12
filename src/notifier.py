"""Notification management for FCC Monitor."""
import json
import mimetypes
import uuid
from pathlib import Path
from typing import List, Optional
from urllib import request, parse

from loguru import logger

from .models import FCCRecord

_C2PC_KEYWORDS = {"class ii permissive change", "class ii", "c2pc", "permissive change"}

def _is_c2pc(application_type: Optional[str]) -> bool:
    if not application_type:
        return False
    t = application_type.lower()
    return any(k in t for k in _C2PC_KEYWORDS)

def _fmt_date(date_str: str) -> str:
    """Normalize MM/DD/YYYY → YYYY-MM-DD, leave other formats as-is."""
    if not date_str:
        return ""
    parts = date_str.split("/")
    if len(parts) == 3 and len(parts[2]) == 4:
        return f"{parts[2]}-{parts[0].zfill(2)}-{parts[1].zfill(2)}"
    return date_str


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

    def send_telegram_document(self, file_path: Path, caption: str = "", parse_mode: str = "HTML") -> bool:
        """Upload a file (PDF) to Telegram via sendDocument."""
        if not self.telegram_token or not self.telegram_chat_id:
            return False

        url = f"https://api.telegram.org/bot{self.telegram_token}/sendDocument"
        boundary = uuid.uuid4().hex

        with open(file_path, "rb") as f:
            file_data = f.read()

        def _field(name, value):
            return (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                f"{value}\r\n"
            ).encode()

        body_bytes = (
            _field("chat_id", self.telegram_chat_id)
            + (_field("caption", caption) if caption else b"")
            + (_field("parse_mode", parse_mode) if caption else b"")
            + (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="document"; filename="{file_path.name}"\r\n'
                f"Content-Type: application/pdf\r\n\r\n"
            ).encode()
            + file_data
            + f"\r\n--{boundary}--\r\n".encode()
        )

        try:
            req = request.Request(
                url,
                data=body_bytes,
                headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            )
            with request.urlopen(req, timeout=30) as resp:
                if resp.status == 200:
                    logger.info(f"Telegram document sent: {file_path.name}")
                    return True
        except Exception as e:
            logger.error(f"Telegram document upload failed: {e}")
        return False

    def notify_single_record(
        self,
        record: FCCRecord,
        brand_name: str,
        pdfs: Optional[List[Path]] = None,
    ):
        """Send one Telegram message per FCC record.

        If PDFs are available, the first PDF is sent as a document with the
        record details as its caption.  Additional PDFs (rare) are sent plain.
        If no PDF, the record details are sent as a text message.
        Discord always receives a plain-text summary (no file upload).
        """
        is_c2pc = _is_c2pc(record.application_type)
        header  = "⚠️ <b>C2PC 變更</b>" if is_c2pc else "📡 <b>新 FCC 申請</b>"
        date    = _fmt_date(record.grant_date or record.filing_date or "")
        name    = record.product_name or record.product_description or "—"
        app_type = record.application_type or "New Filing"
        fcc_url = f"https://fccid.io/{record.fcc_id}"

        msg = (
            f"{header}\n\n"
            f"🏢 <b>{brand_name}</b> ({record.grantee_code})\n"
            f"📋 FCC ID: <code>{record.fcc_id}</code>\n"
            f"📦 產品: {name}\n"
            f"📝 類型: {app_type}\n"
            f"📅 日期: {date}\n"
            f'🔗 <a href="{fcc_url}">FCC 查詢</a>'
        )

        if pdfs:
            # First PDF carries the full caption
            self.send_telegram_document(pdfs[0], caption=msg)
            # Extra PDFs (uncommon) sent without repeating caption
            for extra_pdf in pdfs[1:]:
                self.send_telegram_document(extra_pdf, caption=record.fcc_id)
        else:
            self.send_telegram(msg)

        # Discord: plain text only (strip HTML tags)
        discord_msg = (
            msg.replace("<b>", "**").replace("</b>", "**")
               .replace("<code>", "`").replace("</code>", "`")
               .replace(f'<a href="{fcc_url}">FCC 查詢</a>', fcc_url)
        )
        self.send_discord(discord_msg)

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

    def notify_new_records(
        self,
        new_records: List[FCCRecord],
        brand_names: Optional[dict] = None,
    ):
        """Format and send notification for new FCC records, grouped by grantee.

        brand_names: dict mapping grantee_code -> brand name from settings.
        """
        if not new_records:
            return

        brand_names = brand_names or {}

        # Separate C2PC from brand-new filings
        c2pc_records = [r for r in new_records if _is_c2pc(r.application_type)]
        new_filings  = [r for r in new_records if not _is_c2pc(r.application_type)]

        # Group by grantee code
        def _group(records):
            grouped: dict[str, List[FCCRecord]] = {}
            for r in records:
                grouped.setdefault(r.grantee_code, []).append(r)
            return grouped

        lines = []

        if new_filings:
            lines.append(f"📡 <b>FCC 新申請</b> — {len(new_filings)} 筆\n")
            for code, records in _group(new_filings).items():
                brand = brand_names.get(code, code)
                lines.append(f"🏢 <b>{brand}</b> ({code})")
                for r in records[:20]:
                    name = r.product_name or r.product_description or ""
                    date = _fmt_date(r.grant_date or r.filing_date or "")
                    app_type = r.application_type or "New Filing"
                    lines.append(f"  • <code>{r.fcc_id}</code>  {name}  [{app_type}]  {date}")
                lines.append("")

        if c2pc_records:
            lines.append(f"⚠️ <b>C2PC 變更偵測</b> — {len(c2pc_records)} 筆\n")
            for code, records in _group(c2pc_records).items():
                brand = brand_names.get(code, code)
                lines.append(f"🏢 <b>{brand}</b> ({code})")
                for r in records[:20]:
                    name = r.product_name or r.product_description or ""
                    date = _fmt_date(r.grant_date or r.filing_date or "")
                    lines.append(f"  ⚡ <code>{r.fcc_id}</code>  {name}  {date}")
                lines.append("")

        msg = "\n".join(lines).rstrip()
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

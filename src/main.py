"""FCC Monitor — main entry point.

Run modes:
  python -m src.main            — single scan run (default)
  python -m src.main --daemon   — loop every N hours (reads scheduler.run_time)
  python -m src.main --dry-run  — fetch + detect, no DB writes, no notifications

Flow per grantee code:
  1. Fetch current FCC records (via configured fetcher strategy)
  2. Filter against DB to find new records
  3. For each new record:
       a. Download Label PDFs from FCC
       b. Send Telegram / Discord notification
  4. Persist new records to DB
"""

import argparse
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from loguru import logger

from .config import get_settings
from .database import Database
from .fetcher import get_fetcher
from .models import FCCRecord
from .notifier import Notifier
from .pdf_fetcher import PDFFetcher


def _parse_fcc_date(date_str: str) -> datetime | None:
    """Parse common FCC date formats (MM/DD/YYYY or YYYY-MM-DD)."""
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y"):
        try:
            return datetime.strptime(date_str.strip(), fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _within_days(record: FCCRecord, days: int) -> bool:
    """Return True if record's grant_date or filing_date is within the last N days."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    for field in (record.grant_date, record.filing_date):
        if field:
            dt = _parse_fcc_date(field)
            if dt and dt >= cutoff:
                return True
    return False


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

def _configure_logging(level: str, log_file: str):
    logger.remove()
    logger.add(sys.stderr, level=level, colorize=True,
               format="<green>{time:HH:mm:ss}</green> | <level>{level:<8}</level> | {message}")
    Path(log_file).parent.mkdir(parents=True, exist_ok=True)
    logger.add(log_file, level=level, rotation="10 MB", retention="30 days",
               encoding="utf-8")


# ---------------------------------------------------------------------------
# Core scan logic
# ---------------------------------------------------------------------------

def run_scan(dry_run: bool = False, since_days: int | None = None, strategy: str | None = None) -> dict:
    """
    Execute one full monitoring scan across all configured grantee codes.

    Returns a summary dict with counts per grantee.
    """
    settings = get_settings()
    db = Database(settings.database.path)
    fetcher = get_fetcher(strategy)
    pdf_fetcher = PDFFetcher(output_dir="data/pdfs")
    notifier = Notifier(
        telegram_token=settings.telegram.bot_token,
        telegram_chat_id=settings.telegram.chat_id,
        discord_webhook=settings.discord.webhook_url,
    )

    all_new_records: list[FCCRecord] = []
    summary: dict = {}

    try:
        for target in settings.target_grantees:
            for grantee_code in target.codes:
                logger.info(
                    f"── Scanning {target.name} / {grantee_code} ──"
                )

                # 1. Fetch current records from FCC
                current_records = fetcher.fetch_by_grantee(grantee_code)
                if not current_records:
                    logger.warning(
                        f"No records returned for {grantee_code} "
                        f"(fetcher may have failed or grantee has no filings)"
                    )
                    summary[grantee_code] = {"fetched": 0, "new": 0}
                    continue

                logger.info(
                    f"Fetched {len(current_records)} records for {grantee_code}"
                )

                # 2. Filter to find genuinely new records
                new_records = db.filter_new(current_records)

                # On first run the DB is empty; limit notifications to recent
                # records only (--since-days window).  All records are still
                # saved to DB so subsequent runs have a baseline.
                notify_records = new_records
                if since_days is not None and new_records:
                    notify_records = [r for r in new_records if _within_days(r, since_days)]
                    skipped = len(new_records) - len(notify_records)
                    if skipped:
                        logger.info(
                            f"[since-days={since_days}] Suppressing {skipped} older "
                            f"record(s) for {grantee_code} (outside window)"
                        )

                summary[grantee_code] = {
                    "fetched": len(current_records),
                    "new": len(new_records),
                    "notify": len(notify_records),
                }

                if not notify_records:
                    logger.info(f"No new records for {grantee_code}")
                    continue

                logger.info(
                    f"🆕 {len(notify_records)} record(s) to notify for {grantee_code}: "
                    + ", ".join(r.fcc_id for r in notify_records[:5])
                    + ("..." if len(notify_records) > 5 else "")
                )

                # 3a. Download Label PDFs for each new FCC ID
                for record in notify_records:
                    if dry_run:
                        logger.info(
                            f"[dry-run] Would download PDF for {record.fcc_id}"
                        )
                        continue

                    pdfs = pdf_fetcher.fetch_label_pdfs(record.fcc_id, record.application_id)
                    if pdfs:
                        logger.info(
                            f"📄 Downloaded {len(pdfs)} PDF(s) for {record.fcc_id}: "
                            + ", ".join(p.name for p in pdfs)
                        )
                        summary[grantee_code].setdefault("pdfs_downloaded", 0)
                        summary[grantee_code]["pdfs_downloaded"] += len(pdfs)
                        # Send each PDF directly to Telegram
                        for pdf_path in pdfs:
                            notifier.send_telegram_document(
                                pdf_path,
                                caption=f"📄 {record.fcc_id} Label PDF",
                            )
                    else:
                        logger.warning(
                            f"No Label PDFs found for {record.fcc_id}"
                        )

                all_new_records.extend(notify_records)

                # 3b. Persist ALL new records to DB (not just notify window,
                #     so next run has a complete baseline).
                if not dry_run:
                    saved = db.save_records(new_records)
                    logger.info(f"Saved {saved} records to DB for {grantee_code}")
                else:
                    logger.info(
                        f"[dry-run] Would save {len(new_records)} records to DB"
                    )

        # 4. Send grouped notification for all new records across all grantees
        if all_new_records:
            logger.info(
                f"📡 Sending notification: {len(all_new_records)} new record(s) total"
            )
            if not dry_run:
                notifier.notify_new_records(all_new_records)
            else:
                logger.info("[dry-run] Notification skipped")
        else:
            logger.info("No new records across all grantees — nothing to notify")

    finally:
        db.close()
        pdf_fetcher.close()

    return summary


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="FCC Equipment Authorization Monitor"
    )
    parser.add_argument(
        "--daemon",
        action="store_true",
        help="Run continuously on a fixed interval (set in settings.yaml scheduler.run_time)",
    )
    parser.add_argument(
        "--interval-hours",
        type=float,
        default=24.0,
        metavar="N",
        help="Hours between scans in daemon mode (default: 24)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and detect changes but do not write to DB or send notifications",
    )
    parser.add_argument(
        "--since-days",
        type=int,
        default=None,
        metavar="N",
        help="Only notify about records with grant/filing date within last N days "
             "(useful on first run when DB is empty; all records are still saved)",
    )
    parser.add_argument(
        "--strategy",
        choices=["spidercloud", "browserless", "playwright", "spidercloud_legacy"],
        default=None,
        help="Override fetcher strategy (default: auto-detect from settings)",
    )
    return parser.parse_args()


def main():
    args = _parse_args()

    settings = get_settings()
    _configure_logging(settings.logging.level, settings.logging.file)

    logger.info("=" * 60)
    logger.info("FCC Monitor starting")
    if args.dry_run:
        logger.info("*** DRY-RUN MODE — no DB writes, no notifications ***")
    logger.info("=" * 60)

    # Override fetcher strategy if requested
    if args.strategy:
        import os
        os.environ["FCC_FETCHER_STRATEGY"] = args.strategy

    if args.daemon:
        interval_s = args.interval_hours * 3600
        logger.info(f"Daemon mode: scanning every {args.interval_hours}h")
        while True:
            try:
                summary = run_scan(dry_run=args.dry_run, since_days=args.since_days, strategy=args.strategy)
                logger.info(f"Scan summary: {summary}")
            except Exception:
                logger.exception("Unhandled error during scan — will retry next interval")
            logger.info(f"Sleeping {args.interval_hours}h until next scan…")
            time.sleep(interval_s)
    else:
        summary = run_scan(dry_run=args.dry_run, since_days=args.since_days, strategy=args.strategy)
        logger.info(f"Scan complete. Summary: {summary}")


if __name__ == "__main__":
    main()

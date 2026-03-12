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


def _after_date(record: FCCRecord, cutoff: datetime) -> bool:
    """Return True if record's grant_date or filing_date is on or after cutoff."""
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

def run_scan(
    dry_run: bool = False,
    seed: bool = False,
    since_days: int | None = None,
    since_date: datetime | None = None,
    strategy: str | None = None,
) -> dict:
    """
    Execute one full monitoring scan across all configured grantee codes.

    seed=True: write to DB but send no notifications (used for initial DB population).
    since_date: only save/notify records on or after this date (seed mode filter).
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

    # Build grantee_code -> brand name lookup from settings
    brand_names: dict[str, str] = {
        code: target.name
        for target in settings.target_grantees
        for code in target.codes
    }

    all_new_records: list[FCCRecord] = []
    summary: dict = {}

    try:
        for target in settings.target_grantees:
            for grantee_code in target.codes:
                logger.info(f"── Scanning {target.name} / {grantee_code} ──")

                # 1. Fetch current records from FCC
                current_records = fetcher.fetch_by_grantee(grantee_code)
                if not current_records:
                    logger.warning(
                        f"No records returned for {grantee_code} "
                        f"(fetcher may have failed or grantee has no filings)"
                    )
                    summary[grantee_code] = {"fetched": 0, "new": 0}
                    continue

                logger.info(f"Fetched {len(current_records)} records for {grantee_code}")

                # 2. Filter to find genuinely new records
                new_records = db.filter_new(current_records)

                # Apply date cutoff (--since-date) — limits what gets saved in seed mode
                if since_date is not None and new_records:
                    filtered = [r for r in new_records if _after_date(r, since_date)]
                    skipped = len(new_records) - len(filtered)
                    if skipped:
                        logger.info(
                            f"[since-date] Skipping {skipped} record(s) before "
                            f"{since_date.date()} for {grantee_code}"
                        )
                    new_records = filtered

                # Determine which records to notify about
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
                    "notify": 0 if seed else len(notify_records),
                }

                # 3. Persist new records to DB
                if not dry_run and new_records:
                    saved = db.save_records(new_records)
                    logger.info(f"Saved {saved} record(s) to DB for {grantee_code}")
                elif dry_run and new_records:
                    logger.info(f"[dry-run] Would save {len(new_records)} record(s) to DB")

                # Seed mode: no notifications, move on
                if seed:
                    logger.info(f"[seed] {len(new_records)} record(s) saved, no notification sent")
                    continue

                if not notify_records:
                    logger.info(f"No new records for {grantee_code}")
                    continue

                logger.info(
                    f"🆕 {len(notify_records)} record(s) to notify for {grantee_code}: "
                    + ", ".join(r.fcc_id for r in notify_records[:5])
                    + ("..." if len(notify_records) > 5 else "")
                )

                # 4a. Download Label PDFs and send to Telegram
                for record in notify_records:
                    if dry_run:
                        logger.info(f"[dry-run] Would download PDF for {record.fcc_id}")
                        continue

                    pdfs = pdf_fetcher.fetch_label_pdfs(record.fcc_id, record.application_id)
                    if pdfs:
                        logger.info(
                            f"📄 Downloaded {len(pdfs)} PDF(s) for {record.fcc_id}: "
                            + ", ".join(p.name for p in pdfs)
                        )
                        summary[grantee_code].setdefault("pdfs_downloaded", 0)
                        summary[grantee_code]["pdfs_downloaded"] += len(pdfs)
                        for pdf_path in pdfs:
                            notifier.send_telegram_document(
                                pdf_path,
                                caption=f"📄 {record.fcc_id} Label PDF",
                            )
                    else:
                        logger.warning(f"No Label PDFs found for {record.fcc_id}")

                all_new_records.extend(notify_records)

        # 4b. Send grouped Telegram/Discord notification
        if all_new_records:
            logger.info(f"📡 Sending notification: {len(all_new_records)} new record(s) total")
            if not dry_run:
                notifier.notify_new_records(all_new_records, brand_names=brand_names)
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
        "--seed",
        action="store_true",
        help="Populate DB with existing records (no notifications). Use with --since-date.",
    )
    parser.add_argument(
        "--since-date",
        type=str,
        default=None,
        metavar="YYYY-MM-DD",
        help="Only save/notify records on or after this date (e.g. 2023-01-01)",
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
    if args.seed:
        logger.info("*** SEED MODE — writing to DB, no notifications ***")
    logger.info("=" * 60)

    # Parse --since-date (CLI overrides settings.yaml)
    since_date: datetime | None = None
    since_date_str = args.since_date or settings.database.since_date
    if since_date_str:
        try:
            since_date = datetime.strptime(since_date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            logger.info(f"Date filter: records on or after {since_date_str}")
        except ValueError:
            logger.error(f"Invalid since_date format: {since_date_str!r} (expected YYYY-MM-DD)")
            sys.exit(1)

    # Override fetcher strategy if requested
    if args.strategy:
        import os
        os.environ["FCC_FETCHER_STRATEGY"] = args.strategy

    scan_kwargs = dict(
        dry_run=args.dry_run,
        seed=args.seed,
        since_days=args.since_days,
        since_date=since_date,
        strategy=args.strategy,
    )

    if args.daemon:
        interval_s = args.interval_hours * 3600
        logger.info(f"Daemon mode: scanning every {args.interval_hours}h")
        while True:
            try:
                summary = run_scan(**scan_kwargs)
                logger.info(f"Scan summary: {summary}")
            except Exception:
                logger.exception("Unhandled error during scan — will retry next interval")
            logger.info(f"Sleeping {args.interval_hours}h until next scan…")
            time.sleep(interval_s)
    else:
        summary = run_scan(**scan_kwargs)
        logger.info(f"Scan complete. Summary: {summary}")


if __name__ == "__main__":
    main()

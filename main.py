#!/usr/bin/env python3
"""
FCC Monitor - Unified Entry Point
Consolidated version of various crawlers.
"""
import argparse
import logging
import sys
import time
from datetime import datetime
from typing import List

from loguru import logger

from src.config import get_settings
from src.database import DatabaseManager
from src.fetcher import get_fetcher
from src.notifier import Notifier
from src.models import FCCRecord

# ============================================================================
# Logging Setup
# ============================================================================

def setup_logging(verbose: bool = False):
    """Refine logging setup."""
    settings = get_settings()
    level = "DEBUG" if verbose else settings.logging.level
    
    # Configure Loguru
    logger.remove()
    logger.add(sys.stderr, level=level, format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{message}</cyan>")
    logger.add(settings.logging.file, level=level, rotation="10 MB", retention="1 month")
    
    logger.info(f"FCC Monitor starting with log level {level}")

# ============================================================================
# Core Logic
# ============================================================================

def run_monitor(strategy: str = "spidercloud", notify: bool = True):
    """Main monitor execution logic."""
    settings = get_settings()
    db = DatabaseManager(settings.database.path)
    
    # Flexible token lookup for telegram
    tg_token = settings.telegram.bot_token or settings.telegram.token
    if not tg_token and hasattr(settings.telegram, 'bot') and isinstance(settings.telegram.bot, dict):
        tg_token = settings.telegram.bot.get('token', '')
    
    notifier = Notifier(
        telegram_token=tg_token,
        telegram_chat_id=settings.telegram.chat_id,
        discord_webhook=settings.discord.webhook_url
    )
    
    try:
        fetcher = get_fetcher(strategy)
    except ValueError as e:
        logger.error(str(e))
        return

    all_new_records: List[FCCRecord] = []
    
    # Iterate through all target grantees
    for grantee_config in settings.target_grantees:
        company_name = grantee_config.name
        
        for code in grantee_config.codes:
            logger.info(f"=== Objective: {company_name} ({code}) via {strategy} ===")
            
            try:
                records = fetcher.fetch_by_grantee(code)
                new_count = 0
                
                for r in records:
                    if db.save_record(r):
                        all_new_records.append(r)
                        new_count += 1
                
                logger.info(f"Status: {len(records)} found, {new_count} new entries archived.")
                
                # Log crawl history
                db.log_crawl_history(code, len(records))
                
                # Small delay to avoid hitting rate limits
                if code != grantee_config.codes[-1] or grantee_config != settings.target_grantees[-1]:
                    time.sleep(2)
                    
            except Exception as e:
                logger.exception(f"Error fetching data for {code}: {e}")
                db.log_crawl_history(code, 0, errors=str(e))

    # Notifications
    if notify and all_new_records:
        notifier.notify_new_records(all_new_records)

    logger.info(f"Operation complete. Synchronization Rate: {len(all_new_records)} new targets.")
    return all_new_records

# ============================================================================
# CLI Entry Point
# ============================================================================

def main():
    parser = argparse.ArgumentParser(description="NERV FCC Monitor Unified Entry Point")
    parser.add_argument(
        "--strategy", "-s", 
        choices=["spidercloud", "browserless", "playwright"],
        default="spidercloud",
        help="Fetching strategy to use"
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable verbose logging")
    parser.add_argument("--no-notify", action="store_true", help="Disable notifications")
    parser.add_argument("--daemon", action="store_true", help="Run in daemon mode (scheduled)")
    parser.add_argument("--interval", type=int, default=3600, help="Interval for daemon mode in seconds")
    
    args = parser.parse_args()
    
    setup_logging(args.verbose)
    
    if args.daemon:
        logger.info(f"Entering daemon mode with {args.interval}s interval")
        while True:
            run_monitor(strategy=args.strategy, notify=not args.no_notify)
            logger.info(f"Sleeping for {args.interval} seconds...")
            time.sleep(args.interval)
    else:
        run_monitor(strategy=args.strategy, notify=not args.no_notify)

if __name__ == "__main__":
    main()

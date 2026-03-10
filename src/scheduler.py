"""Scheduler for dual-track scanning mechanism."""
import logging
import time
from datetime import datetime, timedelta
from typing import List, Optional, Dict, Any
from dataclasses import dataclass

from loguru import logger

from .brand_db import BrandDatabaseManager, STATUS_PENDING, STATUS_COMPLETED, STATUS_PENDING_C2PC, STATUS_FAILED
from .brand_crawlers import get_brand_crawler, get_brand_by_grantee_code, get_all_brands
from .c2pc_detector import C2PCDetector
from .circuit_breaker import get_circuit_breaker, CircuitBreakerOpen, CircuitState
from .config import get_settings

logger = logging.getLogger("fcc_monitor.scheduler")

# Maximum retry attempts before marking as failed
MAX_RETRY_COUNT = 5

# Scheduled run interval (in seconds) - 2 days
SCHEDULED_INTERVAL_SECONDS = 2 * 24 * 60 * 60


@dataclass
class CrawlResult:
    """Result of a brand crawl operation."""
    fcc_id: str
    brand: str
    success: bool
    data: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    is_c2pc: bool = False
    source_url: Optional[str] = None  # Product page URL on brand website


class BrandCrawlScheduler:
    """
    Handles dual-track scanning for brand-specific crawlers.
    
    Track 1 (Triggered): Called when new FCC ID is discovered
    Track 2 (Scheduled): Periodic job every 2 days for pending devices
    """
    
    def __init__(self, db_path: str):
        self.db_manager = BrandDatabaseManager(db_path)
        self.c2pc_detector = C2PCDetector(self.db_manager)
        self.settings = get_settings()
        
        # Initialize circuit breakers for each brand
        self._init_circuit_breakers()
    
    def _init_circuit_breakers(self):
        """Initialize circuit breakers for each supported brand."""
        brands = get_all_brands()
        for brand in brands:
            # Circuit breaker: 5 failures opens, 60s timeout, 2 successes to close
            get_circuit_breaker(
                f"crawler_{brand}",
                failure_threshold=5,
                success_threshold=2,
                timeout=60.0
            )
        logger.info(f"Initialized circuit breakers for brands: {brands}")
    
    # =========================================================================
    # Track 1: Triggered Mode (called when new FCC ID discovered)
    # =========================================================================
    
    def trigger_brand_crawl(
        self,
        fcc_id: str,
        grantee_code: str,
        application_type: str = "",
        equipment_class: str = "",
        grant_date: str = "",
        applicant_name: str = ""
    ) -> CrawlResult:
        """
        Triggered mode: Called when main.py discovers a new FCC ID.
        
        This is the entry point for Track 1 scanning.
        
        Args:
            fcc_id: The FCC ID
            grantee_code: The grantee code
            application_type: FCC application type (for C2PC detection)
            equipment_class: FCC equipment class
            grant_date: Grant date from FCC
            applicant_name: Applicant/Company name for pending brand tracking
            
        Returns:
            CrawlResult with operation status
        """
        logger.info(f"🔔 Triggered brand crawl for {fcc_id} ({grantee_code})")
        
        # Determine brand from grantee code
        brand = get_brand_by_grantee_code(grantee_code)
        
        if not brand:
            logger.info(f"No brand crawler for grantee code: {grantee_code}")
            # FIX: Add to pending brand requests list for future crawler development
            # This implements the "待開發品牌爬蟲名單" feature
            try:
                self.db_manager.add_pending_brand_request(
                    grantee_code=grantee_code,
                    applicant_name=applicant_name,
                    fcc_id=fcc_id
                )
                logger.info(f"📝 Added {grantee_code} ({applicant_name}) to pending brand requests list")
            except Exception as e:
                logger.warning(f"Failed to add pending brand request: {e}")
            
            # FIX: Return success=False so notification won't say "successfully crawled"
            return CrawlResult(
                fcc_id=fcc_id,
                brand="",
                success=False,  # Changed to False - no crawler available is not a success
                error="No brand crawler available for this grantee code"
            )
        
        # Initialize brand crawl status
        self.db_manager.init_brand_status(fcc_id, brand)
        
        # Check for C2PC
        is_c2pc = self.c2pc_detector.is_c2pc(application_type, equipment_class)
        
        if is_c2pc:
            # C2PC detected - mark as pending_c2pc and re-trigger
            logger.info(f"🎯 C2PC detected for {fcc_id} - marking for C2PC scan")
            self.db_manager.update_brand_status(fcc_id, brand, STATUS_PENDING_C2PC)
            # Proceed to crawl anyway
        
        # Execute the crawl
        return self._execute_crawl(fcc_id, brand, grant_date)
    
    # =========================================================================
    # Track 2: Scheduled Mode (periodic job for pending devices)
    # =========================================================================
    
    def run_scheduled_scan(self, brand: Optional[str] = None) -> List[CrawlResult]:
        """
        Scheduled mode: Scan all pending devices.
        
        This should be called by cron job or scheduler every 2 days.
        
        Args:
            brand: Optional brand filter (None = all brands)
            
        Returns:
            List of CrawlResult objects
        """
        logger.info(f"📅 Starting scheduled brand scan (brand: {brand or 'all'})")
        
        results = []
        
        # Get all pending devices
        pending_devices = self.db_manager.get_pending_devices(brand)
        
        if not pending_devices:
            logger.info("No pending devices to scan")
            return results
        
        logger.info(f"Found {len(pending_devices)} pending devices")
        
        for device in pending_devices:
            # Check retry count
            if device.retry_count >= MAX_RETRY_COUNT:
                logger.warning(f"Max retries exceeded for {device.fcc_id}/{device.brand}, marking failed")
                self.db_manager.update_brand_status(
                    device.fcc_id, 
                    device.brand, 
                    STATUS_FAILED,
                    error_message="Max retries exceeded"
                )
                continue
            
            # Get grant date from brand specs if available
            grant_date = ""
            latest_specs = self.db_manager.get_latest_specs(device.fcc_id, device.brand)
            if latest_specs:
                grant_date = latest_specs.fcc_grant_date or ""
            
            # Execute crawl
            result = self._execute_crawl(device.fcc_id, device.brand, grant_date)
            results.append(result)
            
            # Rate limiting between crawls
            time.sleep(2)
        
        logger.info(f"Scheduled scan complete: {len(results)} devices processed")
        return results
    
    # =========================================================================
    # Internal Methods
    # =========================================================================
    
    def _execute_crawl(
        self, 
        fcc_id: str, 
        brand: str, 
        grant_date: str = ""
    ) -> CrawlResult:
        """
        Execute the actual brand crawl operation.
        
        Args:
            fcc_id: FCC ID to crawl
            brand: Brand name
            grant_date: FCC grant date
            
        Returns:
            CrawlResult
        """
        logger.info(f"🔍 Executing brand crawl: {fcc_id} -> {brand}")
        
        # Get circuit breaker for this brand
        circuit_breaker = get_circuit_breaker(f"crawler_{brand}")
        
        # Check circuit state before attempting
        if circuit_breaker.stats.state == CircuitState.OPEN:
            cb_status = circuit_breaker.get_status()
            logger.warning(f"Circuit breaker OPEN for {brand}, rejecting crawl for {fcc_id}")
            return CrawlResult(
                fcc_id=fcc_id,
                brand=brand,
                success=False,
                error=f"Circuit breaker is OPEN (consecutive failures: {cb_status['consecutive_failures']})"
            )
        
        # Get appropriate crawler
        crawler = get_brand_crawler(brand)
        
        if not crawler:
            return CrawlResult(
                fcc_id=fcc_id,
                brand=brand,
                success=False,
                error="No crawler available"
            )
        
        # Configure Browserless for Zebra crawler if needed
        if brand.lower() == "zebra":
            browserless_cfg = self.settings.data_source.browserless
            if browserless_cfg.api_key:
                crawler._init_browserless(browserless_cfg.api_key, browserless_cfg.region)
        
        try:
            # Fetch product info through circuit breaker
            product_data = circuit_breaker.call(crawler.fetch_product_info, fcc_id)
            
            if product_data is None or not product_data.get("fcc_id_found"):
                # No data found - keep as pending
                logger.info(f"No product data found for {fcc_id}/{brand}")
                self.db_manager.update_brand_status(fcc_id, brand, STATUS_PENDING)
                
                return CrawlResult(
                    fcc_id=fcc_id,
                    brand=brand,
                    success=False,
                    error="No product data found on brand website"
                )
            
            # Save the specs
            self.db_manager.save_brand_specs(
                fcc_id=fcc_id,
                brand=brand,
                specs=product_data,
                fcc_grant_date=grant_date,
                is_c2pc=False  # Will be set by C2PC analysis
            )
            
            # Check for C2PC changes (compare with previous specs)
            c2pc_result = self.c2pc_detector.analyze_c2pc_change(
                fcc_id=fcc_id,
                brand=brand,
                new_specs=product_data,
                fcc_grant_date=grant_date
            )
            
            # Update status to completed
            self.db_manager.update_brand_status(fcc_id, brand, STATUS_COMPLETED)
            
            logger.info(f"✅ Brand crawl complete: {fcc_id}/{brand}")
            
            # Extract source_url from product_data if available
            source_url = product_data.get("search_url") or product_data.get("product_url") or product_data.get("source_url")
            
            return CrawlResult(
                fcc_id=fcc_id,
                brand=brand,
                success=True,
                data=product_data,
                is_c2pc=c2pc_result is not None,
                source_url=source_url
            )
            
        except CircuitBreakerOpen as e:
            logger.warning(f"Circuit breaker open for {brand}: {e}")
            self.db_manager.update_brand_status(
                fcc_id, 
                brand, 
                STATUS_PENDING,
                error_message=str(e)
            )
            return CrawlResult(
                fcc_id=fcc_id,
                brand=brand,
                success=False,
                error=str(e)
            )
        except Exception as e:
            logger.error(f"Error crawling {fcc_id}/{brand}: {e}")
            self.db_manager.update_brand_status(
                fcc_id, 
                brand, 
                STATUS_PENDING,
                error_message=str(e)
            )
            
            return CrawlResult(
                fcc_id=fcc_id,
                brand=brand,
                success=False,
                error=str(e)
            )
    
    def process_c2pc_alerts(self) -> List[Dict[str, Any]]:
        """
        Process any pending C2PC changes and generate alerts.
        
        Should be called after crawl operations to detect and alert
        on any hardware silent upgrades.
        
        Returns:
            List of C2PC alert messages ready to send
        """
        alerts = []
        
        # Get all pending C2PC devices
        c2pc_devices = self.db_manager.get_pending_devices()
        c2pc_devices = [d for d in c2pc_devices if d.status == STATUS_PENDING_C2PC]
        
        for device in c2pc_devices:
            # Get latest C2PC record
            c2pc_records = self.db_manager.get_c2pc_records(device.fcc_id)
            
            if c2pc_records:
                latest = c2pc_records[0]
                
                if not latest.notified and latest.diff_summary:
                    # Generate alert message
                    alert_msg = self.c2pc_detector.generate_c2pc_alert_message({
                        "fcc_id": device.fcc_id,
                        "brand": device.brand,
                        "diff_summary": latest.diff_summary,
                        "change_type": latest.change_type,
                        "change_count": len(latest.diff_summary.split('\n')) if latest.diff_summary else 0
                    })
                    
                    alerts.append({
                        "fcc_id": device.fcc_id,
                        "brand": device.brand,
                        "message": alert_msg,
                        "c2pc_record": latest
                    })
                    
                    # Mark as notified
                    self.db_manager.mark_c2pc_notified(device.fcc_id, device.brand)
                    
                    # Reset status to completed after C2PC processing
                    self.db_manager.update_brand_status(device.fcc_id, device.brand, STATUS_COMPLETED)
        
        return alerts


# CLI helper for scheduled runs
def run_schedulerDaemon(db_path: str, interval_hours: int = 48):
    """
    Run scheduler as a daemon process.
    
    Args:
        db_path: Path to database
        interval_hours: Interval between scheduled runs (default 48 = 2 days)
    """
    import os
    
    logger.info(f"Starting brand crawl scheduler daemon (interval: {interval_hours}h)")
    
    scheduler = BrandCrawlScheduler(db_path)
    interval_seconds = interval_hours * 3600
    
    while True:
        try:
            # Run scheduled scan
            results = scheduler.run_scheduled_scan()
            
            # Process C2PC alerts
            alerts = scheduler.process_c2pc_alerts()
            
            if alerts:
                logger.info(f"Generated {len(alerts)} C2PC alerts")
            
            logger.info(f"Scan complete. Sleeping for {interval_hours} hours...")
            
        except Exception as e:
            logger.exception(f"Scheduler error: {e}")
        
        time.sleep(interval_seconds)


if __name__ == "__main__":
    from src.config import get_settings
    
    settings = get_settings()
    run_schedulerDaemon(settings.database.path)

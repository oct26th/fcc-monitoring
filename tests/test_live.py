"""Live integration test for fetchers."""
import sys
from loguru import logger
from src.fetcher import get_fetcher
from src.database import DatabaseManager

def test_live_fetch():
    logger.info("=== NERV: LIVE FETCH TEST INITIATED ===")
    
    fetcher = get_fetcher("browserless")
    # Grantee Code '2AR9L' (Newland) is known to have few records (4)
    target_code = "2AR9L"
    
    logger.info(f"Targeting Code: {target_code}")
    records = fetcher.fetch_by_grantee(target_code)
    
    logger.info(f"Received {len(records)} records from Browserless.")
    if records:
        for r in records[:3]:
            logger.debug(f" --> {r.fcc_id} | {r.product_name} | {r.status}")
        
        logger.info("Test fetch successful. Data pipeline geometry is solid.")
        sys.exit(0)
    else:
        logger.error("No records returned. Potential fetch failure or zero records.")
        sys.exit(1)

if __name__ == "__main__":
    test_live_fetch()

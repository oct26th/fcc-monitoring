"""Live integration test for fetchers."""
import pytest
from loguru import logger
from src.fetcher import get_fetcher

def test_live_fetch():
    logger.info("=== NERV: LIVE FETCH TEST INITIATED ===")

    fetcher = get_fetcher("browserless")
    # Grantee Code '2AR9L' (Newland) is known to have few records
    target_code = "2AR9L"

    logger.info(f"Targeting Code: {target_code}")
    records = fetcher.fetch_by_grantee(target_code)

    logger.info(f"Received {len(records)} records from Browserless.")
    for r in records[:3]:
        logger.debug(f" --> {r.fcc_id} | {r.product_name} | {r.status}")

    assert len(records) > 0, "No records returned — fetch failure or zero records."
    logger.info("Test fetch successful. Data pipeline geometry is solid.")

if __name__ == "__main__":
    test_live_fetch()

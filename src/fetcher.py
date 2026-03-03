"""Data fetcher for FCC/SpiderCloud API."""
import json
from typing import Optional
from dataclasses import dataclass

import requests
import httpx
from loguru import logger

from .config import get_settings


@dataclass
class FCCRecord:
    """FCC Equipment Authorization record."""
    fcc_id: str
    grantee_code: str
    product_code: str
    applicant_name: str
    product_name: str
    certification_date: str
    status: str
    expires_on: Optional[str] = None


class FCCFetcher:
    """Fetch FCC certification data from SpiderCloud or FCC API."""
    
    def __init__(self):
        self.settings = get_settings()
        self._session: Optional[httpx.Client] = None
    
    @property
    def session(self) -> httpx.Client:
        """Lazy initialize HTTP session."""
        if self._session is None:
            self._session = httpx.Client(
                timeout=30.0,
                follow_redirects=True,
                headers={"User-Agent": "FCC-Monitor/1.0"}
            )
        return self._session
    
    def fetch_by_grantee(self, grantee_code: str) -> list[FCCRecord]:
        """
        Fetch FCC records for a specific grantee code.
        
        This method tries SpiderCloud first (if enabled), then falls back
        to direct FCC API calls.
        """
        records = []
        
        # Try SpiderCloud first
        if self.settings.data_source.spidercloud.enabled:
            records = self._fetch_from_spidercloud(grantee_code)
        
        # Fallback to FCC direct
        if not records and self.settings.data_source.fcc.enabled:
            records = self._fetch_from_fcc(grantee_code)
        
        if not records:
            logger.warning(f"No records found for grantee: {grantee_code}")
        
        return records
    
    def _fetch_from_spidercloud(self, grantee_code: str) -> list[FCCRecord]:
        """Fetch data from SpiderCloud API."""
        cfg = self.settings.data_source.spidercloud
        
        if not cfg.api_key:
            logger.warning("SpiderCloud API key not configured")
            return []
        
        url = f"{cfg.base_url}/fcc/search"
        headers = {
            "Authorization": f"Bearer {cfg.api_key}",
            "Content-Type": "application/json"
        }
        params = {
            "grantee_code": grantee_code,
            "limit": 1000
        }
        
        try:
            response = self.session.get(url, headers=headers, params=params)
            response.raise_for_status()
            
            data = response.json()
            return self._parse_spidercloud_response(data)
            
        except httpx.HTTPError as e:
            logger.error(f"SpiderCloud API error: {e}")
            return []
    
    def _fetch_from_fcc(self, grantee_code: str) -> list[FCCRecord]:
        """
        Fetch data from FCC EAS.
        
        Note: FCC provides bulk data downloads, but for real-time
        queries we can use their web services.
        """
        # This would require implementing FCC's actual API
        # For now, return empty - implement based on actual FCC API
        logger.info(f"FCC direct API not fully implemented for: {grantee_code}")
        return []
    
    def _parse_spidercloud_response(self, data: dict) -> list[FCCRecord]:
        """Parse SpiderCloud API response into FCCRecord objects."""
        records = []
        
        # Adjust parsing based on actual SpiderCloud response format
        items = data.get("results", data.get("data", []))
        
        for item in items:
            record = FCCRecord(
                fcc_id=item.get("fcc_id", ""),
                grantee_code=item.get("grantee_code", ""),
                product_code=item.get("product_code", ""),
                applicant_name=item.get("applicant_name", ""),
                product_name=item.get("product_name", ""),
                certification_date=item.get("certification_date", ""),
                status=item.get("status", "Granted"),
                expires_on=item.get("expires_on")
            )
            records.append(record)
        
        logger.info(f"Parsed {len(records)} records from SpiderCloud")
        return records
    
    def fetch_all_targets(self) -> dict[str, list[FCCRecord]]:
        """Fetch FCC records for all target companies."""
        all_records = {}
        
        for grantee_config in self.settings.target_grantees:
            company_name = grantee_config.name
            
            for code in grantee_config.codes:
                logger.info(f"Fetching FCC data for {company_name} ({code})")
                records = self.fetch_by_grantee(code)
                
                if company_name not in all_records:
                    all_records[company_name] = []
                all_records[company_name].extend(records)
        
        return all_records
    
    def close(self):
        """Close HTTP session."""
        if self._session:
            self._session.close()
            self._session = None

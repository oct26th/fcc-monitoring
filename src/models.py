"""Data models for FCC Monitor."""
import hashlib
from dataclasses import dataclass, asdict
from typing import Optional
from datetime import datetime


@dataclass
class FCCRecord:
    """FCC Equipment Authorization record."""
    fcc_id: str
    grantee_code: str
    product_code: str
    applicant_name: str
    product_description: str = ""
    product_name: str = ""
    grant_date: str = ""
    filing_date: str = ""
    certification_date: str = ""
    application_type: str = ""
    status: str = "Granted"
    equipment_class: Optional[str] = None
    expires_on: Optional[str] = None
    
    def to_dict(self) -> dict:
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: dict) -> 'FCCRecord':
        return cls(**data)
    
    def fingerprint(self) -> str:
        """Generate unique fingerprint for this record."""
        # Use key fields that uniquely identify a filing
        unique_string = f"{self.fcc_id}:{self.grantee_code}:{self.product_code}:{self.application_type}:{self.grant_date}"
        return hashlib.sha256(unique_string.encode()).hexdigest()[:16]

    def __post_init__(self):
        # Ensure product_name and product_description are synced if one is missing
        if not self.product_name and self.product_description:
            self.product_name = self.product_description
        elif not self.product_description and self.product_name:
            self.product_description = self.product_name
            
        # Ensure certification_date and grant_date are synced
        if not self.certification_date and self.grant_date:
            self.certification_date = self.grant_date
        elif not self.grant_date and self.certification_date:
            self.grant_date = self.certification_date

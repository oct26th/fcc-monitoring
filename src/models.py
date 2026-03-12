"""Shared data models for FCC Monitor."""
import hashlib
from dataclasses import dataclass
from typing import Optional


@dataclass
class FCCRecord:
    """Represents a single FCC Equipment Authorization record."""
    fcc_id: str
    grantee_code: str
    product_code: str
    applicant_name: str = ""
    product_name: str = ""
    product_description: str = ""
    certification_date: str = ""
    grant_date: str = ""
    filing_date: str = ""
    status: str = "Granted"
    application_type: str = ""
    expires_on: Optional[str] = None
    application_id: Optional[str] = None   # FCC application_id (for PDF/exhibit lookup)

    def __post_init__(self):
        # Sync product_name ← product_description if name is empty
        if not self.product_name and self.product_description:
            self.product_name = self.product_description
        # Sync grant_date ← certification_date if grant_date is empty
        if not self.grant_date and self.certification_date:
            self.grant_date = self.certification_date

    def fingerprint(self) -> str:
        """Hash of core identity fields (excludes mutable fields like status)."""
        key = "|".join([
            self.fcc_id, self.grantee_code, self.product_code,
            self.applicant_name, self.grant_date, self.application_type,
        ])
        return hashlib.sha256(key.encode()).hexdigest()

    def __hash__(self):
        return hash(self.fcc_id)

    def __eq__(self, other):
        if not isinstance(other, FCCRecord):
            return False
        return self.fcc_id == other.fcc_id

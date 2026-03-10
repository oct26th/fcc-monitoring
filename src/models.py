"""Shared data models for FCC Monitor."""
from dataclasses import dataclass, field
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

    def __hash__(self):
        return hash(self.fcc_id)

    def __eq__(self, other):
        if not isinstance(other, FCCRecord):
            return False
        return self.fcc_id == other.fcc_id

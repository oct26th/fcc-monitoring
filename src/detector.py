"""Change detection for FCC certification data."""
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from loguru import logger

from .models import FCCRecord


class ChangeType(Enum):
    """Types of changes detected."""
    NEW = "new"
    REMOVED = "removed"
    STATUS_CHANGED = "status_changed"
    UPDATED = "updated"


@dataclass
class Change:
    """Represents a detected change."""
    fcc_id: str
    company: str
    change_type: ChangeType
    old_value: Optional[str] = None
    new_value: Optional[str] = None
    record: Optional[FCCRecord] = None


class ChangeDetector:
    """Detect changes between current and previous FCC data."""
    
    def __init__(self):
        self.changes: list[Change] = []
    
    def detect_changes(
        self,
        current_data: dict[str, list[FCCRecord]],
        previous_data: dict[str, list[FCCRecord]]
    ) -> list[Change]:
        """
        Compare current and previous data to detect changes.
        
        Args:
            current_data: Current FCC records keyed by company name
            previous_data: Previous FCC records keyed by company name
        
        Returns:
            List of detected changes
        """
        self.changes = []
        
        # Check each company
        for company, current_records in current_data.items():
            previous_records = previous_data.get(company, [])
            
            # Build lookup maps
            current_map = {r.fcc_id: r for r in current_records}
            previous_map = {r.fcc_id: r for r in previous_records}
            
            # Detect new records
            for fcc_id, record in current_map.items():
                if fcc_id not in previous_map:
                    self.changes.append(Change(
                        fcc_id=fcc_id,
                        company=company,
                        change_type=ChangeType.NEW,
                        record=record
                    ))
            
            # Detect removed records
            for fcc_id, record in previous_map.items():
                if fcc_id not in current_map:
                    self.changes.append(Change(
                        fcc_id=fcc_id,
                        company=company,
                        change_type=ChangeType.REMOVED,
                        old_value=record.status
                    ))
            
            # Detect status changes
            for fcc_id, current_record in current_map.items():
                if fcc_id in previous_map:
                    previous_record = previous_map[fcc_id]
                    
                    if current_record.status != previous_record.status:
                        self.changes.append(Change(
                            fcc_id=fcc_id,
                            company=company,
                            change_type=ChangeType.STATUS_CHANGED,
                            old_value=previous_record.status,
                            new_value=current_record.status,
                            record=current_record
                        ))
        
        logger.info(f"Detected {len(self.changes)} changes total")
        return self.changes
    
    def has_changes(self) -> bool:
        """Check if any changes were detected."""
        return len(self.changes) > 0
    
    def get_summary(self) -> dict:
        """Get summary of changes by type."""
        summary = {
            "total": len(self.changes),
            "new": 0,
            "removed": 0,
            "status_changed": 0,
            "by_company": {}
        }
        
        for change in self.changes:
            if change.change_type == ChangeType.NEW:
                summary["new"] += 1
            elif change.change_type == ChangeType.REMOVED:
                summary["removed"] += 1
            elif change.change_type == ChangeType.STATUS_CHANGED:
                summary["status_changed"] += 1
            
            # Count by company
            company = change.company
            if company not in summary["by_company"]:
                summary["by_company"][company] = 0
            summary["by_company"][company] += 1
        
        return summary

"""C2PC (Class II Permissive Change) detector and diff analyzer."""
import json
import logging
import difflib
from typing import Dict, List, Any, Optional, Tuple

logger = logging.getLogger("fcc_monitor.c2pc")

# C2PC detection keywords in FCC application type
C2PC_KEYWORDS = [
    "class ii permissive change",
    "class ii",
    "c2pc",
    "permissive change",
    "pc"
]


class C2PCDetector:
    """Detect C2PC from FCC data and generate diff reports."""
    
    def __init__(self, brand_db):
        self.brand_db = brand_db
    
    def is_c2pc(self, application_type: str, equipment_class: str = "") -> bool:
        """
        Check if an FCC application is a Class II Permissive Change.
        
        Args:
            application_type: The application type from FCC data (e.g., "Class II Permissive Change")
            equipment_class: Optional equipment class
            
        Returns:
            True if C2PC detected, False otherwise
        """
        if not application_type:
            return False
        
        app_type_lower = application_type.lower()
        
        # Check for C2PC keywords
        for keyword in C2PC_KEYWORDS:
            if keyword in app_type_lower:
                logger.info(f"C2PC detected: {application_type}")
                return True
        
        return False
    
    def generate_diff(
        self, 
        old_specs: Dict[str, Any], 
        new_specs: Dict[str, Any]
    ) -> Tuple[str, List[str]]:
        """
        Generate a diff between old and new specifications.
        
        Args:
            old_specs: Previous specification dictionary
            new_specs: New specification dictionary
            
        Returns:
            Tuple of (diff_summary string, list of change descriptions)
        """
        changes = []
        
        # Compare each key
        all_keys = set(old_specs.keys()) | set(new_specs.keys())
        
        for key in sorted(all_keys):
            old_val = old_specs.get(key, "<not set>")
            new_val = new_specs.get(key, "<not set>")
            
            # Handle nested dictionaries
            if isinstance(old_val, dict) and isinstance(new_val, dict):
                sub_changes, _ = self.generate_diff(old_val, new_val)
                if sub_changes:
                    changes.append(f"  {key}:")
                    changes.append(f"    {sub_changes}")
            elif old_val != new_val:
                changes.append(f"  {key}: '{old_val}' → '{new_val}'")
        
        diff_summary = "\n".join(changes) if changes else "No changes detected"
        
        return diff_summary, changes
    
    def analyze_c2pc_change(
        self,
        fcc_id: str,
        brand: str,
        new_specs: Dict[str, Any],
        fcc_grant_date: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Analyze a potential C2PC change for an FCC ID.
        
        This checks if there's a previous spec to compare against,
        and if so, generates a diff report.
        
        Args:
            fcc_id: The FCC ID
            brand: The brand name
            new_specs: Newly fetched specifications
            fcc_grant_date: Grant date from FCC
            
        Returns:
            Dictionary with diff results, or None if no previous data
        """
        # Get previous specs
        previous = self.brand_db.get_previous_specs(fcc_id, brand)
        
        if not previous:
            logger.info(f"No previous specs for {fcc_id}/{brand}, skipping diff")
            return None
        
        # Parse previous specs with robust error handling
        try:
            if isinstance(previous.specs_json, str):
                prev_specs = json.loads(previous.specs_json)
            elif previous.specs_json is not None:
                prev_specs = previous.specs_json
            else:
                prev_specs = {}
        except (json.JSONDecodeError, json.JSONDecodeError, TypeError, ValueError, Exception) as e:
            # Fallback for malformed JSON or unexpected types
            logger.warning(f"Failed to parse previous specs for {fcc_id}: {e}")
            prev_specs = {"raw": str(previous.specs_json) if previous.specs_json else ""}
        
        # Generate diff
        diff_summary, change_list = self.generate_diff(prev_specs, new_specs)
        
        if not change_list:
            logger.info(f"No actual changes detected for {fcc_id}/{brand}")
            return None
        
        # Determine change type
        change_type = self._categorize_changes(change_list)
        
        # Save C2PC record
        self.brand_db.save_c2pc_record(
            fcc_id=fcc_id,
            brand=brand,
            previous_specs=prev_specs,
            new_specs=new_specs,
            diff_summary=diff_summary,
            change_type=change_type
        )
        
        logger.info(f"C2PC change analyzed for {fcc_id}/{brand}: {change_type}")
        
        return {
            "fcc_id": fcc_id,
            "brand": brand,
            "previous_specs": prev_specs,
            "new_specs": new_specs,
            "diff_summary": diff_summary,
            "change_type": change_type,
            "change_count": len(change_list),
            "changes": change_list
        }
    
    def _categorize_changes(self, changes: List[str]) -> str:
        """
        Categorize the type of changes detected.
        
        Categories:
        - HARDWARE_UPGRADE: Physical/hardware changes
        - FIRMWARE_UPDATE: Software/firmware changes
        - SPECIFICATION_CHANGE: Other specification changes
        - UNKNOWN: Unclassified changes
        """
        hardware_keywords = [
            "antenna", "rf", "transmitter", "receiver", "power", 
            "battery", "chip", "module", "frequency", "band"
        ]
        firmware_keywords = [
            "firmware", "software", "version", "bluetooth", "wifi",
            "protocol", "encryption"
        ]
        
        hardware_count = 0
        firmware_count = 0
        
        for change in changes:
            change_lower = change.lower()
            for kw in hardware_keywords:
                if kw in change_lower:
                    hardware_count += 1
                    break
            for kw in firmware_keywords:
                if kw in change_lower:
                    firmware_count += 1
                    break
        
        if hardware_count > firmware_count:
            return "HARDWARE_UPGRADE"
        elif firmware_count > 0:
            return "FIRMWARE_UPDATE"
        elif changes:
            return "SPECIFICATION_CHANGE"
        else:
            return "UNKNOWN"
    
    def generate_c2pc_alert_message(self, c2pc_result: Dict[str, Any]) -> str:
        """
        Generate a formatted alert message for C2PC changes.
        
        Args:
            c2pc_result: Result from analyze_c2pc_change
            
        Returns:
            Formatted Discord/Telegram message string
        """
        brand_emoji = {
            "honeywell": "🐝",
            "zebra": "🦓"
        }
        
        brand = c2pc_result.get("brand", "Unknown")
        emoji = brand_emoji.get(brand.lower(), "📡")
        
        change_type = c2pc_result.get("change_type", "UNKNOWN")
        change_type_emoji = {
            "HARDWARE_UPGRADE": "⚠️",
            "FIRMWARE_UPDATE": "🔄",
            "SPECIFICATION_CHANGE": "📝",
            "UNKNOWN": "❓"
        }
        
        change_count = c2pc_result.get("change_count", 0)
        diff_summary = c2pc_result.get("diff_summary", "No details")
        
        # Truncate diff if too long
        if len(diff_summary) > 1500:
            diff_summary = diff_summary[:1500] + "\n... (truncated)"
        
        message = f"""🚨 **C2PC 硬體暗中升級警報**

{emoji} **Brand:** {brand.upper()}
🆔 **FCC ID:** `{c2pc_result.get("fcc_id", "N/A")}`
{change_type_emoji.get(change_type, "📡")} **Change Type:** {change_type}
📊 **Changes Detected:** {change_count} item(s)

**變更詳情:**
```
{diff_summary}
```

_此裝置已被偵測為 Class II Permissive Change，規格已悄悄升級。_"""
        
        return message

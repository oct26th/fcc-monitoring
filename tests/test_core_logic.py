import os
import unittest
import tempfile
from datetime import datetime

from src.models import FCCRecord
from src.database import DatabaseManager
from src.detector import ChangeDetector, ChangeType

class TestCoreLogic(unittest.TestCase):
    def setUp(self):
        # Use temp file because DatabaseManager re-opens connections per query.
        # :memory: DBs get wiped every time a new connection is established.
        self.temp_db_fd, self.temp_db_path = tempfile.mkstemp(suffix=".db")
        os.close(self.temp_db_fd)
        self.db = DatabaseManager(self.temp_db_path)
        
    def tearDown(self):
        if os.path.exists(self.temp_db_path):
            os.remove(self.temp_db_path)

    def test_record_sync_fields(self):
        """Test if FCCRecord correctly syncs symmetric fields (name<->desc, dates)."""
        # Case 1: description given, name empty
        r1 = FCCRecord(
            fcc_id="TEST-01", grantee_code="TST", product_code="01",
            applicant_name="DevCompany", product_description="Scanner"
        )
        self.assertEqual(r1.product_name, "Scanner", "Product name should sync from description")
        
        # Case 2: certification_date given, grant_date empty
        r2 = FCCRecord(
            fcc_id="TEST-02", grantee_code="TST", product_code="02",
            applicant_name="DevCompany", certification_date="2026-03-05"
        )
        self.assertEqual(r2.grant_date, "2026-03-05", "Grant date should sync from certification_date")

    def test_record_fingerprint_determinism(self):
        """Test if the fingerprint calculation is deterministic and identical for same core fields."""
        r1 = FCCRecord(
            fcc_id="EX-1", grantee_code="EX", product_code="1",
            applicant_name="Nerv", grant_date="1995-10-04", application_type="Original"
        )
        r2 = FCCRecord(
            fcc_id="EX-1", grantee_code="EX", product_code="1",
            applicant_name="Nerv", grant_date="1995-10-04", application_type="Original",
            status="Dismissed" # Status variation doesn't change fingerprint according to models.py
        )
        self.assertEqual(r1.fingerprint(), r2.fingerprint(), "Fingerprints must match for identical core identity")

    def test_database_idempotent_save(self):
        """Test database duplicate handling and update logic."""
        r = FCCRecord(
            fcc_id="DB-TEST-01", grantee_code="DB", product_code="01",
            applicant_name="GeoFront", product_description="Evangelion Unit 01",
            status="Approved"
        )
        
        # Action: Save new record
        is_new = self.db.save_record(r)
        self.assertTrue(is_new, "First save should report as new")
        
        # Action: Save exact same record
        is_new_again = self.db.save_record(r)
        self.assertFalse(is_new_again, "Second save of same fingerprint should report existing (False)")
        
        # Action: Modify non-fingerprint field and save (Simulate an update)
        r.status = "Dismissed"
        r.product_description = "Evangelion Unit 01 (Decommissioned)"
        is_new_update = self.db.save_record(r)
        self.assertFalse(is_new_update, "Saving update to existing record should report existing")
        
        # Verification: Check database
        records = self.db.get_latest_records(10)
        self.assertEqual(len(records), 1, "Database should contain exactly 1 canonical record after updates")
        self.assertEqual(records[0].status, "Dismissed", "Record status should be updated")

    def test_change_detector_logic(self):
        """Test ChangeDetector logic for accurate structural changes."""
        old_record = FCCRecord(
            fcc_id="SYS-01", grantee_code="SYS", product_code="01",
            applicant_name="System", status="Pending"
        )
        new_record_updated = FCCRecord(
            fcc_id="SYS-01", grantee_code="SYS", product_code="01",
            applicant_name="System", status="Granted"
        )
        new_record_created = FCCRecord(
            fcc_id="SYS-02", grantee_code="SYS", product_code="02",
            applicant_name="System", status="Granted"
        )
        
        prev_data = {"System": [old_record]}
        curr_data = {"System": [new_record_updated, new_record_created]}
        
        detector = ChangeDetector()
        changes = detector.detect_changes(curr_data, prev_data)
        
        self.assertEqual(len(changes), 2, "Should detect exactly 2 changes (1 update, 1 creation)")
        
        # Map changes by FCC ID
        c_map = {c.fcc_id: c for c in changes}
        
        self.assertIn("SYS-01", c_map)
        self.assertEqual(c_map["SYS-01"].change_type, ChangeType.STATUS_CHANGED)
        self.assertEqual(c_map["SYS-01"].old_value, "Pending")
        self.assertEqual(c_map["SYS-01"].new_value, "Granted")
        
        self.assertIn("SYS-02", c_map)
        self.assertEqual(c_map["SYS-02"].change_type, ChangeType.NEW)

if __name__ == '__main__':
    unittest.main()

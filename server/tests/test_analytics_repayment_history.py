import unittest
from datetime import datetime

from app.services.analytics import _month_key


class AnalyticsRepaymentHistoryTest(unittest.TestCase):
    def test_month_key_converts_utc_storage_to_local_timezone(self):
        paid_at = datetime(2026, 8, 31, 16, 30, 0)
        self.assertEqual(_month_key(paid_at, "Asia/Shanghai"), "2026-09")

    def test_month_key_keeps_previous_local_month_when_expected(self):
        paid_at = datetime(2026, 8, 15, 12, 0, 0)
        self.assertEqual(_month_key(paid_at, "Asia/Shanghai"), "2026-08")


if __name__ == "__main__":
    unittest.main()

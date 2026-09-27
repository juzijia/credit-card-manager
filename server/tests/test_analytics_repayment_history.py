import unittest
from datetime import date, datetime
from types import SimpleNamespace

from app.services.analytics import _month_key


class AnalyticsRepaymentHistoryTest(unittest.TestCase):
    def test_month_key_converts_utc_storage_to_local_timezone(self):
        paid_at = datetime(2026, 8, 31, 16, 30, 0)
        self.assertEqual(_month_key(paid_at, "Asia/Shanghai"), "2026-09")

    def test_month_key_keeps_previous_local_month_when_expected(self):
        paid_at = datetime(2026, 8, 15, 12, 0, 0)
        self.assertEqual(_month_key(paid_at, "Asia/Shanghai"), "2026-08")

    def test_historical_mark_paid_can_fall_back_to_due_month(self):
        start_month = date(2025, 10, 1)
        repayment = SimpleNamespace(
            paid_at=datetime(2026, 9, 27, 5, 0, 0),
            note="Marked paid in full",
            statement=SimpleNamespace(due_date=date(2025, 12, 20)),
        )
        key = _month_key(repayment.paid_at, "Asia/Shanghai")
        if (
            repayment.note == "Marked paid in full"
            and key not in {f"2025-{m:02d}" for m in range(10, 13)}
            and repayment.statement.due_date < date(2026, 1, 1)
        ):
            key = _month_key(repayment.statement.due_date)
        self.assertEqual(key, "2025-12")


if __name__ == "__main__":
    unittest.main()

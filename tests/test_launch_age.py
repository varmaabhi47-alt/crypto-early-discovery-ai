import unittest
from datetime import datetime, timezone

import app


class LaunchAgeTests(unittest.TestCase):
    def test_iso_timestamp_age(self):
        now = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc).timestamp()
        created = "2026-11-17T06:00:00Z"
        self.assertEqual(app.pool_age_hours(created, now), 6.0)

    def test_millisecond_timestamp_age(self):
        now = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc).timestamp()
        created_ms = datetime(2026, 11, 17, 11, 30, tzinfo=timezone.utc).timestamp() * 1000
        self.assertEqual(app.pool_age_hours(created_ms, now), 0.5)

    def test_naive_timestamp_is_treated_as_utc(self):
        now = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc).timestamp()
        self.assertEqual(app.pool_age_hours("2026-11-17T10:00:00", now), 2.0)

    def test_missing_or_invalid_timestamp_is_unknown(self):
        self.assertIsNone(app.pool_age_hours(None, 0))
        self.assertIsNone(app.pool_age_hours("not-a-timestamp", 0))

    def test_future_timestamp_is_clamped_to_zero(self):
        now = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc).timestamp()
        future = "2026-11-17T13:00:00Z"
        self.assertEqual(app.pool_age_hours(future, now), 0.0)

    def test_launch_stage_boundaries(self):
        self.assertEqual(app.launch_stage(None), "Age unavailable")
        self.assertEqual(app.launch_stage(0.5), "Just launched")
        self.assertEqual(app.launch_stage(1), "Just launched")
        self.assertEqual(app.launch_stage(4), "Very early")
        self.assertEqual(app.launch_stage(6), "Very early")
        self.assertEqual(app.launch_stage(12), "Early")
        self.assertEqual(app.launch_stage(24), "Early")
        self.assertEqual(app.launch_stage(30), "Older pool")


if __name__ == "__main__":
    unittest.main()

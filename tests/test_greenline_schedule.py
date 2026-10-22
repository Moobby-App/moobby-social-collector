from datetime import datetime, timezone
import importlib.util
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('greenline_schedule', Path(__file__).resolve().parents[1] / 'scripts/run_greenline_scheduled.py')
schedule = importlib.util.module_from_spec(spec)
spec.loader.exec_module(schedule)


class ScheduleTests(unittest.TestCase):
    def test_wib_hours_every_ten_minutes_and_first_start(self):
        config = {'start_at': '2026-10-22T04:00:00+07:00'}
        for instant, expected in [
            ('2026-10-21T21:00:00+07:00', False),
            ('2026-10-22T03:50:00+07:00', False),
            ('2026-10-22T04:00:00+07:00', True),
            ('2026-10-22T04:01:00+07:00', False),
            ('2026-10-22T04:10:00+07:00', True),
            ('2026-10-22T23:50:00+07:00', True),
            ('2026-10-23T00:00:00+07:00', False),
        ]:
            self.assertEqual(schedule.due(datetime.fromisoformat(instant), config, {}), expected)

    def test_pause_cooldown_and_login_response(self):
        now = datetime(2026, 10, 22, 0, 0, tzinfo=timezone.utc)  # 07:00 WIB
        config = {'start_at': '2026-10-22T04:00:00+07:00'}
        self.assertFalse(schedule.due(now, config, {'paused': True}))
        self.assertFalse(schedule.due(now, config, {'cooldown_until': '2026-10-22T01:00:00+00:00'}))
        self.assertTrue(schedule.due(now, config, {'cooldown_until': '2026-10-21T23:00:00+00:00'}))
        self.assertTrue(schedule.next_state({'queries': [{'status': 'login_or_verification'}]}, now)['paused'])
        self.assertIn('cooldown_until', schedule.next_state({'queries': [{'status': 'access_or_rate_limited'}]}, now))

    def test_only_one_scheduler_can_hold_lock(self):
        with tempfile.TemporaryDirectory() as temp:
            lock = Path(temp) / 'lock'
            with schedule.process_lock(lock) as first:
                self.assertTrue(first)
                with schedule.process_lock(lock) as second:
                    self.assertFalse(second)
            with schedule.process_lock(lock) as third:
                self.assertTrue(third)


if __name__ == '__main__':
    unittest.main()

import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from bin import ics_events as ics

ET = ZoneInfo("America/New_York")
FIXTURE = """BEGIN:VCALENDAR
BEGIN:VEVENT
UID:weekly-1
DTSTART;TZID=Eastern Standard Time:20260907T100000
DTEND;TZID=Eastern Standard Time:20260907T103000
RRULE:FREQ=WEEKLY;BYDAY=MO,WE;INTERVAL=1
EXDATE;TZID=Eastern Standard Time:20260916T100000
SUMMARY:Weekly Opportunity Review Sprint
LOCATION:Teams
DESCRIPTION:Line one\\nLine two\\, with comma
X-MICROSOFT-CDO-BUSYSTATUS:BUSY
ORGANIZER;CN=Lee Park:mailto:lee.park@example.com
ATTENDEE;CN=Jordan Example:mailto:jordan@example.com
END:VEVENT
BEGIN:VEVENT
UID:weekly-1
RECURRENCE-ID;TZID=Eastern Standard Time:20260914T100000
DTSTART;TZID=Eastern Standard Time:20260914T140000
DTEND;TZID=Eastern Standard Time:20260914T143000
SUMMARY:Weekly Opportunity Review Sprint (moved)
END:VEVENT
BEGIN:VEVENT
UID:single-1
DTSTART;TZID=Central Standard Time:20260915T090000
DTEND;TZID=Central Standard Time:20260915T100000
SUMMARY:Partner | Civic Data Co-op - Partnership Discussion
END:VEVENT
BEGIN:VEVENT
UID:allday-1
DTSTART;VALUE=DATE:20260915
DTEND;VALUE=DATE:20260916
SUMMARY:Office closed
END:VEVENT
BEGIN:VEVENT
UID:old-1
DTSTART:16010101T000000
DTEND:16010101T003000
RRULE:FREQ=DAILY;UNTIL=17000101T000000Z
SUMMARY:Ancient recurring junk
END:VEVENT
BEGIN:VEVENT
UID:cancelled-1
DTSTART;TZID=Eastern Standard Time:20260915T160000
DTEND;TZID=Eastern Standard Time:20260915T163000
STATUS:CANCELLED
SUMMARY:Cancelled thing
END:VEVENT
END:VCALENDAR
"""


class TestIcs(unittest.TestCase):
    def setUp(self):
        self.events = ics.parse_events(FIXTURE)

    def test_parse_basics(self):
        ev = self.events[0]
        self.assertEqual(ev["SUMMARY"], "Weekly Opportunity Review Sprint")
        self.assertEqual(ev["DESCRIPTION"], "Line one\nLine two, with comma")
        self.assertEqual(ev["organizer"], "Lee Park")
        self.assertEqual(ev["attendees"], ["Jordan Example"])
        self.assertEqual(ev["DTSTART"].tzinfo.key, "America/New_York")

    def test_expand_window_monday_sept14(self):
        now = datetime(2026, 9, 14, 8, 0, tzinfo=ET)
        out = ics.expand(self.events, now, 48)
        titles = [(e["summary"], e["start"][:16]) for e in out]
        self.assertIn(("Weekly Opportunity Review Sprint (moved)", "2026-09-14T14:00"), titles)  # override applied
        self.assertNotIn(("Weekly Opportunity Review Sprint", "2026-09-14T10:00"), titles)         # original slot replaced
        self.assertIn(("Partner | Civic Data Co-op - Partnership Discussion", "2026-09-15T09:00"), titles)
        self.assertTrue(any(e["all_day"] and e["summary"] == "Office closed" for e in out))
        self.assertFalse(any(e["summary"].startswith("Ancient") for e in out))
        self.assertFalse(any(e["summary"] == "Cancelled thing" for e in out))
        central = [e for e in out if e["summary"].startswith("Unified")][0]
        self.assertTrue(central["start"].endswith("-05:00"))                                       # Central kept its zone

    def test_window_excludes_single_events_outside_48h(self):
        now = datetime(2026, 9, 11, 15, 0, tzinfo=ET)          # Friday afternoon: fixture events are Mon/Tue next week
        out = ics.expand(self.events, now, 48)
        self.assertEqual(out, [])
        now = datetime(2026, 9, 14, 8, 0, tzinfo=ET)
        out = ics.expand(self.events, now, 48)
        self.assertTrue(all(e["start"] >= "2026-09-14" and e["start"] < "2026-09-17" for e in out))

    def test_exdate_skips_wednesday_sept16(self):
        now = datetime(2026, 9, 16, 8, 0, tzinfo=ET)
        out = ics.expand(self.events, now, 24)
        self.assertFalse(any(e["summary"].startswith("Weekly Opportunity") and e["start"].startswith("2026-09-16") for e in out))

    def test_cli_file_mode_and_not_configured(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "k.ics"; p.write_text(FIXTURE)
            import io, contextlib
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                ics.main(["--file", str(p), "--hours", "48"])
            data = json.loads(buf.getvalue())
            self.assertEqual(data["status"], "ok")
            self.assertTrue(all(e["calendar"] == "file" for e in data["events"]))
            buf = io.StringIO()
            with unittest.mock.patch.object(ics, "ENV_FILE", Path(d) / "nope"), unittest.mock.patch.dict("os.environ", {}, clear=True), contextlib.redirect_stdout(buf):
                ics.main([])
            self.assertEqual(json.loads(buf.getvalue())["status"], "not_configured")


import unittest.mock  # noqa: E402

if __name__ == "__main__":
    unittest.main()

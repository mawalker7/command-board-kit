#!/usr/bin/env python3
"""ics_events.py: read published ICS calendar feeds (Outlook "publish calendar" links) for the board.

Feed URLs are unguessable secrets: they live in ~/.config/command-board/env as <NAME>_CAL_ICS=https://...
(NAME becomes the calendar label, e.g. CIVIC_CAL_ICS -> "civic"). Never commit or print them.

Outputs JSON: [{calendar, start, end, all_day, summary, location, description, busy, organizer, attendees}]
for occurrences overlapping [now - 1 h, now + HOURS]. Recurrence is expanded for the common Outlook shapes
(FREQ=DAILY/WEEKLY/MONTHLY(BYDAY|BYMONTHDAY)/YEARLY, INTERVAL, COUNT, UNTIL, BYDAY, EXDATE, RECURRENCE-ID overrides).
Windows time-zone ids are mapped to IANA zones; unknown ones fall back to the machine's zone.
"""

import json
import os
import re
import sys
import urllib.request
from datetime import datetime, date, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ENV_FILE = Path(os.environ.get("BOARD_CONFIG_DIR", Path.home() / ".config" / "command-board")) / "env"
WINDOWS_TZ = {
    "Eastern Standard Time": "America/New_York", "Central Standard Time": "America/Chicago",
    "Mountain Standard Time": "America/Denver", "Pacific Standard Time": "America/Los_Angeles",
    "SA Pacific Standard Time": "America/Bogota", "E. South America Standard Time": "America/Sao_Paulo",
    "Atlantic Standard Time": "America/Halifax", "UTC": "UTC", "GMT Standard Time": "Europe/London",
    "W. Europe Standard Time": "Europe/Berlin", "Romance Standard Time": "Europe/Paris",
    "Central Europe Standard Time": "Europe/Budapest", "India Standard Time": "Asia/Kolkata",
    "AUS Eastern Standard Time": "Australia/Sydney", "Tokyo Standard Time": "Asia/Tokyo",
    "China Standard Time": "Asia/Shanghai", "Singapore Standard Time": "Asia/Singapore",
}
WEEKDAYS = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}


def local_tz():
    return datetime.now().astimezone().tzinfo


def zone(tzid):
    if not tzid:
        return None
    name = WINDOWS_TZ.get(tzid, tzid)
    try:
        return ZoneInfo(name)
    except Exception:
        return local_tz()


# ---------- parsing ----------

def unfold(text):
    return re.sub(r"\r?\n[ \t]", "", text.replace("\r\n", "\n"))


def unescape(v):
    return v.replace("\\n", "\n").replace("\\N", "\n").replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\")


def parse_line(line):
    """'DTSTART;TZID=Eastern Standard Time:20260914T110000' -> ('DTSTART', {'TZID': '...'}, '2026...')."""
    head, _, value = line.partition(":")
    parts = head.split(";")
    params = {}
    for p in parts[1:]:
        k, _, v = p.partition("=")
        params[k.upper()] = v.strip('"')
    return parts[0].upper(), params, value


def parse_dt(value, params):
    value = value.strip()
    if params.get("VALUE") == "DATE" or re.fullmatch(r"\d{8}", value):
        return date(int(value[:4]), int(value[4:6]), int(value[6:8])), True
    m = re.fullmatch(r"(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})(Z?)", value)
    if not m:
        raise ValueError(f"bad datetime {value!r}")
    y, mo, d, h, mi, s, z = m.groups()
    naive = datetime(int(y), int(mo), int(d), int(h), int(mi), int(s))
    tz = timezone.utc if z else (zone(params.get("TZID")) or local_tz())
    return naive.replace(tzinfo=tz), False


def parse_events(text):
    events, cur = [], None
    for line in unfold(text).split("\n"):
        line = line.rstrip()
        if line == "BEGIN:VEVENT":
            cur = {"attendees": [], "exdates": set(), "raw": {}}
        elif line == "END:VEVENT":
            if cur and "DTSTART" in cur:
                events.append(cur)
            cur = None
        elif cur is not None and ":" in line:
            name, params, value = parse_line(line)
            if name in ("DTSTART", "DTEND", "RECURRENCE-ID"):
                cur[name], cur[name + "_ALLDAY"] = parse_dt(value, params)
            elif name == "EXDATE":
                for v in value.split(","):
                    dt, _ = parse_dt(v, params)
                    cur["exdates"].add(_key(dt))
            elif name == "ATTENDEE":
                cur["attendees"].append(params.get("CN") or value.replace("mailto:", ""))
            elif name == "ORGANIZER":
                cur["organizer"] = params.get("CN") or value.replace("mailto:", "")
            elif name in ("SUMMARY", "LOCATION", "DESCRIPTION", "UID", "RRULE", "X-MICROSOFT-CDO-BUSYSTATUS", "STATUS", "TRANSP"):
                cur[name] = unescape(value) if name in ("SUMMARY", "LOCATION", "DESCRIPTION") else value
    return events


def _key(dt):
    """Comparable key for EXDATE / RECURRENCE-ID matching (minute precision, UTC for datetimes)."""
    if isinstance(dt, datetime):
        return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M")
    return dt.strftime("%Y%m%d")


# ---------- recurrence ----------

def parse_rrule(s):
    out = {}
    for part in s.split(";"):
        k, _, v = part.partition("=")
        out[k.upper()] = v
    return out


def occurrences(ev, win_start, win_end):
    """Yield start datetimes/dates of this event (expanded) that begin before win_end and end after win_start."""
    start = ev["DTSTART"]
    all_day = ev["DTSTART_ALLDAY"]
    duration = (ev.get("DTEND") - start) if ev.get("DTEND") is not None else (timedelta(days=1) if all_day else timedelta(hours=1))
    rrule = parse_rrule(ev["RRULE"]) if ev.get("RRULE") else None
    if not rrule:
        yield start, start + duration
        return
    freq = rrule.get("FREQ", "").upper()
    interval = int(rrule.get("INTERVAL", "1") or 1)
    count = int(rrule["COUNT"]) if rrule.get("COUNT") else None
    until = None
    if rrule.get("UNTIL"):
        until, _ = parse_dt(rrule["UNTIL"], {})
        if isinstance(until, datetime) and isinstance(start, datetime):
            until = until.astimezone(start.tzinfo)
        elif isinstance(until, datetime) and not isinstance(start, datetime):
            until = until.date()
    bydays = [d for d in rrule.get("BYDAY", "").split(",") if d]
    bymonthday = [int(x) for x in rrule.get("BYMONTHDAY", "").split(",") if x]
    produced, cursor, guard = 0, start, 0
    limit_end = win_end if isinstance(start, datetime) else win_end.date()

    def emit(candidate):
        nonlocal produced
        if until is not None and candidate > until:
            return "stop"
        if candidate > limit_end:
            return "stop"
        produced += 1
        if count is not None and produced > count:
            return "stop"
        end = candidate + duration
        if _key(candidate) in ev["exdates"]:
            return None
        if (candidate if isinstance(candidate, datetime) else datetime.combine(candidate, time.min, local_tz())) < win_start and \
           (end if isinstance(end, datetime) else datetime.combine(end, time.min, local_tz())) <= win_start:
            return None
        return candidate, end

    while guard < 5000:
        guard += 1
        if freq == "WEEKLY" and bydays:
            week_start = cursor - timedelta(days=cursor.weekday())
            for d in sorted(WEEKDAYS[x[-2:]] for x in bydays if x[-2:] in WEEKDAYS):
                cand = week_start + timedelta(days=d)
                if cand < start:
                    continue
                r = emit(cand)
                if r == "stop":
                    return
                if r:
                    yield r
            cursor = week_start + timedelta(weeks=interval)
            continue
        if freq == "MONTHLY" and bydays and re.match(r"^-?\d", bydays[0]):
            # e.g. 2TU = second Tuesday, -1FR = last Friday
            n, wd = int(bydays[0][:-2]), WEEKDAYS[bydays[0][-2:]]
            first = cursor.replace(day=1)
            days = [first + timedelta(days=i) for i in range(31) if (first + timedelta(days=i)).month == first.month and (first + timedelta(days=i)).weekday() == wd]
            cand = days[n - 1] if n > 0 and len(days) >= n else (days[n] if n < 0 else None)
            if cand is not None and cand >= start:
                r = emit(cand)
                if r == "stop":
                    return
                if r:
                    yield r
            cursor = (first + timedelta(days=32)).replace(day=1) + (cursor - first)
            for _ in range(interval - 1):
                cursor = (cursor.replace(day=1) + timedelta(days=32)).replace(day=1) + (cursor - cursor.replace(day=1))
            continue
        # DAILY, plain WEEKLY, MONTHLY by month-day, YEARLY
        cand = cursor
        if freq == "MONTHLY" and bymonthday and cand.day not in bymonthday:
            pass
        else:
            r = emit(cand)
            if r == "stop":
                return
            if r:
                yield r
        if freq == "DAILY":
            cursor = cursor + timedelta(days=interval)
        elif freq == "WEEKLY":
            cursor = cursor + timedelta(weeks=interval)
        elif freq == "MONTHLY":
            nxt = cursor
            for _ in range(interval):
                nxt = (nxt.replace(day=1) + timedelta(days=32)).replace(day=min(start.day, 28)) if start.day > 28 else (nxt.replace(day=1) + timedelta(days=32)).replace(day=start.day)
            cursor = nxt
        elif freq == "YEARLY":
            cursor = cursor.replace(year=cursor.year + interval)
        else:
            return


def expand(events, now, hours):
    win_start, win_end = now - timedelta(hours=1), now + timedelta(hours=hours)
    overrides = {(e.get("UID"), _key(e["RECURRENCE-ID"])): e for e in events if e.get("RECURRENCE-ID") is not None}
    out = []
    for ev in events:
        if ev.get("RECURRENCE-ID") is not None:
            continue  # rendered via its master's occurrence
        for start, end in occurrences(ev, win_start, win_end):
            src = overrides.get((ev.get("UID"), _key(start)), ev)
            if src is not ev:
                start, end = src["DTSTART"], src.get("DTEND") or (src["DTSTART"] + (end - start))
            if src.get("STATUS", "").upper() == "CANCELLED":
                continue
            s_dt = start if isinstance(start, datetime) else datetime.combine(start, time.min, local_tz())
            e_dt = end if isinstance(end, datetime) else datetime.combine(end, time.min, local_tz())
            if not (s_dt < win_end and e_dt > win_start):
                continue  # the window is enforced here for every event, recurring or not
            out.append({
                "start": start.isoformat(), "end": end.isoformat(), "all_day": not isinstance(start, datetime),
                "summary": src.get("SUMMARY", "(no title)"), "location": src.get("LOCATION", ""),
                "description": re.sub(r"\s+", " ", src.get("DESCRIPTION", ""))[:600],
                "busy": src.get("X-MICROSOFT-CDO-BUSYSTATUS", ""), "organizer": src.get("organizer", ""),
                "attendees": src.get("attendees", [])[:12],
            })
    out.sort(key=lambda e: e["start"])
    return out


# ---------- feeds ----------

def feeds():
    urls = {}
    for k, v in os.environ.items():
        if k.endswith("_CAL_ICS") and v:
            urls[k[: -len("_CAL_ICS")].lower()] = v
    if ENV_FILE.exists():
        for raw in ENV_FILE.read_text().splitlines():
            line = raw.strip()
            if line.endswith("_CAL_ICS") or "=" not in line or line.startswith("#"):
                continue
            k, _, v = line.partition("=")
            if k.strip().endswith("_CAL_ICS") and v.strip():
                urls.setdefault(k.strip()[: -len("_CAL_ICS")].lower(), v.strip().strip('"'))
    return urls


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "command-board-ics/1.0"})
    try:
        import certifi, ssl
        ctx = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        ctx = None
    with urllib.request.urlopen(req, timeout=45, context=ctx) as resp:
        return resp.read().decode("utf-8", errors="replace")


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=48)
    ap.add_argument("--file", help="parse a local .ics instead of the configured feeds (label 'file')")
    args = ap.parse_args(argv)
    now = datetime.now(local_tz())
    result, errors = [], []
    sources = {"file": args.file} if args.file else feeds()
    if not sources:
        print(json.dumps({"events": [], "errors": ["no *_CAL_ICS feeds configured"], "status": "not_configured"}))
        return 0
    for name, src in sources.items():
        try:
            text = Path(src).read_text() if args.file else fetch(src)
            for e in expand(parse_events(text), now, args.hours):
                e["calendar"] = name
                result.append(e)
        except Exception as e:  # network, parse: report, never crash the collector
            errors.append(f"{name}: {type(e).__name__}: {str(e)[:160]}")
    result.sort(key=lambda e: e["start"])
    print(json.dumps({"events": result, "errors": errors, "status": "ok" if not errors else ("partial" if result else "failed")}, indent=1, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())

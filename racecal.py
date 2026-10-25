#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The Racecal contributors
"""Build a single filtered motorsport calendar (.ics) from several series.

Session data comes from TheSportsDB; events without a feed come from
events.toml. Uses only the Python standard library (3.11+).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import tomllib
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

API = "https://www.thesportsdb.com/api/v1/json/123"
REQUEST_GAP = 2.2  # seconds between API calls (free tier rate limit)
MAX_EMPTY_ROUNDS = 3  # stop scanning a season after this many empty rounds
MAX_ROUND = 40

SESSION_WORDS = re.compile(
    r"Practice|Qualifying|Hyperpole|Shootout|Warm.?up|Test|Prologue|FP\d", re.I
)
HOURS_RACE = re.compile(r"^(\d+)\s*Hours\b", re.I)


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------


@dataclass
class Session:
    name: str
    start: datetime | date  # datetime (UTC) when timed, date when time unknown
    minutes: int
    venue: str = ""
    uid: str = ""

    @property
    def timed(self) -> bool:
        return isinstance(self.start, datetime)

    @property
    def end(self) -> datetime | date:
        if self.timed:
            return self.start + timedelta(minutes=self.minutes)
        return self.start


@dataclass
class Event:
    uid: str
    summary: str
    start: datetime | date
    end: datetime | date  # exclusive for all-day events
    location: str = ""
    description: str = ""
    sessions: list[Session] = field(default_factory=list)


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------


class Api:
    def __init__(self, cache_dir: Path | None):
        self.cache_dir = cache_dir
        self._last = 0.0

    def get(self, endpoint: str) -> dict:
        cache = None
        if self.cache_dir:
            cache = self.cache_dir / (re.sub(r"[^\w.-]", "_", endpoint) + ".json")
            if cache.exists():
                return json.loads(cache.read_text())

        for attempt in range(5):
            wait = REQUEST_GAP - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            try:
                req = urllib.request.Request(
                    f"{API}/{endpoint}", headers={"User-Agent": "racecal/1.0"}
                )
                with urllib.request.urlopen(req, timeout=30) as resp:
                    body = resp.read().decode("utf-8")
                data = json.loads(body)
                break
            except (urllib.error.URLError, json.JSONDecodeError, TimeoutError):
                # Rate limited or transient failure: back off and retry.
                time.sleep(5 * (attempt + 1))
        else:
            raise RuntimeError(f"giving up on {endpoint}")

        if cache:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(data))
        return data

    def season_events(self, league: int, season: int) -> list[dict]:
        """All events for a season, walked round by round.

        The free API caps season listings at 15 events, but per-round
        queries return the full round.
        """
        events: list[dict] = []
        empty = 0
        for rnd in range(0, MAX_ROUND + 1):
            data = self.get(f"eventsround.php?id={league}&r={rnd}&s={season}")
            found = data.get("events") or []
            if found:
                events.extend(found)
                empty = 0
            else:
                empty += 1
                # Round 0 is often unused; don't let it count towards stopping.
                if rnd > 0 and empty >= MAX_EMPTY_ROUNDS:
                    break
        return events


# --------------------------------------------------------------------------
# Session building
# --------------------------------------------------------------------------


def parse_start(raw: dict) -> datetime | date | None:
    stamp = raw.get("strTimestamp") or ""
    day = raw.get("dateEvent") or ""
    if stamp:
        try:
            dt = datetime.fromisoformat(stamp.replace("Z", "")).replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            dt = None
        if dt is not None:
            # A midnight timestamp means the source has no time for it.
            if dt.hour == 0 and dt.minute == 0 and dt.second == 0:
                return dt.date()
            return dt
    if day:
        try:
            return date.fromisoformat(day)
        except ValueError:
            return None
    return None


def session_minutes(name: str, series: dict) -> int:
    default = int(series.get("duration", 60))
    if not SESSION_WORDS.search(name):
        hours = HOURS_RACE.match(name)
        if hours:
            return int(hours.group(1)) * 60
    long = series.get("long_sessions")
    if long and re.search(long["pattern"], name, re.I):
        return int(long["minutes"])
    return default


def keep(name: str, series: dict) -> bool:
    inc = series.get("include")
    if inc and not any(re.search(p, name, re.I) for p in inc):
        return False
    exc = series.get("exclude") or []
    return not any(re.search(p, name, re.I) for p in exc)


def to_sessions(raw_events: list[dict], series: dict) -> dict[str, list[Session]]:
    """Sessions grouped by round key (season + round)."""
    rounds: dict[str, list[Session]] = {}
    seen: set[str] = set()
    for raw in raw_events:
        name = (raw.get("strEvent") or "").strip()
        eid = str(raw.get("idEvent") or "")
        if not name or eid in seen or not keep(name, series):
            continue
        start = parse_start(raw)
        if start is None:
            continue
        seen.add(eid)
        if series.get("group_by") == "season":
            key = f"{raw.get('strSeason')}"
        else:
            key = f"{raw.get('strSeason')}-r{raw.get('intRound')}"
        rounds.setdefault(key, []).append(
            Session(
                name=name,
                start=start,
                minutes=session_minutes(name, series),
                venue=(raw.get("strVenue") or "").strip(),
                uid=f"tsdb-{eid}@racecal",
            )
        )
    for sessions in rounds.values():
        sessions.sort(key=sort_key)
    return rounds


def sort_key(s: Session):
    if s.timed:
        return s.start
    return datetime.combine(s.start, datetime.min.time(), tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# Event building
# --------------------------------------------------------------------------


def fmt_session(s: Session, tz: ZoneInfo) -> str:
    if s.timed:
        local = s.start.astimezone(tz)
        return f"{local:%a %d %b %H:%M} {s.name}"
    return f"{s.start:%a %d %b} {s.name}"


def common_name(sessions: list[Session], series: dict) -> str:
    patterns = series.get("strip") or []
    if isinstance(patterns, str):
        patterns = [patterns]
    names = []
    for s in sessions:
        name = s.name
        for p in patterns:
            name = re.sub(p, "", name, flags=re.I).strip()
        names.append(name or s.name)
    # Most frequent cleaned name wins (handles a stray odd session title).
    return max(set(names), key=names.count)


def grouped_event(key: str, sessions: list[Session], series: dict, tz) -> Event:
    first, last = sessions[0], sessions[-1]
    title = series.get("title")
    if title:
        # Fixed title for series whose session names aren't event names
        # (e.g. Dakar stages are just "Stage 4").
        name = title.format(season=key.split("-")[0])
    else:
        name = common_name(sessions, series)
    venue = next((s.venue for s in sessions if s.venue), "")
    lines = [fmt_session(s, tz) for s in sessions]
    tzname = "" if tz.key == "UTC" else f" ({tz.key})"
    desc = f"{len(sessions)} sessions{tzname}:\n" + "\n".join(lines)

    if first.timed:
        end = max((s.end for s in sessions if s.timed), default=first.end)
        start: datetime | date = first.start
    else:
        start = first.start
        last_day = last.start.date() if last.timed else last.start
        end = last_day + timedelta(days=1)

    return Event(
        uid=f"racecal-{series['league']}-{key}@racecal",
        summary=f"{series['name']} · {name}",
        start=start,
        end=end,
        location=venue,
        description=desc,
        sessions=sessions,
    )


def single_events(sessions: list[Session], series: dict) -> list[Event]:
    out = []
    for s in sessions:
        end = s.end if s.timed else s.start + timedelta(days=1)
        out.append(
            Event(
                uid=s.uid,
                summary=f"{series['name']} · {s.name}",
                start=s.start,
                end=end,
                location=s.venue,
            )
        )
    return out


def manual_events(path: Path) -> list[Event]:
    if not path.exists():
        return []
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    out = []
    for item in data.get("event", []):
        start_d: date = item["start"]
        end_d: date = item.get("end", start_d)
        slug = re.sub(r"[^a-z0-9]+", "-", f"{item['series']}-{item['name']}".lower())
        uid = f"manual-{slug}-{start_d.isoformat()}@racecal"
        if "start_time" in item:
            hh, mm = (int(x) for x in item["start_time"].split(":"))
            start: datetime | date = datetime(
                start_d.year, start_d.month, start_d.day, hh, mm, tzinfo=timezone.utc
            )
            end: datetime | date = datetime(
                end_d.year, end_d.month, end_d.day, 23, 59, tzinfo=timezone.utc
            )
        else:
            start, end = start_d, end_d + timedelta(days=1)
        out.append(
            Event(
                uid=uid,
                summary=f"{item['series']} · {item['name']}",
                start=start,
                end=end,
                location=item.get("location", ""),
                description=item.get("notes", ""),
            )
        )
    return out


# --------------------------------------------------------------------------
# iCalendar output
# --------------------------------------------------------------------------


def ical_escape(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
    )


def fold(line: str) -> str:
    """Fold to 75 octets per RFC 5545."""
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return line
    parts, chunk = [], b""
    for ch in line:
        b = ch.encode("utf-8")
        if len(chunk) + len(b) > (75 if not parts else 74):
            parts.append(chunk.decode("utf-8"))
            chunk = b""
        chunk += b
    parts.append(chunk.decode("utf-8"))
    return "\r\n ".join(parts)


def ical_time(prop: str, value: datetime | date) -> str:
    if isinstance(value, datetime):
        return f"{prop}:{value.astimezone(timezone.utc):%Y%m%dT%H%M%SZ}"
    return f"{prop};VALUE=DATE:{value:%Y%m%d}"


def render(events: list[Event], name: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Racecal//Racecal//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{ical_escape(name)}",
        "X-PUBLISHED-TTL:PT12H",
        "REFRESH-INTERVAL;VALUE=DURATION:PT12H",
    ]
    for ev in events:
        lines += [
            "BEGIN:VEVENT",
            f"UID:{ev.uid}",
            f"DTSTAMP:{stamp}",
            ical_time("DTSTART", ev.start),
            ical_time("DTEND", ev.end),
            f"SUMMARY:{ical_escape(ev.summary)}",
        ]
        if ev.location:
            lines.append(f"LOCATION:{ical_escape(ev.location)}")
        if ev.description:
            lines.append(f"DESCRIPTION:{ical_escape(ev.description)}")
        lines.append("TRANSP:TRANSPARENT")
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(fold(l) for l in lines) + "\r\n"


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


def build(root: Path, cache_dir: Path | None, verbose: bool) -> list[Event]:
    config = tomllib.loads((root / "config.toml").read_text(encoding="utf-8"))
    settings = config.get("settings", {})
    tz = ZoneInfo(settings.get("timezone", "UTC"))
    this_year = datetime.now(timezone.utc).year
    seasons = [this_year + off for off in settings.get("seasons", [0, 1])]
    api = Api(cache_dir)

    events: list[Event] = []
    for series in config.get("series", []):
        raw: list[dict] = []
        for season in seasons:
            try:
                raw += api.season_events(int(series["league"]), season)
            except RuntimeError as err:
                print(f"warning: {series['name']} {season}: {err}", file=sys.stderr)
        rounds = to_sessions(raw, series)
        before = len(events)
        for key, sessions in rounds.items():
            if series.get("group"):
                events.append(grouped_event(key, sessions, series, tz))
            else:
                events += single_events(sessions, series)
        if verbose:
            print(f"{series['name']}: {len(events) - before} events", file=sys.stderr)

    manual = manual_events(root / "events.toml")
    if verbose:
        print(f"manual: {len(manual)} events", file=sys.stderr)
    events += manual

    events.sort(key=lambda e: (sort_key(Session("", e.start, 0)), e.summary))
    return events


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).parent)
    parser.add_argument("--output", type=Path, help="override settings.output")
    parser.add_argument("--cache", type=Path, help="cache API responses here")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    root = args.root.resolve()
    config = tomllib.loads((root / "config.toml").read_text(encoding="utf-8"))
    output = args.output or root / config.get("settings", {}).get(
        "output", "public/racecal.ics"
    )
    name = config.get("settings", {}).get("calendar_name", "Racecal")

    events = build(root, args.cache, args.verbose)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render(events, name), encoding="utf-8", newline="")
    print(f"wrote {len(events)} events to {output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())

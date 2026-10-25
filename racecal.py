#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The Racecal contributors
"""Build a single filtered motorsport calendar (.ics) from several series.

Sources:
  sportstimes  open schedule data on GitHub (F1, IndyCar, ...)
  fiawec       the official FIA WEC website's structured race data
  thesportsdb  TheSportsDB free API (WRC, DTM, Dakar)
  events.toml  hand-maintained events with no data feed

Uses only the Python standard library (3.11+).
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
from typing import Callable
from zoneinfo import ZoneInfo

USER_AGENT = "Mozilla/5.0 (compatible; racecal/2.0)"
SPORTSTIMES = "https://raw.githubusercontent.com/sportstimes/f1/main/_db"
FIAWEC = "https://www.fiawec.com"
THESPORTSDB = "https://www.thesportsdb.com/api/v1/json/123"

RETRY_BACKOFF = (20, 40, 60, 0)  # wait after each failed attempt (4 attempts)
MAX_FAILURES = 4  # consecutive given-up requests before a host is skipped
MAX_EMPTY_ROUNDS = 2  # stop scanning a TheSportsDB season after this many
MAX_ROUND = 40

HOURS_RACE = re.compile(r"\b(\d+)\s*Hours?\b", re.I)


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------


@dataclass
class Session:
    name: str  # full title shown in the calendar (after the series prefix)
    label: str  # session label used for filters and durations ("Qualifying")
    start: datetime | date  # datetime (UTC) when timed, date when time unknown
    minutes: int = 0  # 0 = not known yet (see apply_duration)
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
class Round:
    key: str  # stable id, e.g. "2026-r19"
    name: str  # event name, e.g. "United States Grand Prix"
    sessions: list[Session] = field(default_factory=list)


@dataclass
class Event:
    uid: str
    summary: str
    start: datetime | date
    end: datetime | date  # exclusive for all-day events
    location: str = ""
    description: str = ""


def start_key(value: datetime | date) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.combine(value, datetime.min.time(), tzinfo=timezone.utc)


def parse_time(stamp: str) -> datetime | date | None:
    """ISO timestamp -> UTC datetime; midnight local time -> date (unknown)."""
    if not stamp:
        return None
    try:
        dt = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        try:
            return date.fromisoformat(stamp[:10])
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    # Sources use midnight as a placeholder for "time not announced".
    if dt.hour == 0 and dt.minute == 0 and dt.second == 0:
        return dt.date()
    return dt.astimezone(timezone.utc)


def label_from_key(key: str) -> str:
    """'sprintQualifying' -> 'Sprint Qualifying', 'practice1' -> 'Practice 1'."""
    special = {"fp1": "Practice 1", "fp2": "Practice 2", "fp3": "Practice 3",
               "gp": "Race", "warmup": "Warm-up"}
    if key in special:
        return special[key]
    words = re.sub(r"(?<=[a-z])(?=[A-Z0-9])", " ", key)
    return words[:1].upper() + words[1:]


# --------------------------------------------------------------------------
# HTTP + stored responses
# --------------------------------------------------------------------------


class FetchError(Exception):
    pass


class Client:
    """Polite HTTP client for one host: spacing, retries, Retry-After."""

    def __init__(self, name: str, gap: float, deadline: float):
        self.name = name
        self.gap = gap
        self.deadline = deadline
        self.blocked = False
        self.requests = 0
        self._last = 0.0
        self._failures = 0

    def get(self, url: str) -> bytes:
        if not self.blocked and time.monotonic() > self.deadline:
            log(f"  {self.name}: time budget used up, using stored data")
            self.blocked = True
        if self.blocked:
            raise FetchError(f"{self.name} skipped for the rest of this run")
        reason = ""
        for attempt, backoff in enumerate(RETRY_BACKOFF):
            wait = self.gap - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            self.requests += 1
            try:
                req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
                with urllib.request.urlopen(req, timeout=30) as resp:
                    body = resp.read()
                self._failures = 0
                return body
            except urllib.error.HTTPError as err:
                if err.code == 404:
                    raise FetchError("HTTP 404") from err
                if err.code not in (429, 500, 502, 503, 504):
                    raise FetchError(f"HTTP {err.code}") from err
                retry_after = err.headers.get("Retry-After", "")
                pause = int(retry_after) if retry_after.isdigit() else backoff
                if err.code == 429 and retry_after.isdigit():
                    # Slow down for the rest of the run instead of tripping
                    # the limit again straight away.
                    self.gap = max(self.gap, int(retry_after) / 9)
                reason = f"HTTP {err.code}"
            except (urllib.error.URLError, TimeoutError) as err:
                pause, reason = backoff, type(err).__name__
            if attempt < len(RETRY_BACKOFF) - 1:
                log(f"  {self.name}: {reason}, waiting {pause}s")
                time.sleep(pause)
        self._failures += 1
        if self._failures >= MAX_FAILURES:
            log(f"  {self.name}: keeps refusing requests, using stored data")
            self.blocked = True
        raise FetchError(f"gave up ({reason})")


class Store:
    """Responses kept between runs (.cache/store.json).

    Each entry holds the parsed data plus the dates it covers, and is only
    fetched again when stale:
      - covers dates that ended more than 2 days ago: never
      - starts within 14 days, or is under way: after 12 hours
      - further out, or no dates (empty rounds, missing pages): after about
        a week, spread over several days so entries don't expire together
    A failed fetch falls back to the stored copy.
    """

    def __init__(self, path: Path | None):
        self.path = path
        self.entries: dict[str, dict] = {}
        if path and path.exists():
            try:
                self.entries = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                log(f"warning: ignoring unreadable store {path}")
        self.fetched = self.reused = self.stale = self.missing = 0

    def save(self) -> None:
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.entries), encoding="utf-8")

    def fresh(self, key: str) -> bool:
        entry = self.entries.get(key)
        if entry is None or "dates" not in entry:
            return False
        now = datetime.now(timezone.utc)
        age = now - datetime.fromisoformat(entry["fetched"])
        dates = [date.fromisoformat(d) for d in entry["dates"]]
        if dates:
            if max(dates) < (now - timedelta(days=2)).date():
                return True
            if min(dates) <= (now + timedelta(days=14)).date():
                return age < timedelta(hours=12)
        spread = sum(key.encode()) % 72  # 0-71 extra hours, stable per key
        return age < timedelta(days=5, hours=spread)

    def get(
        self,
        key: str,
        client: Client,
        url: str,
        parse: Callable[[bytes], object],
        dates: Callable[[object], list[str]],
        refresh: bool = True,
    ):
        """Parsed data for `key`, from the store when fresh, else fetched.

        Returns None when the resource doesn't exist (404) or is unavailable
        with no stored copy.
        """
        entry = self.entries.get(key)
        if entry is not None and (not refresh or self.fresh(key)):
            self.reused += 1
            return entry["data"]
        try:
            data = parse(client.get(url))
        except FetchError as err:
            if str(err) == "HTTP 404":
                self._put(key, None, [])
                self.fetched += 1
                return None
            if entry is not None:
                self.stale += 1
                return entry["data"]
            self.missing += 1
            log(f"  warning: {url} unavailable: {err}")
            return None
        except (ValueError, KeyError) as err:
            if entry is not None:
                self.stale += 1
                return entry["data"]
            self.missing += 1
            log(f"  warning: could not read {url}: {err}")
            return None
        self._put(key, data, dates(data) if data is not None else [])
        self.fetched += 1
        return data

    def _put(self, key: str, data, dates: list[str]) -> None:
        self.entries[key] = {
            "fetched": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "dates": sorted(set(d[:10] for d in dates if d)),
            "data": data,
        }


@dataclass
class Context:
    store: Store
    clients: dict[str, Client]
    seasons: list[int]


# --------------------------------------------------------------------------
# Sources
# --------------------------------------------------------------------------


def source_sportstimes(series: dict, ctx: Context) -> list[Round]:
    """github.com/sportstimes/f1: one JSON file per season, UTC times."""
    key = series["key"]
    client = ctx.clients["sportstimes"]
    config = ctx.store.get(
        f"sportstimes/{key}/config", client, f"{SPORTSTIMES}/{key}/config.json",
        json.loads, lambda d: [],
    ) or {}
    lengths = config.get("sessionLengths", {})
    suffix = series.get("name_suffix", "")
    rounds: list[Round] = []
    for season in ctx.seasons:
        data = ctx.store.get(
            f"sportstimes/{key}/{season}", client, f"{SPORTSTIMES}/{key}/{season}.json",
            json.loads,
            lambda d: [t for r in d.get("races", []) for t in r["sessions"].values()],
        )
        if not data:
            continue
        for race in data.get("races", []):
            name = race["name"]
            if suffix and suffix.strip().lower() not in name.lower():
                name += suffix
            rnd = Round(key=f"{season}-r{race.get('round')}", name=name)
            for skey, stamp in race.get("sessions", {}).items():
                start = parse_time(stamp)
                if start is None:
                    continue
                label = label_from_key(skey)
                minutes = lengths.get(skey) or lengths.get("other")
                rnd.sessions.append(Session(
                    name=f"{name} · {label}", label=label, start=start,
                    minutes=int(minutes) if minutes else 0,
                    venue=race.get("location", ""),
                    uid=f"sportstimes-{key}-{season}-{race.get('slug')}-{skey}@racecal",
                ))
            rounds.append(rnd)
    return rounds


def _wec_race_json(body: bytes) -> dict | None:
    html = body.decode("utf-8", errors="replace")
    for m in re.finditer(
        r'<script type="application/ld\+json">(.*?)</script>', html, re.S
    ):
        try:
            data = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and data.get("@type") == "SportsEvent":
            location = data.get("location") or {}
            return {
                "name": data.get("name", ""),
                "location": location.get("name", "") if isinstance(location, dict) else "",
                "sessions": [
                    {"name": s.get("name", ""), "start": s.get("startDate", "")}
                    for s in data.get("subEvent", [])
                ],
            }
    raise ValueError("no SportsEvent data on page")


def source_fiawec(series: dict, ctx: Context) -> list[Round]:
    """fiawec.com: race pages carry schema.org SportsEvent JSON-LD.

    The season page links most race pages but leaves out the next
    upcoming one, so each slug is also tried with the other seasons' years.
    """
    client = ctx.clients["fiawec"]
    excluded = ("prologue", "test")
    slugs: set[str] = set()
    for season in ctx.seasons:
        found = ctx.store.get(
            f"fiawec/season/{season}", client, f"{FIAWEC}/en/season/{season}",
            lambda b: sorted(set(re.findall(r"/en/race/([a-z0-9-]+-\d{4})\b",
                                            b.decode("utf-8", errors="replace")))),
            lambda d: [],
        )
        slugs.update(found or [])
    slugs = {s for s in slugs if not any(x in s for x in excluded)}
    years = {str(y) for y in ctx.seasons}
    candidates = set()
    for slug in slugs:
        base, year = slug.rsplit("-", 1)
        if year in years:
            candidates.update(f"{base}-{y}" for y in years)

    rounds: list[Round] = []
    for slug in sorted(candidates):
        data = ctx.store.get(
            f"fiawec/race/{slug}", client, f"{FIAWEC}/en/race/{slug}",
            _wec_race_json, lambda d: [s["start"] for s in d["sessions"]],
        )
        if not data or not data["sessions"]:
            continue
        name = re.sub(r"^WEC\s+|\s+\d{4}$", "", data["name"]).strip()
        rnd = Round(key=slug, name=name)
        for i, s in enumerate(data["sessions"]):
            start = parse_time(s["start"])
            if start is None:
                continue
            # "Qualifying - LMGT3 - 8 Hours of Bahrain" -> "Qualifying LMGT3"
            parts = [p.strip() for p in s["name"].split(" - ")]
            label = " ".join(parts[:-1]) if len(parts) > 1 else parts[0]
            rnd.sessions.append(Session(
                name=f"{name} · {label}", label=label, start=start,
                venue=data["location"], uid=f"fiawec-{slug}-{i}@racecal",
            ))
        rounds.append(rnd)
    return rounds


def source_thesportsdb(series: dict, ctx: Context) -> list[Round]:
    """TheSportsDB free API, walked round by round.

    The free key caps season listings at 15 events, but per-round queries
    return the full round.
    """
    client = ctx.clients["thesportsdb"]
    league = int(series["league"])
    by_key: dict[str, list[dict]] = {}

    def add(raw: dict, season: int) -> None:
        if series.get("group_by") == "season":
            rkey = f"{season}"
        else:
            rkey = f"{season}-r{raw.get('intRound')}"
        by_key.setdefault(rkey, []).append(raw)

    for season in ctx.seasons:
        if series.get("listing") == "season":
            # One request for the whole season; fine while it has fewer
            # than 15 events (the free key's cap on this endpoint).
            found = ctx.store.get(
                f"tsdb/{league}/{season}/all", client,
                f"{THESPORTSDB}/eventsseason.php?id={league}&s={season}",
                lambda b: json.loads(b).get("events") or [],
                lambda d: [e.get("dateEvent") or "" for e in d],
            ) or []
            if len(found) >= 15:
                log(f"  warning: {series['name']} {season} has 15+ events; "
                    "remove `listing = \"season\"` to fetch it round by round")
            for raw in found:
                add(raw, season)
            continue
        empty = 0
        last_known = max(
            (int(m.group(1)) for k, v in ctx.store.entries.items()
             if (m := re.match(rf"tsdb/{league}/{season}/(\d+)$", k)) and v["data"]),
            default=-1,
        )
        for rnd in range(0, MAX_ROUND + 1):
            key = f"tsdb/{league}/{season}/{rnd}"
            known = key in ctx.store.entries
            found = ctx.store.get(
                key, client, f"{THESPORTSDB}/eventsround.php?id={league}&r={rnd}&s={season}",
                lambda b: json.loads(b).get("events") or [],
                lambda d: [e.get("dateEvent") or "" for e in d],
            )
            if found is None and not known:
                # Unknown is not the same as empty; once the API refuses
                # everything, stop past the last round we know about.
                if client.blocked and rnd >= last_known:
                    break
                continue
            if found:
                empty = 0
                for raw in found:
                    add(raw, season)
            else:
                empty += 1
                if rnd > 0 and empty >= MAX_EMPTY_ROUNDS:
                    break

    strip = series.get("strip") or []
    if isinstance(strip, str):
        strip = [strip]
    rounds: list[Round] = []
    for rkey, raws in by_key.items():
        seen: set[str] = set()
        sessions = []
        for raw in raws:
            eid = str(raw.get("idEvent") or "")
            name = (raw.get("strEvent") or "").strip()
            if not name or eid in seen:
                continue
            seen.add(eid)
            start = parse_time(raw.get("strTimestamp") or "") or parse_time(
                raw.get("dateEvent") or ""
            )
            if start is None:
                continue
            sessions.append(Session(
                name=name, label=name, start=start,
                venue=(raw.get("strVenue") or "").strip(), uid=f"tsdb-{eid}@racecal",
            ))
        if not sessions:
            continue
        cleaned = []
        for s in sessions:
            n = s.name
            for p in strip:
                n = re.sub(p, "", n, flags=re.I).strip()
            cleaned.append(n or s.name)
        rounds.append(Round(key=rkey, name=max(set(cleaned), key=cleaned.count),
                            sessions=sessions))
    return rounds


SOURCES = {
    "sportstimes": source_sportstimes,
    "fiawec": source_fiawec,
    "thesportsdb": source_thesportsdb,
}


# --------------------------------------------------------------------------
# Building events
# --------------------------------------------------------------------------


def keep(session: Session, series: dict) -> bool:
    inc = series.get("include")
    if inc and not any(re.search(p, session.label, re.I) for p in inc):
        return False
    exc = series.get("exclude") or []
    return not any(re.search(p, session.label, re.I) for p in exc)


def apply_duration(session: Session, rnd: Round, series: dict) -> None:
    for rule in series.get("durations", []):
        if re.search(rule["pattern"], session.label, re.I):
            session.minutes = int(rule["minutes"])
            return
    if session.minutes:
        return  # the source gave a length
    if re.match(r"^Race\b", session.label, re.I):
        for pattern, hours in (series.get("race_hours") or {}).items():
            if re.search(pattern, rnd.name, re.I):
                session.minutes = int(float(hours) * 60)
                return
        hours = HOURS_RACE.search(rnd.name)
        if hours:
            session.minutes = int(hours.group(1)) * 60
            return
    session.minutes = int(series.get("duration", 60))


def fmt_session(s: Session, tz: ZoneInfo) -> str:
    if s.timed:
        return f"{s.start.astimezone(tz):%a %d %b %H:%M} {s.label}"
    return f"{s.start:%a %d %b} {s.label}"


def grouped_event(rnd: Round, series: dict, tz: ZoneInfo) -> Event:
    sessions = rnd.sessions
    first, last = sessions[0], sessions[-1]
    title = series.get("title")
    name = title.format(season=rnd.key.split("-")[0]) if title else rnd.name
    tzname = "" if tz.key == "UTC" else f" ({tz.key})"
    desc = f"{len(sessions)} sessions{tzname}:\n" + "\n".join(
        fmt_session(s, tz) for s in sessions
    )
    if first.timed:
        end = max((s.end for s in sessions if s.timed), default=first.end)
    else:
        last_day = last.start.date() if last.timed else last.start
        end = last_day + timedelta(days=1)
    return Event(
        uid=f"racecal-{series['name'].lower()}-{rnd.key}@racecal",
        summary=f"{series['name']} · {name}",
        start=first.start,
        end=end,
        location=next((s.venue for s in sessions if s.venue), ""),
        description=desc,
    )


def single_events(rnd: Round, series: dict) -> list[Event]:
    return [
        Event(
            uid=s.uid,
            summary=f"{series['name']} · {s.name}",
            start=s.start,
            end=s.end if s.timed else s.start + timedelta(days=1),
            location=s.venue,
        )
        for s in rnd.sessions
    ]


def manual_events(path: Path) -> list[Event]:
    if not path.exists():
        return []
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    out = []
    for item in data.get("event", []):
        start_d: date = item["start"]
        end_d: date = item.get("end", start_d)
        slug = re.sub(r"[^a-z0-9]+", "-", f"{item['series']}-{item['name']}".lower())
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
        out.append(Event(
            uid=f"manual-{slug}-{start_d.isoformat()}@racecal",
            summary=f"{item['series']} · {item['name']}",
            start=start,
            end=end,
            location=item.get("location", ""),
            description=item.get("notes", ""),
        ))
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
    if len(line.encode("utf-8")) <= 75:
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
        lines += ["TRANSP:TRANSPARENT", "END:VEVENT"]
    lines.append("END:VCALENDAR")
    return "\r\n".join(fold(line) for line in lines) + "\r\n"


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


def build(root: Path, store: Store, verbose: bool) -> tuple[list[Event], dict[str, int]]:
    """Return all events and the event count per series."""
    config = tomllib.loads((root / "config.toml").read_text(encoding="utf-8"))
    settings = config.get("settings", {})
    tz = ZoneInfo(settings.get("timezone", "UTC"))
    this_year = datetime.now(timezone.utc).year
    deadline = time.monotonic() + float(settings.get("time_budget_minutes", 20)) * 60
    ctx = Context(
        store=store,
        clients={
            "sportstimes": Client("GitHub", 0.2, deadline),
            "fiawec": Client("fiawec.com", 1.0, deadline),
            "thesportsdb": Client("TheSportsDB", 2.5, deadline),
        },
        seasons=[this_year + off for off in settings.get("seasons", [0, 1])],
    )

    events: list[Event] = []
    counts: dict[str, int] = {}
    for series in config.get("series", []):
        source = SOURCES.get(series.get("source", ""))
        if source is None:
            log(f"warning: {series.get('name')}: unknown source {series.get('source')!r}")
            continue
        if verbose:
            log(f"{series['name']} ({series['source']}):")
        before = len(events)
        for rnd in source(series, ctx):
            rnd.sessions = [s for s in rnd.sessions if keep(s, series)]
            if not rnd.sessions:
                continue
            for s in rnd.sessions:
                apply_duration(s, rnd, series)
            rnd.sessions.sort(key=lambda s: start_key(s.start))
            if series.get("group"):
                events.append(grouped_event(rnd, series, tz))
            else:
                events += single_events(rnd, series)
        counts[series["name"]] = len(events) - before
        if verbose:
            log(f"  {counts[series['name']]} events")

    manual = manual_events(root / "events.toml")
    events += manual
    if verbose:
        log(f"manual: {len(manual)} events")
        log(
            f"store: {store.fetched} fetched, {store.reused} reused, "
            f"{store.stale} stale copies used, {store.missing} unavailable; requests: "
            + ", ".join(f"{c.name} {c.requests}" for c in ctx.clients.values())
        )
    events.sort(key=lambda e: (start_key(e.start), e.summary))
    return events, counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", type=Path, default=Path(__file__).parent)
    parser.add_argument("--output", type=Path, help="override settings.output")
    parser.add_argument(
        "--store", type=Path,
        help="file keeping fetched data between runs (default .cache/store.json)",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    root = args.root.resolve()
    config = tomllib.loads((root / "config.toml").read_text(encoding="utf-8"))
    settings = config.get("settings", {})
    output = args.output or root / settings.get("output", "public/racecal.ics")

    store = Store(args.store or root / ".cache" / "store.json")
    events, counts = build(root, store, args.verbose)
    store.save()
    if not any(counts.values()):
        # Don't replace a good published calendar with an empty one.
        log("error: no events from any series; not writing the calendar")
        return 1
    for series, n in counts.items():
        if n == 0:
            log(f"warning: {series} produced no events")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render(events, settings.get("calendar_name", "Racecal")),
                      encoding="utf-8", newline="")
    log(f"wrote {len(events)} events to {output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

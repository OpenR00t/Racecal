# Racecal

One subscribable calendar for the motorsport you actually follow.

Racecal pulls session schedules for several championships, filters them,
merges in hand-maintained events that have no data feed, and publishes a
single `.ics` file. Subscribe to that one URL in Google Calendar, Apple
Calendar or Outlook and every race shows up next to everything else, with
your own reminders and no app notifications.

## What it covers

| Series | Source | Shown as |
|---|---|---|
| Formula 1 | [sportstimes/f1](https://github.com/sportstimes/f1) | one event per session |
| IndyCar | [sportstimes/f1](https://github.com/sportstimes/f1) | one event per session |
| WEC | [fiawec.com](https://www.fiawec.com) (official site) | one event per session |
| DTM | dtm.com's own API | one event per session, with real end times |
| WRC | the WRC+ app's data (next season from TheSportsDB until it's loaded) | **one event per rally**, from the first stage to the last |
| Dakar | [dakar.com](https://www.dakar.com/en/overall-route) official route (past editions from TheSportsDB) | **one event per edition**, all-day across the stage days |
| W2RC (other rounds) | [fia.com](https://www.fia.com) championship calendar | one all-day event per rally |
| SCORE Baja series | [score-raceinfo.com](https://score-raceinfo.com) | one all-day event per race week |
| King of the Hammers | [kingofthehammers.com](https://kingofthehammers.com) schedule | one event per race, with start and end times |

All times are written in UTC; calendar apps convert them to your local
time. Grouped events (WRC, Dakar) carry the full session list in their
description.

## Setup

1. Push this repository to GitHub.
2. In the repository settings, open **Pages** and set **Source** to
   **GitHub Actions**.
3. Open the **Actions** tab, select **Build and publish calendar** and click
   **Run workflow** (after that it rebuilds daily, and whenever the config
   changes).
4. Subscribe to
   `https://<your-github-user>.github.io/<repository>/racecal.ics`.
   - **Google Calendar:** Other calendars → **+** → **From URL**.
   - **Apple Calendar:** File → New Calendar Subscription.

Google Calendar refreshes subscribed calendars on its own schedule, usually
within a day, so schedule changes do not appear instantly.

## Customising

### Choosing series and sessions — `config.toml`

Each `[[series]]` block is one championship. Remove a block to drop the
series, or narrow it with regex filters on the session label (the part
after the event name, such as `Practice 1`, `Qualifying` or `Race`):

```toml
[[series]]
name = "F1"
source = "sportstimes"
key = "f1"
name_suffix = " Grand Prix"
group = false
include = ["Qualifying", "Sprint", "Race"]   # skip practice
```

Set `group = true` to collapse a round into a single event that starts at
the first session and ends at the last one. The header of `config.toml`
documents every option.

Set `settings.timezone` (for example `"Europe/London"`) to have the
session lists inside grouped events shown in that zone.

### Filling gaps by hand — `events.toml`

Add an `[[event]]` block with `series`, `name`, `start` and `end` dates
(inclusive). Add `start_time = "HH:MM"` (UTC) for a timed first day.

These only fill gaps: an event is skipped when a feed already has an event
for the same series name on overlapping dates. That makes it safe to enter
an announced date ahead of its source (for example a W2RC season before the
FIA posts its calendar page) and leave it there.

### Adding a series

- **sportstimes** also has F2 (`f2`), F3 (`f3`), Formula E (`fe`), MotoGP
  (`motogp`) and F1 Academy (`f1-academy`); set `key` to the folder name
  under `_db/` in that repository.
- **fia** reads any championship calendar on fia.com, such as
  `world-rally-championship` or `fia-world-endurance-championship`; set
  `championship` to the part of the calendar URL after `/events/`. It has
  event dates but no session times.
- **thesportsdb** covers many more championships; set `league` to the
  number in the league's page URL on [TheSportsDB](https://www.thesportsdb.com).
  Prefer another source where one exists (see below).

## Running locally

Requires Python 3.11 or newer and no extra packages.

```sh
python racecal.py --verbose   # writes public/racecal.ics
```

## Requests and rate limits

TheSportsDB's free API is shared by everyone using its public key and
throttles hard (on GitHub's runners, about 10 requests before a two-minute
pause), so it is only used as a fallback for seasons the main sources don't have
(next year's WRC, past Dakar editions). sportstimes
data is one file per season, and the WEC site is a page per race.

Racecal keeps everything it fetches in `.cache/store.json` (restored
between workflow runs with `actions/cache`) and only fetches it again when
the stored copy is stale:

- data for rounds that are over is never fetched again;
- rounds starting within two weeks, or under way, are refreshed every 12
  hours;
- rounds further out are refreshed about weekly, spread over several days.

A normal daily build makes a handful of requests. Rate-limit responses are
retried, honouring `Retry-After`, and after one the client slows down for
the rest of the run. Anything that still fails uses its last stored copy;
after repeated refusals, or once `time_budget_minutes` is used up, the build
finishes from stored data. Gaps are filled on the next run. If no series
produced any events, the build exits with an error so the published
calendar is left as it was.

The first build starts with an empty store and takes about 15 minutes,
almost all of it spent waiting on TheSportsDB.

## Notes and limits

- Sessions are only as current as their source. Future seasons often
  start as date-only placeholders and gain times closer to the event.
- A timestamp of exactly midnight (local to the source) is treated as
  "time not announced" and becomes an all-day event.
- Session end times are estimated where the source has none (`duration`,
  `durations` and `race_hours` in `config.toml`); endurance races named
  "N Hours of …" use N hours.
- WEC races are taken only from the races the season pages link. Pages for
  races dropped from the calendar stay online, still marked as scheduled
  with their old dates (Qatar and Bahrain 2026, for example).
- Sources sometimes keep listing a cancelled event. Hide it with `skip` in
  that series' `config.toml` block (regexes on "<year> <event name>"), and
  add a corrected placeholder to `events.toml` if needed.
- sportstimes publishes a season's file when the calendar is set, so next
  year's F1 and IndyCar appear later than other series.
- The DTM and WRC sources are the APIs behind dtm.com and the WRC+ app.
  They are not documented public APIs and could change without notice; the
  build keeps using stored data if they do.
- WRC stage times are published in local time. The time zone comes from
  each rally's country (`WRC_TIMEZONES` in `racecal.py`); add or override
  one in `config.toml` with `timezones = { "<regex on rally name>" =
  "<IANA zone>" }`. A rally with no known zone gets all-day stages and a
  warning in the build log.
- SCORE dates are read from the season list repeated on score-raceinfo.com's
  pages ("59th Baja 1000 – Nov. 9-15, 2026"); King of the Hammers times are
  read from its schedule page and are Pacific time. Both are page text
  rather than data feeds, so a site redesign can break them; the build then
  keeps using the last stored copy and logs a warning.

## License

Copyright (C) 2026 The Racecal contributors.

Licensed under the GNU Affero General Public License v3.0 or later. See
[LICENSE](LICENSE).

Schedule data comes from [sportstimes/f1](https://github.com/sportstimes/f1),
[fiawec.com](https://www.fiawec.com), [fia.com](https://www.fia.com),
[score-raceinfo.com](https://score-raceinfo.com),
[kingofthehammers.com](https://kingofthehammers.com) and
[TheSportsDB](https://www.thesportsdb.com) and remains subject to their terms.

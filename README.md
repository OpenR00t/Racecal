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
| Formula 1 | TheSportsDB | one event per session |
| IndyCar | TheSportsDB | one event per session |
| WEC | TheSportsDB | one event per session (prologue excluded) |
| DTM | TheSportsDB | one event per session |
| WRC | TheSportsDB | **one event per rally**, from the first stage to the last |
| Dakar | TheSportsDB | **one event per edition**, all-day across the stage days |
| W2RC (other rounds) | `events.toml` | one all-day event per rally |
| SCORE Baja series | `events.toml` | one all-day event per race week |
| King of the Hammers | `events.toml` | one all-day event for race week |

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
series, or narrow it with regex filters on the session name:

```toml
[[series]]
name = "F1"
league = 4370
group = false
include = ["Qualifying", "Grand Prix$", "Sprint$"]   # skip practice
```

Set `group = true` to collapse a round into a single event that starts at
the first session and ends at the last one. The header of `config.toml`
documents every option.

Set `settings.timezone` (for example `"Europe/London"`) to have the
session lists inside grouped events shown in that zone.

### Events without a feed — `events.toml`

Add an `[[event]]` block with `series`, `name`, `start` and `end` dates
(inclusive). Add `start_time = "HH:MM"` (UTC) for a timed first day.

### Finding a league id

League ids come from [TheSportsDB](https://www.thesportsdb.com). Search
for the series on the site; the number in the league page URL is the id.

## Running locally

Requires Python 3.11 or newer and no extra packages.

```sh
python racecal.py --verbose   # writes public/racecal.ics
```

## How it stays within the API's rate limit

The free API allows only a limited number of requests, and GitHub's shared
runners can hit that limit quickly. Racecal keeps every round it has
fetched in `.cache/rounds.json` (restored between workflow runs with
`actions/cache`) and only asks the API again when a stored round is stale:

- finished rounds are never fetched again;
- rounds starting within two weeks, or under way, are refreshed every 12
  hours;
- rounds further out, and the empty rounds past a season's end, are
  refreshed about weekly, spread over several days.

A normal daily build makes a handful of requests. Rate-limit responses are
retried with back-off (honouring `Retry-After`); a round that still fails
uses its last stored copy, and after repeated refusals, or once
`time_budget_minutes` is used up, the build finishes from stored data alone.
Rounds missing from a bad run are fetched on the next one. If no series
produced any events, the build exits with an error so the published
calendar is left as it was.

The first build starts with an empty store, so it makes around 200 requests
and may take several runs to fill in completely if the API pushes back.

## Notes and limits

- Session data is only as current and complete as TheSportsDB. Future
  seasons often start as date-only placeholders and gain session times
  closer to the event.
- A source timestamp of exactly midnight UTC is treated as "time unknown"
  and becomes an all-day event.
- Session end times are estimated (see `duration` and `long_sessions` in
  `config.toml`); endurance races named "N Hours of …" use N hours.

## License

Copyright (C) 2026 The Racecal contributors.

Licensed under the GNU Affero General Public License v3.0 or later. See
[LICENSE](LICENSE).

Schedule data comes from [TheSportsDB](https://www.thesportsdb.com) and is
subject to its terms.

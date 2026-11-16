# CW Hunter — Python terminal monitor

Find CW activators reported by **POTA** and **WWFF**, and check whether your
selected **Reverse Beacon Network (RBN) spotters** have heard them. Optional
**SOTA** monitoring uses a local JSON file. A separate **Friends** list can
alert you to callsigns heard by those spotters, independently of activations.

## Requirements

- Linux and **Python 3.10 or newer**.
- HTTPS access for live POTA, WWFF and RBN data.
- No third-party Python packages or POTA/WWFF account are required.
- An interactive terminal with Python's standard `curses` module enables the
  incremental display. Plain-text output works when curses is unavailable.
- Optional Friends Morse audio uses `paplay` or `aplay` from the Linux system.

The interface and CLI help use English. Names of parks, summits and areas retain
the spelling supplied by their source.

## Quick start

Open a terminal in the directory containing `cw_hunter.py` and `cw_monitor/`.
In the full application repository this is `python/`; in a standalone Python
repository the same files may be at its root. All commands below run from that
directory, without an additional `cd python`.

```bash
# Fictional data, without network access:
python3 cw_hunter.py --demo --qth IO91AA

# Live POTA/WWFF monitoring with your own locator and selected spotters:
python3 cw_hunter.py --qth KO02KF --spotters SP5GQ SQ5J

# One live check, then exit:
python3 cw_hunter.py --once --qth KO02KF

# Full list of options:
python3 cw_hunter.py --help
```

Keep the complete `cw_monitor/` directory alongside the launcher. Copying only
`cw_hunter.py` is insufficient. The alternative entry points
`python3 -m cw_monitor` and the compatibility launcher `python3 pota_alert.py`
run the same application.

Relative CSV and JSON paths are resolved from your current working directory.
Live source failures produce a nonzero exit status with `--once`.

## Terminal controls

| Key | Action |
| --- | --- |
| `r` | Refresh enabled sources and RBN, including confirmed active calls |
| `s` | Choose sorting for ACTIVE and HISTORY |
| `f` | Configure band, spotter and SNR filters |
| `Space` | Acknowledge currently highlighted new/updated entries |
| `b` | Enable targeted smaller RBN batches for the current run |
| `q` or `Ctrl+C` | Exit, including during a slow HTTPS request |

Network requests run in a background worker. Timers update locally and only
changed terminal cells are redrawn. Manual refresh bypasses local timing and
failure backoff while respecting a server's `Retry-After` response.

Redirected output, `TERM=dumb`, `--no-clear`, `--once` and `--demo` use plain
snapshots. For example:

```bash
python3 cw_hunter.py --no-clear --qth KO02KF
python3 cw_hunter.py --once --links
```

## Sources, activity and history

The default programs are **POTA and WWFF**. Their source feeds are normally
read every 60 seconds. `--interval` changes that source interval, with a minimum
of 60 seconds. Faster adaptive RBN reads reuse the program-source cache.

| Option | Default | Meaning |
| --- | --- | --- |
| `--pota-max-age` | 300 seconds | Maximum age of a POTA program spot |
| `--sota-max-age` | 600 seconds | Maximum age of a local SOTA program spot |
| `--wwff-max-age` | 900 seconds | Maximum age of a WWFF program spot |
| `--rbn-max-age` | 300 seconds | Fresh RBN limit; older startup matches are amber |
| `--history-max-age` | 1800 seconds | Retention after program activity ends |
| `--timeout` | 20 seconds | Timeout of each HTTPS request |
| `--tolerance` | 0.5 kHz | Allowed initial POTA/WWFF/SOTA versus RBN offset |

```bash
python3 cw_hunter.py --programs POTA
python3 cw_hunter.py --programs POTA WWFF --pota-max-age 300 --wwff-max-age 900
```

An activation identity is **program + callsign + band + reference**. A change
of band or reference creates another row; a frequency adjustment within a band
updates the existing row. Activator callsigns retain portable prefixes/suffixes.

A new activation needs a recent CW program spot and a matching RBN confirmation
from at least one selected spotter on the same band, within the tolerance.
Startup reads up to 100 CW reports, looking back 30 minutes (or the configured
RBN freshness limit if longer). Older matches appear silently with no NEW/audio
alert: their Last RBN clock is amber and details say RBN confirmation outdated.
Unconfirmed activations are checked again in later cycles. Once confirmed, the
original RBN reception is retained; program activity determines ACTIVE status.
The shared feed continues for known active stations and updates their readings.
The amber indication clears when any selected spotter has a newer report within
`--rbn-max-age`. With colours disabled, the textual note remains visible.
Repeated source data does not renew its timestamp. QRT, non-CW reports and
source expiry affect activity, while HISTORY preserves earlier observations.
Restored records await current source data before their activity is known.

The table shows **Last RBN** separately from **Last spot** (POTA/SOTA/WWFF).
Last RBN is the age of the newest original RBN report among the selected
spotters, never the time the data were downloaded. Re-fetching a report or
receiving a new program spot does not reset this clock. Missing RBN timestamps
(such as restored CSV readings) show `—` until a dated reception is available.

The default sort is latest RBN reception first, with newly detected entries given
priority in newest-first time sorts. Sorting changes each section independently.
Supported criteria are last RBN reception, first detection, dB, WPM, distance and
frequency, with ascending/descending directions.

## RBN packet and traffic statistics

The shared RBN stream is fetched for **1–5 selected spotters**. Its row limit
starts at 100 for the historical snapshot, then adapts through 10, 20, 50 and 100. Busy streams can shorten the RBN interval
from 60 to 30 and 15 seconds without increasing program-source polling.

```text
Refresh in 00:00:12 | RBN: 73/100 rows · every 15s
CW: last 12 / 15s · avg ~42.0/min
```

- **73/100 rows:** the final shared response contained 73 records, requested
  with a limit of 100. Intermediate limit-growth requests are not added up;
  later targeted queries do not replace the shared packet's numbers.
- **every 15s:** the current adaptive RBN polling interval.
- **last 12 / 15s:** twelve newly detected CW spot IDs in the latest observation
  window, before matching activators/Friends or applying display filters.
- **avg:** the last five complete windows, calculated as total new spots divided
  by total elapsed time and expressed per minute. Repeated pages do not count
  the same spots again; late-arriving spots can still be counted.

The first window uses `--rbn-max-age`; later windows cover elapsed time since
the previous shared read, capped at that age. A complete live window can supply
statistics while older startup history is still filling. **Collecting…** means
no complete sample is available. **(partial)** marks an incomplete window,
excluded from the average; **last avg** identifies a retained earlier average.

A full 100/100 response remains neutral. The high-traffic warning appears only
when a live gap is detected at the fastest cadence. **idle** retains the last
packet without claiming continued network polling; **waiting** does not invent
zero measurements. Statistics create no extra requests. Spotters remain visible
in the terminal header. The footer combines fields when they fit and wraps
longer telemetry without clipping. At fewer than 14 terminal rows, the compact
footer prioritizes space for activations. Plain snapshots include the statistics.

Press `b`, or use `--rbn-small-batches`, to allow targeted queries when needed.
They share an eight-request budget per cycle; an exceptionally busy stream can
still exceed coverage. The choice lasts for the current run.

## Filters, sorting and colours

```bash
# Global inclusive SNR range; either bound also works independently:
python3 cw_hunter.py --min-snr 10 --max-snr 30
python3 cw_hunter.py --min-snr off --max-snr 12

# Selected bands and a subset of the monitored spotters:
python3 cw_hunter.py --bands 20m 40m --spotters SP5GQ SQ5J --filter-spotters SQ5J

# Per-band rules replace both global bounds on that band:
python3 cw_hunter.py --min-snr 10 --max-snr 30 --band-snr 20m:18:off
python3 cw_hunter.py --band-snr 20m:global

# Show excluded confirmations without alerts, or reset band/spotter selection:
python3 cw_hunter.py --show-filtered
python3 cw_hunter.py --bands all --filter-spotters all

# Frequency ascending, or strongest signal first:
python3 cw_hunter.py --sort frequency --sort-order asc
python3 cw_hunter.py --sort db --sort-order desc
```

Available bands are **160m, 80m, 60m, 40m, 30m, 20m, 17m, 15m, 12m, 10m, 6m,
2m and 70cm**. A disabled per-band rule inherits the global SNR bounds; an
enabled rule replaces both, and can explicitly leave either or both bounds off.
SNR bounds are inclusive integers from -100 to 100 dB. At least one applicable
spotter must satisfy all enabled bounds. Higher RBN dB means higher reported SNR.

Sorting and filters are remembered across runs in
`~/.config/cw-activation-monitor/`, or under `$XDG_CONFIG_HOME` when set to an
absolute path. This compatibility directory keeps the previous product name.
Use explicit `off`/`all` options to reset saved choices.

Default program colours are POTA yellow, WWFF green and SOTA blue. SNR below
10 dB is red, 10–19 dB yellow and 20 dB or more green. WPM up to 15 is green,
16–24 yellow and above 24 red. Adjust colouring with:

```bash
python3 cw_hunter.py --snr-thresholds 10,20 --wpm-thresholds 15,24
python3 cw_hunter.py --color never
```

Colours are enabled automatically on supported interactive terminals;
`--color always` forces them. Colours do not change filtering or ordering.
Identical dB/WPM values across spotters are displayed once; different values
retain the selected spotter order. Ages use `HH:MM:SS`.

## Distance, azimuth and reference names

`--qth` accepts your Maidenhead locator or `latitude,longitude` coordinates.
`CW_ACTIVATION_QTH` can supply the default. Distance and initial azimuth use the
reference coordinates supplied by the source; they are estimates to the
park/summit/area location. Distances default to kilometres.

```bash
python3 cw_hunter.py --qth KO02KF --distance-unit km
python3 cw_hunter.py --qth 52.23,20.88 --distance-unit mi
python3 cw_hunter.py --reference-name-length 24
```

Names are otherwise shown in full when terminal space permits. Set the length
to zero to hide names. `--park-name-length` remains a compatibility alias.

Individual invalid records from POTA, WWFF, local SOTA feeds or RBN are skipped
without a main-screen warning. Validation and incomplete-RBN coverage checks
remain active. Counts are available with `--log-level debug` and an event log.
An entirely unreadable feed, network/source failure, malformed user CSV or
history write error remains visible.

## CSV logs and restoration

```bash
python3 cw_hunter.py --history-max-age 14400 --bell --log-file events.csv --summary-file daily.csv
```

The event log appends activation events. The independent daily summary keeps
one entry per UTC day, program, callsign, reference and band, including the
reference name/QTH, first/last source spots and last frequency. Use
`--log-level debug` with an event log for transport, adaptive-polling and rejected
match details. Log paths must be different files.

Restart with the same paths to restore available history. Event data restores
detailed state and retained readings; the daily summary supplies additional
identities, names and source-time information. Restored status is checked
against fresh program data. Neither CSV is a completed-QSO log.

Log writers use sidecar locks. Stop other instances before sharing their log
paths. Existing CSV schema upgrades retain backups in `.csv-backups/`.
Personal CSV files, backups and Friends state are excluded by `.gitignore`;
keep them when updating the source files.

## Friends

Android calls this list **Callsign watchlist**, for friends and stations you
want to hear. Its **Watchlist alerts** controls share one setting. Python keeps
the existing `--friends-*` options and the compatible `friends.csv` format.

Create a UTF-8 CSV, or export one from the Android application, using this header:

```csv
callsign,first_name,last_name,comment
G4YBU,Alex,Example,Club friend
```

```bash
python3 cw_hunter.py --friends-file friends.csv --bell
python3 cw_hunter.py --friends-file friends.csv --no-friends-alerts
```

The list supports up to 1000 distinct callsigns. Names and comments are optional
and limited to 128 characters each. Base-call matching also recognizes operating
variants such as `ON/G4YBU/P`, retaining the full received callsign for display.
Friends alerts use the selected spotters and applicable band/spotter/SNR filters.

With `--bell`, Friends audio sends **CQ CALLSIGN** in Morse, without a program
prefix, using `paplay` or `aplay`. Defaults are 20 WPM and 750 Hz, configurable
with `--morse-wpm` and `--morse-tone`. Other activation alerts use the terminal
bell. Without a supported Linux audio player, the terminal bell remains the
fallback. A terminal bell also depends on the terminal's sound settings.

A separate `friends.state.json` beside `friends.csv` prevents duplicate alerts
after restart. Override it with `--friends-state-file`. The state file is
independent of event and daily CSV logs. The CSV is read at startup; restart
the monitor after editing it.

## Optional local SOTA feed

The Python monitor imports SOTA from a local JSON file; it does not fetch a
SOTA API. Supply a file from a data source you are authorized to use:

```bash
python3 cw_hunter.py --sota-file /path/to/sota-spots.json
python3 cw_hunter.py --programs SOTA --sota-file /path/to/sota-spots.json
```

Supplying `--sota-file` adds SOTA to the default program selection. An explicit
`--programs` list takes priority. The importer accepts a normalized JSON array:

```json
[
  {
    "activator": "SP9ABC/P",
    "reference": "SP/BZ-001",
    "frequency_khz": 7032.0,
    "mode": "CW",
    "spot_time": 1794324000,
    "reference_name": "Example summit",
    "latitude": 49.57,
    "longitude": 19.53
  }
]
```

Replace `spot_time` with the actual report's Unix UTC timestamp; an old example
will correctly be excluded by the age limit. Names and coordinates are optional.
SOTAwatch-style local exports are also supported, with `frequency` in MHz.
The file is limited to 8 MB and re-read on source cycles. Updating it atomically
avoids partial reads; rereading a record does not renew its timestamp.

## Tests and distribution

Run the offline suite from the same directory as the launcher:

```bash
python3 -m unittest discover -s tests -v
```

The tests use fixtures and a local Linux pseudoterminal, without live service
requests. To update another computer, copy both launchers and the **entire**
`cw_monitor/` directory. Include `README.md`, `LICENSE` and `.gitignore`; the
`tests/` directory is useful for verification but not required at runtime.
Preserve your local logs, Friends CSV/state and user preferences separately.

In the development workspace, `python/` is the canonical source tree;
`cw-hunter-github/` is its standalone publication copy. Update the canonical
source first, then copy the changed source and test files to the publication
directory. Both copies use the same README, license and ignore rules.

## License

Released under the [MIT License](LICENSE).

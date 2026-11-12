# Architecture and review notes

All Python code, tests and personal CSV logs live in the lowercase `python/`
directory. The runtime uses Python 3.10+ and the standard library only.
Commands and CSV schemas remain compatible with the previous script.

## Module responsibilities

```text
python/
  pota_alert.py             CLI entry point
  cw_monitor/
    __main__.py            Alternative: python3 -m cw_monitor
    cli.py                 Arguments, validation, startup and exit codes
    config.py              Source URLs, bands and shared constants
    values.py              Scalar validation and elapsed-time formatting
    geo.py                 Locators, coordinates, distance and azimuth
    models.py              Activation, reception and lifecycle data types
    monitor.py             Identity, confirmation and ACTIVE/HISTORY state
    polling.py             Source scheduling and confirmation caching
    restore.py             Replay of saved event and daily records
    runtime.py             Plain output, network worker and terminal loop
    sources/
      http.py              Bounded HTTPS reads and Retry-After handling
      programs.py          POTA, WWFF and local SOTA parsing and matching
      rbn.py               RBN schema, stream cache, adaptive limits/pacing
    storage/
      files.py             Sidecar locks and atomic schema upgrades
      events.py            Append-only event log
      summary.py           Validated daily summaries and atomic replacement
      session.py           Writer ownership and cleanup
    ui/
      table.py             Column order, labels, values and comments
      colors.py            Colour policy, SNR/WPM thresholds and ANSI output
      layout.py            Cell widths, wrapping and column allocation
      views.py             Plain snapshots and styled terminal frames
      terminal.py          Curses colours and incremental cell updates
  tests/                   Offline unit and real-terminal regression tests
```

The dependency direction runs from configuration, scalar utilities and models
to sources/storage, then lifecycle/scheduling, then presentation and runtime.
Lifecycle state has no curses or column-layout dependency. Changed fields use
semantic identifiers, so moving a column cannot move its update highlight to
another value. The UI owns column-width history across frames and resizing.

## Review findings addressed

- **Maintainability:** the former large script is now an eight-line entry
  point. Modules separate parsing, I/O, state and presentation. Tests import
  their owning modules rather than relying on entry-script exports. Public
  CLI flags and log schemas are preserved.
- **CPU work:** a callsign/program band index replaces repeated full-history
  scans when detecting a new band. Polling indexes received spots by callsign
  once before matching pending activations. Duplicate source reports are
  reduced to the latest identity before lifecycle updates.
- **Terminal work:** idle frames are built once per second instead of five
  times per second. Network events, keyboard acknowledgements and resize
  trigger an immediate frame. Curses still writes only changed cells and
  polls keys every 200 ms. Networking stays off the terminal thread.
- **Log ownership:** both CSV outputs now hold exclusive process locks for
  their full lifetime, including migration and replay. Previously only the
  daily summary rejected a second writer. Constructor failures release locks.
- **Write failures:** shutdown closes both writers even if one close fails.
  Remaining write errors reach standard error and exit code 2. Daily atomic
  replacements preserve existing Unix file permissions.
- **Input reliability:** oversized HTTPS responses are rejected explicitly
  instead of parsing a truncated prefix. HTTP error responses are closed;
  nonfinite Retry-After values cannot poison scheduling. Boolean frequencies
  are rejected. Both CSV readers accept a UTF-8 BOM.
- **Column consistency:** both views place dB and WPM immediately after RBN
  kHz, before Reference. Receiver-value ordering, numerical alignment, wrapping
  and changed-field highlights are preserved.
- **Optional visual encoding:** Program, SNR and WPM cells carry semantic
  colour spans independently of band colours. Curses applies them after
  layout; plain output adds ANSI only after aligning cells. NEW/UPD override
  these colours. CLI thresholds are validated, receiver order and sorting
  stay unchanged, and `--color never` disables every colour category.

## Preserved behaviour and practical limits

Identity remains program + callsign + band + reference. Source updates retain
First seen and cached confirmations; a new band/reference creates a separate
entry. Active older entries can remain in HISTORY. Restoration uses both
optional CSV files without inventing an RBN timestamp.

RBN request sizes still grow through 10/20/50/100 and use the smoothed traffic
rate and recent peaks. When 100 rows fail to cover a minute, RBN can poll at
30 or 15 seconds while program sources retain their regular interval. A
request slower than that interval can extend the actual cycle. Faster polling
does not recover rows already hidden behind the server's limit; incomplete
coverage is reported. RBN uses an internal website interface, whose schema is
validated but can change independently of this code. SOTA remains local-file
input only.

Known identities and the daily summary are indexed in memory for the run.
This deliberately preserves first detection and prevents repeated NEW alerts
after an entry disappears from the screen. Extremely long runs or large
multi-year CSV files can increase memory use; moving that index to a database
would be a separate change requiring retention and replay decisions.

## Validation

Run from `python/`:

```bash
python3 -m unittest discover -s tests -v
python3 pota_alert.py --demo --qth KO02KF
python3 -m cw_monitor --help
```

Tests use controlled data, temporary log directories and a real Linux pseudo
terminal. They cover lifecycle/replay, source errors, adaptive RBN coverage
and pacing, CSV atomicity/locking, column layout, colours, resize and quitting
during a slow request. No live API access is needed for this suite.

The current distribution archives include the whole runtime package. The
source archive also includes tests and CI configuration. Personal CSV files,
CSV backups, locks and bytecode are excluded.

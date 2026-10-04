# DoomSat science data system (SDS)

The flight software and mission control turn Doom into telemetry. This turns the telemetry into **versioned,
cataloged products**, the way a science data system does, in its three usual modes:

- **forward processing**: one Airflow run per finished episode, from the Yamcs archive;
- **near real time**: a live frame capture and a quicklook every minute;
- **reprocessing**: a campaign over every cataloged episode when an algorithm changes.

```
 Yamcs archive ──► sds_episode_watch ──► sds_forward (one run per episode) ──► L1 + L2 ──► catalog ──► sds_rollup (L3)
   (events, TM,      every minute          locate → L1 → path/summary/linkstats     │                     │
    commands)                                                      publish ◄─────────┘          Timeline + bucket
 FRAME_CHUNK ──► capture service ──► capture/ ──► sds_quicklook (every minute) ──► contact sheet + health
 (realtime only)   (scripts/sds.sh)
 catalog ──► sds_reprocess (by hand) ──► L1 rebuilt from the archive, checked ──► L2 at the current version
```

## The rule it lives by: products never flow back to the pilot

A path image is a map of a level, and charter 2.2 says **no map survives an attempt**. So nothing on the pilot
side (`honesty.PILOT_SIDE` in `research/honesty.py`, plus the modules the pilot and payload import that the list
omits) reads anything this system writes: not the product store, not the catalog, not the capture directory, not
the `doomsat-sds` bucket. The honesty suite cannot see `ground/sds`, so `tests/test_sds_guard.py` enforces it,
together with the rest of the boundary:

- products are drawn from telemetry only, and nothing here opens a WAD;
- nothing is written under `out/frames/`, which the pilot reads;
- it sends no commands.

Nothing here is part of a graded run. Fly demonstration episodes on the dev set (`WAD=freedoom1.wad`).

## Running it next to a flight

```bash
scripts/sds.sh setup                      # once: Airflow 3.3.2 in ~/doom/sds/venv (uv + the official constraints)

WAD=freedoom1.wad scripts/flight.sh start # the flight side, as usual
scripts/sds.sh start                      # Airflow (127.0.0.1:8080) + the capture service, from a stopped state
scripts/start_pilot.sh --system-one code --system-two none     # episodes, without a TypeSafe key

scripts/sds.sh status                     # processes, Airflow health, catalog counts, last minute of capture
scripts/sds.sh catalog episodes           # what has been processed
scripts/sds.sh stop
```

`scripts/sds.sh` never starts, stops or commands the flight side, and the flight side does not depend on it:
with the SDS stopped, a flight behaves exactly as before. Start them in either order. The watcher looks back six
hours, so episodes flown while the SDS was down are processed when it comes back, but their context (WAD, map,
pilot) is only known if the processes that flew them are still running; otherwise it is recorded as unknown.
Frames are another matter: they are never archived, so frames from a time the capture service was down are gone.

In a cloud VM that pauses when idle, every background process stops: run `scripts/flight.sh status` and
`scripts/sds.sh status`, and start whatever is down. Restarting the SDS can fail a task that was mid-flight
(it cannot report back while the API server restarts); clear it in the UI or let the next run catch up.

Airflow's UI and API are on http://127.0.0.1:8080 with no login (local only). The CLI with the right
environment is `scripts/sds.sh airflow ...`, e.g. `scripts/sds.sh airflow dags list-runs sds_forward`.

## DAGs

| DAG | Schedule | What it does |
|---|---|---|
| `sds_episode_watch` | every minute | Finds closed episodes in the archive and triggers one `sds_forward` run each (`run_id fwd__<episode id>`, never twice) |
| `sds_forward` | per episode | locate → L1 → `l2_path`, `l2_summary`, `l2_linkstats` → publish (Timeline item, bucket) |
| `sds_rollup` | on the L2-summary asset | L3 rollup across the current summaries, copied to the bucket |
| `sds_quicklook` | every minute | Contact sheet of the latest captured frames plus link and payload health |
| `sds_reprocess` | by hand | Reprocessing campaign: rebuild L1 from the archive, check it reproduces, build every L2 lacking its current version, republish |

The capture service is not a DAG: it has to run continuously, so `scripts/sds.sh` runs it beside Airflow.

### Why a poll and not an AssetWatcher

Airflow 3 can schedule on external events (`AssetWatcher`), but it folds events that arrive together into one run,
and the point here is one run per episode. The watcher polls the archive every minute instead and triggers runs
with the episode in their id, so a run is never duplicated however often an episode is seen.

## Episodes

There is no "episode over" message. F´ derives `EpisodeStarted`, `PlayerDied`, `LevelFinished` from edges in the
payload's status, and they have gaps: a flight's first `EpisodeStarted` fires before Yamcs is listening, an
episode ended by `RESET_GAME` has no end event, and `EPISODE` restarts at 1 with the payload. So:

- an episode is **closed** by `PlayerDied` / `LevelFinished`, or by the next `EpisodeStarted` (outcome `reset`,
  or `interrupted` when the payload restarted);
- its **window** is the contiguous run of archived `EPISODE == n` samples ending at the closing event (a silence of
  over 30 s or `TIC` going backwards breaks a run);
- its **id** is the closing time and the number: `20261004T004611Z-e0001`.

## Products

| Level | Type | File | From |
|---|---|---|---|
| L1 | `l1_episode` | JSON | the archive: the ten science channels as one lossless time-ordered table, link housekeeping, the commands in the window (put on the TM time axis by the measured clock offset), the Doom events |
| L2 | `l2_path` | PNG | L1: the path walked, from telemetry only |
| L2 | `l2_summary` | JSON | L1: duration, kills, cells explored, health, outcome, distance |
| L2 | `l2_linkstats` | JSON | L1: status-stream completeness, frames and chunks sent, uplink completeness, link uptime, clock offset |
| L3 | `l3_rollup` | JSON | every current `l2_summary` |
| QL | `ql_health`, `ql_contact_sheet` | JSON, PNG | the capture directory and realtime values (kept a day) |

Products are pure functions of their inputs, serialised one way, with no processing time inside, so the same
inputs and algorithm version give the same bytes and the same sha256. Algorithm versions are in
`doomsat_sds/products.py` (`ALGORITHMS`); a change to what a builder produces is a version bump there.

Files live under `$DOOMSAT_HOME/sds/products/` (`episodes/<id>/<type>-<version>.<ext>`, `l3/`, `quicklook/`).
Nothing generated goes in the repo.

### The catalog

`$DOOMSAT_HOME/sds/catalog.sqlite`: `episodes` (window, outcome, WAD, map, skill, seed, pilot mode, repo commit,
dev/test set), `products` (id `<episode>/<type>@<version>`, level, version, path, sha256, input references,
Airflow run id, code commit, `is_current`), and `findings` (a checksum that moved).

```bash
scripts/sds.sh catalog summary
scripts/sds.sh catalog episodes
scripts/sds.sh catalog products --type l2_path
scripts/sds.sh catalog show 20261004T004611Z-e0001/l2_summary@1.0.0
scripts/sds.sh catalog findings
scripts/sds.sh catalog sql "SELECT outcome, COUNT(*) FROM episodes GROUP BY 1"
```

### Reprocessing (Phase C)

Bump a version in `ALGORITHMS`, then `scripts/sds.sh airflow dags trigger sds_reprocess` (optionally
`-c '{"episodes": [...], "types": ["l2_path"]}'`). Each episode's L1 is rebuilt from the archive with the
cataloged window and context, and must reproduce its cataloged checksum; a mismatch is a finding and the rebuilt
file is kept beside the original, never over it. The new L2 versions become current, the old ones stay.
`l2_path` 2.0.0 is the first such campaign: it breaks the line at teleports and colours the path by time.

## Publishing (Phase E)

Every episode gets one Yamcs Timeline item (source `rdb`) spanning its window, with properties linking to its
current products, and its L2 products are copied to the bucket `doomsat-sds`. Items have deterministic ids, so
republishing after a campaign updates them in place. The latest quicklook and rollup go to fixed object names
(`quicklook/latest.png`, `l3/rollup-current.json`), which the dashboard can show through its `/api` proxy, e.g.
`<img src="/api/storage/buckets/doomsat-sds/objects/quicklook/latest.png">`.

```bash
curl -s "http://localhost:8090/api/timeline/fprime-project/items?source=rdb&details=true"
curl -s "http://localhost:8090/api/storage/buckets/doomsat-sds/objects?prefix=episodes/"
```

L1 records are not copied: at about 3 KB per second of play they would fill the bucket's default 100 MB after a
few hundred episodes.

Every product written also appends an OpenLineage `RunEvent` to `$DOOMSAT_HOME/sds/lineage/openlineage.jsonl`
(inputs, output, checksum, Airflow run, code commit), so run history and lineage can be read without opening
Airflow's database.

## Tests

```bash
ground/.venv/bin/python -m unittest discover -s tests                                   # the repo's suite, SDS included
ground/.venv/bin/python -m unittest discover -s ground/sds/tests -p "test_sds_*.py"     # the SDS alone
```

No network, no game, no Airflow: Yamcs is replaced by a recorded window of the real archive
(`tests/data/two_deaths.json.gz`, two episodes). Tests that need Pillow skip themselves in `ground/.venv`; run
them with `~/doom/sds/venv/bin/python` to include those.

## Layout

| Path | What |
|---|---|
| `dags/` | thin DAG files: each task calls one function in the package |
| `doomsat_sds/` | the product code: `archive` (Yamcs reads, recorded or live), `episodes`, `products`, `png`, `catalog`, `context`, `pipeline`, `frames` and `capture` (Phase B), `quicklook`, `publish` (Phase E), `lineage`, `store`, `config` |
| `tests/` | unit tests and the recorded archive window |
| `../../scripts/sds.sh` | setup, start, stop, status |

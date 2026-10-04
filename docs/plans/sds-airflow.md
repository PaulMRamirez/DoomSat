# Task: a science data system for DoomSat on Apache Airflow

You are working in a fork of `Devonance/DoomSat`: Doom runs as a spacecraft payload behind F Prime, CCSDS framing, Yamcs, and a dashboard. Read `CLAUDE.md`, `README.md`, and section 2 of `docs/CHARTER.md` first.

## Goal

DoomSat has flight software and mission control but no data system: analysis is done by hand with `tools/run_report.py`, `tools/replay.py`, and `tools/compare_runs.py`. Build the missing piece as Airflow pipelines that turn what comes down the link into versioned, cataloged products, in the three modes a real science data system runs: near real time, forward processing, and reprocessing.

## Branch and first actions

1. Create `feature/sds-airflow` from `main`. Do not base it on `feature/wad-uplink` or `feature/cfdp-spike`, and do not depend on anything in them. Save this whole message as `docs/plans/sds-airflow.md`, commit it, and keep a "Progress and findings" section at the bottom up to date.
2. Start the DoomSat install in the background (`scripts/flight.sh setup`, then `scripts/setup_ground.sh python`; skip Open MCT). If a download or clone is refused, stop and tell me which URL failed.
3. While it builds, read the code and write your plan into the plan file. Use subagents where the work separates (Airflow stack, product code, Yamcs integration).
4. Prove the baseline first: `python3 tools/doctor.py`, `scripts/flight.sh start`, `scripts/flight.sh check` after about 40 seconds, and `ground/.venv/bin/python -m unittest discover -s tests`.

No TypeSafe key is available. Generate episodes with the code autopilot: `scripts/start_pilot.sh --system-one code --system-two none`.

## What is already there

Read at commit `fe2666b` (24 September 2026), not run. Confirm each before relying on it.

- Yamcs instance `fprime-project`, processor `realtime`, HTTP API on port 8090. It records packets, parameters, events, and command history, and has `ReplayServer` and `TimelineService` enabled.
- Doom telemetry lives under `/DoomSat_DoomSat/DoomSat/doom/` and includes `POS_X`, `POS_Y`, `KILLS`, `HEALTH`, `TIC`, `EPISODE`, `DEAD`, `LEVEL_DONE`, `EXPLORED_CELLS`, and `LEVEL`. Episode boundaries are events: `EpisodeStarted`, `PlayerDied`, `LevelFinished`, `LevelStarted`.
- Frames go down as `FRAME_CHUNK` telemetry records and are reassembled on the ground by sequence and index. The flight launcher marks that channel realtime-only, so do not expect frames in the parameter archive.
- File downlink already works end to end in configuration: `FprimeFilePacketService` reassembles files into the `fprimeFilesIn` bucket and mirrors them to `$DOOMSAT_HOME/run/downlink`. The command `FileHandling/fileDownlink/SendFile` is in the dictionary. Nothing in DoomSat currently produces a file to send; the `DataProducts` subtopology is wired but the Doom component writes no products.
- The pilot logs to `out/decisions.jsonl`, and `out/` is git-ignored.
- Ports in use: 8070 dashboard, 8090 Yamcs, 9000 Open MCT, 5000 F Prime GDS. Airflow's default 8080 is free.

## Design

Treat this as the intended shape; if the code suggests better, say why in the plan file and proceed.

**Layout.** Everything new lives in `ground/sds/`: `dags/`, a small importable package for product code, tests, and a README. One script, `scripts/sds.sh` with `setup`, `start`, `stop`, and `status`, mirrors how `scripts/flight.sh` works. Airflow's home, its database, and all products live under `$DOOMSAT_HOME/sds/`, never in the repo.

**Airflow.** Use the current stable Airflow 3 release (3.3.x when I checked on 3 October 2026), pinned to an exact version in its own virtual environment with the official constraints file for that version. Use uv to create the environment and install. A single-machine setup with the local executor and SQLite is enough.

**Products.** Keep product logic in plain Python functions that take inputs and write outputs, with the DAGs as thin wrappers, so the logic is unit-testable without Airflow. Every product carries an id, a level, an algorithm version, its input references, a checksum, and the episode's context (WAD, map, skill, pilot mode, repo commit) in a catalog you can query. A SQLite file or JSON lines is fine.

- Level 1: the episode record. Time-ordered samples of the telemetry above between an episode's start and end events, plus the commands sent in that window.
- Level 2: derived products per episode. The path walked as an image, a summary (duration, kills, cells explored, outcome), and link statistics.
- Level 3: a rollup across episodes.

**Phase A, forward processing from the archive.** No changes to any existing file. A DAG run per finished episode, triggered when an end-of-episode event appears in Yamcs, reads the archive with `yamcs-client` and writes Level 1 and Level 2 products and catalog entries. The Level 3 rollup updates after each run.

**Phase B, near real time.** A small capture service subscribes to `FRAME_CHUNK`, reassembles JPEGs to disk, and counts complete and incomplete frames. A DAG on a short schedule builds a quicklook: a contact sheet of recent frames plus link and payload health.

**Phase C, reprocessing.** Change a Level 2 algorithm, bump its version, and run a backfill DAG over every cataloged episode from the archive. Old versions stay in the catalog; the newest is marked current.

**Phase D, the file seam.** This is the only phase that edits existing code; keep it last and in its own commits so it can be dropped. The payload writes a small per-episode record file when an episode ends. A DAG task requests it with `SendFile` through Yamcs, a file sensor watches the mirror directory, and the ingested file is checked against the Level 1 product built from the archive. Report any disagreement between the two as a finding. Stretch: have ViZDoom record the episode as a demo file, downlink that, and re-render it on the ground at full quality in a process that is separate from the pilot.

**Phase E, publishing back.** Write one Yamcs Timeline item per episode linking to its products, and copy products to a Yamcs bucket so the dashboard and Open MCT can reach them. Stretch: emit run and lineage records in OpenLineage format to a file, so an operations agent can read run history and lineage later without touching Airflow's database.

## Constraints

1. **Products never flow back to the pilot.** A path map of a level is level knowledge, and charter section 2.2 says no map survives an attempt. Nothing listed as `PILOT_SIDE` in `research/honesty.py` may read from the product store, the catalog, or the capture directory. State this in the README.
2. **The honesty suite keeps passing**, including `python research/honesty.py --canary`. Phase D touches `payload/doom_payload.py`, which the suite reads; one of its checks inspects the body of `new_episode`, so put the record writing where it does not disturb that.
3. **Do not edit** `research/`, `docs/CHARTER.md`, `knowledge/`, or the decision logic in `ground/pilot.py`. Nothing here is part of a graded run.
4. **Default behaviour is unchanged.** With the data system stopped, a flight behaves exactly as before. Outside Phase D, existing files change only to add documentation and the one script.
5. **Read-only by default.** The only command the pipelines may send is `SendFile` in Phase D, in a task whose name makes that obvious.
6. **Nothing generated goes in git**: no products, frames, Airflow database, or logs.
7. Many files use CRLF line endings; keep each file's existing endings and style. Give any standalone tool PEP 723 inline metadata so it runs with `uv run`. Keys and tokens never go in git, logs, or commit messages.

## Acceptance criteria

1. Existing unit tests, the honesty suite, and the canary pass. New unit tests cover the product functions and catalog with recorded sample inputs, and run with no network, no game, and no Airflow.
2. Phase A: fly at least three episodes with the code autopilot; show three forward runs that succeeded, the catalog rows, one Level 2 summary as text, and one path image saved under `out/`.
3. Phase B: show a quicklook contact sheet and the complete and incomplete frame counts for a five-minute window.
4. Phase C: show the catalog before and after a reprocessing campaign, with both versions present for each episode.
5. Phase D: show one record file requested, downlinked, ingested, and compared, with the comparison result.
6. Phase E: show the Timeline items through the Yamcs API.
7. `scripts/sds.sh start` brings the data system up from a stopped state, and the README explains how to run it next to a flight.

If time runs short, finish Phases A through C properly before starting D or E.

## Working in this environment

The VM pauses when idle and background processes die, so check `scripts/flight.sh status` and `scripts/sds.sh status` before each run and restart what is down. I cannot open `localhost` from my phone, so show evidence as text and saved images.

## Finish

Push `feature/sds-airflow` to this fork and open a draft pull request inside the fork only, targeting `main`. Do not open anything against `Devonance/DoomSat`. End with a report that separates what you verified by running from what you only read, lists each deviation from this spec, and says which phases are complete.

---

# Plan

Written on 4 October 2026 after a full install and a baseline run, from six parallel reads of the code and
the live stack (Yamcs, F´, payload, honesty suite, Airflow 3.3.2, yamcs-client 2.1.0). Where the code
disagreed with the brief, the code won; each case is listed under "What the code says" and again under
"Deviations".

## What the code says (facts the design rests on)

1. **Episode boundaries are edges, not messages.** The payload sends only STATUS and FRAME records. F´
   derives `EpisodeStarted`, `PlayerDied`, `LevelFinished` and `LevelStarted` from changes in the STATUS
   fields (`Doom.cpp:376-395`), and the `fprime-yamcs-events` sidecar republishes them as Yamcs events with
   `source=FPrimeEventProcessor`, `event_type=DoomSat.doom.<Name>` and the arguments in `extra` as strings.
2. **Episode 1 of every flight has no start event.** F´ emits it about 5 s before Yamcs listens, so it is
   lost, along with the first seconds of telemetry. An episode ended by `RESET_GAME` (the pilot's 180 s
   level budget) has no end event. `EPISODE` restarts at 1 whenever the payload restarts, so the number is
   not an identity. `LEVEL` counts levels started; it is not a map name. `TIC` restarts each episode.
3. **Frames are not archived anywhere.** `--yamcs-realtime-only-channels` marks the packets do-not-archive,
   so `FRAME_CHUNK` is in neither the tm table nor the parameter archive, and no replay recovers it. Map PNGs
   share the channel (`seq & 0x80000000`). Phase B's live capture is the only frame source.
4. **Archive reads are fresh.** The runtime parameter archive uses the back-filler (every ~10 minutes), but
   a read whose window runs past the archive's end is completed by an automatic replay, so a read made
   seconds after an episode ends is complete. Each read spawns a replay processor, so reads are batched.
5. **TM time runs about 0.95 s ahead of wall clock** (the preprocessor's leap-second offset is 38, not 37),
   while command history is stamped with wall clock. Commands are placed on the TM time axis using the
   offset measured from the samples themselves (generation minus reception).
6. **The downlink mirror is not `$DOOMSAT_HOME/run/downlink`.** The yaml says
   `${FPRIME_DOWNLINK_DIR:...}`, which Yamcs resolves from Java system properties, not the environment, so
   files land in `/tmp/runner/fprime-downlink`. The mirror also cannot create subdirectories.
7. **`SendFile` accepts at most 39 characters per path on board** (`FW_CMD_STRING_MAX_SIZE = 40`), although
   the dictionary says 100. It reports OK even when the file cannot be opened; success is the `FileSent`
   event. The F´ binary's working directory is `build-artifacts/<OS>/DoomSat/bin`, not the project.
8. **The Timeline write calls in yamcs-client 2.1.0 do not work against Yamcs 5.12.8** (PUT-only, mismatched
   field numbers). Items are created with a REST `POST /api/timeline/{instance}/items`; posting a known UUID
   again overwrites, so a `uuid5` per episode makes publishing idempotent.
9. **Buckets** hold at most 1000 objects / 100 MB by default, take object names matching `[ \w\s\-./]+`,
   and must not be deleted on 5.12.8 (a deleted bucket becomes a ghost). The pilot wipes `doomframes` at
   every start, so the SDS uses its own bucket.
10. **Airflow 3.3.2** is the current stable release. It installs with uv against
    `constraints-3.3.2/constraints-3.11.txt` and brings `providers-standard` (FileSensor,
    TriggerDagRunOperator). Four processes: api-server, scheduler, dag-processor, triggerer.
    LocalExecutor on SQLite is supported (WAL). `AssetWatcher` events are batched into one run, so they
    cannot give "one run per episode"; explicit runs with `run_id=<episode id>` can.
11. **The honesty suite scans a fixed file list** (`honesty.PILOT_SIDE`), so it cannot see `ground/sds/`
    and does not enforce "products never flow back". The SDS adds its own guard test. The pilot already
    reads `out/frames/latest_map.png`, so no SDS output goes anywhere near `out/frames/`.
12. **Context is not in telemetry.** WAD, map, skill and seed exist only in the payload's arguments and in
    `payload.log` ("episode N started on MAP"). Pilot mode exists only in the pilot's arguments.

## Design

**Layout.** `ground/sds/` holds `dags/` (thin DAG files), `doomsat_sds/` (the product package: plain Python,
no Airflow imports), `tests/` (unittest, with recorded Yamcs responses under `tests/data/`), and
`README.md`. `scripts/sds.sh setup|start|stop|status` mirrors `scripts/flight.sh`. Everything generated lives
under `$DOOMSAT_HOME/sds/`: `venv/`, `airflow/` (home, SQLite DB, logs), `catalog.sqlite`, `products/`,
`capture/`, `logs/`, `run/` (pid files), `lineage/`. The package name is `doomsat_sds` rather than `sds`
because the test suite puts `ground/` on `sys.path`, where a bare `sds` would resolve to the directory.

**Airflow.** Airflow 3.3.2 in `$DOOMSAT_HOME/sds/venv`, created with uv against the official constraints,
plus yamcs-client 2.1.0 (the version `ground/.venv` uses) and Pillow (constrained). LocalExecutor, SQLite,
parallelism 4, API server on 127.0.0.1:8080 in all-admins mode (local only, so no password to keep),
examples off, new DAGs unpaused, DAG folder `ground/sds/dags`, the package on `PYTHONPATH`. The four
Airflow processes and the capture service are started detached with `setsid`, like the flight side, with
pid files so `stop` and `status` are exact. No SDS command line contains `pilot.py`, `runner.py`,
`doom_payload.py` or `vizdoom` (preflight kills those) or the flight side's `pkill` patterns.

**Episode identity and window.** An episode is closed by a `PlayerDied` or `LevelFinished` event, or by an
`EpisodeStarted(n+1)` that follows `EpisodeStarted(n)` with no end event between (outcome `reset`). Its id is
the closing event's TM time plus the number: `20261004T001512Z-e0003`. That is stable, sortable, unique
across payload restarts, and legal as an Airflow run id and a Yamcs object name. The L1 window is the
contiguous run of archived `EPISODE == n` samples that contains the closing event, so a lost start event (episode 1)
does not matter.

**Products** are pure functions of their inputs, serialized deterministically (sorted keys, fixed
separators, no processing timestamps inside), so the same inputs and algorithm version give the same bytes
and checksum. Processing times go in the catalog, not the file.

- **L1 `l1_episode` (JSON):** the episode window, the ten science channels (`POS_X POS_Y KILLS HEALTH TIC
  EPISODE DEAD LEVEL_DONE EXPLORED_CELLS LEVEL`) as one time-ordered table (one row per distinct TM time,
  null where a channel did not update at that time, so it is lossless), the link housekeeping channels
  (`FRAMES_SENT CHUNKS_SENT CMDS_RECEIVED PAYLOAD_LINK`), the commands in the window (placed on the TM axis
  by the measured clock offset), and the F´ events in the window.
- **L2** (each derived from L1 alone, so L2 can be rebuilt from any L1):
  - `l2_path` (PNG): the walked path, start and end markers, drawn with a stdlib PNG writer (no WAD, no
    background, telemetry only).
  - `l2_summary` (JSON): duration, ticks, kills, cells explored, final health, outcome, distance walked.
  - `l2_linkstats` (JSON): status-stream completeness from `TIC` gaps, frames and chunks sent, commands sent
    versus `CMDS_RECEIVED` (uplink completeness), `PAYLOAD_LINK` uptime, TM latency.
- **L3 `l3_rollup` (JSON):** totals and distributions across the current L2 summaries, grouped by WAD/map.
- **QL `ql_quicklook` (PNG + JSON):** contact sheet of the latest captured frames with link and payload health.

**Catalog** (`catalog.sqlite`): `episodes` (id, number, window, outcome, closing event, WAD, map, skill,
pilot mode, repo commit, level set dev/test from `research/levels.yaml` read-only, context source) and
`products` (id `<episode>/<type>@<version>`, level, type, algorithm version, path, sha256, size, input
references, Airflow run id, created time, `current`). A small CLI (`python -m doomsat_sds.catalog`) prints
it. Context is captured once at forward time (from the running payload's and pilot's arguments, `payload.log`
and `git rev-parse HEAD`) and reused by reprocessing, because it describes the flight rather than the
algorithm.

**DAGs.**
- `sds_episode_watch` runs every minute. It reads the end and start events over a look-back window, works out
  the closed episodes not yet cataloged, and fans out one `sds_forward` run per episode with
  `TriggerDagRunOperator(...).expand_kwargs`, using `run_id=fwd__<episode id>`, `logical_date=None` and
  `skip_when_already_exists=True`, so a run is never duplicated. Polling every minute was chosen over an
  `AssetWatcher` because watcher events are batched (fact 10) and its trigger state does not survive a
  restart. This is the "trigger when an end-of-episode event appears".
- `sds_forward` (one run per episode): locate window → build L1 → build the three L2 products → register →
  publish (Phase E). The summary task updates an Airflow Asset that schedules `sds_rollup` (L3) after every
  run; batching is what an L3 wants.
- `sds_quicklook` runs every minute from the capture directory and Yamcs realtime values.
- `sds_reprocess` (manual): maps over every cataloged episode, re-reads the archive, rebuilds L1, checks its
  checksum against the cataloged L1 (a reproducibility finding if it differs), builds every L2 type that lacks
  the current algorithm version, and registers them as current. Old versions stay.
- Phase D: `sds_record_watch` and `sds_record` (in their own file and commits).

**Capture service** (`doomsat_sds/capture.py`, PEP 723 header, run by `sds.sh` from the SDS venv):
subscribes to `FRAME_CHUNK`, reassembles by `seq`/`index`, writes JPEGs (and the payload's map PNGs
separately) under `$DOOMSAT_HOME/sds/capture/`, and keeps per-minute counts: complete, incomplete (not all
chunks within 3 s, checked by a timer and not only on arrival), missing (whole-`seq` gaps), duplicates,
bytes. It writes nothing to Yamcs, so it cannot collide with the pilot's `/DoomGround` parameters. It
resubscribes when the stream goes quiet, because yamcs-client does not reconnect.

**Phase D, the file seam** (the only edits to existing code):
- A new `payload/episode_record.py` keeps a small per-episode accumulator: status count, first and last tic,
  decimated path, final values, and context (WAD, map, skill, seed). It writes
  `$DOOMSAT_HOME/run/rec/e<NNNN>.json` atomically.
- Hooks in `Payload.run()` only. The record is written before the final STATUS of an ending episode, so the
  file exists before the ground sees the event; an episode change seen by the tracker closes it as `reset`.
- `new_episode` is not touched. The recorder is created with `getattr(args, "records", "off")`, so the bench
  (which builds the payload from its own Namespace and never calls `run()`) is unaffected.
- Off by default; on with `RECORDS=on scripts/flight.sh start` (one more pass-through in
  `wsl_run_flight.sh`).
- The yaml mirror path is fixed to read the environment.
- The DAG sends `SendFile` with paths of 39 characters or fewer: absolute if it fits, otherwise relative to the
  binary's directory. A deferrable FileSensor watches the mirror, then the DAG ingests the file and compares
  it with L1. Disagreements are reported as findings.

**Phase E.** One Timeline item per episode (`uuid5`, REST POST, properties linking to products). Products
are copied to bucket `doomsat-sds` as `episodes/<id>/<type>-<version>.<ext>` plus fixed names for the latest
quicklook and rollup, within the 1000-object cap. Stretch: OpenLineage RunEvents written by the product layer
to `$DOOMSAT_HOME/sds/lineage/openlineage.jsonl`.

**Honesty.**
- No pilot-side file may reference the SDS (package, `$DOOMSAT_HOME/sds`, the bucket, `out/sds`). A new
  test enforces this over `honesty.PILOT_SIDE` plus the pilot-process modules it omits.
- Path images are drawn from telemetry alone; nothing opens a WAD.
- Demonstration flights use the dev set (`WAD=freedoom1.wad`), not the locked test level.

**Tests.** `ground/sds/tests/` with recorded Yamcs responses, no network, game or Airflow. A shim
`tests/test_sds.py` loads them, so the repo's one test command covers them.

## Deviations from the brief (and why)

1. The mirror directory is not where the brief says (fact 6). Phase D fixes the yaml in its own commit.
2. "Between an episode's start and end events" becomes "the contiguous run of `EPISODE == n` samples that
   contains the closing event", because start events are lost and resets have no end event (fact 2).
   Reset-ended episodes are cataloged too, with outcome `reset`.
3. Frames cannot come "from the archive" for reprocessing (fact 3). Only the capture service sees them.
4. The F´ binary's working directory is `bin/`, and `SendFile` paths are limited to 39 characters (fact 7).
5. Timeline items are created through REST rather than yamcs-client (fact 8).
6. One file outside `ground/sds/` and `scripts/sds.sh` is new: `tests/test_sds.py`, a loader so that
   `python -m unittest discover -s tests` also runs the SDS tests.
7. The demonstration flights use `WAD=freedoom1.wad` (dev set) rather than the default test level.

## Order of work

Baseline → plan → Airflow stack → product package and tests → Phase A (fly 3+ episodes) → B → C → E → D
(last, own commits) → review → push and draft PR in the fork.


# Progress and findings

- 2026-10-03: branch `feature/sds-airflow` cut from `main` at `fe2666b`. Install started
  (`scripts/flight.sh setup`, then `scripts/setup_ground.sh python`).
- 2026-10-04: install finished (about 3 minutes; nothing refused, including deb.debian.org for doom1.wad).
  Baseline verified by running:
  - doctor clean except Open MCT, which was skipped as asked;
  - `play.py --check` passes;
  - 369 unit tests pass; honesty 8/8 and canary 17/17 pass;
  - `flight.sh check` shows FRAMES_SENT rising and PAYLOAD_LINK True.

  Findings on the way:
  - `scripts/flight.sh` is committed without the executable bit (mode 100644), so it has to be run as
    `bash scripts/flight.sh`. Left alone, since it is an existing file.
  - fprime-bootstrap installed fprime-gds 4.4.0 where it expected 4.3.0 (a warning only).
  - `scripts/wsl_check.sh` polls TARGET_KIND and ROUTE_BEARING, which no longer exist (404).
  - The repo's `ground/yamcs/mdb/fprime.xtce.xml` is stale; the runtime MDB is regenerated from the
    dictionary on every start.
  - `tools/serve_dashboard.py` serves the whole repo root, `.env` included, on 127.0.0.1:8070.
  - A research probe sent one `CMD_NO_OP` to the live stack while mapping the command API.
- 2026-10-04, Airflow stack: `scripts/sds.sh setup` installs Airflow 3.3.2 with uv against
  `constraints-3.3.2/constraints-3.11.txt` in 15 s, plus yamcs-client 2.1.0 and Pillow 12.3.0, which are not in
  the constraints and are pinned. `start` brings up api-server, scheduler, dag-processor, triggerer and the
  capture service; all report healthy about 10 s later.
- Phase A, verified by running: the watcher triggered one `sds_forward` run per closed episode. Six runs
  succeeded, each with all six tasks, and the L3 rollup was rebuilt by asset trigger after each.
  - A flight's episode 1 shares `EPISODE == 1` with the previous flight's idle episode 1. The silence between
    them was 26 s, under the 30 s threshold; the TIC-going-backwards rule is what split them.
  - Findings:
    - The status stream reached the archive only 91.8% complete in the first episode: 191 of 2332 statuses were
      lost as 6-tic gaps.
    - The archive starts about 20 s into a flight's first episode.
    - With `--system-two none` the pilot never applies its 180 s level budget (the reset sits inside the
      System Two check), so a code-autopilot episode can run indefinitely. One ran for 18 minutes until I
      restarted the payload, and was closed correctly as `interrupted`.
- Phase B, verified: in the window 01:05–01:09 the capture service counted 2432 complete frames, 0 incomplete,
  0 missing and 57 maps, against 2488 images sent on board.
  - The payload restart at 01:05 was counted as a sequence restart, not a gap.
  - The one incomplete frame of the first subscription was a frame already half-sent when it subscribed;
    those are now counted as `cut`.
- Phase C, verified: `l2_path` went from 1.0.0 to 2.0.0 (breaks at teleports, colour by time, kill markers).
  - `sds_reprocess` rebuilt L1 from the archive for all 5 episodes, and every one reproduced its cataloged
    sha256 bit for bit. The ParameterArchive had been back-filled in between, but the replay-based reader
    returns the same values.
  - It built 5 `l2_path@2.0.0` products; the v1 ones stay in the catalog, not current. Every episode was
    republished, so the Timeline items link to v2.
- Phase E, verified:
  - One Timeline item per episode, created through REST (`POST /api/timeline/.../items` with a uuid5 id); a
    second publish overwrites rather than duplicates.
  - Products are copied to bucket `doomsat-sds`, served with their content types.
  - OpenLineage RunEvents are written to `lineage/openlineage.jsonl`.
- Phase D, verified:
  - With `RECORDS=on`, the payload wrote `run/rec/1-4247.json` when the episode died. `sds_record` sent
    `SendFile`, the file arrived in `fprimeFilesIn` and in the mirror `run/downlink`, which only works since
    the yaml fix (`downlinkMirror=/root/doom/run/downlink` in yamcs.log). The deferrable FileSensor fired, the
    record was ingested as `l0_record` and compared.
  - 15 of 16 checks agree exactly, including last tic, kills, cells, final health, final position, and 89 path
    positions at 0.0 units. The one disagreement is a finding: the record's first tic is 4, the archive's 712.
    Of the statuses sent while the archive was listening, 95.8% arrived.
  - On the way, the record watcher first asked for records of episodes flown before records were on. Each got
    a `FileOpenError` (found from the events, since `SendFile` itself answered OK) and a `record_unavailable`
    finding. Requests are now limited to episodes closed after record requests were first switched on.

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

_To be written while the install runs._

# Progress and findings

- 2026-10-03: branch `feature/sds-airflow` cut from `main` at `fe2666b`. Install started
  (`scripts/flight.sh setup`, then `scripts/setup_ground.sh python`).

# Task: in-flight WAD uplink for DoomSat, Stage 1 (native F Prime file transfer)

You are working in a fork of `Devonance/DoomSat`: Doom runs as a spacecraft payload behind F Prime flight software, CCSDS framing, Yamcs, and a dashboard. Read `CLAUDE.md`, `README.md`, and section 2 of `docs/CHARTER.md` before anything else.

## Goal

An operator can send a new level to the running spacecraft and switch the game to it without restarting anything. The WAD file goes from a Yamcs bucket, up the real command link as F Prime file packets, onto the flight filesystem; a new `LOAD_WAD` command then tells the payload to restart the game on it. Today the WAD is fixed at launch by `--wad`.

## First actions

1. Create branch `feature/wad-uplink`. Save this whole message verbatim as `docs/plans/wad-uplink-stage1.md` and commit it, so the spec survives context compaction. Keep a short "Progress and findings" section at the bottom of that file up to date as you work.
2. Start the install in the background, because the F Prime build takes 5 to 10 minutes: `scripts/flight.sh setup` then `scripts/setup_ground.sh python`. Skip the Open MCT build. If any download or clone is refused by the network or GitHub proxy, stop and tell me exactly which URL failed; do not work around it.
3. While it builds, read the code and write your plan into the plan file. Use subagents where it helps (flight component, payload, ground and tests are separable).
4. Prove the baseline before changing anything: `python3 tools/doctor.py`, the headless Doom check (`payload/play.py --check`), `scripts/flight.sh start` then `scripts/flight.sh check` after about 40 seconds, and the unit tests with `ground/.venv/bin/python -m unittest discover -s tests`. Record the results in the plan file. If the baseline does not work here, fixing that comes first.

No TypeSafe key is available. Fly with the code rules: `scripts/play.sh --autopilot` or `scripts/start_pilot.sh --system-one code --system-two none`.

## What is already there

I read these at commit `fe2666b` (24 September 2026). I did not build or run the stack, so treat them as strong leads and confirm each one.

- `flight/DoomSat/Top/topology.fpp` imports the `FileHandling` and `DataProducts` subtopologies. The router's file port is wired to `FileHandling.fileUplink`, and `fileDownlink` is wired to the com queue.
- `ground/yamcs/etc/yamcs.fprime-project.yaml` loads `com.example.myproject.FprimeFilePacketService` from fprime-yamcs 0.2.1. It implements Yamcs's `FileTransferService`, so it shows up under `/api/filetransfer/fprime-project/services` and in the Yamcs web File Transfer page. Uplink reads an object from a bucket and sends Start, Data, End `Fw::FilePacket`s on APID 3, 512 bytes per chunk (`uplinkChunkSize`), with a 20 ms sleep per packet. There is no retransmission.
- The payload (`payload/doom_payload.py`) resolves `--wad` once in `__init__` via `_find_wad` and builds the game in `_make_game` with `set_doom_game_path`. `new_episode` already throws the world model away, as the charter requires.
- The Doom component (`flight/Components/Doom/`) forwards each command to the payload over a local TCP socket as a small record: `'D'`, kind byte, 16-bit length, body. Uplink kinds `0x10` to `0x15` are taken. Payload-to-flight kinds are `1` (status) and `2` (frame). Command opcodes `0x00` to `0x05`, event ids 0 to 11, and telemetry ids 0 to 84 are taken.
- The ViZDoom 1.3.0 wheel ships test WADs inside the payload venv, under `site-packages/vizdoom/`: `freedoom1.wad` and `freedoom2.wad` (28.8 MB each) and `scenarios/*.wad` from 2.7 KB (`basic.wad`) to 471 KB (`cig_with_unknown.wad`). The scenarios are PWADs meant to load over a Doom II compatible IWAD.

Expected uplink times at the current settings (about 25 KB/s): a scenario WAD takes seconds, `doom1.wad` (4.2 MB) about 3 minutes, `freedoom2.wad` about 19 minutes.

## Design

Treat this as the intended shape. If the code tells you a better one, say why in the plan file and proceed.

**Flight component.** Add `LOAD_WAD(iwad: string, pwad: string, map: string)` to `Doom.fpp` at opcode `0x06`. `pwad` empty means none. The operator states which file is the IWAD and which is the PWAD, so nothing has to open a file to find out. The handler follows the pattern of `RESET_GAME`: pack a record of a new kind (`0x16`) and send it to the payload. Add a payload-to-flight record of kind `3` for the result, and turn it into events `WadLoaded(name, map)` and `WadLoadFailed(name, reason)`, plus telemetry for the active WAD name and a count of loads. Prefer the new record kind over extending the status record, because `STATUS_LEN` in `Doom.cpp` and `STATUS_FMT` in the payload must match byte for byte.

**Payload.** On kind `0x16`: validate the names, close the game, rebuild it through `_make_game` with the new paths (`set_doom_game_path` for the IWAD, `set_doom_scenario_path` for a PWAD), reset carried loadout and the level counter, and call `new_episode`. If ViZDoom fails to start on the new file, restore the previous WAD and report failure. Report the outcome with a kind `3` record.

**Where files land.** One uplink directory, created by the run script (for example `$DOOMSAT_HOME/wads/uplink`). `LOAD_WAD` accepts basenames only: no path separators, no `..`, must end in `.wad`, and must resolve inside the uplink directory or the installed `wads` directory. Decide how to avoid loading a file while it is still arriving (upload to a temporary name and rename with a FileManager command, or check for the FileUplink completion event) and document the choice.

**Ground.** Add `tools/wad_uplink_demo.py`: put a WAD in a Yamcs bucket, start the upload through the file transfer service, wait for completion, issue `LOAD_WAD`, and confirm the switch from telemetry and events. Use `yamcs-client` from `ground/.venv`. Add a small panel to `ground/dashboard/index.html` showing the active WAD with a control that issues `LOAD_WAD` through the existing proxy (see the `issue()` function). Add the WAD name to `scripts/wsl_check.sh`. An Open MCT display is a stretch goal only if you can build and verify it.

## Constraints

1. **The honesty suite must keep passing**, including `python research/honesty.py --canary`. Pilot-side code (the files listed as `PILOT_SIDE` in `research/honesty.py`) must never open a WAD. `LOAD_WAD` only hands paths to ViZDoom; use `os.path` for existence checks. Map names in pilot-side code are checked by a regex, and a line that passes one through as a launch parameter needs the launch context the suite expects (see `LAUNCH_CONTEXT`).
2. **Do not edit** `research/`, `docs/CHARTER.md`, `knowledge/`, or the decision logic in `ground/pilot.py`. If a change there seems necessary, stop and explain.
3. **Uplinked-WAD flights are demonstrations, never scored.** Do not run the bench or any graded flight on an uplinked WAD. The dev set is frozen to Freedoom Phase 1. Put a suggested charter note in the pull request description instead of editing the charter.
4. **Default behaviour is unchanged.** With no `LOAD_WAD` commanded, a flight launches and behaves exactly as before.
5. **Never commit a WAD.** Tests copy fixtures from the installed ViZDoom package or generate them at test time. Unit tests must still run with no network and no game.
6. **Small diffs.** Many files use CRLF line endings; keep each file's existing endings and style. `scripts/wsl_sync.sh` copies an explicit list of flight files, so add any new flight file to it. Check whether the committed `ground/yamcs/mdb/fprime.xtce.xml` needs regenerating after the dictionary changes.
7. **Tooling.** Keep the repo's own venv scripts. Give any new standalone tool PEP 723 inline metadata so it also runs with `uv run`.
8. Keys and tokens never go in git, logs, or commit messages.

## Acceptance criteria

1. Unit tests, the honesty suite, and the canary all pass. New unit tests cover the record encoding and decoding and the name validation.
2. End to end with the stack running under the code autopilot: `tools/wad_uplink_demo.py` uplinks `scenarios/basic.wad`, loads it over `freedoom2.wad`, and you show the `WadLoaded` event text, the WAD telemetry value, an incremented `EPISODE`, `FRAMES_SENT` still rising, and one frame saved to `out/` from after the switch.
3. An IWAD swap works: start the flight on `freedoom1.wad`, uplink `doom1.wad` under a new name, load it on `E1M1`.
4. Each negative case produces `WadLoadFailed` with a reason and the game keeps running on the previous WAD: a truncated file, a name that does not exist, a path traversal attempt, a wrong extension, and a load requested during an unfinished uplink.
5. Commands still work during a multi-minute uplink. Measure `CONTROL` round-trip latency before and during the `doom1.wad` uplink and report both numbers.
6. Docs: a short "Uplink a new level" section in `README.md`, a note in `docs/ARCHITECTURE.md`, and the new command in the `CLAUDE.md` tables.

Optional if time allows: uplink `freedoom2.wad` (about 19 minutes) in the background while flying, and check Yamcs bucket size limits for a file that large.

## Assumptions to check early

- That the Yamcs file transfer API accepts an upload to a spacecraft path of your choosing with this service, and where FileUplink writes relative paths (the flight binary's working directory).
- That ViZDoom scenario PWADs run with this payload's buttons, automap, and status code. If they do not, generate a minimal one-room test map instead and say so.
- That closing and re-creating the `DoomGame` inside the running payload loop is safe.
- Which FileManager commands exist at F Prime v4.3.0 for rename and checksum.

## Working in this environment

The VM pauses when idle and background processes die, so check `scripts/flight.sh status` before each test run and restart with `scripts/flight.sh start` when needed (Yamcs needs about 30 seconds). I cannot open `localhost` from my phone, so show evidence as text and saved images.

## Finish

Push `feature/wad-uplink` to this fork and open a draft pull request **inside the fork only**. Do not open a pull request against `Devonance/DoomSat`; I will do that. End with a report that separates what you verified by running from what you only read, lists every deviation from this spec, and notes anything worth raising upstream with fprime-yamcs or F Prime.

---

## Progress and findings

_Kept up to date by Claude during the session. Newest first within each section._

### Status

- [x] Branch `feature/wad-uplink` created from `fe2666b`, spec saved here.
- [x] Install (`scripts/flight.sh setup`, `scripts/setup_ground.sh python`), after one fix (below)
- [ ] Plan
- [x] Baseline (below)
- [ ] Implementation
- [ ] End to end
- [ ] Docs, push, draft PR

### Cloud environment (checked 2026-10-03)

Session runs in environment `doomsat-flight` (`env_016fDJXXdxUQxZyWfF7Gov2D`). Verified from inside the VM:
`BASH_DEFAULT_TIMEOUT_MS=600000`, `BASH_MAX_TIMEOUT_MS=1200000`; 4 vCPU, 15 GB RAM, 30 GB free disk;
Python 3.11.15 with `venv`; `build-essential` and `binutils` installed; `deb.debian.org`, PyPI, Ubuntu apt,
`raw.githubusercontent.com` and `nodejs.org` reachable; `git ls-remote` of `nasa/fprime` and
`fprime-community/fprime-yamcs` works through the proxy (the github.com web UI returns 403, which does not
affect git).

### Install (2026-10-03)

- `scripts/flight.sh`, `start_pilot.sh`, `wsl_*.sh` and others are committed without the executable bit (mode
  100644), so `scripts/flight.sh setup` fails with `Permission denied` in a fresh clone. Running them as
  `bash scripts/...` works; CLAUDE.md and the README call them directly. Left unchanged (mode change only;
  worth raising upstream).
- A transient PyPI read error (`IncompleteRead`, not a proxy refusal) killed `fprime-bootstrap`'s
  `pip install -r lib/fprime/requirements.txt` half way, and the bootstrap carried on, so CMake later failed
  in `lib/fprime/cmake/required.cmake:30` (no fpp tools). Fixed by re-running that pip install with
  `--retries 10`, then `scripts/flight.sh setup fprime`.
- **Bug fixed (`f4f7c93`)**: `scripts/wsl_sync.sh` was not re-runnable. `grep -q "/config" ... || { ...; } > .cm
  && mv .cm ...` parses as `(grep || ...) && mv`, so once the config line exists the `mv` runs on a file
  nothing wrote and `set -e` stops every later `flight.sh build` and any re-run of `flight.sh setup fprime`.
- No URL was refused. Freedoom's GitHub release asset downloaded fine; `deb.debian.org` served `doom1.wad`.
- Versions in `fprime-venv`: fprime-tools 4.3.0, fprime-fpp 3.3.0, fprime-gds 4.4.0 (pulled up by
  fprime-yamcs 0.2.1, which needs `>=4.4.0a3`), fprime-yamcs 0.2.1, fprime-xtce 0.2.0 (the PR branch),
  yamcs-client 1.13.0. First F´ build took about 3 minutes on 4 vCPUs.

### Baseline (2026-10-03, before any change)

| Check | Result |
|---|---|
| `python3 tools/doctor.py` | all ok except `Open MCT built` (skipped on purpose) and no TypeSafe key (expected) |
| `payload/play.py --check` | `vizdoom 1.3.0: doom1.wad E1M1 ran 100 tics, health 100; screenshot out/doom_check.png` |
| `scripts/flight.sh start`, `check` after 40 s | 4 processes up; `FRAMES_SENT` 203 and rising, `PAYLOAD_LINK True`, `UDP_TM_IN` 1717 frames; XTCE loaded 194 parameters and 55 commands |
| `ground/.venv/bin/python -m unittest discover -s tests` | 369 tests OK |
| `research/honesty.py --canary` | 17 checks, 0 failed |
| `start_pilot.sh --system-one code --system-two none --duration 40` | 160+ decisions, `CMDS_RECEIVED` 163, frames ok=325 lost=1 |

### Assumption probes (payload venv, no flight software)

- **Scenario PWADs run with this payload's setup.** `basic.wad` over `freedoom2.wad` on `MAP01`, with
  `set_doom_scenario_path`, ran 300 tics through `observe`, `action` and `pack_status` without error.
- **Closing and re-creating the `DoomGame` in one process is safe.** Four `close()` / `_make_game()` /
  `new_episode()` cycles alternating `freedoom1.wad` and `doom1.wad`: 0.4 to 0.6 s each, flies normally after.
- **A truncated WAD kills the process.** `doom1.wad` cut to 12 bytes, 100 KB or 2 MB: ViZDoom prints
  `Failed to allocate memory from system heap` and the Python process dies with SIGSEGV (exit 139). Nothing
  to catch. So the payload must never hand an unproven file to its own game: each candidate is first
  launched in a short-lived child process, and only a clean child result lets the swap happen.

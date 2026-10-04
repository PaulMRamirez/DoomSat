# COMMIT_WAD and LOAD_WAD, safe to send again

A command goes up as one unprotected TC frame (no COP-1: `docs/plans/cop1-investigation.md` on
`feature/cop1-investigation`), so on a lossy link the ground resends a command when no answer comes. The demo does
this with `--tries`, and the dashboard's `LOAD_WAD` form does it on `feature/dashboard-load-resend`. Often only the
answer was lost, and the command had already run. Before this change, a resend did one of two things:

- **`COMMIT_WAD`** found no `.part` and answered `WadUplinkFailed`. That is the same answer as for a file that never
  arrived. The demo then had to let `LOAD_WAD` decide, and `LOAD_WAD` would fly an older file of the same name if
  there was one.
- **`LOAD_WAD`** proved and switched the game a second time. Or, if it arrived during the first one's proof, it was
  refused (`WadLoadFailed`, "another LOAD_WAD is still being checked").

Now a repeat answers as the first did and changes nothing.

## What changed

- **`COMMIT_WAD` (`Doom.cpp`).**
  - If the `.part` is there, nothing changed: it is checked against the size and CFDP checksum and renamed, or
    refused.
  - If the `.part` is gone, the command now checks `NAME.wad` against the same size and checksum. If they match,
    the answer is `WadUplinked` and OK, the same answer the first commit gave, or the guard's commit at the FIN.
  - Anything else is still `WadUplinkFailed` with EXECUTION_ERROR: nothing under either name, or an older
    `NAME.wad` with other bytes.
  - A `.part` that is present but wrong is still refused, even when a matching `NAME.wad` exists, because the
    command names the `.part`.
- **`LOAD_WAD` (`payload/doom_payload.py`, `payload/wad_uplink.py`).** The payload resolves the request first, then
  compares it with `wad_uplink.load_key`: each file's name and identity (device, inode, size, modification time), and
  the map.
  - A request for what is flying now is answered with the new result `ALREADY`. The Doom component logs it as
    `WadLoaded`, the answer a load gives. `WAD_LOADS` does not move, the level is not restarted, and no new level
    is announced (`m_lastLevel` is kept).
  - A request equal to the one being proven gets no answer of its own. The proof's answer serves both.
  - Any other request during a proof is refused, as before.
  - A new upload of the same name is a new file, renamed over the old one, so it gets a new key and is loaded.
  - "What is flying now" includes the current map. If the level has moved on, a repeat flies the map it asks for
    again. `RESET_GAME` restarts the current level.
- **`tools/wad_uplink_demo.py`.**
  - A `WadUplinkFailed` now fails the run. A repeat that found the file in place would have said `WadUplinked`,
    so there is no longer an "unconfirmed, let `LOAD_WAD` decide" case.
  - A `WadLoaded` whose `WAD_LOADS` has not moved within 3 s is reported as "already flying".
- **No change to the dashboard.** Its resend accepts a fresh `WadLoaded` event as the answer, and that is what a
  repeat now gets.

## Tests

| Suite | What |
|---|---|
| `scripts/flight.sh ut`: Doom GTest suite, 9 tests, new (`flight/Components/Doom/test/ut`) | `COMMIT_WAD` against real files in a scratch `$DOOMSAT_HOME`: a commit; a repeat, twice; a repeat after the guard's `fileAnnounce`; an older `NAME.wad` with other bytes or another size; nothing on board; a wrong `.part` with and without a matching `NAME.wad`; names that are not bare. Also: `fileAnnounce` outside the uplink directory, and every WAD report result, including `ALREADY` keeping the level |
| `scripts/flight.sh ut`: the guard's 14 | Unchanged, still pass. Both suites run under the address, undefined-behaviour and leak sanitizers |
| `tests/test_payload_load_wad.py`, 7 tests, new | The payload's own `request_wad` and `poll_wad`, with the game and the probe child stood in for: a repeat after the switch, a repeat during the proof, another request during the proof, a new upload of the same name, the game as launched, another map, a refusal. They need the payload venv (`~/doom/payload-venv/bin/python -m unittest tests.test_payload_load_wad`) and are skipped in the ground venv |
| `tests/test_wad_uplink.py` | `load_key` (5 tests), the result codes against `Doom.cpp`, and the payload and flight branches pinned as text, so the ground venv checks them too |
| `tests/test_wad_uplink_demo.py` | The stand-in stack now does what the flight software and payload do. New tests: a repeat commit answers as the first did; an older file of the same name is never taken; a load of what is flying; a load resent after its answer was lost switches once |

## What ran live (4 October 2026, flight `2026_10_04-16_46_37`, behind `tools/lossy_relay.py`)

| Check | Result |
|---|---|
| `COMMIT_WAD` for `cig.wad`, already in place, with its size and checksum | `WadUplinked …/cig.wad`, `OpCodeCompleted` |
| The same with the checksum off by one | `WadUplinkFailed`, `OpCodeError … EXECUTION_ERROR` |
| A `.part` in place as class 1 leaves it. `COMMIT_WAD` with the downlink blacked out (TM 100 %), then sent again | The first renamed it on board, and its answer was lost. The resend got `WadUplinked …/cigrep.wad` and `OpCodeCompleted` |
| The same `LOAD_WAD` three times, 0.1 s apart (`cig.wad` over `freedoom2.wad`, MAP02) | The payload absorbed both repeats while proving. One `WadLoaded`, `WAD_LOADS` +1, `EPISODE` +1 |
| The same `LOAD_WAD` once more | `WadLoaded` after 0.1 s. `WAD_LOADS` and `EPISODE` did not move, and frames kept coming. Payload: "already flying it; nothing to do" |
| `doom1.wad` on E1M2, then E1M3 | Two switches, `WAD_LOADS` +1 each |
| `LOAD_WAD doom1.wad E1M1` with the downlink blacked out, then the downlink restored and the same command resent | Nothing heard for 25 s. `WAD_LOADS` had already moved by 1. The resend got `WadLoaded`, and `WAD_LOADS` was +1 in all |
| Demo, 5 % loss each way (seed 11), `cig.wad` as `cigr1`–`cigr3` | 3 of 3 OK, each committed on board at the FIN. Nothing needed resending |
| Demo, TC 5 % / TM 30 % (seed 23), `cigr4`–`cigr7` | 4 of 4 OK. In one, the `WadLoaded` was lost and the WAD channels stood in for it |
| Demo for what was already flying (`--pwad cigr7.wad --map map02`), same link | The first `LOAD_WAD` got no answer and was sent again. `WadLoaded` came, `WAD_LOADS` 12 → 12, `EPISODE` 13 → 13: "OK: already flying" |
| Demo with the downlink blacked out from full size for 12 s (`cigfc.wad`) | The guard committed it on board during the blackout, and its `WadUplinked` was lost. The demo sent `COMMIT_WAD` 5 s after the FIN and got `WadUplinked` from the repeat, then `LOAD_WAD` flew it. Before this change, that repeat answered `WadUplinkFailed` |

Seen along the way, not caused by this change: `cig.wad` over `freedoom2.wad` on MAP01 killed the probe child with
signal 11. The payload refused the load and kept flying what it had, which is what the probe is for.

## Limits

- **Older flight builds.** One built before this change answers a repeat `COMMIT_WAD` with `WadUplinkFailed`, and
  the demo now fails on that. That errs on the safe side. Commit by hand after checking what is on board.
- **What makes two files the same.** Identity is device, inode, size and modification time. A file rewritten in
  place with the same size and time would count as the same file. Nothing on board does that: an upload is renamed
  over the old file.
- **A no-op whose answer is lost.** Telemetry cannot stand in for it, because the count does not move. The ground
  asks again, and each repeat is answered. The dashboard gives up after three unanswered tries, as it would for
  any load.
- **Other commands.** `RESET_GAME`, `EXPLORE_HINT` and the relative turn in `CONTROL` are still not safe to
  repeat. Nothing resends them. Changing them would change the pilot's behaviour, which goes through the experiment
  ledger (`research/PROGRAM.md`).

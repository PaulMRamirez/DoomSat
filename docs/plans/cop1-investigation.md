# COP-1 for DoomSat's commands: investigation

The question: would COP-1 (CCSDS 232.1-B: FOP-1 on the ground, FARM-1 on board, the CLCW in every downlink frame)
give DoomSat's commands exactly-once, in-order delivery on a lossy link? This was a short investigation, not a
build. It was done on 4 October 2026 against the versions DoomSat pins: F´ v4.3.0, fprime-yamcs 0.2.1 and Yamcs
5.12.8. F´ v4.4.0 and `devel` (55f597d) were checked as well.

## Verdict

**No, not on this stack as it stands, and configuration alone cannot fix it.**

- Yamcs has a complete FOP-1, the ground half.
- F´ has no FARM-1, the on-board half, and never sends a CLCW. Its uplink frame detector also discards every
  COP-1 frame (Type-AD and Type-BC) a byte at a time, with no event.
- Turning `useCop1` on today would break commanding. Every sequence-controlled command would either wait forever
  in Yamcs or vanish on board without a trace.

COP-1 can be built inside DoomSat without forking F´, in about 6 to 9 days. **It is not worth building now:**

- The normal link loses nothing.
- The pilot's command stream already tolerates loss, and COP-1 would make that stream worse.
- The real exposure is two one-shot commands, `COMMIT_WAD` and `LOAD_WAD`. Fixing those directly takes about a
  day, and two of the four parts of that fix are already built (see the recommendation).

## Why it does not work today

### On board (F´)

All paths below are in the F´ tree (`$DOOMSAT_HOME/DoomSat/lib/fprime`).

| What | Where |
|---|---|
| The TC frame detector accepts only Type-BD frames. Its token is `(1 << BypassFlagOffset) \| SpacecraftId`, which is 0x2044 for DoomSat, compared with the whole first 16 bits of the frame. An AD frame starts 0x0044 and a BC frame 0x3044, so neither is detected | `Svc/FrameAccumulator/FrameDetector/CcsdsTcFrameDetector.hpp:45`, `CcsdsTcFrameDetector.cpp:40-43, 88-91` |
| A frame that is not detected is dropped silently. The accumulator moves on one byte with no event and no counter. Because the detector also checks the CRC, a corrupt frame never reaches `TcDeframer`, so its `InvalidCrc` event cannot fire on this path either | `Svc/FrameAccumulator/FrameAccumulator.cpp:177-184` (events only at 123, 153 and 164) |
| `TcDeframer` runs no FARM. Its comment reads "F Prime uses TC Type-BD frames for now, so the FARM checks are not ran". It checks the spacecraft id, length, virtual channel and CRC, and forwards the data field only. A duplicated frame runs twice: `ApidManager` logs `UnexpectedSequenceCount` and carries on | `Svc/Ccsds/TcDeframer/TcDeframer.cpp:66-91`, `Svc/Ccsds/ApidManager/ApidManager.cpp:24-35` |
| No CLCW goes down. `TmFramer` writes the Operational Control Field (OCF) flag as 0 (`globalVcId \|= 0x0`) and has no port that could receive a CLCW | `Svc/Ccsds/TmFramer/TmFramer.cpp:39-41`, `TmFramer.fpp` |
| v4.4.0 and `devel` are the same: identical detector header, the same comment, the OCF flag still 0 | `TmFramer.cpp:43` on both |

Three things make a DoomSat-built COP-1 more than a component:

- **The OCF needs 4 bytes and only 3 are spare.** A TM frame carries 1024 − 8 = 1016 bytes, and the framer's
  `static_assert` needs `FW_COM_BUFFER_MAX_SIZE` (1000) + 13 = 1013. CFDP's `MaxPduSize` (1001) is sized to the
  aggregation size exactly. So either `FW_COM_BUFFER_MAX_SIZE`, `MaxPduSize` and `OutgoingFileChunkSize` each
  shrink by 4, or the TM frame grows to 1028 bytes. Either way, CFDP Class 2 and the frame downlink must be
  proven again. This same budget has caused a `ComAggregator` assert once before (`docs/plans/cfdp-stage2-spike.md`).
- **A FARM cannot be spliced into the imported subtopology.** It has to sit between `frameAccumulator.dataOut`
  and `tcDeframer.dataIn`, because the header is gone after `TcDeframer`. `ComCcsds.TmTcFraming` already makes
  that connection, and FPP refuses a second one: `fpp-check` on `cop1/splice.fpp` fails with "too many ports
  connected here (found 2, max is 1)". `cop1/splice_ok.fpp`, which lists and wires the frame layer itself,
  passes. So DoomSat would import only `ComCcsds.SpacePacketFraming` and wire its own frame layer, which then
  diverges from the stock subtopology. v4.4.0 moves idle fill into `ComAggregator`, so that copy would have to
  be redone at the next upgrade.
- **The detector is chosen in the subtopology's phase code** for its `frameAccumulator` instance
  (`Svc/Subtopologies/ComCcsds/ComCcsds.fpp:49-65`), so a COP-1 detector needs a `frameAccumulator` instance of
  DoomSat's own, not a configuration value.

### On the ground (Yamcs and the bridge)

- **Yamcs 5.12.8's FOP-1 is complete** (`Cop1TcPacketHandler`): AD, BD and BC frames, the window, the timer, the
  transmission limit, and suspend and resume.
- **It needs a `clcwStream`** on the TM link, which `Cop1TcPacketHandler.java:180-184` reads with no default.
  `TmFrameDecoder` takes the OCF from the frame header's own flag bit, so DoomSat's three `ocfPresent: false`
  lines in `ground/yamcs/etc/yamcs.fprime-project.yaml` do nothing for TM/TC frames: only the AOS classes read
  that key. DoomSat has `useCop1: false` on both virtual channels and no `clcwStream`.
- **`bdAbsolutePriority` works only from the YAML.** Set through the API it has no effect, and `GET /config`
  reports the wrong value. Without it, a bypass command waits behind a full AD window.
- **Other FOP behaviour found in scratch runs** (scratch Yamcs instance; not re-run for this report):
  - An AD command sent to an uninitialised FOP queues with no NACK.
  - Initialising purges the queue without failing those commands in command history.
  - Commands queued during initialisation are not sent on sync until another command arrives.
  - `cop1TxLimit` counts all transmissions, not retransmissions.
- **The fprime-yamcs bridge would pass frames with the OCF flag set.** `tm_frame_aggregator.py:37-47` checks the
  version, the spacecraft id and the data-field status only.

## The command stream is not what COP-1 is for

The pilot's stream is not 10 Hz. 10 Hz is the frame downlink (`--fps 10`). The pilot decides at most every
0.25 s (`--period`, `ground/pilot.py`), about every 0.5 s in practice (`docs/ARCHITECTURE.md`). The dashboard
repeats `CONTROL` every 150 ms while a key is held. The stream already heals itself:

- each `INTENT` replaces the one before and has a 1.5 s time to live;
- the executor drops stale intents (`payload/executor.py`);
- the payload releases every control after 3 s without `CONTROL` (`UPLINK_TIMEOUT_S`, `payload/doom_payload.py`).

COP-1 is go-back-N: one lost frame holds back every later command until the CLCW reports the gap. For a stream
where only the newest command matters, that is the wrong trade. The scratch model `cop1/fop_model.py` transcribes
Yamcs's FOP-1 from its source and pairs it with an ideal FARM. It gives the following numbers (5 seeds × 1 hour
each, Yamcs defaults: window 10, T1 3 s, transmission limit 3).

| 4 commands a second, loss each way | Delivered | p99 latency | Uplink silent > 1.5 s | > 3 s (safe mode) | FOP suspends an hour |
|---|---|---|---|---|---|
| 1 %, BD (today) | 99.0 % | 20 ms | 0 | 0 | 0 |
| 1 %, COP-1 | 100 % | 0.32 s | 0.13 % | 0.08 % | 0 |
| 5 %, BD (today) | 95.1 % | 20 ms | 0 | 0 | 0 |
| 5 %, COP-1 | 99.3 % | 4.4 s | 4.3 % | 2.8 % | 1.6 |
| 10 %, BD (today) | 90.2 % | 20 ms | 0 | 0 | 0 |
| 10 %, COP-1 | 93.0 % | 5.3 s | 18 % | 13 % | 13.6 |

"Silent" is the share of the time with no command arriving on board. Every FOP suspend needs an operator or a tool
to resume. Tuning helps but never matches BD: at 5 % loss, T1 1 s with a window of 3 brings p99 to 0.77 s and
silence above 1.5 s to 0.74 % (`cop1/fop_variants.py`).

**This is a model, not a measurement.** It assumes a CLCW in every TM frame at 20 Hz, an ideal FARM, and one-way
delays of 20 ms up and 50 ms down. Real TM cadence depends on `ComQueue` traffic. Quote it as an indication.

## Where commands are actually lost

- **On the normal link, nowhere that was found.** None of the 30 flight logs on this VM has a
  `FrameDetectionValidFrameDropped`, `NoBufferAvailable` or `FrameDetectionSizeError` event. A burst of 4,745
  commands (flight `2026_10_03-23_26_33`) gave 4,745 `OpCodeDispatched`, 4,745 `OpCodeCompleted` and no
  `UnexpectedSequenceCount`. This shows no loss on loopback. It is not a measurement under loss.
- **Behind `tools/lossy_relay.py`**, which drops datagrams on purpose. There, the commands that matter are the
  one-shots:
  - A `COMMIT_WAD` resent after its answer was lost finds no `.part` and answers `WadUplinkFailed`, the same as
    for a file that never arrived (`flight/Components/Doom/Doom.cpp`, the missing-file branch of
    `COMMIT_WAD_cmdHandler`). The spike recorded this twice at 5 % loss.
  - `LOAD_WAD` is not safe to repeat. A duplicate that arrives while the first is being proven is refused
    ("another LOAD_WAD is still being checked"). One that arrives after the switch proves and switches again.
  - `RESET_GAME`, `EXPLORE_HINT` and `CONTROL`'s relative turn are not safe to repeat either. None of them is
    resent today.

## Options

| Option | What it takes | Effort | What it gives | What it costs |
|---|---|---|---|---|
| Today: BD frames, and the demo's `--tries` | Nothing | 0 | The stream is unchanged and loss-tolerant | A resend after a lost answer runs the command again |
| **Make the one-shot commands safe to repeat, and confirm each retry on 1 Hz telemetry (recommended)** | See below | About 1 day | For `COMMIT_WAD` and `LOAD_WAD`, a repeat has one effect and a clear answer. A retry confirmed on the `WAD_*` channels (sent every second) misses its confirmation with probability (1 − (1 − p)²)ᵏ: 0.093 % at 5 % loss and 3 tries | Nothing for `RESET_GAME`, `EXPLORE_HINT` or `CONTROL`'s relative turn. No ordering |
| A DoomSat router that downlinks a receipt for each command | Through the `ComCcsdsRouterConfig.fpp` override: pass the CCSDS sequence count as the dispatcher context, and downlink the last sequence, a 32-bit received window and the last result every tick. About 150 to 250 lines | 1 to 2 days more | An on-board receipt and completion status for every command, repeated in telemetry so it survives TM loss | Replaces the stock router. The Yamcs sequence count restarts at 0 with Yamcs, so any de-duplication needs a time window |
| Send each TC frame N times | A UDP repeater at the relay splice, about 20 lines. Never without the router's de-duplication | 0.5 day more | With de-duplication, a command is lost with probability pᴺ (0.25 % at 5 %, N = 2) | Without de-duplication, about 90 % of commands run twice at 5 % loss, because F´ forwards duplicates. N times the uplink frames, on the link CFDP shares |
| COP-1 on VC1 for one-shot commands only; the stream as BD; VC2 (CFDP) unchanged | Flight: a detector that takes AD and BC frames (0.5 day), a `Farm1` component (2 to 3 days), a `TmFramer` copy that writes the CLCW (0.5 to 1 day), a locally wired frame layer (0.5 to 1 day), the 4-byte frame budget, and CFDP proven again. Ground: `clcwStream`, `useCop1` and the timers in the YAML; `cop1Bypass` on every stream command (`ground/pilot.py`, the dashboard); tools that watch the COP-1 status and re-initialise after every F´ restart. A live proof behind the relay | 6 to 9 days | While the FOP is active, every AD command is delivered in order and exactly once at the link layer | About 1,000 to 1,300 lines. 4 bytes off every TM frame, and smaller CFDP PDUs. Diverges from the stock subtopology. Every F´ restart resets the FARM, and AD commands then queue silently until re-initialisation. The Yamcs behaviour listed above must be worked around |
| COP-1 for everything, the stream included | The same build, without `cop1Bypass` | 6 to 9 days | Every command in order, exactly once, while active | The model above: p99 4.4 s, silent 4.3 % of the time, 1.6 suspends an hour at 5 % loss. Not recommended |
| Switch `useCop1` on with no flight work | Two lines of YAML plus `clcwStream` | 0 | Nothing | Breaks commanding: AD frames are dropped on board with no event, the FOP resends and suspends, no command runs |

## Recommendation

Make the one-shot commands safe to repeat, and keep the stream on BD. About a day in all: half a day of code and
tests, and half a day for a `DOOMSAT_RELAY=1` run at 5 % loss.

1. **`COMMIT_WAD` answers OK when `NAME.wad` already matches.** When the `.part` is gone, check `NAME.wad`'s size
   and checksum against the arguments (`fileSum`, which the command already uses), and answer `WadUplinked` if
   they match. About 15 lines in `Doom.cpp`, plus a test. *Not done.*
2. **`LOAD_WAD` does nothing when the same WAD and map are already flying or being proven.** Compare with
   `wu.identity` in `request_wad` (`payload/doom_payload.py`). A no-op does not raise `WAD_LOADS`, so
   `tools/wad_uplink_demo.py`'s check must accept that. About 15 lines, plus tests. *Not done.*
3. **The dashboard resends `LOAD_WAD`.** *Done* on `feature/dashboard-load-resend` (6ee396f3): up to 3 tries,
   each confirmed by `WadLoaded` / `WadLoadFailed` or the 1 Hz `WAD_*` channels within 25 s. Until step 2
   lands, a resend could still switch the game twice, but only if the event and 25 s of the `WAD_*` channels
   were all lost.
4. **The guard commits uploads on board.** *Built* on `feature/cfdp-guard` (`docs/plans/cfdp-guard.md`): a Class
   2 upload is committed when the receiver's own FIN says it is complete, with no `COMMIT_WAD` from the ground.
   That takes the ambiguous retry out of the Class 2 path. Step 1 still matters for Class 1 and for a commit by
   hand.

Keep COP-1 as a documented option for the day every one-shot command needs exactly-once, in-order delivery. Even
then, use it on VC1 for one-shot commands only, and keep the stream on BD.

Making `CONTROL`'s turn absolute, or adding conditions to `RESET_GAME`, would change the pilot's behaviour. That
goes through the experiment ledger (`research/PROGRAM.md`), not a direct edit, and is not proposed here.

## What ran and what was read

**Ran:**

- `cop1/detector_probe.cpp`, linked against DoomSat's own build of F´'s `CcsdsTcFrameDetector`. BD frames on VC1
  and VC2 were detected. AD, BC Unlock, BC Set V(R) and AC frames were not. A stream of AD + BC + BD frames gave
  one frame, with 27 bytes discarded one at a time (`cop1/detector_probe.out`). Run twice, with identical output.
- `fpp-check` (from DoomSat's F´ venv) on `cop1/splice.fpp` (fails) and `cop1/splice_ok.fpp` (passes).
- A string scan of the bundled `yamcs-core-5.12.8.jar`, and `javap -l` on its `TmFrameDecoder`, to confirm the
  source read matches the jar DoomSat runs.
- `grep` over the 30 flight logs, and `git log` for when each was flown relative to the relay's commit.
- `cop1/fop_model.py` (output `cop1/fop_model_out.txt`, re-run byte-identical) and `cop1/fop_variants.py`.
- Scratch Yamcs runs of an uninitialised FOP and of AD frames with no CLCW. Their saved outputs were re-read for
  this report; they were not re-run.

**Read:** everything else, in the F´ v4.3.0, v4.4.0 and `devel` sources, the Yamcs 5.12.8 source at its tag, the
fprime-yamcs 0.2.1 package, and DoomSat itself.

**Not done:** no measurement of command loss on the real stack under the relay. Every guarantee above, for the
recommended option and for COP-1 alike, is either not built or modelled until a `DOOMSAT_RELAY=1` run at 1 %, 5 %
and 10 % counts commands received on board against commands sent.

To repeat the runs:

```bash
cd docs/plans/cop1
python3 fop_model.py            # about 20 s; compare with fop_model_out.txt
python3 fop_variants.py         # compare with fop_variants_out.txt
F=$DOOMSAT_HOME/DoomSat; "$F/fprime-venv/bin/fpp-check" splice.fpp; "$F/fprime-venv/bin/fpp-check" splice_ok.fpp
B=$F/build-fprime-automatic-native; L=$B/lib/Linux
g++ -std=c++14 -DTGT_OS_TYPE_LINUX -I"$F" -I"$B" -I"$F/lib/fprime" -I"$B/F-Prime" \
    -I"$B/cmake/platform/unix/Platform/.." -I"$B/.." -I"$B/F-Prime/default/config/.." \
    -I"$B/F-Prime/Svc/Ccsds/Types/config/SdlsKeyConfig/.." -o /tmp/detector_probe detector_probe.cpp \
    "$L/libSvc_FrameAccumulator.a" "$L/libSvc_Ccsds_Types.a" "$L/libUtils_Types.a" "$L/libUtils_Hash.a" \
    "$L/libFw_Types.a" "$B/F-Prime/Fw/Types/CMakeFiles/Fw_StringFormat_snprintf.dir/snprintf_format.cpp.o" \
    "$L/libdefault_config.a" "$L/libOs.a" "$L/libFw_Types.a"
/tmp/detector_probe              # compare with detector_probe.out
```

## Upstream note (draft, not filed)

For `nasa/fprime`, if the user wants to file it:

> **TcDeframer SDD says Type-A frames are deframed without FARM checks, but CcsdsTcFrameDetector drops them first**
>
> `Svc/Ccsds/TcDeframer/docs/sdd.md` says: "should Type-A frames be received, no FARM checks would be performed
> on board". In v4.3.0 (and v4.4.0 and `devel` 55f597d), `CcsdsTcFrameDetector` compares the whole first 16 bits of a frame with
> `(1 << BypassFlagOffset) | SpacecraftId`. So an AD frame (bypass 0) or a BC frame (control 1) is never
> detected. `FrameAccumulator` then discards it one byte at a time with no event, and `TcDeframer` never sees
> it. A ground station with COP-1 switched on therefore sees every sequence-controlled command vanish with no
> trace on board. Suggest either correcting the SDD to say only Type-BD frames reach the deframer, or emitting
> an event (throttled) when the detector rejects a frame whose version and spacecraft id match.

## Files

| Path | What |
|---|---|
| `docs/plans/cop1/detector_probe.cpp`, `.out` | The detector probe and its output |
| `docs/plans/cop1/splice.fpp`, `splice_ok.fpp` | The two FPP models: a splice into an imported subtopology (refused), and a locally wired frame layer (accepted) |
| `docs/plans/cop1/fop_model.py`, `fop_model_out.txt` | The FOP-1 / ideal FARM model and its output |
| `docs/plans/cop1/fop_variants.py`, `fop_variants_out.txt` | The same model with tuned timers and window |

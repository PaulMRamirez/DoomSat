# Task: CFDP for DoomSat file transfer, Stage 2 (time-boxed spike)

Stage 1 gave DoomSat in-flight WAD uplink over F Prime's native file packets, which have no retransmission. This stage replaces that transfer with CCSDS CFDP so a file survives a lossy link. It is a spike: the outcome is either a working Class 2 uplink or a clear findings report. Both are acceptable.

Read `CLAUDE.md` and `docs/plans/wad-uplink-stage1.md` first. Branch from `feature/wad-uplink` as `feature/cfdp-spike`. Save this message as `docs/plans/cfdp-stage2-spike.md`, commit it, and keep a findings section in it up to date.

## What I found by reading (not by running)

Checked on 3 October 2026 against the versions DoomSat pins. Confirm each before relying on it.

- **F Prime v4.3.0 ships a CFDP engine.** `Svc/Ccsds/CfdpManager` implements Class 1 and Class 2. Per channel it has `dataIn` and `dataInReturn` for incoming PDUs, `dataOut` and `dataReturnIn` for outgoing PDUs, `bufferAllocate` and `bufferDeallocate`, a `run1Hz` port that drives transmit and timers, a `SendFile` command, and a `fileIn` port of type `Svc.SendFileRequest` so another component can ask for a downlink. Compile-time settings are in `default/config/CfdpCfg.fpp`.
- **There is a ready subtopology.** `Svc/Subtopologies/FileHandlingCfdp` contains `cfdpManager`, `fileManager`, and `prmDb`. It replaces `FileHandling`; both define `fileManager` and `prmDb`, so a topology imports one or the other.
- **The com side is not pre-wired.** At v4.3.0 the `ComCcsds` subtopology's buffer queue enum has only `FILE`. The `FileHandlingCfdp` design doc shows an example using dedicated CFDP ports on `ComCcsds` that I did not find at that tag, and I found no reference deployment that wires `cfdpManager`. Check whether a newer F Prime tag or `devel` has one to copy from. Upgrading F Prime is out of scope unless it is trivial.
- **fprime-yamcs has no CFDP bridge.** Version 0.2.1 (pinned) and 0.2.4 (latest on 30 September 2026) carry only `FprimeFilePacketService` for `Fw::FilePacket`. The launcher does accept extra plugin jars through `--yamcs-plugin-jars`.
- **Yamcs 5.12.8 has a native `CfdpService`** that exchanges raw PDUs on streams.
- The subtopology doc warns that CFDP transfers are not sandboxed: a ground-commanded transaction can read or write any path the process can reach.

## Questions the spike must answer

1. What exactly does `cfdpManager` put in a `dataOut` buffer and expect in a `dataIn` buffer: a bare PDU, or a PDU behind an F Prime packet descriptor? How does `FprimeRouter` tell a CFDP PDU from an `Fw::FilePacket`, and on which APID?
2. What must the ground shim do so Yamcs's `CfdpService` and F Prime agree: wrap and unwrap the space packet, and match entity id length, sequence number length, checksum type, and maximum PDU size against the 1024-byte TC and TM frames in `ground/yamcs/etc/yamcs.fprime-project.yaml`?
3. Can the shim be a small Yamcs plugin jar loaded with `--yamcs-plugin-jars`, or stream configuration alone, without forking fprime-yamcs?
4. What throughput do you get? Transmit is driven at 1 Hz with a per-cycle PDU limit, so measure it rather than guess.

## Steps

1. **Flight.** Swap `FileHandling` for `FileHandlingCfdp` in `flight/DoomSat/Top/topology.fpp`, wire the PDU, buffer, and 1 Hz ports, and connect `DataProducts.dpCat.fileOut` to `cfdpManager.fileIn`. Remember `scripts/wsl_sync.sh` copies an explicit file list.
2. **Ground.** Configure `CfdpService` in the Yamcs instance and build the shim.
3. **Class 1 first**, one small scenario WAD up, then one file down.
4. **Class 2**, same files.
5. **Lossy link harness.** Put a small UDP relay between Yamcs and the F Prime comm bridge (TM on port 50000, TC on port 50001) that drops a configurable percentage of datagrams.
6. **The demonstration.** Uplink a WAD at 5 percent loss both ways. The native Stage 1 path should fail its checksum; CFDP Class 2 should complete through NAK retransmission. Then `LOAD_WAD` it and show the level running.

## Constraints

Everything in the Stage 1 constraints still holds: the honesty suite passes, no edits to `research/`, `docs/CHARTER.md`, `knowledge/`, or pilot decision logic, no WADs or keys in git, existing line endings kept, and no graded runs on uplinked WADs. Keep the Stage 1 native path recoverable (a branch is enough). Restrict CFDP destination paths to the uplink directory where the component allows it, and document what it does not allow.

## Finish

Push `feature/cfdp-spike` to this fork and open a draft pull request inside the fork only. Do not open anything against `Devonance/DoomSat`, `nasa/fprime`, or `fprime-community/fprime-yamcs`; draft the text of any upstream issue or contribution in the findings file and I will file it. The final report answers the four questions, separates what ran from what was only read, and says plainly whether CFDP is ready to merge, needs upstream work first, or should wait for a later F Prime release.

Out of scope here, noted for later: F Prime v4.3.0 also ships a `ComCcsdsSdls` subtopology for SDLS on the command link (its default decryptor is clear text).

---

## Findings

_Kept up to date by Claude during the session._

- Branched from `feature/wad-uplink` at `cddba1b`. Stage 1's native path stays recoverable on that branch (draft PR
  PaulMRamirez/DoomSat#1).

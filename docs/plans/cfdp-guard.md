# cfdpGuard: uploads confined to the uplink directory, committed on board

The CFDP spike (`docs/plans/cfdp-stage2-spike.md`) left two gaps. First, `cfdpManager` wrote an upload wherever
its Metadata said (risk 1). Second, a received WAD only got its real name when the ground sent `COMMIT_WAD`, a
single unprotected command (risk 3). `cfdpGuard` (`flight/Components/CfdpGuard`) closes both with no change to F´.

## What it does

It is a passive component on both of `cfdpManager`'s paths:

```
ComCcsds.fprimeRouter.fileOut -> cfdpGuard.uplinkIn      cfdpGuard.uplinkOut -> cfdpManager.dataIn[0]
cfdpManager.dataOut[n] -> cfdpGuard.downlinkIn[n]        cfdpGuard.downlinkOut[n] -> comQueue FILE
cfdpGuard.fileAnnounceOut -> doom.fileAnnounce
```

- **Up.** Every Metadata PDU's destination must be `NAME.wad.<nonce>.part`, directly in
  `$DOOMSAT_HOME/wads/uplink`, using only letters, digits and `_ . + -`. This is the rule in
  `flight/Components/Doom/WadPath.hpp`, which the Doom component uses as well. A Metadata that fails the check never
  reaches `cfdpManager` as a PDU. Everything else passes through untouched.
- **Down.** When `cfdpManager`'s own FIN for an upload the guard let through says no error, delivery complete and
  file retained, the guard announces the file to the Doom component, which renames it to `NAME.wad`. It does this
  before the FIN leaves, so the file is in place by the time the ground hears of it. The receiver sends that FIN
  only after its checksum over the whole file, read back from disk at the Metadata's destination, matched the
  EOF's (`TransactionRx.cpp` `r2CalcCrcChunk`).

## Decisions, and why

- **It reads PDUs with F´'s own classes.** The guard cuts the buffer the way `CfdpManager::dataIn_handler` does,
  types it with `peekPduType` and decodes it with `MetadataPdu` / `FinPdu`, the same code `cfdpManager` runs. So
  it sees a Metadata exactly when `cfdpManager` would, and it checks the destination string `cfdpManager` would
  open. That string is NUL-terminated at the first embedded NUL; a unit test pins this. A hand-written parser
  could disagree with F´'s, and a crafted PDU would then get through.
- **A refused Metadata goes on to `cfdpManager` with its descriptor changed.** Returning the buffer to the router
  from inside the router's own call would deadlock. The router still holds its guarded-port mutex
  (`PTHREAD_MUTEX_ERRORCHECK`), so the relock returns `EDEADLK` and `Os::Mutex` asserts. `cfdpManager` instead hands
  back any buffer whose descriptor is not `FW_PACKET_FILE`, unread, on its own thread, through the return path the
  router already has. `tests/test_cfdp_guard.py` reads that branch from F´ when the install is there.
- **Drop the Metadata, not the transaction.** The file data and EOF still reach `cfdpManager`. With no Metadata,
  it writes them to `<uplink>/.cfdp-tmp/<eid>:<seq>.tmp`, asks for the Metadata ten times, and ends with a FIN
  carrying `NAK_LIMIT_REACHED`. The ground then shows FAILED. If the guard dropped every PDU instead, Yamcs would
  end PAUSED with no reason given. The bytes stay inside the uplink directory, and `scripts/wsl_run_flight.sh`
  clears `.cfdp-tmp` at each start.
- **Commit only a clean Class 2 FIN.** The FIN must decode, and a `FinPdu` that failed to decode still reads
  NO_ERROR / COMPLETE / RETAINED, so that check comes first. It must point toward the sender, match a recorded
  (source entity, sequence number, destination entity) by decoded value (Yamcs writes ids in 2 and 4 bytes, F´ in
  the fewest), and carry NO_ERROR, COMPLETE and RETAINED. The first such FIN commits, and F´'s repeats find
  nothing to match. Class 1 has no FIN, so the guard lets it into the uplink directory but never commits it.
  `COMMIT_WAD`, with its size and checksum check, stays for class 1 and for commits by hand.
- **Belt and braces.** `Doom::fileAnnounce_handler` now refuses any path outside the uplink directory as well.

## What ran (4 October 2026, this VM)

| Check | Result |
|---|---|
| Unit tests: `scripts/flight.sh ut` (GTest, 14) | All pass. They cover every refused shape of destination, an embedded NUL both ways, Yamcs-width ids, a truncated FIN, seven failed or partial FINs, class 1, resends and a full table |
| Python suite | Passes, with `tests/test_cfdp_guard.py` (wiring, parser reuse, the shared name rule, the F´ branch the refusal relies on) and three demo tests with an on-board stand-in for the guard |
| Class 2 upload, no loss | `MetadataReceived`, then `UploadCommitted`, then `WadUplinked`, then `RxFileTransferCompleted`. No `COMMIT_WAD` was sent; `LOAD_WAD` flew it |
| Uploads to `/tmp/…`, `<uplink>/../…`, `<uplink>/notawad.bin`, `<uplink>/.cfdp-tmp/x.wad.1.part` | Each was refused on board (`UploadRefused`, once per transaction although its Metadata came about ten times). The ground showed FAILED `NAK_LIMIT_REACHED` after 28.0 s. No file at any target; the bytes sat in `.cfdp-tmp` until the next start cleared them |
| Class 1 upload to `/tmp/…` | Refused on board, nothing written. The ground showed COMPLETED, because class 1 never hears back |
| Class 1 WAD upload | Not committed by the guard; `COMMIT_WAD` with its checksum put it in place, and it flew |
| `doom1.wad` (4.2 MB) at 5 % loss each way | FIN after 40.2 s, committed on board with no `COMMIT_WAD`, byte-identical; its first `LOAD_WAD` was lost and resent |
| Class 2 downlink at 5 % loss (`SendFile`) | Completed and byte-identical, every PDU through the guard |

The rename happens while `cfdpManager` still holds the file open read-only, between the FIN and the FIN-ACK.
POSIX keeps the descriptor valid, and no error followed: `RxFileTransferCompleted` came as usual, naming the old
`.part`, which is only cosmetic.

## What it does not cover

- **Ground commands and parameters still name paths.** `cfdpManager.SendFile`, `PlaybackDirectory`,
  `PollDirectory` and the `ChannelConfig` parameter (`tmp_dir`, `move_dir`, `fail_dir`) take any path, and the
  guard never sees them. Anyone who can command the spacecraft can still read, and with `keep` DELETE remove,
  any file the flight process can. Run the flight side as an ordinary user.
- **A refused upload costs a whole transfer.** The ground's failure reason, `NAK_LIMIT_REACHED`, reads like a link
  fault. The `UploadRefused` event and the `UPLOADS_REFUSED` channel say what happened. Injecting a FIN with
  `FILESTORE_REJECTION` would fail the transfer at once, but it means building PDUs in the guard and was not done.
- **Up to 8 uploads are followed at once** (`MAX_UPLOADS`, above `cfdpManager`'s `MaxSimultaneousRx` of 5). A
  ninth pushes out the oldest (`UploadForgotten`), and `COMMIT_WAD` can still commit that one.
- **The temp files of refused uploads** stay until the next start.
- **Read, not run.** If a file-data PDU's body fails to decode before any Metadata, F´ ends the transaction with
  no error status and could send a NO_ERROR FIN for the temp file. The guard has no Metadata for it, so it commits
  nothing. If a Metadata did arrive and the file is short, `r2CalcCrcChunk` would loop. Yamcs never sends such a
  PDU, and TC frames are FECF-checked.
- **On `feature/wad-uplink`** (the native build) there is no guard, and FileUplink calls `fileAnnounce` itself.

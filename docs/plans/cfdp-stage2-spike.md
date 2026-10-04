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

_Written by Claude on 4 October 2026, at the end of the spike. Branch `feature/cfdp-spike`, built from
`feature/wad-uplink` at `cddba1b`; Stage 1's native path stays there (draft PR PaulMRamirez/DoomSat#1)._

Each claim is marked with how it is known:

- **ran**: ran on the DoomSat stack in this container (F´ v4.3.0, fprime-yamcs 0.2.1, Yamcs 5.12.8), with the
  evidence named. Times are wall clock on the ground unless they say "on board".
- **ran (harness)**: ran in an isolated scratch harness during the spike (a separate F´ build, an in-process
  Yamcs), not on the stack.
- **read**: read in source or docs, not executed.
- **inferred**: reasoned from what was read or ran, not tested.

Source paths: `F´:` is `lib/fprime` in the F´ project (v4.3.0, `7d8f579`), `Y:` is Yamcs 5.12.8
`yamcs-core/src/main/java/org/yamcs/`, `FY:` is the installed `fprime_yamcs` 0.2.1 package.

### Verdict

**Ready to merge, Class 2 only. No upstream work is needed first, and waiting for a later F´ release would not
help.** On the pinned versions the whole demonstration ran: CFDP Class 1 and Class 2 up and down, a 4.2 MB IWAD
both ways, native file packets failing 3 of 3 at 5 % frame loss each way while CFDP Class 2 delivered every file
whole, then `COMMIT_WAD` and `LOAD_WAD` with the level flying. The suite (431 tests) and the honesty suite pass.

What makes that true is four local workarounds, each small and each pinned by a test: local instances in place
of the `FileHandlingCfdp` subtopology, which does not compile; `MaxPduSize` 1001 in place of a stock 1024 that
asserts; a 42-line launcher wrapper because fprime-yamcs sorts the keys of the instance YAML; and
`COMMIT_WAD` because `cfdpManager` has no `fileAnnounce`. The upstream drafts below would retire them, but
none blocks. F´ v4.4.0 and `devel` (`55f597d`, 2 October 2026) still carry the `configure` bug and still have no
reference wiring, so an upgrade buys only the framer fix (risk 4) and costs a port (v4.4.0 removes
`ComCfg.AggregationSize`, which the PDU size here is set against) [read]. Revisit at the next planned F´ bump.

Two conditions come with the merge:

1. **WADs go Class 2.** Class 1 has no retransmission, and F´ keeps a Class 1 file whose checksum failed and
   reports it completed [ran]. The demo defaults to Class 2 on this build and refuses to commit a Class 1 file
   after `RxCrcMismatch`, but at 5 % downlink loss that event can itself be lost [inferred].
2. **Destination paths are not sandboxed**, as they were not with F´ file packets either. What is and is not
   restricted is set out below. That is acceptable for a demonstration stack; anything more needs the guard
   component or an upstream sandbox first.

Merging replaces the native file packets: FileUplink and FileDownlink leave the topology, because `FprimeRouter`
has one file output and CFDP and `Fw::FilePacket` both arrive on APID 3 (Q1). The native path stays on
`feature/wad-uplink`.

### What ran

| Run | Result | Evidence |
|---|---|---|
| Class 1 up, `basic.wad` (2,704 B), no loss | Byte-identical on board | ran: `MetadataReceived`, `RxFileTransferCompleted`; md5 |
| Class 2 up, `basic.wad` and `doom1.wad` (4,196,020 B) at 40, 10, 5 and 2 ms per PDU, no loss | All byte-identical | ran; md5 on board |
| Class 1 down (`cfdpManager.SendFile`), `basic.wad` | Byte-identical in the `cfdpDown` bucket | ran |
| Class 2 down, `doom1.wad`, no loss | Byte-identical, 67 s | ran |
| Native file packets at 5 % loss each way (Stage 1 build), `cig.wad` (214,730 B) ×3 | 3 of 3 `BadChecksum`; 59 `PacketOutOfOrder` in all, about 20 lost packets a file. The same file through the relay at 0 % arrived whole | ran: `logs/fprime-yamcs-2026_10_03-23_59_21` |
| Class 2 up at 5 % each way, `cig.wad` ×2 and `doom1.wad` | All byte-identical | ran |
| Class 2 down at 5 % each way, `doom1.wad` | Byte-identical, 74.6 s | ran |
| Class 1 up at 5 % each way, `cig.wad` ×4 | 0 of 4 whole. Twice the metadata PDU was lost: nothing written on board, only `RxInactivityTimeout` (WARNING_LO). Twice data PDUs were lost: `RxCrcMismatch` (WARNING_LO), then `RxFileTransferCompleted` (ACTIVITY_HI), and the corrupt file kept at full size. Yamcs reported all four COMPLETED | ran |
| `COMMIT_WAD` then `LOAD_WAD` after a Class 2 upload at 5 % each way | `WadUplinked`, `WadLoaded`, a frame from the new level (`cig.wad` over `freedoom2.wad` on MAP02) | ran, twice (3 and 4 October) |
| The README's own command, unchanged, on this build | Detected CFDP, Class 2 up, committed, loaded, frame saved | ran |
| `COMMIT_WAD` refusals | `../etc/x.wad.1.part` VALIDATION_ERROR; a missing `.part` EXECUTION_ERROR | ran |
| Destination paths outside the uplink directory | Written; see "Destination paths" | ran |
| Boot parameters (`flight/config/PrmDb.json` → `PrmDb.dat`) | `PrmFileLoadComplete`, 10 records, no warnings; the fast CRC pass with no `PRM_SET` sent | ran |
| CONTROL round trips during Class 2 uplinks | Median 100 ms idle and during, at 10 ms and at 5 ms pacing | ran |
| Unit tests | 431 pass (412 before this stage); honesty 8 checks and 17 with `--canary`, 0 failed | ran |

The relay's drops are seeded and reproducible. In the Class 1 runs on 4 October its TC drop list, replayed
offline (`random.Random("11-TC")`), matched the `UnexpectedSequenceCount` gaps the flight software logged one for
one, including the two lost metadata PDUs (frames 0 and 438): every uplink loss was the relay's, none the
stack's. The corrupt Class 1 files have exactly the lost PDUs' bytes zeroed (13 and 10 PDUs).

### Q1. What `cfdpManager` sends and expects, and how the router tells it apart

**A bare CFDP PDU behind the two-byte F´ packet descriptor `0x0003` (`FW_PACKET_FILE`), with no PDU CRC, both
ways, on APID 3. `FprimeRouter` cannot tell it from an `Fw::FilePacket`.**

- Down: `sendPduBuffer` writes `00 03` and then the PDU (`F´:Svc/Ccsds/CfdpManager/CfdpManager.cpp:290-309`)
  [read]. ComQueue takes the APID from the descriptor and `SpacePacketFramer` keeps the descriptor in the data
  field [read]. Every APID 3 packet starts `00 03`, and a full file-data PDU is 2 + 1001 bytes [ran (harness)];
  the stack's downlink filter depends on that descriptor, and every downlink arrived [ran].
- Up: `dataIn` drops any buffer whose first U16 is not `0x0003`, then strips it (`CfdpManager.cpp:155-191`)
  [read]. Every upload here went up as `1003 C000 LLLL 0003 <PDU>` and was accepted [ran].
- Routing: `FprimeRouter` switches on the APID alone (`F´:Svc/FprimeRouter/FprimeRouter.cpp:32-90`), APID 3 goes
  to its one `fileOut`, and `ComCfg.Apid` has no CFDP entry [read]. So on board CFDP and `Fw::FilePacket` are
  rival consumers of the same output, and this build wires it to `cfdpManager.dataIn[0]`
  (`flight/DoomSat/Top/topology.fpp:77`). The ground can tell them apart: a CFDP header's first byte carries
  version `001` in its top three bits, while a FilePacket's first byte is a type 0 to 3; `cfdp_in` filters on
  that (`ground/yamcs/etc/cfdp_streams.sql:6`) [ran].
- A side effect: ApidManager keeps one sequence counter per APID for both checking uplink and stamping downlink,
  and logs every mismatch unthrottled (`F´:Svc/Ccsds/ApidManager/ApidManager.cpp:24-35`) [read]. It does not drop
  the packet [read]. During a lossy 4.2 MB upload that was 486 `UnexpectedSequenceCount` WARNING_LO events [ran].
  They make a fair uplink-loss counter.

### Q2. What the ground shim must do, and the parameters that must agree

**Wrap and unwrap the space packet and the descriptor, in stream SQL; nothing else.** Down: keep APID 3 packets
with descriptor 3 and CFDP version 1, strip 6 + 2 bytes. Up: prepend `1003 C000 0000 0003`; fprime-yamcs's
`FprimeCommandPostprocessor` fills the length and the per-APID count [ran]. Feeding a TC virtual channel from
`cfdp_out` directly fails in Yamcs's `PreparedCommand.fromTuple`, so the SQL copies into a TC-shaped
`cfdp_tc` stream [ran (harness)].

| Item | Yamcs `CfdpService` | F´ `cfdpManager` | Set here |
|---|---|---|---|
| Entity ids | `ground` 100, `doomsat` 42 | `LocalEid` 42, `FileInDefaultDestEntityId` 100; a PDU for another destination is dropped [read] | Both sides from the repo: the Yamcs YAML and `flight/config/PrmDb.json`, pinned equal by a test [ran] |
| Entity id length | `entityIdLength` sets outgoing only; incoming is read from each header | Sends the fewest bytes the value needs; reads 1 to 8 [read] | 2 [ran] |
| Sequence number length | `sequenceNrLength` | Fewest bytes; kept in a U32 [read] | 4 [ran] |
| Checksum | MODULAR or NULL; no CRC32 [read] | Always modular, whatever the metadata says [read] | MODULAR; NULL would fail on board [read] |
| PDU CRC, large file | Never set; refuses large-file PDUs [read] | Ignores both flags on receipt instead of refusing them [read] | Off |
| Largest PDU up | `maxPduSize` | No limit checked on receipt [read] | 1009 = 1024 − 5 (TC frame header) − 2 (FECF) − 6 (space packet header) − 2 (descriptor). 1010 is refused as "cmd size 1018" [ran (harness)]; 1009 ran |
| Largest PDU down | Any | `ComAggregator` asserts on a space packet over `AggregationSize` 1009 (`F´:Svc/ComAggregator/ComAggregator.cpp:124`), so `MaxPduSize` ≤ 1001 [read]; the stock 1024 is FATAL on the first full file-data PDU [ran (harness)] | `MaxPduSize = 1001` (`flight/config/CfdpCfg.fpp:69`); 4.2 MB down with no assert [ran] |
| NAK segments | Up to 124 in one NAK | Keeps the first 58 [read] | Harmless: the rest are asked for again |
| Closure requested (Class 1) | Bit 0x40, as the CFDP standard lays out the byte | Bit 0x80 (`F´:.../Types/MetadataPdu.cpp:123,180`) [read] | Not used |
| Proxy put, directory listing | On by default | Not implemented [read] | `hasDownloadCapability` and `hasFileListingCapability` false: downlinks start on board with `cfdpManager.SendFile` |
| Packets per TC frame | Packs several by default | `SpacePacketDeframer` keeps only the first (Stage 1) [ran] | `multiplePacketsPerFrame: false` on both TC channels |
| Commands beside a transfer | One TC link, virtual channels multiplexed FIFO by default [read] | Any VCID | `priorityScheme: ABSOLUTE`, commands on vc1 (priority 10), PDUs on vc2 (priority 1): CONTROL stays at 100 ms during an upload [ran] |
| Finished transfers | Answer PDUs by (entity, sequence) for `pendingAfterCompletion`, 10 min by default [read] | Numbers its transactions from 1 at every boot [read] | 60 s, still longer than F´'s FIN retries (2 s × 10), so a flight-only restart cannot land a new downlink on an old transfer [inferred] |
| On-board CRC pass | n/a | `RxCrcCalcBytesPerCycle` default 64 KiB a 1 Hz tick: about a minute for `doom1.wad` [ran (harness)] | 16 MiB in `PrmDb.json`, loaded at boot [ran] |

### Q3. Plugin jar, or stream configuration alone?

**Stream configuration alone, with no fork and no Java, but not with fprime-yamcs's launcher as it is.** The
launcher rewrites the instance YAML with `yaml.safe_dump`, which sorts keys (`FY:__main__.py:561`) [read]. Yamcs
creates `streamConfig` entries in the order it reads them, so the sort puts `sqlFile` ahead of the `tm` and `tc`
streams its SQL reads, and Yamcs fails with `RESOURCE_NOT_FOUND 'tm_realtime'` [ran (harness)].
`ground/yamcs/launch.py` imports fprime-yamcs with `safe_dump` set to keep key order, and the scripts start
Yamcs through it [ran]. The SQL path is absolute (`${env.DOOMSAT_REPO}`) because Yamcs resolves it against its
working directory, the F´ project [ran].

A plugin jar works too, as a fallback: a small `AbstractYamcsService` that creates its own streams compiled with
`javac` against the bundled Yamcs jars and passed byte-level checks offline [ran (harness)]. It was never loaded
into Yamcs. It would not depend on key order, at the cost of a JDK at build time (fprime-yamcs's bundled JRE has
no `javac`, and CLAUDE.md promises Java is not needed).

### Q4. Throughput, measured

| Direction | Configuration | Size | Time | Rate | |
|---|---|---|---|---|---|
| Up, Class 2 | 40 ms per PDU (the default), no loss | 4,196,020 B | 171.1 s (170.7 on board) | 24.5 kB/s | ran |
| Up, Class 2 | 10 ms | 4,196,020 B | 44.1 to 45.1 s | 93 to 95 kB/s | ran |
| Up, Class 2 | 5 ms | 4,196,020 B | 23.0 to 24.0 s | 175 to 182 kB/s | ran, three times |
| Up, Class 2 | 2 ms | 4,196,020 B | 10.0 s | 418 kB/s | ran, once |
| Up, Class 2 | 10 ms, 5 % loss each way | 4,196,020 B | 121.1 s: about 45 s for the first pass, 76 s of NAK rounds | 34.6 kB/s | ran |
| Up, Class 2 | 5 % loss each way, `cig.wad` | 214,730 B | 5.1 s at 10 ms; 12.0 s at 20 ms | 42; 18 kB/s | ran |
| Down, Class 2 | 64 PDUs a 1 Hz tick (default), no loss | 4,196,020 B | 67.1 s on board | 62.6 kB/s | ran |
| Down, Class 2 | the same, 5 % loss each way | 4,196,020 B | 74.6 s | 56.3 kB/s | ran |
| Up, native file packets (Stage 1) | 512 B chunks, 20 ms apart | 4,196,020 B | 167.7 s | 25.0 kB/s | ran |

kB is 1000 bytes. How to read it:

- Uplink time is about (PDUs × the delay between them) + 1 to 2 s. `doom1.wad` is 4,226 file-data PDUs of 993
  bytes plus metadata and EOF: 4,228 × 40 ms = 169.1 s against 170.7 s on board [ran]. Yamcs paces every PDU,
  retransmissions included, so the delay sets the rate. The offered delays are 5 to 100 ms
  (`pduDelayPredefinedValues`). 2 ms ran clean once; 1 ms dropped frames on board in the harness, because F´
  v4.3.0's framer stalls when its buffers run out (risk 4) [ran (harness)].
- Downlink is about `max_outgoing_pdus_per_cycle` (64) × 990 bytes per `run1Hz` tick [read]. Raising it in
  `PrmDb.json` should raise the ceiling, but file PDUs go out ahead of telemetry (FILE has queue priority 1,
  TELEMETRY 2), so a big downlink holds up game frames while it runs [read; not measured].
- Under loss, the tail is NAK rounds. F´ v4.3.0 keeps at most 58 gaps per NAK and waits an `ack_timer` (2 s)
  between rounds [read], so a few hundred lost PDUs take tens of seconds [inferred].
- At the default 40 ms, CFDP Class 2 matches the native rate; at 5 ms it is seven times faster, and it survives
  loss.

### The demonstration at 5 % loss each way

Behind `tools/lossy_relay.py --loss 5` (Yamcs started with `DOOMSAT_RELAY=1`; `ground/yamcs/launch.py` moves its
frame links to 51000/51001 and the relay forwards to the comm bridge's 50000/50001):

- **Native file packets** (Stage 1 build): `cig.wad` failed 3 of 3 with `BadChecksum` [ran].
- **CFDP Class 2**: `cig.wad` and `doom1.wad` arrived byte-identical; `doom1.wad` also came down identical [ran].
- **`COMMIT_WAD` and `LOAD_WAD`**: on 4 October, `cig.wad` as `cigc2.wad` (FIN after 12.0 s), then
  `WadUplinked`, `WadLoaded: Now flying cigc2.wad over freedoom2.wad on MAP02`, WAD_LOADS 0 → 1, EPISODE 1 → 2
  and a frame from MAP02 [ran]. On both days a command or its answer was lost on the way: the demo's `--tries`
  resent it. On 4 October the first `COMMIT_WAD` had worked but its `WadUplinked` never reached the ground; the
  retry found no `.part` and said `WadUplinkFailed`, and `LOAD_WAD` then showed the file was in place [ran].
- **CFDP Class 1** at the same loss: 0 of 4 whole (see "What ran"). The demo refused to commit [ran].

Commands are single unprotected TC frames: there is no COP-1 here. That is why the demo resends them, and it is
the next thing to look at for a lossy link, not CFDP.

### Destination paths: what is restricted and what is not

Restricted [ran]:

- `COMMIT_WAD(part)` renames only a bare `NAME.wad[.<nonce>].part` inside `$DOOMSAT_HOME/wads/uplink`
  (`flight/Components/Doom/Doom.cpp:112-131`): any `/` is a VALIDATION_ERROR, and a missing file an
  EXECUTION_ERROR.
- `LOAD_WAD` names only bare `.wad` files in `wads/uplink` and `wads/`, refuses `.part` names, and proves the
  file in a child process before the game switches (Stage 1).

Not restricted, because `cfdpManager` uses the metadata's destination path as it stands
(`F´:Svc/Ccsds/CfdpManager/Engine.cpp:405-406`) and opens it to create or overwrite
(`TransactionRx.cpp:368`) [read]:

- An upload to `/tmp/doomsat_cfdp_escape.bin` and one to `/root/doom/wads/uplink/../escape_dotdot.part` were
  both written, byte-identical [ran].
- An upload to an existing file overwrote it in place [ran].
- `cfdpManager.SendFile` downlinked `/etc/hostname` [ran]. With `keep` DELETE it deletes the source after
  sending, and so do downlinks asked for through `fileIn` unless `FileInDefaultKeep` says KEEP, as `PrmDb.json`
  now does [read].
- All test files were removed afterwards.

F´ file packets were no better: FileUplink also opens the START packet's path as given
(`F´:Svc/FileUplink/File.cpp:20-34`) [read]. The component offers no hook to restrict this, so there was nothing
to restrict "where the component allows it" beyond the commit step. F´ says the same in
`Svc/Subtopologies/FileHandlingCfdp/docs/sdd.md`. v4.4.0 adds a sandbox to FileManager and `devel` one to PrmDb,
but `cfdpManager` has none in either [read]. The ways to close it, in order:

1. Run the flight side as an ordinary user that can write little besides the uplink directory (no code).
2. A small guard component between `fprimeRouter.fileOut` and `cfdpManager.dataIn` that drops any metadata PDU
   whose destination is not `<uplink>/<basename>.part` (not built; about a day).
3. Upstream: a destination root in `cfdpManager`, like FileManager's (draft F5).

### Risks and limits, most serious first

1. **No destination sandbox** (above).
2. **Class 1 is unsafe for WADs** (above). The demo defaults to Class 2 on this build.
3. **Commands have no retransmission.** A lost `COMMIT_WAD` or `LOAD_WAD` needs a resend; the demo's `--tries`
   does it, and the dashboard's `LOAD_WAD` button does not.
4. **The v4.3.0 framer stalls** when `commsBufferManager` runs dry: it drops the packet without a `comStatus`,
   and ComQueue then waits for ever (`F´:Svc/Ccsds/SpacePacketFramer/SpacePacketFramer.cpp:40-46`) [read; the
   harness saw frames dropped at 1 ms pacing]. Fixed in v4.4.0 [read]. It applies to the current stack too,
   CFDP or not [inferred]. Keep PDU pacing at 5 ms or more.
5. **Big downlinks hold up game frames** (Q4) [read]. Downlink large files while not flying.
6. **The 40-character command string cap**: `COMMIT_WAD` carries `NAME.<13 digits>.part`, so an uplinked name can
   be at most 19 characters on this build (38 natively). The demo checks before anything goes up [ran].
   `SendFile` paths are capped the same way [read].
7. **Yamcs's sender inactivity timer never arms** (`eofAckReceived` is never set,
   `Y:cfdp/CfdpOutgoingTransfer.java:93`) [read]. If every FIN were lost the upload would stay RUNNING; F´ sends
   ten, so that needs ten in a row lost [inferred].
8. **Channel 1 is wired but must not carry Class 2**: its ACK, NAK and FIN would come back on `dataIn[0]`
   [inferred]. Everything here uses channel 0.
9. **Cosmetic**: one command-history entry per transfer, rewritten for every PDU; APID 3 packets archived with a
   1970 gentime; the XTCE decodes CFDP PDUs as file-packet noise [read].

### The reading notes at the top, checked

- F´ v4.3.0 ships a CFDP engine with those ports, Class 1 and Class 2: **confirmed** [ran].
- The `FileHandlingCfdp` subtopology: **exists, but does not compile** at v4.3.0, v4.4.0 or `devel`: it calls
  `cfdpManager.configure(memAllocator)`, and `configure` needs a file queue depth too
  (`F´:Svc/Subtopologies/FileHandlingCfdp/FileHandlingCfdp.fpp:13`, `CfdpManager.hpp:53`) [ran (harness) at
  v4.3.0; read at the others]. This build declares the instances itself.
- The com side is not pre-wired: **confirmed**, and v4.4.0 and `devel` add no CFDP slot to `ComCcsds` and no
  reference deployment either [read]. The SDD's example ports do not exist [read].
- fprime-yamcs has no CFDP bridge: **confirmed** [read]. None was needed (Q3).
- Yamcs 5.12.8's `CfdpService` exchanges raw PDUs on streams: **confirmed** [ran].
- CFDP transfers are not sandboxed: **confirmed** [ran].

### Suggested charter note (for the pull request, not the charter)

> Uplinked WADs stay demonstrations whatever carries them, F´ file packets or CFDP. CFDP changes how a file
> reaches the spacecraft, not what the pilot may see: the pilot side reads no file-transfer state, and no bench
> or graded flight runs on an uplinked WAD.

### Upstream drafts (not filed)

Written for you to file. Each says what ran and what was only read.

**nasa/fprime**

**F1. `FileHandlingCfdp` subtopology does not compile: `cfdpManager.configure` gets one argument of two**
> `Svc/Subtopologies/FileHandlingCfdp/FileHandlingCfdp.fpp:13` emits
> `FileHandlingCfdp::cfdpManager.configure(FileHandlingCfdp::Allocation::memAllocator);`, but
> `Svc/Ccsds/CfdpManager/CfdpManager.hpp:53` declares
> `void configure(Fw::MemAllocator& allocator, FwSizeType fileQueueDepth, FwEnumStoreType memId = 0);`.
> A topology that imports the subtopology fails to build at v4.3.0 (built) and, by reading, at v4.4.0 and at
> `devel` 55f597d. Suggest a `FileHandlingCfdpConfig` constant for the queue depth, passed here.

**F2. Stock `CfdpCfg.MaxPduSize` (1024) asserts in `ComAggregator` with the stock `ComCcsds` (v4.3.0)**
> A file-data PDU of `MaxPduSize` bytes travels as a space packet of 6 + 2 + 1024 bytes, more than
> `ComCfg.AggregationSize` (1009), and `ComAggregator::Svc_AggregationMachine_guard_isFull` asserts on the first
> one: FATAL on the first full downlinked PDU (seen in a v4.3.0 build). 1001 works. Suggest deriving the default
> from the frame size, or a static assert tying the two. (v4.4.0 removes `AggregationSize`; the bound there was
> not checked.)

**F3. CFDP Class 1 receive keeps a file whose checksum failed and reports the transfer completed**
> Class 1 upload with lost file-data PDUs, F´ v4.3.0: `RxCrcMismatch` (WARNING_LO), then
> `RxFileTransferCompleted` (ACTIVITY_HI), and the file stays at its destination at full size with zeroed holes
> (seen on a lossy link, twice). `TransactionRx` never sets an error condition for Class 1, and keep defaults to
> KEEP. Suggest: treat a checksum failure as a failed transaction (condition code "file checksum failure"),
> report `RxFileTransferFailed`, and move the file to `fail_dir` or delete it.

**F4. Metadata PDU: closure-requested is written at bit 0x80, not 0x40**
> `Svc/Ccsds/CfdpManager/Types/MetadataPdu.cpp:123` shifts `closureRequested` by 7 and line 180 reads it from bit
> 7. The CFDP standard's metadata layout is a reserved bit, then closure requested, so the bit is 0x40, which is
> where Yamcs (`MetadataPacket.java:47`) puts it. Class 1 transfers that ask for closure do not interoperate. (Read,
> not run.)

**F5. `cfdpManager`: no destination sandbox and no notice of a received file**
> Metadata destination paths are used as given (`Engine.cpp:405-406`) and opened to create or overwrite
> (`TransactionRx.cpp:368`). On a v4.3.0 deployment, uploads to an absolute path outside any intended directory
> and to `<dir>/../file` were written, an existing file was overwritten, and `SendFile` sent `/etc/hostname`.
> Suggest a root-directory parameter or `configure` argument like FileManager's in v4.4.0, refusing paths outside
> it, and an output port that announces a completed, verified received file, as FileUplink's `fileAnnounce`
> does, so a deployment can act on it.

**F6. Receive ignores the PDU-CRC and large-file flags instead of refusing them**
> With the CRC flag set, the two CRC bytes are taken as file data (`PduHeader.cpp:177`, `FileDataPdu.cpp:144-151`).
> Suggest refusing such PDUs until they are supported. (Read, not run.)

**F7. `SpacePacketDeframer` keeps only the first space packet in a TC frame**
> A TC frame may carry several space packets; Yamcs packs them by default. v4.3.0 forwards the first and drops the
> rest without an event. On a live stack 19 to 36 % of commands sent close together were lost until the ground was
> set to one packet per frame. Suggest deframing every packet, or at least an event for the dropped bytes.

**F8. ApidManager: one counter for both directions on an APID, and an unthrottled event**
> APID 3 carries file traffic both ways, and `validateApidSeqCountIn` (uplink) and `getApidSeqCountIn` (downlink
> stamping) share `m_apidSequences`. With both directions active, or any uplink loss, every packet can log
> `UnexpectedSequenceCount`: 486 WARNING_LO events in one 4 MB upload at 5 % loss. Suggest separate counters per
> direction and a throttle.

**F9. `FileHandlingCfdp` SDD**
> `docs/sdd.md` shows `ComCcsds` CFDP ports that do not exist at v4.3.0, says received files are staged in
> `tmp_dir` (they are written at the destination), and gives timer defaults that differ from
> `Parameters.fppi`. (Read.)

**nasa/fprime-gds**

**G1. `fprime-prm-write dat --defaults` fails on a struct array whose enum default is fully qualified**
> With the v4.3.0 `CfdpManager` dictionary, `--defaults` raises
> `EnumMismatchException: Invalid enum member Fw.Enabled.ENABLED` for `ChannelConfig`: the dictionary gives the
> default as `Fw.Enabled.ENABLED`, and the enum type accepts `ENABLED`. Writing the value explicitly as `ENABLED`
> works.

**G2. A parameter file next to the binary stops the launcher finding the app**
> `PrmDb` opens a relative `PrmDb.dat` in its working directory, which the launcher sets to the binary's own
> directory (`run_deployment.py`, `launch_app`, `cwd=app_path.parent`). With `PrmDb.dat` there, `find_app`
> (`executables/utils.py:170-189`) sees two files and exits: "Multiple app candidates ... Specify app manually
> with --app". Suggest ignoring non-executable files when guessing.

**fprime-community/fprime-yamcs**

**Y1. The instance YAML is written back with sorted keys, which breaks `streamConfig.sqlFile`**
> `__main__.py` rewrites the instance configuration with `yaml.safe_dump(...)`, which sorts keys by default
> (lines 501 and 561 in 0.2.1). Yamcs creates `streamConfig` entries in order, so an `sqlFile` whose SQL reads
> `tm_realtime` now runs before that stream exists and Yamcs fails with `RESOURCE_NOT_FOUND`. One-word fix:
> `sort_keys=False`. Any order-sensitive Yamcs config is affected; CFDP through stream SQL is how this surfaced.

**Y2. `${FPRIME_*}` placeholders in the shipped instance YAML read Java system properties, not the environment**
> Yamcs's `${NAME}` reads system properties; environment variables need `${env.NAME}`. (Found in Stage 1.)

**Y3. An upload's COMPLETED means sent, not received**
> `FprimeFilePacketService` marks an upload COMPLETED once its last packet is queued; F´ file packets have no
> acknowledgement, so the ground cannot know the file arrived. Worth saying in the UI and docs. (Stage 1.)

**Y4. Feature: CFDP to `Svc/Ccsds/CfdpManager`**
> Yamcs's own `CfdpService` reaches F´ with four lines of stream SQL (`ground/yamcs/etc/cfdp_streams.sql` in
> DoomSat: APID 3, descriptor `0x0003`, CFDP version bits) and a second TC virtual channel. Shipping that as an
> option, with Y1 fixed, would give fprime-yamcs users reliable file transfer. Its `SendFile` discovery should
> then not pick up `cfdpManager.SendFile`. (DoomSat ran it; the discovery point is read.)

**yamcs/yamcs**

**YA1. `CfdpOutgoingTransfer`: `eofAckReceived` is never set**
> The field is declared false (`CfdpOutgoingTransfer.java:93`) and only read (lines 302, 410, 422), so the
> sender's inactivity timer never arms after the EOF is acknowledged. A Class 2 upload whose FINs are all lost
> stays RUNNING. (Read, 5.12.8.)

**YA2. The `CfdpService` documentation and its Spec disagree**
> The docs list options the Spec rejects at startup (for example `maxFileSize`), and give different names or
> defaults for `eofAckTimeout`, `finAckTimeout` and `allowConcurrentFileOverwrites`. (Read, 5.12.8.)

### What is in the branch

- **Flight**: `cfdpManager`, `fileManager`, `prmDb` and a dedicated 96 × 1024 B buffer pool, declared in
  `flight/DoomSat/Top/instances.fpp` (queue 200; async inputs assert when the queue is full) and wired in
  `topology.fpp` to the com queue's FILE slot, the router's `fileOut`, the 1 Hz group and `dpCat`.
  `DoomSatTopologyDefs.hpp` is committed because the generated one names FileHandling. `MaxPduSize` 1001.
  `COMMIT_WAD` in the Doom component. Boot parameters in `flight/config/PrmDb.json`.
- **Ground**: `CfdpService` in `ground/yamcs/etc/yamcs.fprime-project.yaml` with TC virtual channel 2,
  `cfdp_streams.sql`, `ground/yamcs/launch.py`.
- **Tools**: `tools/lossy_relay.py`; `tools/wad_uplink_demo.py` uses CFDP Class 2 when Yamcs offers it, with
  `--cfdp 1|2`, `--pdu-delay`, `--tries`.
- **Tests**: `tests/test_cfdp_spike.py` (YAML, SQL, PDU sizes, boot parameters, `COMMIT_WAD`, the launcher, the
  relay) and the demo's CFDP and lossy paths in `tests/test_wad_uplink_demo.py`.
- **Docs**: README "Uplink a new level", `docs/ARCHITECTURE.md`, CLAUDE.md.

### Not done

- The guard component (Destination paths, 2), and running the flight side as a confined user.
- Downlink pacing above 64 PDUs a tick, and its effect on game frames, measured.
- The dashboard's `LOAD_WAD` button does not resend on a lossy link, and the dashboard has no upload.
- COP-1 for commands.
- Charter: no edit. The suggested note goes in the pull request.

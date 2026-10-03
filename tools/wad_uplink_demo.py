# /// script
# requires-python = ">=3.10"
# dependencies = ["yamcs-client==2.1.0"]
# ///
"""Send a level to the running spacecraft and switch the game to it, printing the evidence as it goes.

    ground/.venv/bin/python tools/wad_uplink_demo.py --wad PATH [--iwad NAME] --map MAP
    uv run tools/wad_uplink_demo.py ...                      (the same, with the PEP 723 header above)

1. The file goes into a Yamcs bucket, then up the command link through the F' file transfer service
   (fprime-yamcs's FprimeFilePacketService: Fw::FilePacket Start, Data, End on APID 3) to
   <remote-dir>/NAME.<nonce>.part on the spacecraft.
2. FileUplink checks the checksum (FileReceived); the Doom component then renames it to NAME (WadUplinked).
   Only that event says the file is there whole: the service calls an upload complete once it has SENT it.
3. LOAD_WAD; the payload proves the game starts on it in a child process, then switches (WadLoaded) or keeps
   flying what it had (WadLoadFailed). The tool shows WAD_IWAD / WAD_PWAD / WAD_LOADS, EPISODE and
   FRAMES_SENT, and saves the first whole frame from after the switch in out/.

What is loaded: with --iwad, the uplinked file is the PWAD over that IWAD; without, it is the IWAD. With no
--wad, nothing is uplinked and LOAD_WAD names files already on board (--iwad, --pwad).

Negative cases: --truncate N sends only the first N bytes (the game must refuse it); --load-early issues
LOAD_WAD while the file is still arriving (it must be refused, and the uplink still completes);
--expect-fail makes a refusal the success. --latency N measures CONTROL round trips (command up, the onboard
CMDS_RECEIVED count back down) N times before and N times during the uplink: run it without a pilot, whose
own commands would be counted too.

Uplinked-WAD flights are demonstrations. They are never benched or graded: the dev set is Freedoom Phase 1
and the test set the shareware episode (charter 2.5), and this tool is not part of either.
"""
import argparse
import base64
import os
import posixpath
import statistics
import struct
import sys
import threading
import time
from pathlib import Path

from yamcs.client import YamcsClient
from yamcs.client.core.exceptions import YamcsError

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "payload"))
import wad_uplink as wu  # noqa: E402  the payload's own name rules, so a bad name fails before the uplink
# Yamcs 5.12.8 caps a bucket upload over HTTP at the larger of the server's maxContentLength and
# UploadObject's own 5 MiB (buckets.proto). ground/yamcs/etc/yamcs.yaml raises the first to 32 MiB, room for a
# whole IWAD; the multipart wrapping takes a little of it.
BUCKET_UPLOAD_MAX = 32 * 1024 * 1024 - 64 * 1024
DOOM = "/DoomSat_DoomSat/DoomSat/doom"
CHUNK_HEADER = struct.Struct("!IHHH")   # seq, index, count, length: the FrameChunk header (Doom.fpp)
WATCHED = ["WAD_IWAD", "WAD_PWAD", "WAD_LOADS", "EPISODE", "FRAMES_SENT", "CMDS_RECEIVED", "PAYLOAD_LINK",
           "FRAME_CHUNK"]


def say(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def wad_name(value):
    """WAD_IWAD / WAD_PWAD: zero-padded ASCII bytes (F' string telemetry does not survive fprime-xtce)."""
    if isinstance(value, str):
        value = base64.b64decode(value)
    return bytes(value or b"").rstrip(b"\0").decode("ascii", "replace")


class Link:
    """Events and the watched channels, live, from one Yamcs connection."""

    def __init__(self, url, instance):
        self.client = YamcsClient(url)
        self.instance = instance
        self.processor = self.client.get_processor(instance, "realtime")
        self.lock = threading.Condition()
        self.events = []                # (arrival time, message)
        self.values = {}                # channel -> (arrival time, value)
        self.counts = {}                # channel -> updates seen
        self.partial, self.frames = {}, []   # frame reassembly: seq -> parts; whole frames (time, seq, jpeg)
        self.client.create_event_subscription(instance, on_data=self._on_event)
        self.processor.create_parameter_subscription([f"{DOOM}/{c}" for c in WATCHED], on_data=self._on_data)

    def _on_event(self, event):
        with self.lock:
            self.events.append((time.time(), event.message or ""))
            self.lock.notify_all()

    def _on_data(self, data):
        with self.lock:
            for pv in data.parameters:
                name = pv.name.rsplit("/", 1)[-1]
                if name == "FRAME_CHUNK":
                    self._chunk(pv.eng_value)
                    continue
                v = pv.eng_value
                if name in ("WAD_IWAD", "WAD_PWAD"):
                    v = wad_name(v)
                self.values[name] = (time.time(), v)
                self.counts[name] = self.counts.get(name, 0) + 1
            self.lock.notify_all()

    def _chunk(self, value):
        try:
            d = dict(value)
            data = d["data"] if isinstance(d["data"], (bytes, bytearray)) else base64.b64decode(d["data"])
            seq, index, count, length = int(d["seq"]), int(d["index"]), int(d["count"]), int(d["length"])
        except (KeyError, TypeError, ValueError):
            return
        if seq & 0x80000000:
            return   # the payload's own map, not a frame
        frame = self.partial.setdefault(seq, {})
        frame[index] = bytes(data[:length])
        if len(frame) == count:
            self.frames.append((time.time(), seq, b"".join(frame[i] for i in range(count))))
            del self.partial[seq]
            for old in [s for s in self.partial if s < seq - 50]:
                del self.partial[old]

    def value(self, name):
        with self.lock:
            return self.values.get(name, (0, None))[1]

    def wait_event(self, since, *needles, timeout=60.0):
        """The first event after `since` whose message contains any needle; None on timeout."""
        end = time.time() + timeout
        with self.lock:
            while True:
                for t, msg in self.events:
                    if t >= since and any(n in msg for n in needles):
                        return msg
                left = end - time.time()
                if left <= 0:
                    return None
                self.lock.wait(min(left, 0.5))

    def wait(self, predicate, timeout):
        end = time.time() + timeout
        with self.lock:
            while not predicate():
                left = end - time.time()
                if left <= 0:
                    return False
                self.lock.wait(min(left, 0.5))
            return True

    def command(self, name, **args):
        return self.processor.issue_command(f"{DOOM}/{name}", args=args)


def control_round_trip(link, owed=0, timeout=5.0):
    """Seconds from issuing CONTROL to the onboard command count arriving back on the ground one higher.

    `owed` is how many earlier commands timed out and may still arrive: their counts must not be taken for
    this one's. Returns (seconds or None, owed after this sample).
    """
    count = lambda: link.values.get("CMDS_RECEIVED", (0, None))[1]
    with link.lock:
        before = count()
    if before is None:
        return None, owed
    t0 = time.time()
    link.command("CONTROL", move=0, strafe=0, turn=0.0, fire=False, use=False, weapon="FIST")
    ok = link.wait(lambda: (count() or 0) >= before + owed + 1, timeout)
    return (time.time() - t0, 0) if ok else (None, owed + 1)


def latency_summary(label, samples):
    got = [s for s in samples if s is not None]
    if not got:
        return f"{label}: no answers in {len(samples)} tries"
    got.sort()
    return (f"{label}: median {statistics.median(got) * 1000:.0f} ms, min {got[0] * 1000:.0f}, "
            f"max {got[-1] * 1000:.0f} ms over {len(got)}/{len(samples)} answered")


def measure_latency(link, n, label, gap=0.5, stop=None):
    samples, owed = [], 0
    for _ in range(n):
        if stop is not None and stop():
            break
        rtt, owed = control_round_trip(link, owed)
        samples.append(rtt)
        time.sleep(gap)
    say(latency_summary(label, samples))
    return samples


def flight_home():
    """Where the flight side is installed, found the way scripts/common.sh finds it: the environment, then the
    repo's .env, then ~/doom. On a Windows ground with the flight side in WSL, give --remote-dir instead."""
    if os.environ.get("DOOMSAT_HOME"):
        return os.environ["DOOMSAT_HOME"]
    env = ROOT / ".env"
    if env.is_file():
        for line in env.read_text(encoding="utf-8", errors="replace").splitlines():
            k, _, v = line.partition("=")
            if k.strip() == "DOOMSAT_HOME" and v.strip().strip("'\""):
                return v.strip().strip("'\"")
    return os.path.expanduser("~/doom")


def main():
    home = flight_home()
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--wad", help="local WAD file to uplink (none: load files already on board)")
    p.add_argument("--as", dest="name", help="its name on the spacecraft (default: its own file name)")
    p.add_argument("--iwad", help="the IWAD to load; with --wad, the uplinked file becomes the PWAD over it")
    p.add_argument("--pwad", default="", help="with no --wad: a PWAD already on board")
    p.add_argument("--map", help="the map to start on (required unless --no-load)")
    p.add_argument("--no-load", action="store_true", help="uplink only")
    p.add_argument("--truncate", type=int, help="uplink only the first N bytes (a negative case)")
    p.add_argument("--load-early", action="store_true", help="issue LOAD_WAD while the file is still arriving")
    p.add_argument("--expect-fail", action="store_true", help="succeed only if LOAD_WAD is refused")
    p.add_argument("--latency", type=int, default=0, help="CONTROL round trips to time before and during the uplink")
    p.add_argument("--latency-gap", type=float, default=0.5,
                   help="seconds between round trips during the uplink (spread them over it; default %(default)s)")
    p.add_argument("--remote-dir", default=posixpath.join(home, "wads", "uplink"),
                   help="the uplink directory on the spacecraft, as an absolute path (default %(default)s)")
    p.add_argument("--yamcs", default="localhost:8090")
    p.add_argument("--instance", default="fprime-project")
    p.add_argument("--service", default="FprimeFilePacketService")
    p.add_argument("--bucket", default="wadUplink")
    p.add_argument("--out", default=str(ROOT / "out"), help="where the frame from after the switch goes")
    a = p.parse_args()
    if not a.no_load and not a.map:
        p.error("--map is required (or --no-load)")
    if not a.wad and not a.iwad:
        p.error("give --wad (to uplink) and/or --iwad (to load)")
    if a.wad and a.truncate is not None and not a.name:
        a.name = "trunc-" + os.path.basename(a.wad)   # never under the real name, where it would shadow the good copy
    name = (a.name or os.path.basename(a.wad)) if a.wad else None
    # The payload's own rules, before anything goes up (an uplink can take minutes); --expect-fail is for
    # sending bad names on purpose, so the payload can be seen refusing them
    if not a.expect_fail:
        loading = (a.iwad, name) if a.wad and a.iwad and a.iwad != name else ((name, "") if a.wad else (a.iwad, a.pwad))
        problem = ((name and wu.name_problem(name, "uplinked file")) or (a.iwad and wu.name_problem(a.iwad, "IWAD"))
                   or (loading[1] and wu.name_problem(loading[1], "PWAD")) or (not a.no_load and wu.map_problem(a.map)))
        if problem:
            p.error(problem)

    link = Link(a.yamcs.replace("http://", ""), a.instance)
    if not link.wait(lambda: "CMDS_RECEIVED" in link.values and "WAD_IWAD" in link.values, 15):
        say("no telemetry from the spacecraft within 15 s: is the flight side up (scripts/flight.sh status)?")
        return 2
    say(f"on board now: {link.value('WAD_IWAD')!r} {link.value('WAD_PWAD')!r}, loads {link.value('WAD_LOADS')}, "
        f"episode {link.value('EPISODE')}, frames sent {link.value('FRAMES_SENT')}")

    if a.latency:
        measure_latency(link, a.latency, "CONTROL round trip, link idle")

    name = None
    if a.wad:
        content = Path(a.wad).read_bytes()          # ground tooling: the bytes go into the bucket, nothing more
        if a.truncate is not None:
            content = content[:a.truncate]
        if len(content) > BUCKET_UPLOAD_MAX:
            say(f"{a.wad} is {len(content)} bytes; Yamcs takes at most {BUCKET_UPLOAD_MAX} in one bucket upload "
                "over HTTP (maxContentLength in ground/yamcs/etc/yamcs.yaml), so it cannot be uplinked this way")
            return 2
        part = f"{name}.{time.time_ns() // 1000000}.part"
        remote = a.remote_dir.rstrip("/") + "/" + part
        storage = link.client.get_storage_client()
        if a.bucket not in [b.name for b in storage.list_buckets()]:
            storage.create_bucket(a.bucket)
        bucket = storage.get_bucket(a.bucket)
        try:
            bucket.upload_object(part, content, content_type="application/octet-stream")
        except YamcsError as e:
            say(f"Yamcs would not take {len(content)} bytes into bucket {a.bucket}: {e}. A bucket upload is capped at "
                "maxContentLength (ground/yamcs/etc/yamcs.yaml) and the bucket at 100 MiB / 1000 objects")
            return 2
        service = link.client.get_file_transfer_client(a.instance).get_service(a.service)
        updates = service.create_transfer_subscription()
        t0 = time.time()
        transfer = service.upload(a.bucket, part, remote)
        say(f"uplink started: {len(content)} bytes -> {remote} (transfer {transfer.id})")

        def refresh():   # a Transfer is a snapshot: the subscription has the live one
            return updates.get_transfer(transfer.id) or transfer

        if a.load_early and not a.no_load:
            for _ in range(60):
                transfer = refresh()
                if transfer.transferred_size > 0 or transfer.is_complete():
                    break
                time.sleep(0.5)
            iwad, pwad = (a.iwad, name) if a.iwad and a.iwad != name else (name, "")
            say(f"LOAD_WAD {iwad} {pwad!r} {a.map} while the uplink is at {transfer.transferred_size}/{len(content)} bytes")
            t_cmd = time.time()
            link.command("LOAD_WAD", iwad=iwad, pwad=pwad, map=a.map)
            said = link.wait_event(t_cmd, "[WadLoaded]", "[WadLoadFailed]", timeout=40)
            say(f"event: {said}")
            if not said or "[WadLoadFailed]" not in said:
                say("FAIL: a load during the uplink was not refused")
                return 1

        stop = threading.Event()
        lat = []
        if a.latency:
            th = threading.Thread(target=lambda: lat.extend(
                measure_latency(link, a.latency, "CONTROL round trip, during the uplink", gap=a.latency_gap,
                                stop=stop.is_set)), daemon=True)
            th.start()
        budget = len(content) / 15000 + 60     # ~25 KB/s at 512-byte chunks, with room
        last = 0
        while not transfer.is_complete() and time.time() - t0 < budget:
            time.sleep(1)
            transfer = refresh()
            if time.time() - last > 15:
                last = time.time()
                say(f"  uplink {transfer.state}: {transfer.transferred_size}/{len(content)} bytes, "
                    f"{transfer.transferred_size / max(time.time() - t0, 1e-3) / 1000:.1f} KB/s")
        stop.set()
        if a.latency:
            th.join(timeout=30)
        if not transfer.is_success():
            say(f"FAIL: transfer {transfer.state} {transfer.error or ''}")
            return 1
        sent = time.time() - t0
        say(f"all packets sent in {sent:.1f} s ({len(content) / sent / 1000:.1f} KB/s); waiting for the spacecraft")
        said = link.wait_event(t0, f"[WadUplinked] Uplinked WAD ready to load: {a.remote_dir.rstrip('/')}/{name}",
                               "[BadChecksum]", "[WadUplinkFailed]", "[FileWriteError]", "[FileOpenError]", timeout=30)
        received = link.wait_event(t0, "[FileReceived]", timeout=1)
        say(f"event: {received}")
        say(f"event: {said}")
        try:
            bucket.delete_object(part)   # it has gone up; a bucket holds 1000 objects and 100 MiB
        except YamcsError:
            pass
        if said and "[BadChecksum]" in said:
            say("FAIL: the file arrived damaged. F' file packets are not retransmitted, so one packet lost on the "
                f"command link fails the whole file; it stays a .part and cannot be loaded. Uplink it again")
            return 1
        if not said or "[WadUplinked]" not in said:
            say("FAIL: the spacecraft did not confirm the file")
            return 1

    if a.no_load:
        return 0
    if a.wad:
        iwad, pwad = (a.iwad, name) if a.iwad and a.iwad != name else (name, "")
    else:
        iwad, pwad = a.iwad, a.pwad
    episode0, frames0, loads0 = link.value("EPISODE"), link.value("FRAMES_SENT"), link.value("WAD_LOADS")
    say(f"LOAD_WAD iwad={iwad} pwad={pwad!r} map={a.map}")
    t_cmd = time.time()
    link.command("LOAD_WAD", iwad=iwad, pwad=pwad, map=a.map)
    said = link.wait_event(t_cmd, "[WadLoaded]", "[WadLoadFailed]", timeout=40)
    say(f"event: {said}")
    if said is None:
        say("FAIL: no WadLoaded or WadLoadFailed within 40 s")
        return 1
    failed = "[WadLoadFailed]" in said
    # The old game flies on while the new WAD is proven, and the event can overtake that game's last frame
    # or two on the way down (events and frames queue separately): skip those.
    with link.lock:
        seq0 = max([s for _, s, _ in link.frames], default=-1) + 2
    time.sleep(3)   # the once-a-second WAD channels, and some frames on the new game
    say(f"telemetry: WAD_IWAD={link.value('WAD_IWAD')!r} WAD_PWAD={link.value('WAD_PWAD')!r} "
        f"WAD_LOADS {loads0} -> {link.value('WAD_LOADS')}, EPISODE {episode0} -> {link.value('EPISODE')}, "
        f"FRAMES_SENT {frames0} -> {link.value('FRAMES_SENT')}")
    if failed:
        if a.expect_fail:
            say("OK: refused, as expected; the game carries on with what it had")
            return 0
        say("FAIL: LOAD_WAD was refused")
        return 1
    if a.expect_fail:
        say("FAIL: LOAD_WAD was expected to be refused")
        return 1
    if not link.wait(lambda: any(s > seq0 for _, s, _ in link.frames), 10):
        say("FAIL: no whole frame from after the switch within 10 s")
        return 1
    with link.lock:
        t, seq, jpeg = next((t, s, j) for t, s, j in link.frames if s > seq0)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"wad_uplink_{(pwad or iwad).replace('.wad', '')}_{a.map}.jpg"
    path.write_bytes(jpeg)
    say(f"frame {seq} from after the switch: {path} ({len(jpeg)} bytes)")
    say("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())

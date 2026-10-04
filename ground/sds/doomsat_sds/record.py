"""Phase D, the file seam: ask the payload for its own record of an episode, downlink it, check it against L1.

    plan      which file to ask for and what to call it on the ground
    request   the one command the data system ever sends: FileDownlink SendFile, through Yamcs
    ingest    the mirrored file becomes the L0 product `l0_record`
    compare   the record against the L1 episode built from the archive; every disagreement is a finding

The payload (payload/episode_record.py, with --records on) writes $DOOMSAT_HOME/run/rec/<episode>-<last tic>.json
when an episode ends. The ground knows both numbers without asking: the episode from L1, the last tic from the
PlayerDied/LevelFinished event (or, after a reset, from the last TIC sample, which only works if that status
reached the ground). FprimeFilePacketService puts the downlinked file in the `fprimeFilesIn` bucket and mirrors it
to $FPRIME_DOWNLINK_DIR ($DOOMSAT_HOME/run/downlink); the mirror cannot make subdirectories, so names are flat.

Two limits from the flight software shape the paths:
- F' command strings hold at most 39 characters (FW_CMD_STRING_MAX_SIZE = 40), although the dictionary says 100;
  a longer path is a FORMAT_ERROR on board. The absolute path is used when it fits, otherwise the same file
  relative to the F' binary's working directory, which is build-artifacts/<OS>/DoomSat/bin.
- SendFile answers OK even when it cannot open the file; the FileSent / FileOpenError events say what happened.
"""
from __future__ import annotations

import json
from pathlib import Path

from . import config, products
from .archive import iso
from .catalog import Catalog
from .config import Settings
from .store import episode_product_path, read_json

MAX_CMD_STRING = 39
BIN_TO_HOME = "../../../../../"     # bin -> DoomSat -> <OS> -> build-artifacts -> DoomSat (project) -> $DOOMSAT_HOME
DOWNLINK_EVENTS = tuple("FileHandling.fileDownlink." + n for n in (
    "SendStarted", "FileSent", "FileOpenError", "FileReadError", "SendDataFail", "DownlinkZeroSizeFile",
    "SourceOutOfSandbox", "FilenameSourceOverflow", "FilenameDestinationOverflow", "DownlinkCanceled"))


def record_name(number: int, last_tic: int) -> str:
    return "%d-%d.json" % (number, last_tic)


def source_path(settings: Settings, name: str) -> str:
    absolute = str(settings.doomsat_home / "run" / "rec" / name)
    if len(absolute) <= MAX_CMD_STRING:
        return absolute
    relative = BIN_TO_HOME + "run/rec/" + name
    if len(relative) <= MAX_CMD_STRING:
        return relative
    raise ValueError("no path to %s fits F' command strings (%d characters)" % (name, MAX_CMD_STRING))


def dest_name(episode_id: str) -> str:
    return "rec_%s.json" % episode_id


def last_tic(l1: dict) -> int | None:
    """The episode's final tic as the ground knows it: the closing event's, else the last TIC sample's."""
    for e in reversed(l1["events"]):
        if e["type"].endswith((".PlayerDied", ".LevelFinished")) and "tic" in e["extra"]:
            return int(e["extra"]["tic"])
    tic = products.column(l1, "TIC")
    return tic[-1][1] if tic else None


def plan(settings: Settings, catalog: Catalog, episode_id: str) -> dict:
    l1row = catalog.current(episode_id, "l1_episode")
    if l1row is None:
        raise LookupError("no L1 for %s" % episode_id)
    l1 = read_json(l1row["path"])
    tic = last_tic(l1)
    if tic is None:
        raise LookupError("%s has no TIC to name its record by" % episode_id)
    name = record_name(l1["episode"]["number"], tic)
    dest = dest_name(episode_id)
    if len(dest) > MAX_CMD_STRING:
        raise ValueError("destination name too long for F': " + dest)
    return {"episode_id": episode_id, "name": name, "source": source_path(settings, name), "dest": dest,
            "mirror_path": str(settings.downlink / dest), "l1_product_id": l1row["product_id"]}


def request(settings: Settings, p: dict) -> dict:
    """Send SendFile(source, dest) through Yamcs. The only command the SDS sends."""
    from yamcs.client import YamcsClient
    client = YamcsClient(settings.yamcs)
    try:
        cmd = client.get_processor(settings.instance, config.PROCESSOR).issue_command(
            config.SENDFILE, args={"sourceFileName": p["source"], "destFileName": p["dest"]},
            comment="doomsat-sds: record of episode %s" % p["episode_id"])
        return {"command_id": cmd.id, "command": config.SENDFILE, "issued_utc": iso(int(cmd.generation_time.timestamp() * 1000)),
                "issued_ms": int(cmd.generation_time.timestamp() * 1000), "args": {"sourceFileName": p["source"],
                                                                                 "destFileName": p["dest"]}}
    finally:
        client.close()


def downlink_events(archive, since_ms: int, until_ms: int) -> list[dict]:
    """What FileDownlink said about the request, from the archived events (a SendFile response is OK regardless)."""
    return [{"utc": iso(e["t"]), "type": e["type"].rsplit(".", 1)[-1], "message": e["message"]}
            for e in archive.events(since_ms, until_ms, types=list(DOWNLINK_EVENTS))]


def ingest(settings: Settings, catalog: Catalog, p: dict, command: dict, run_id: str | None = None) -> dict:
    """The mirrored file, checked to be the payload's record, becomes L0 `l0_record`."""
    from .pipeline import _register
    data = Path(p["mirror_path"]).read_bytes()
    rec = json.loads(data)
    if rec.get("record", {}).get("format") != "doomsat-episode-record":
        raise ValueError("%s is not an episode record" % p["mirror_path"])
    ver = products.version("l0_record")
    eid = p["episode_id"]
    return _register(settings, catalog, product_type="l0_record", episode_id=eid, product_id=f"{eid}/l0_record@{ver}",
                     path=episode_product_path(settings, eid, "l0_record", ver, "json"), data=data,
                     inputs=[{"source": "downlink", "mirror": p["mirror_path"], "bucket": "fprimeFilesIn/" + p["dest"],
                              "on_board": p["source"], "command_id": command.get("command_id")}], run_id=run_id)


def _check(name: str, record_value, archive_value, agree, note: str = "") -> dict:
    return {"check": name, "record": record_value, "archive": archive_value, "agree": agree, "note": note}


def compare(record: dict, l1: dict, context: dict | None = None) -> dict:
    """Every way the payload's record and the archive's L1 can be held against each other. Pure.

    `agree` is True, False, or None where the two cannot be compared (say, a context the ground never learnt).
    A False is a disagreement worth a person's attention; the counts of statuses sent and received are a
    measurement and only disagree if the ground somehow received more than was sent.
    """
    ep = l1["episode"]
    tic = products.column(l1, "TIC")
    last = lambda name: (products.column(l1, name) or [(None, None)])[-1][1]
    fin = record["final"]
    checks = [
        _check("episode number", record["episode"], ep["number"], record["episode"] == ep["number"]),
        _check("outcome", record["outcome"], ep["outcome"], record["outcome"] == ep["outcome"]),
        _check("last tic (closing event)", record["last_tic"], last_tic(l1), record["last_tic"] == last_tic(l1)),
        _check("last tic (last TIC sample)", record["last_tic"], last("TIC"), record["last_tic"] == last("TIC"),
               "a mismatch means the final status never reached the archive"),
        _check("kills", fin["kills"], last("KILLS"), fin["kills"] == last("KILLS")),
        _check("cells explored", fin["explored"], last("EXPLORED_CELLS"), fin["explored"] == last("EXPLORED_CELLS")),
        _check("final health", fin["health"], last("HEALTH"), fin["health"] == last("HEALTH")),
        _check("final position", [fin["x"], fin["y"]], [last("POS_X"), last("POS_Y")],
               [fin["x"], fin["y"]] == [last("POS_X"), last("POS_Y")]),
        _check("lowest health", record["health_min"], min((v for _, v in products.column(l1, "HEALTH")), default=None),
               record["health_min"] <= min((v for _, v in products.column(l1, "HEALTH")), default=record["health_min"]),
               "the archive can only miss a low point, never invent one"),
    ]
    first_archived = tic[0][1] if tic else None
    checks.append(_check("first tic", record["first_tic"], first_archived, record["first_tic"] == first_archived,
                         "the archive starts later when statuses were sent before Yamcs was listening"))
    received = len({v for _, v in tic})
    sent = record["statuses_sent"]
    checks.append(_check("statuses sent vs received", sent, received, received <= sent,
                         "%d of %d statuses reached the archive (%.1f%%)" % (received, sent, 100.0 * received / sent)
                         if sent else ""))
    # Statuses go out on a fixed cadence of tics, so the ones sent while the archive was listening can be counted
    # from the record's first and last tic; that separates loss on the link from a late start of the archive.
    cadence = (record["last_tic"] - record["first_tic"]) / (sent - 1) if sent > 1 else None
    listening = None
    if cadence and first_archived is not None:
        listening = int(round((record["last_tic"] - first_archived) / cadence)) + 1
        checks.append(_check("statuses while the archive was listening", listening, received, received <= listening,
                             "%d of about %d statuses sent from tic %d on reached the archive (%.1f%%)" % (
                                 received, listening, first_archived, 100.0 * received / listening)))
    # The record's path, a position per second of game time, against the archived position at the same tic.
    cols = l1["telemetry"]["columns"]
    it, ix, iy = cols.index("TIC"), cols.index("POS_X"), cols.index("POS_Y")
    at_tic = {r[it]: (r[ix], r[iy]) for r in l1["telemetry"]["rows"] if r[it] is not None and r[ix] is not None
              and r[iy] is not None}
    matched = [(t, x, y, at_tic[t]) for t, x, y in record["path"] if t in at_tic]
    off = [abs(x - a[0]) + abs(y - a[1]) for _, x, y, a in matched]
    in_window = sum(1 for t, _, _ in record["path"] if first_archived is not None and t >= first_archived)
    checks.append(_check("path points", len(record["path"]), len(matched), all(d == 0 for d in off) if off else None,
                         "%d of %d record positions (%d after the archive began) have an archived sample at the same "
                         "tic; largest difference %s units" % (len(matched), len(record["path"]), in_window,
                                                                max(off) if off else None)))
    ctx = context or {}
    for key in ("wad", "map", "skill", "seed"):
        mine = ctx.get(key)
        theirs = record["context"].get(key)
        checks.append(_check("context " + key, theirs, mine, None if mine is None else theirs == mine,
                             "the ground never learnt it; the record supplies it" if mine is None else ""))
    disagreements = [c for c in checks if c["agree"] is False]
    return {"product": {"type": "qa_record_check", "level": "QA", "version": products.version("qa_record_check")},
            "episode_id": ep["id"], "agree": not disagreements, "disagreements": len(disagreements),
            "statuses": {"sent": sent, "received": received,
                         "delivery": round(received / sent, 4) if sent else None,
                         "sent_while_archiving": listening,
                         "delivery_while_archiving": round(received / listening, 4) if listening else None},
            "checks": checks}


def compare_and_register(settings: Settings, catalog: Catalog, p: dict, l0_ref: dict, run_id: str | None = None) -> dict:
    from .pipeline import _register
    from .store import canonical_json
    record = read_json(l0_ref["path"])
    l1row = catalog.current(p["episode_id"], "l1_episode")
    l1 = read_json(l1row["path"])
    ep = catalog.episode(p["episode_id"])
    result = compare(record, l1, ep["context"] if ep else None)
    ver = products.version("qa_record_check")
    eid = p["episode_id"]
    ref = _register(settings, catalog, product_type="qa_record_check", episode_id=eid,
                    product_id=f"{eid}/qa_record_check@{ver}",
                    path=episode_product_path(settings, eid, "qa_record_check", ver, "json"),
                    data=canonical_json(result), run_id=run_id,
                    inputs=[{"product_id": l0_ref["product_id"], "sha256": l0_ref["sha256"]},
                            {"product_id": l1row["product_id"], "sha256": l1row["sha256"]}])
    if ref["status"] != "unchanged":
        for c in result["checks"]:
            if c["agree"] is False:
                catalog.add_finding("record_disagrees", c, episode_id=eid, product_id=ref["product_id"])
    ref.update(agree=result["agree"], disagreements=result["disagreements"], statuses=result["statuses"])
    return ref

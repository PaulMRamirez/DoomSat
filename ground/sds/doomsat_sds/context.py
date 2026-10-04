"""The context an episode was flown in: WAD, map, skill, seed, pilot mode, repo commit, dev or test set.

None of it is in telemetry (LEVEL counts levels started; it is not a map name). It exists only in the running
payload's arguments, in payload.log ("episode N started on MAP (level L)") and in the pilot's arguments. So it
is captured once, when the forward run processes the episode, and kept in the catalog; reprocessing reuses it.

Each value says where it came from. A process that started after the episode began did not fly it, and then
the value is "unknown" rather than a guess, and so is a pilot that is not running when the context is captured
(it may have exited after flying the episode). A downlinked payload record, where there is one, carries the
payload's own account of WAD, map and skill as a cross-check.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from .config import Settings

LOG_START = re.compile(r"^\[payload\] episode (\d+) started on (\S+) \(level (\d+)\)")
PAYLOAD_DEFAULTS = {"--wad": "doom1.wad", "--map": "E1M1", "--skill": "2", "--seed": "7",   # doom_payload.py argparse
                    "--geometry": "off", "--oracle": "off", "--records": "off"}


def _boot_time() -> float | None:
    try:
        for line in Path("/proc/stat").read_text().splitlines():
            if line.startswith("btime "):
                return float(line.split()[1])
    except OSError:
        pass
    return None


def processes() -> list[dict]:
    """[{pid, argv, start_s}] from /proc (Linux, WSL); empty where there is no /proc."""
    out, boot, hz = [], _boot_time(), os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
    for d in Path("/proc").glob("[0-9]*"):
        try:
            argv = (d / "cmdline").read_bytes().split(b"\0")
            stat = (d / "stat").read_text()
        except OSError:
            continue
        argv = [a.decode("utf-8", "replace") for a in argv if a]
        if not argv:
            continue
        ticks = int(stat.rsplit(")", 1)[1].split()[19])          # field 22, after the parenthesised name
        out.append({"pid": int(d.name), "argv": argv, "start_s": boot + ticks / hz if boot else None})
    return out


def _find(procs: list[dict], script: str) -> dict | None:
    """The interpreter running `script` (not a `bash -c` wrapper that merely mentions it); oldest wins."""
    hits = [p for p in procs if not p["argv"][0].endswith("bash")
            and any(a == script or a.endswith("/" + script) for a in p["argv"][1:])]
    return min(hits, key=lambda p: p["start_s"] or 0) if hits else None


def _options(argv: list[str]) -> dict:
    """--key value, --key=value and bare flags (true). A flag never swallows the option after it; a repeated
    option keeps its last value, as argparse does."""
    opts, i = {}, 0
    while i < len(argv):
        a = argv[i]
        if a.startswith("--"):
            if "=" in a:
                k, v = a.split("=", 1)
                opts[k] = v
            elif i + 1 < len(argv) and not argv[i + 1].startswith("--"):
                opts[a] = argv[i + 1]
                i += 1
            else:
                opts[a] = "true"
        i += 1
    return opts


def map_from_log(log_text: str, number: int) -> tuple[str, int] | None:
    """(map, level) for episode `number` from one payload process's log, or None."""
    hit = None
    for line in log_text.splitlines():
        m = LOG_START.match(line.strip())
        if m and int(m.group(1)) == number:
            hit = (m.group(2), int(m.group(3)))
    return hit


def level_set(repo: Path, wad: str | None, map_name: str | None) -> str:
    """dev, test or other, from research/levels.yaml (read only; it is the harness's file); unknown without both."""
    if not wad or not map_name:
        return "unknown"
    try:
        import yaml
        levels = yaml.safe_load((repo / "research" / "levels.yaml").read_text(encoding="utf-8"))
    except Exception:
        return "unknown"
    for name in ("dev", "test"):
        s = levels.get(name) or {}
        if wad and os.path.basename(wad) == s.get("wad") and map_name in (s.get("maps") or []):
            return name
    return "other"


def repo_commit(repo: Path) -> str | None:
    try:
        head = subprocess.run(["git", "-C", str(repo), "rev-parse", "--short=12", "HEAD"], capture_output=True,
                              text=True, timeout=10).stdout.strip()
        dirty = subprocess.run(["git", "-C", str(repo), "status", "--porcelain", "--untracked-files=no"],
                               capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return (head + ("+dirty" if dirty else "")) or None


def capture(settings: Settings, number: int, start_ms: int, procs: list[dict] | None = None,
            log_text: str | None = None) -> dict:
    """The context of episode `number`, which began at TM time `start_ms`, as far as the live system shows."""
    procs = processes() if procs is None else procs
    started = start_ms / 1000 + 5        # TM time runs ~1 s ahead of the process clock; allow a margin
    ctx: dict = {"sources": {}}

    payload = _find(procs, "doom_payload.py")
    if payload and payload["start_s"] is not None and payload["start_s"] <= started:
        opts = _options(payload["argv"])
        for key in ("--wad", "--skill", "--seed", "--geometry", "--oracle", "--records"):
            ctx[key[2:]] = opts.get(key, PAYLOAD_DEFAULTS.get(key))
        ctx["sources"]["payload"] = "arguments of the running payload (pid %d)" % payload["pid"]
        if log_text is None:
            try:
                log_text = (settings.run_dir / "payload.log").read_text(encoding="utf-8", errors="replace")
            except OSError:
                log_text = ""
        hit = map_from_log(log_text, number)
        if hit:
            ctx["map"], ctx["level"] = hit
            ctx["sources"]["map"] = "payload.log"
        elif opts.get("--map", PAYLOAD_DEFAULTS["--map"]) and number == 1:
            ctx["map"] = opts.get("--map", PAYLOAD_DEFAULTS["--map"])
            ctx["sources"]["map"] = "payload --map (episode 1)"
        else:
            ctx["map"] = None
            ctx["sources"]["map"] = "unknown"
    else:
        for key in ("wad", "skill", "seed", "geometry", "oracle", "records", "map"):
            ctx[key] = None
        ctx["sources"]["payload"] = "unknown: no payload process that was running when the episode began"

    pilot = _find(procs, "pilot.py")
    if pilot and pilot["start_s"] is not None and pilot["start_s"] <= started:
        o = _options(pilot["argv"])
        ctx["pilot_mode"] = "system-one=%s system-two=%s" % (o.get("--system-one", "typesafe"),
                                                             o.get("--system-two", "claude-cli"))
        ctx["sources"]["pilot"] = "arguments of the running pilot (pid %d)" % pilot["pid"]
    elif pilot:
        ctx["pilot_mode"] = None
        ctx["sources"]["pilot"] = "unknown: the running pilot started after the episode began"
    else:
        ctx["pilot_mode"] = None
        ctx["sources"]["pilot"] = ("unknown: no pilot process when the context was captured (it may have exited "
                                   "after flying the episode, or a person drove from the dashboard)")

    for key in ("skill", "seed"):
        if ctx.get(key) is not None:
            try:
                ctx[key] = int(ctx[key])
            except ValueError:
                pass
    ctx["repo_commit"] = repo_commit(settings.repo)
    ctx["level_set"] = level_set(settings.repo, ctx.get("wad"), ctx.get("map"))
    return ctx

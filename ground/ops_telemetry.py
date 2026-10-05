"""The autonomy block the Open MCT GROUND screens read, published from each decision row.

ground/pilot.py hands every decision row to OpsTelemetry.update (from intent_step, and from control_step with
no candidates). It reads only what the pilot already logs, writes only /DoomGround parameters, and nothing in
the loop reads them back: the displays sit downstream of the stack, never upstream of a decision.

Every value is published with an expiry, so a stalled pilot shows as stale (grey) in Open MCT instead of
freezing on its last good number.

tools/build_openmct_replay.py derives the same values from a recorded flight with decision_source and Rolling.
"""
import time
from collections import deque

GROUND = "/DoomGround"
KIND = {"frontier": "FRONTIER", "door": "DOOR", "exit": "EXIT", "key": "KEY", "item": "ITEM",
        "switch": "SWITCH", "enemy": "ENEMY"}


def decision_source(row):
    """Who settled this decision, under the labels of decision_source_t: summary.json's decision_reasons
    (research/frozen_metrics.py), checked in the same order. A rule's answer is never jev's. A jev outage,
    which targeting.decide logs as model "code (fallback)" with the rules' answers, is UNAVAILABLE, as is a
    decision with nothing to ask. Under --system-one code (scripts/play.sh --autopilot, model "code") every
    decision is RULE, where decision_reasons says asked and used."""
    sel = row.get("select") or {}
    model = row.get("model") or ""
    if row.get("unavailable") or model.startswith("code (fallback)"):
        return "UNAVAILABLE"
    if not row.get("answers") or sel.get("fallback") == "no answers":
        return "UNAVAILABLE"            # nothing to ask (no candidates), or nothing came back
    if model == "code":
        return "RULE"
    if sel.get("fallback"):
        return "UNSURE_BAND"
    if sel.get("held"):
        return "HELD"
    if sel.get("gave_up"):
        return "RULE"
    if row.get("cached"):
        return "CACHED"
    return "JEV"


def jev_caused(row):
    """Charter 7's test for an intent change the model caused (frozen_metrics.jev_share), less the rules'
    answers: jev answered, fresh or cached, and code did not overrule it with the unsure band or a hold.
    summary.json's jev_share also credits a fallback's rule answers to the model, so this can read lower."""
    sel = row.get("select") or {}
    return (bool(row.get("answers")) and not sel.get("fallback") and not sel.get("held")
            and not row.get("unavailable") and not (row.get("model") or "").startswith("code"))


class Rolling:
    """JevShare over the last `window` intent changes, a new mode or a new pick as frozen_metrics.intent_changes
    counts them (the first row is not one), and FallbackRate over the last `window` decisions."""
    def __init__(self, window=40):
        self.changes = deque(maxlen=window)     # per intent change: did jev cause it
        self.sources = deque(maxlen=window)     # per decision
        self.prev = None

    def add(self, row):
        src = decision_source(row)
        self.sources.append(src)
        now = (row.get("mode"), row.get("pick"))
        if self.prev is not None and now != self.prev:
            self.changes.append(jev_caused(row))
        self.prev = now
        return src

    def jev_share(self):
        """None until the first intent change: no share yet, rather than a zero that reads as a warning."""
        return sum(self.changes) / len(self.changes) if self.changes else None

    def fallback_rate(self):
        return sum(s != "JEV" for s in self.sources) / len(self.sources) if self.sources else None


class OpsTelemetry:
    def __init__(self, processor, window=40, every_s=1.0, expires_s=3.0):
        self.proc = processor
        self.rolling = Rolling(window)
        self.every_s, self.expires_s = every_s, expires_s
        self.last_pub = 0.0

    def update(self, row, cands):
        src = self.rolling.add(row)
        if time.time() - self.last_pub < self.every_s:
            return
        self.last_pub = time.time()
        sel, ans = row.get("select") or {}, row.get("answers") or {}
        pick = row.get("pick") if isinstance(row.get("pick"), int) else None   # the sector pilot picks a word
        values = {
            # Approximation until the payload reports the tic an intent first took effect (charter 3.4):
            # telemetry age at decision + jev round trip + command issue time.
            "DecisionAgeMs": float((row.get("tel_age_ms") or 0) + (row.get("latency_ms") or 0) + (row.get("cmd_ms") or 0)),
            "IntentMode": row.get("mode"),
            "DecisionSource": src,
            "PickSlot": pick if pick is not None else 255,
            "PickKind": KIND.get(cands[pick]["kind"], "NONE") if pick is not None and pick < len(cands) else "NONE",
            "PickGap": float(sel.get("gap") or 0),
            "PickConfidence": float(sel.get("confidence") or 0),
            "JevShare": self.rolling.jev_share(),
            "FallbackRate": self.rolling.fallback_rate(),
            "GraphVersion": int(row.get("graph_version") or 0),
            "Attempt": int(row.get("episode") or 0),
        }
        for n in range(8):
            v = ans.get(f"g_t{n}")
            values[f"Score{n}"] = float(v) if v not in (None, "") else 0.0
        if "engage" in ans:
            values["EngageAnswer"] = str(ans["engage"])
        for k, v in values.items():
            if v is None:
                continue
            try:
                try:
                    self.proc.set_parameter_value(f"{GROUND}/{k}", v, expires_in=self.expires_s)
                except TypeError:   # yamcs-client without expires_in: publish without the staleness expiry
                    self.proc.set_parameter_value(f"{GROUND}/{k}", v)
            except Exception as e:  # never let a display feed take the loop down
                print(f"[ops] {k}: {e}")

"""The dashboard's LOAD_WAD form on a lossy link: it waits for the spacecraft's answer and sends again if none comes.

The page's own `loadAnswer` (what counts as an answer) runs under node, taken from ground/dashboard/index.html as
it is; the rest is checked as text against the flight software and the payload. No network, no browser, no game:
without node the logic test is skipped.
"""
import json
import os
import re
import shutil
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read().replace("\r\n", "\n")


PAGE = read("ground/dashboard/index.html")


def js_function(name):
    """The page's `function name(...) {...}`, ending at the first line that is just `}`."""
    m = re.search(rf"^function {name}\(.*?^\}}$", PAGE, re.S | re.M)
    if not m:
        raise AssertionError(f"no function {name} in the dashboard")
    return m.group(0)


class TestTheLoadForm(unittest.TestCase):
    def test_it_sends_the_commands_real_arguments(self):
        fpp = read("flight/Components/Doom/Doom.fpp")
        block = re.search(r"async command LOAD_WAD\((.*?)\)\s*opcode", fpp, re.S).group(1)
        wanted = {m.lstrip("$") for m in re.findall(r"^\s*(\$?\w+)\s*:", block, re.M)}
        sent = re.search(r"const args = \{(.*?)\};", PAGE[PAGE.index('$("wadform").addEventListener'):]).group(1)
        self.assertEqual(set(re.findall(r"\b(\w+):", sent)), wanted)

    def test_it_tries_again_and_waits_out_the_payloads_proof(self):
        tries = int(re.search(r"const LOAD_TRIES = (\d+)", PAGE).group(1))
        wait_ms = int(re.search(r"LOAD_ANSWER_MS = (\d+)", PAGE).group(1))
        proof_s = float(re.search(r"^WAD_PROBE_TIMEOUT_S = ([\d.]+)", read("payload/doom_payload.py"), re.M).group(1))
        self.assertGreaterEqual(tries, 2)
        self.assertGreater(wait_ms, proof_s * 1000 + 5000, "a slow refusal must not be taken for no answer")
        handler = PAGE[PAGE.index('$("wadform").addEventListener'):]
        self.assertIn("for (let t = 1; t <= LOAD_TRIES; t++)", handler)
        self.assertIn('issue("LOAD_WAD", args)', handler)


@unittest.skipIf(shutil.which("node") is None, "node is not installed")
class TestWhatCountsAsAnAnswer(unittest.TestCase):
    """`loadAnswer(before, events, tlm, args)` from the page, run under node."""

    ARGS = {"iwad": "freedoom2.wad", "pwad": "basic.wad", "map": "MAP01"}

    def answer(self, seen, loads0, events, tlm):
        script = js_function("loadAnswer") + f"""
const before = {{seen: new Set({json.dumps(seen)}), loads: {json.dumps(loads0)}}};
console.log(JSON.stringify(loadAnswer(before, {json.dumps(events)}, {json.dumps(tlm)}, {json.dumps(self.ARGS)})));
"""
        out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def ev(self, key, message):
        return {"key": key, "message": message}

    def test_already_flying_is_an_answer(self):
        # A repeat of a load whose answer was lost, or a load of what flies: the payload answers WadAlreadyFlying
        msg = "[WadAlreadyFlying] Already flying basic.wad over freedoom2.wad on MAP01: LOAD_WAD changed nothing"
        self.assertEqual(self.answer([], 0, [self.ev("b", msg)], None), msg)
        self.assertIsNone(self.answer(["b"], 0, [self.ev("b", msg)], None), "seen before it was sent: old news")

    def test_it_asks_the_archive_for_every_answer(self):
        fetcher = PAGE[PAGE.index("async function wadEvents()"):PAGE.index("function loadAnswer(")]
        for q in ("WadLoad", "WadAlreadyFlying"):
            self.assertIn(f'"{q}"', fetcher)
        fpp = read("flight/Components/Doom/Doom.fpp")
        for event in ("WadLoaded", "WadLoadFailed"):
            self.assertRegex(fpp, rf"event {event}\(")

    def test_nothing_new_is_no_answer(self):
        old = self.ev("a", "[WadLoaded] Now flying x.wad on E1M1")
        self.assertIsNone(self.answer(["a"], 0, [old], {"iwad": "freedoom1.wad", "pwad": "", "loads": 0}))

    def test_a_new_refusal_or_load_is_the_answer(self):
        for msg in ("[WadLoadFailed] Could not load basic.wad: no", "[WadLoaded] Now flying basic.wad on MAP01"):
            with self.subTest(msg=msg):
                got = self.answer(["a"], 0, [self.ev("b", msg), self.ev("a", "[WadLoaded] old")], None)
                self.assertEqual(got, msg)

    def test_the_earliest_new_event_wins(self):
        events = [self.ev("c", "[WadLoaded] Now flying basic.wad on MAP01"),     # newest first, as the archive
                  self.ev("b", "[WadLoadFailed] Could not load basic.wad: no")]  # returns them
        self.assertEqual(self.answer([], 0, events, None), events[1]["message"])

    def test_other_wad_events_are_not_answers(self):
        up = self.ev("b", "[WadUplinked] Uplinked WAD ready to load: /x/basic.wad")
        self.assertIsNone(self.answer([], 0, [up], None))

    def test_the_channels_stand_in_only_for_this_load(self):
        this = {"iwad": "freedoom2.wad", "pwad": "basic.wad", "loads": 1}
        self.assertIn("event not seen", self.answer([], 0, [], this))
        self.assertIsNone(self.answer([], 0, [], dict(this, iwad="other.wad")), "another load's count")
        self.assertIsNone(self.answer([], 1, [], this), "the count has not moved")
        self.assertIsNone(self.answer([], None, [], this), "no count from before: the channels prove nothing")


if __name__ == "__main__":
    unittest.main()

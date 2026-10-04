"""Phase D on the ground: naming the payload's record, and holding it against the L1 built from the archive.

Two things here would fail silently on the flight side, so they are pinned: F' accepts command strings of at most
39 characters whatever the dictionary says (a longer path is a FORMAT_ERROR on board), and the record is named by
episode and last tic, which the ground must work out from L1 exactly as the payload does. The comparison is the
point of the phase: every disagreement must show up as agree=False, and things the archive can legitimately miss
(a late start, a lost status) must not.
"""
import copy
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SDS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SDS)
from doomsat_sds import config, episodes, pipeline, products, record  # noqa: E402
from doomsat_sds.archive import RecordedArchive  # noqa: E402
from doomsat_sds.catalog import Catalog  # noqa: E402

FIXTURE = os.path.join(SDS, "tests", "data", "two_deaths.json.gz")
WINDOW = [1791074773533, 1791074807183]
CONTEXT = {"wad": "freedoom1.wad", "map": "E1M1", "skill": 3, "seed": 7, "pilot_mode": "test", "repo_commit": "test",
           "level_set": "dev", "sources": {}}


def episode_two_l1():
    arch = RecordedArchive(FIXTURE)
    evs = arch.events(0, 2 ** 62, types=[config.EVENT_PREFIX + n for n in config.EPISODE_EVENTS])
    ep2 = [e for e in episodes.closed_episodes(evs) if e.number == 2][0]
    settings = config.Settings(home=Path("/nonexistent/sds"), doomsat_home=Path("/nonexistent"))
    doc, _ = pipeline.make_l1(settings, arch, dict(ep2.as_conf(), window=WINDOW), CONTEXT)
    return doc


def record_from(l1, first_tic=None):
    """The record the payload would have written for this episode, had every status reached the ground."""
    col = lambda n: products.column(l1, n)
    tics = [v for _, v in col("TIC")]
    path, last = [], None
    cols = l1["telemetry"]["columns"]
    it, ix, iy = cols.index("TIC"), cols.index("POS_X"), cols.index("POS_Y")
    for r in l1["telemetry"]["rows"]:
        if r[it] is not None and r[ix] is not None and r[iy] is not None:
            if last is None or r[it] - last >= 35:
                path.append([r[it], r[ix], r[iy]])
                last = r[it]
    final = {"tic": tics[-1], "kills": col("KILLS")[-1][1], "explored": col("EXPLORED_CELLS")[-1][1],
             "health": col("HEALTH")[-1][1], "x": col("POS_X")[-1][1], "y": col("POS_Y")[-1][1], "keys": 0,
             "level": 1, "dead": 1, "level_done": 0}
    return {"record": {"format": "doomsat-episode-record", "version": 1}, "episode": 2, "outcome": "died",
            "context": {"wad": "freedoom1.wad", "map": "E1M1", "skill": 3, "seed": 7},
            "statuses_sent": len(set(tics)), "first_tic": tics[0] if first_tic is None else first_tic,
            "last_tic": record.last_tic(l1), "final": final,
            "health_min": min(v for _, v in col("HEALTH")), "path_every_tics": 35, "path": path}


class TestNaming(unittest.TestCase):
    def test_absolute_path_when_it_fits(self):
        s = config.Settings(home=Path("/root/doom/sds"), doomsat_home=Path("/root/doom"))
        self.assertEqual(record.source_path(s, "3-7788.json"), "/root/doom/run/rec/3-7788.json")

    def test_relative_to_the_flight_binary_when_the_absolute_path_is_too_long(self):
        s = config.Settings(home=Path("/x"), doomsat_home=Path("/home/a-rather-long-user-name/doom"))
        p = record.source_path(s, "3-7788.json")
        self.assertEqual(p, "../../../../../run/rec/3-7788.json")
        self.assertLessEqual(len(p), record.MAX_CMD_STRING)
        # bin -> DoomSat -> <OS> -> build-artifacts -> project -> DOOMSAT_HOME
        binary_cwd = Path("/home/a-rather-long-user-name/doom/DoomSat/build-artifacts/Linux/DoomSat/bin")
        self.assertEqual(os.path.normpath(binary_cwd / p), "/home/a-rather-long-user-name/doom/run/rec/3-7788.json")

    def test_nothing_fits(self):
        s = config.Settings(home=Path("/x"), doomsat_home=Path("/home/a-rather-long-user-name/doom"))
        with self.assertRaises(ValueError):
            record.source_path(s, "12345-4294967295-and-more.json")

    def test_destination_names_fit_and_are_flat(self):
        d = record.dest_name(episodes.episode_id(1791074807184, 65535))
        self.assertLessEqual(len(d), record.MAX_CMD_STRING)
        self.assertNotIn("/", d)


class TestAgainstL1(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.l1 = episode_two_l1()

    def test_last_tic_comes_from_the_closing_event(self):
        self.assertEqual(record.last_tic(self.l1), 1180)

    def test_a_faithful_record_agrees(self):
        out = record.compare(record_from(self.l1), self.l1, CONTEXT)
        bad = [c for c in out["checks"] if c["agree"] is False]
        self.assertEqual(bad, [])
        self.assertTrue(out["agree"])
        self.assertEqual(out["product"]["version"], products.version("qa_record_check"))

    def test_each_kind_of_disagreement_is_caught(self):
        base = record_from(self.l1)
        for name, change in (
                ("kills", lambda r: r["final"].__setitem__("kills", r["final"]["kills"] + 1)),
                ("final position", lambda r: r["final"].__setitem__("x", r["final"]["x"] + 1.0)),
                ("outcome", lambda r: r.__setitem__("outcome", "level_finished")),
                ("episode number", lambda r: r.__setitem__("episode", 7)),
                ("context map", lambda r: r["context"].__setitem__("map", "E1M2")),
                ("path points", lambda r: r["path"][3].__setitem__(1, r["path"][3][1] + 5.0))):
            r = copy.deepcopy(base)
            change(r)
            checks = {c["check"]: c for c in record.compare(r, self.l1, CONTEXT)["checks"]}
            self.assertIs(checks[name]["agree"], False, name)

    def test_what_the_archive_may_legitimately_miss(self):
        first_archived = products.column(self.l1, "TIC")[0][1]
        r = record_from(self.l1, first_tic=first_archived - 30)   # statuses sent before the archive was listening
        r["statuses_sent"] += 200
        out = record.compare(r, self.l1, CONTEXT)
        checks = {c["check"]: c for c in out["checks"]}
        self.assertIs(checks["first tic"]["agree"], False)    # reported, because it is worth knowing
        self.assertIs(checks["statuses sent vs received"]["agree"], True)
        self.assertLess(out["statuses"]["delivery"], 1.0)

    def test_a_position_lost_on_board_is_unattributable_not_a_disagreement(self):
        # Seen live (episode 20261004T020515Z-e0002): two statuses, tics 29308 and 29311, shared one F' time tag;
        # the archive kept both TICs but only the second position. Pairing that position with tic 29308 gave a
        # false 30-unit "disagreement". A time tag with fewer positions than tics must not be compared.
        ep = episodes.ClosedEpisode(4, 2_000, "PlayerDied", "died", None)
        science = {n: [] for n in config.SCIENCE}
        science["TIC"] = [(1_000, 900, 10), (1_000, 950, 13), (1_100, 1_000, 16)]
        science["POS_X"] = [(1_000, 950, 5.0), (1_100, 1_000, 7.0)]
        science["POS_Y"] = [(1_000, 950, 6.0), (1_100, 1_000, 8.0)]
        l1 = products.build_l1(ep, (1_000, 1_100), science, {}, [], [], CONTEXT, [])
        rec = {"episode": 4, "outcome": "died", "first_tic": 10, "last_tic": 16, "statuses_sent": 3,
               "health_min": 0, "context": {}, "path": [[10, 2.0, 3.0], [16, 7.0, 8.0]],
               "final": {"kills": None, "explored": None, "health": None, "x": 7.0, "y": 8.0}}
        checks = {c["check"]: c for c in record.compare(rec, l1, CONTEXT)["checks"]}
        self.assertIs(checks["path points"]["agree"], True)       # tic 16 compared and equal; tic 10 not compared
        self.assertEqual(checks["path points"]["archive"], 1)
        self.assertIn("1 unattributable", checks["path points"]["note"])
        rec["path"][1][1] = 7.5                                   # a real disagreement is still caught
        checks = {c["check"]: c for c in record.compare(rec, l1, CONTEXT)["checks"]}
        self.assertIs(checks["path points"]["agree"], False)

    def test_context_the_ground_never_learnt_is_not_a_disagreement(self):
        out = record.compare(record_from(self.l1), self.l1, {"wad": None, "map": None})
        checks = {c["check"]: c for c in out["checks"]}
        self.assertIsNone(checks["context wad"]["agree"])
        self.assertTrue(out["agree"])


class TestPlan(unittest.TestCase):
    def test_plan_from_the_catalog(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = config.Settings(home=Path(tmp) / "sds", doomsat_home=Path("/root/doom"))
            arch = RecordedArchive(FIXTURE)
            evs = arch.events(0, 2 ** 62, types=[config.EVENT_PREFIX + n for n in config.EPISODE_EVENTS])
            ep2 = [e for e in episodes.closed_episodes(evs) if e.number == 2][0]
            with Catalog(s.catalog) as c, mock.patch("doomsat_sds.context.capture", return_value=CONTEXT), \
                    mock.patch("doomsat_sds.pipeline._commit", return_value="test"):
                pipeline.l1(s, arch, c, dict(ep2.as_conf(), window=WINDOW))
                p = record.plan(s, c, ep2.episode_id)
            self.assertEqual(p["name"], "2-1180.json")
            self.assertEqual(p["source"], "/root/doom/run/rec/2-1180.json")
            self.assertEqual(p["dest"], "rec_%s.json" % ep2.episode_id)
            self.assertTrue(p["mirror_path"].endswith("/run/downlink/" + p["dest"]))


if __name__ == "__main__":
    unittest.main()

"""The context an episode was flown in (doomsat_sds.context): WAD, map, skill, seed, pilot mode, dev or test set.

None of it is in telemetry (docs/plans/sds-airflow.md, fact 12): it exists only in the running payload's and
pilot's command lines and in payload.log, so the forward run reads it from the live process table once and the
catalog keeps it for good. Two mistakes would poison every product built on it. Crediting an episode to a process
that started after the episode began (a payload restarted with another WAD, a pilot restarted in another mode)
records a guess as a fact, so such values must come back unknown, with a source that says why. And confusing the
dev set with the locked test level (charter 2.5) would put tuning runs into the score, so the dev/test label is
read from the harness's own research/levels.yaml, which these tests read and never write.
"""
import hashlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SDS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SDS)

from doomsat_sds import config, context  # noqa: E402

REPO = Path(SDS).parents[1]
LEVELS = REPO / "research" / "levels.yaml"
START_MS = 1791074773533                  # episode 2 of the recorded flight began here (TM time)
BEFORE = START_MS / 1000 - 600            # a process start time (wall clock, s) well before the episode
AFTER = START_MS / 1000 + 60              # and one after it
PAYLOAD_PY = "/root/doom/payload-venv/bin/python"
PAYLOAD_SCRIPT = "/home/user/DoomSat/payload/doom_payload.py"
# As scripts/wsl_run_flight.sh starts it (abridged).
PAYLOAD_ARGV = [PAYLOAD_PY, PAYLOAD_SCRIPT, "--fps", "10", "--skill", "4", "--wad", "freedoom1.wad", "--map", "E1M1",
                "--geometry", "on", "--oracle", "off", "--seed=11"]
# As scripts/start_pilot.sh execs it.
PILOT_ARGV = ["/home/user/DoomSat/ground/.venv/bin/python", "pilot.py", "--duration", "120",
              "--system-one", "code", "--system-two", "none"]
LOG = "\n".join([
    "[payload] listening on port 4242",
    "[payload] episode 1 started on E1M1 (level 1)",
    "[payload] episode 2 started on E1M2 (level 2)",
    "[payload] episode 3 started on E1M3 (level 3)",
])


def proc(pid, argv, start_s):
    return {"pid": pid, "argv": list(argv), "start_s": start_s}


class TestOptions(unittest.TestCase):
    def test_space_separated_values(self):
        self.assertEqual(context._options(["python", "x.py", "--wad", "freedoom1.wad", "--skill", "3"]),
                         {"--wad": "freedoom1.wad", "--skill": "3"})

    def test_equals_values(self):
        self.assertEqual(context._options(["x.py", "--seed=11", "--map-png=/tmp/a=b.png"]),
                         {"--seed": "11", "--map-png": "/tmp/a=b.png"})

    def test_a_flag_at_the_end_reads_true(self):
        self.assertEqual(context._options(["pilot.py", "--duration", "120", "--log-questions"]),
                         {"--duration": "120", "--log-questions": "true"})

    # Regression test for a bug the tests found (now fixed): context._options (context.py:66-67) reads the token after a flag with next(it) and, when that
    # token is another option, records the flag as "true" but drops the option it consumed: "--geometry --seed 9"
    # loses --seed (and "9" is then skipped as a positional).
    def test_a_flag_followed_by_another_option_keeps_both(self):
        self.assertEqual(context._options(["x.py", "--geometry", "--seed", "9", "--oracle"]),
                         {"--geometry": "true", "--seed": "9", "--oracle": "true"})

    def test_positionals_and_short_options_are_ignored(self):
        self.assertEqual(context._options(["python3", "-u", "x.py", "extra", "--wad", "a.wad", "-v"]),
                         {"--wad": "a.wad"})


class TestFind(unittest.TestCase):
    def test_the_interpreter_wins_over_a_bash_wrapper_that_mentions_the_script(self):
        # setsid -f bash -c "... doom_payload.py" starts first, and its argv ends with the script's path.
        wrapper = proc(10, ["bash", "-c", "exec %s %s" % (PAYLOAD_PY, PAYLOAD_SCRIPT)], BEFORE - 1)
        wrapper2 = proc(11, ["/bin/bash", PAYLOAD_SCRIPT], BEFORE - 2)
        python = proc(12, PAYLOAD_ARGV, BEFORE)
        self.assertEqual(context._find([wrapper, wrapper2, python], "doom_payload.py")["pid"], 12)

    def test_the_oldest_interpreter_wins(self):
        procs = [proc(21, PAYLOAD_ARGV, BEFORE + 30), proc(22, PAYLOAD_ARGV, BEFORE), proc(23, PAYLOAD_ARGV, BEFORE + 9)]
        self.assertEqual(context._find(procs, "doom_payload.py")["pid"], 22)

    def test_a_bare_script_name_matches(self):
        self.assertEqual(context._find([proc(31, PILOT_ARGV, BEFORE)], "pilot.py")["pid"], 31)

    def test_only_the_whole_file_name_matches(self):
        procs = [proc(41, ["python", "ground/autopilot.py"], BEFORE), proc(42, ["python", "my_doom_payload.py"], BEFORE),
                 proc(43, ["python", "payload/doom_payload.py.orig"], BEFORE)]
        self.assertIsNone(context._find(procs, "doom_payload.py"))
        self.assertIsNone(context._find(procs, "pilot.py"))

    def test_none_when_nothing_runs_it(self):
        self.assertIsNone(context._find([], "pilot.py"))


class TestMapFromLog(unittest.TestCase):
    def test_map_and_level_for_the_episode(self):
        self.assertEqual(context.map_from_log(LOG, 3), ("E1M3", 3))
        self.assertEqual(context.map_from_log(LOG, 1), ("E1M1", 1))
        self.assertIsInstance(context.map_from_log(LOG, 2)[1], int)

    def test_none_when_the_episode_is_not_in_the_log(self):
        self.assertIsNone(context.map_from_log(LOG, 4))
        self.assertIsNone(context.map_from_log("", 1))

    def test_the_number_must_match_exactly(self):
        self.assertIsNone(context.map_from_log("[payload] episode 13 started on E1M3 (level 3)", 1))

    def test_only_payload_lines_count(self):
        text = "[pilot] episode 2 started on E1M9 (level 9)\n  [payload] episode 2 started on E1M2 (level 2)  "
        self.assertEqual(context.map_from_log(text, 2), ("E1M2", 2))

    def test_the_latest_line_for_a_number_wins(self):
        # The log of one payload process that restarted the game counts from 1 again; the latest is this flight.
        text = LOG + "\n[payload] episode 1 started on E1M4 (level 4)"
        self.assertEqual(context.map_from_log(text, 1), ("E1M4", 4))


class TestLevelSet(unittest.TestCase):
    """Against the real research/levels.yaml, which belongs to the harness and is only read."""

    def setUp(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.repo = config.Settings(home=Path(tmp) / "sds", doomsat_home=Path(tmp) / "doom").repo
        self.before = hashlib.sha256(LEVELS.read_bytes()).hexdigest()

    def tearDown(self):
        self.assertEqual(hashlib.sha256(LEVELS.read_bytes()).hexdigest(), self.before, "levels.yaml was written")

    def test_settings_point_at_this_repo(self):
        self.assertEqual(Path(self.repo).resolve(), REPO.resolve())

    def test_dev_test_and_other(self):
        self.assertEqual(context.level_set(self.repo, "freedoom1.wad", "E1M1"), "dev")
        self.assertEqual(context.level_set(self.repo, "doom1.wad", "E1M1"), "test")
        self.assertEqual(context.level_set(self.repo, "freedoom1.wad", "E1M7"), "other")
        self.assertEqual(context.level_set(self.repo, "freedoom2.wad", "MAP01"), "other")

    def test_a_wad_given_as_a_path_counts_by_its_name(self):
        self.assertEqual(context.level_set(self.repo, "/root/doom/wads/freedoom1.wad", "E1M2"), "dev")

    def test_unknown_without_both_or_without_the_file(self):
        self.assertEqual(context.level_set(self.repo, None, "E1M1"), "unknown")
        self.assertEqual(context.level_set(self.repo, "doom1.wad", None), "unknown")
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(context.level_set(Path(tmp), "doom1.wad", "E1M1"), "unknown")


class TestCapture(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.settings = config.Settings(home=Path(tmp.name) / "sds", doomsat_home=Path(tmp.name) / "doom")
        for target, kw in (("doomsat_sds.context.repo_commit", {"return_value": "feedc0ffee12"}),
                           ("doomsat_sds.context.processes", {"side_effect": AssertionError("read /proc")})):
            p = mock.patch(target, **kw)
            setattr(self, target.rsplit(".", 1)[1], p.start())
            self.addCleanup(p.stop)

    def capture(self, procs, number=2, log_text=LOG):
        return context.capture(self.settings, number, START_MS, procs=procs, log_text=log_text)

    def test_a_payload_running_before_the_episode_gives_its_arguments(self):
        ctx = self.capture([proc(101, PAYLOAD_ARGV, BEFORE), proc(202, PILOT_ARGV, BEFORE)])
        self.assertEqual((ctx["wad"], ctx["skill"], ctx["seed"], ctx["geometry"], ctx["oracle"]),
                         ("freedoom1.wad", 4, 11, "on", "off"))
        self.assertEqual((ctx["map"], ctx["level"]), ("E1M2", 2))
        self.assertIn("pid 101", ctx["sources"]["payload"])
        self.assertTrue(ctx["sources"]["payload"].startswith("arguments of the running payload"))
        self.assertEqual(ctx["sources"]["map"], "payload.log")
        self.assertEqual(ctx["level_set"], "dev")
        self.assertEqual(ctx["repo_commit"], "feedc0ffee12")
        self.repo_commit.assert_called_once_with(self.settings.repo)

    def test_skill_and_seed_are_integers(self):
        for argv in (PAYLOAD_ARGV, [PAYLOAD_PY, PAYLOAD_SCRIPT]):
            ctx = self.capture([proc(101, argv, BEFORE)])
            for key in ("skill", "seed"):
                self.assertIs(type(ctx[key]), int, (key, argv))

    def test_unset_arguments_take_the_payload_defaults(self):
        ctx = self.capture([proc(101, [PAYLOAD_PY, PAYLOAD_SCRIPT], BEFORE)])
        self.assertEqual((ctx["wad"], ctx["skill"], ctx["seed"]), ("doom1.wad", 2, 7))
        self.assertEqual(ctx["level_set"], "test")          # doom1.wad E1M2, from the log

    # Regression test for a bug the tests found (now fixed): context.PAYLOAD_DEFAULTS (context.py:21) mirrors doom_payload.py's argparse defaults but leaves
    # out --geometry and --oracle (both default "off", doom_payload.py:1417-1420), so a payload known to have flown
    # the episode, started without those options, is recorded with geometry and oracle None: the value capture()
    # reserves for "no payload process was running", not the "off" that payload actually ran with.
    def test_unset_geometry_and_oracle_are_the_payload_defaults(self):
        ctx = self.capture([proc(101, [PAYLOAD_PY, PAYLOAD_SCRIPT, "--wad", "freedoom1.wad"], BEFORE)])
        self.assertEqual((ctx["geometry"], ctx["oracle"]), ("off", "off"))

    def test_a_payload_started_after_the_episode_did_not_fly_it(self):
        ctx = self.capture([proc(101, PAYLOAD_ARGV, AFTER), proc(202, PILOT_ARGV, BEFORE)])
        for key in ("wad", "skill", "seed", "geometry", "oracle", "map"):
            self.assertIsNone(ctx[key], key)
        self.assertNotIn("level", ctx)
        self.assertTrue(ctx["sources"]["payload"].startswith("unknown"), ctx["sources"]["payload"])
        self.assertEqual(ctx["level_set"], "unknown")

    def test_a_payload_with_no_start_time_is_not_trusted(self):
        ctx = self.capture([proc(101, PAYLOAD_ARGV, None)])
        self.assertIsNone(ctx["wad"])
        self.assertTrue(ctx["sources"]["payload"].startswith("unknown"))

    def test_the_tm_clock_running_ahead_is_allowed_for(self):
        # TM time runs about a second ahead of the process clock, so a payload whose start_s is a few seconds
        # past the episode's TM start was still there when it began.
        ctx = self.capture([proc(101, PAYLOAD_ARGV, START_MS / 1000 + 3)])
        self.assertEqual(ctx["wad"], "freedoom1.wad")

    def test_episode_1_without_a_log_line_takes_the_map_argument(self):
        argv = PAYLOAD_ARGV[:]
        argv[argv.index("--map") + 1] = "E1M3"
        ctx = self.capture([proc(101, argv, BEFORE)], number=1, log_text="")
        self.assertEqual(ctx["map"], "E1M3")
        self.assertEqual(ctx["sources"]["map"], "payload --map (episode 1)")
        self.assertEqual(ctx["level_set"], "dev")

    def test_a_later_episode_without_a_log_line_has_no_map(self):
        # The payload advances maps on its own; after episode 1 the --map argument says nothing.
        ctx = self.capture([proc(101, PAYLOAD_ARGV, BEFORE)], number=5)
        self.assertIsNone(ctx["map"])
        self.assertEqual(ctx["sources"]["map"], "unknown")
        self.assertEqual(ctx["level_set"], "unknown")

    def test_payload_log_is_read_from_the_run_directory(self):
        self.settings.run_dir.mkdir(parents=True)
        (self.settings.run_dir / "payload.log").write_text(LOG + "\n", encoding="utf-8")
        ctx = self.capture([proc(101, PAYLOAD_ARGV, BEFORE)], number=3, log_text=None)
        self.assertEqual((ctx["map"], ctx["level"], ctx["sources"]["map"]), ("E1M3", 3, "payload.log"))

    def test_a_missing_payload_log_is_not_an_error(self):
        ctx = self.capture([proc(101, PAYLOAD_ARGV, BEFORE)], number=1, log_text=None)
        self.assertEqual((ctx["map"], ctx["sources"]["map"]), ("E1M1", "payload --map (episode 1)"))

    def test_a_pilot_running_before_the_episode_gives_its_mode(self):
        ctx = self.capture([proc(101, PAYLOAD_ARGV, BEFORE), proc(202, PILOT_ARGV, BEFORE)])
        self.assertEqual(ctx["pilot_mode"], "system-one=code system-two=none")
        self.assertIn("pid 202", ctx["sources"]["pilot"])

    def test_pilot_defaults_and_equals_form(self):
        ctx = self.capture([proc(202, ["python", "ground/pilot.py"], BEFORE)])
        self.assertEqual(ctx["pilot_mode"], "system-one=typesafe system-two=claude-cli")
        ctx = self.capture([proc(202, ["python", "ground/pilot.py", "--system-two=anthropic"], BEFORE)])
        self.assertEqual(ctx["pilot_mode"], "system-one=typesafe system-two=anthropic")

    # Regression test for a bug the tests found (now fixed): the same _options defect (context.py:66-67) as seen in the catalog. pilot.py has flags that take
    # no value (--no-after-action, --auto-apply-graph, --log-questions, --no-reset; pilot.py:657-677), and
    # "pilot.py --log-questions --system-two none" swallows --system-two, so the episode is cataloged as flown
    # with "system-two=claude-cli", a mode it did not have. scripts/play.sh documents such a call: "play.sh
    # --no-reset --system-two claude-cli" runs "pilot.py --system-one manual --system-two none --no-reset
    # --system-two claude-cli", which argparse reads as claude-cli (the last one wins) and capture() records as none.
    def test_a_pilot_flag_before_the_mode_does_not_hide_it(self):
        mode = lambda *args: self.capture([proc(202, ["python", "pilot.py", *args], BEFORE)])["pilot_mode"]
        start_pilot = mode("--log-questions", "--system-two", "none", "--auto-apply-graph", "--system-one", "code")
        play_sh = mode("--system-one", "manual", "--system-two", "none", "--no-reset", "--system-two", "claude-cli")
        self.assertEqual([start_pilot, play_sh],
                         ["system-one=code system-two=none", "system-one=manual system-two=claude-cli"])

    def test_a_pilot_started_after_the_episode_is_unknown(self):
        ctx = self.capture([proc(101, PAYLOAD_ARGV, BEFORE), proc(202, PILOT_ARGV, AFTER)])
        self.assertIsNone(ctx["pilot_mode"])
        self.assertTrue(ctx["sources"]["pilot"].startswith("unknown"), ctx["sources"]["pilot"])
        self.assertEqual(ctx["wad"], "freedoom1.wad")       # the payload's values do not depend on the pilot

    def test_no_pilot_running_is_unknown_not_none(self):
        # The context is captured a minute or so after the episode; a pilot run with --duration may have exited by
        # then. Its absence is not evidence that nobody flew, so it must not be recorded as "none".
        ctx = self.capture([proc(101, PAYLOAD_ARGV, BEFORE)])
        self.assertIsNone(ctx["pilot_mode"])
        self.assertTrue(ctx["sources"]["pilot"].startswith("unknown"), ctx["sources"]["pilot"])

    def test_a_pilot_wrapper_does_not_stand_in_for_the_pilot(self):
        wrapper = proc(201, ["bash", "-c", "exec python /home/user/DoomSat/ground/pilot.py"], BEFORE - 5)
        ctx = self.capture([wrapper, proc(202, PILOT_ARGV, AFTER)])
        self.assertIsNone(ctx["pilot_mode"])
        ctx = self.capture([wrapper])
        self.assertIsNone(ctx["pilot_mode"])

    def test_nothing_running(self):
        ctx = self.capture([])
        self.assertIsNone(ctx["wad"])
        self.assertIsNone(ctx["pilot_mode"])
        self.assertEqual(ctx["level_set"], "unknown")
        self.assertEqual(ctx["repo_commit"], "feedc0ffee12")


if __name__ == "__main__":
    unittest.main()

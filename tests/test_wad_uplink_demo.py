"""tools/wad_uplink_demo.py against a stand-in Yamcs: no network, no game, no flight software.

The stand-in answers the way the stack does (FileReceived and WadUplinked once a file is sent, WadLoaded
and new WAD telemetry and a frame once LOAD_WAD is issued), so the tool's own logic is what is tested: the
name the file goes up under, the remote path, the bucket clean-up, the command it sends, and the exit code.
"""
import base64
import os
import sys
import tempfile
import threading
import types
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

try:
    import wad_uplink_demo as demo  # noqa: E402  needs yamcs-client (ground/.venv)
except ImportError:  # pragma: no cover
    demo = None

DOOM = "/DoomSat_DoomSat/DoomSat/doom/"


def wad_bytes(name):
    return base64.b64encode(name.encode().ljust(40, b"\0")).decode()


class FakeStack:
    """Just enough of YamcsClient, a processor, the storage and file transfer clients."""

    def __init__(self, refuse_load=False, cfdp=False, lose=()):
        self.refuse_load, self.cfdp = refuse_load, cfdp
        self.lose = list(lose)    # what a lossy link drops, once each: a command name, or an event's [Id]
        self.parts = set()        # with cfdp: .part files on board, waiting for COMMIT_WAD
        self.uploaded, self.deleted, self.transfers, self.commands = [], [], [], []
        self.on_event = self.on_data = None
        self.tlm = {"CMDS_RECEIVED": 7, "WAD_IWAD": wad_bytes("freedoom1.wad"), "WAD_PWAD": wad_bytes(""),
                    "WAD_LOADS": 0, "EPISODE": 1, "FRAMES_SENT": 100, "PAYLOAD_LINK": "True"}

    # -- YamcsClient
    def __call__(self, *_a, **_k):
        return self

    def get_processor(self, *_a):
        return self

    def create_event_subscription(self, _instance, on_data):
        self.on_event = on_data

    def create_parameter_subscription(self, _names, on_data):
        self.on_data = on_data
        self.publish()

    def get_storage_client(self):
        return self

    def get_file_transfer_client(self, _instance):
        return self

    # -- storage
    def list_buckets(self):
        return []

    def create_bucket(self, _name):
        pass

    def get_bucket(self, _name):
        return self

    def upload_object(self, name, content, content_type=None):
        self.uploaded.append((name, len(content)))

    def delete_object(self, name):
        self.deleted.append(name)

    # -- file transfer
    def get_service(self, _name):
        return self

    def create_transfer_subscription(self):
        return self

    def get_transfer(self, _id):
        return None

    def upload(self, bucket, obj, remote, **kw):
        self.transfers.append((bucket, obj, remote))
        self.upload_kw = kw
        if self.cfdp:             # cfdpManager writes the file in place and announces nothing
            self.parts.add(obj)
            return types.SimpleNamespace(id="1", transferred_size=10, is_complete=lambda: True,
                                         is_success=lambda: True, state="COMPLETED", error=None)
        name = remote.rsplit("/", 1)[1].rsplit(".", 2)[0]
        threading.Timer(0.1, self.event, [f"[FileReceived] Received file {remote[:40]}"]).start()
        threading.Timer(0.2, self.event, [f"[WadUplinked] Uplinked WAD ready to load: {remote.rsplit('/', 1)[0]}/{name}"]).start()
        return types.SimpleNamespace(id="1", transferred_size=10, is_complete=lambda: True, is_success=lambda: True,
                                     state="COMPLETED", error=None)

    # -- commands
    def issue_command(self, name, args):
        self.commands.append((name.rsplit("/", 1)[1], dict(args)))
        if self.dropped(name.rsplit("/", 1)[1]):
            return
        self.tlm["CMDS_RECEIVED"] += 1
        if name.endswith("COMMIT_WAD"):
            if args["part"] in self.parts:
                self.parts.discard(args["part"])
                final = args["part"].rsplit(".", 2)[0]
                threading.Timer(0.1, self.event, [f"[WadUplinked] Uplinked WAD ready to load: /home/x/doom/wads/uplink/{final}"]).start()
            else:
                threading.Timer(0.1, self.event, [f"[WadUplinkFailed] Could not place {args['part']}"]).start()
        if name.endswith("LOAD_WAD"):
            if self.refuse_load:
                threading.Timer(0.1, self.event, ["[WadLoadFailed] Could not load x: no"]).start()
            else:
                self.tlm.update(WAD_IWAD=wad_bytes(args["iwad"]), WAD_PWAD=wad_bytes(args["pwad"]), WAD_LOADS=1,
                                EPISODE=2)
                threading.Timer(0.1, self.event, [f"[WadLoaded] Now flying {args['iwad']} on {args['map']}"]).start()
                for i in range(10):      # the new game flies on: frames keep coming
                    threading.Timer(0.3 + 0.2 * i, self.frame, [500 + i]).start()
        self.publish()

    def dropped(self, what):
        if what in self.lose:
            self.lose.remove(what)
            return True
        return False

    # -- what the stack sends down
    def event(self, message):
        if not self.dropped(message.split("]", 1)[0] + "]"):
            self.on_event(types.SimpleNamespace(message=message))

    def publish(self):
        if self.on_data:
            pvs = [types.SimpleNamespace(name=DOOM + k, eng_value=v) for k, v in self.tlm.items()]
            self.on_data(types.SimpleNamespace(parameters=pvs))

    def frame(self, seq):
        chunk = dict(seq=seq, index=0, count=1, length=4, data=base64.b64encode(b"jpeg").decode())
        self.on_data(types.SimpleNamespace(parameters=[types.SimpleNamespace(name=DOOM + "FRAME_CHUNK", eng_value=chunk)]))


@unittest.skipIf(demo is None, "yamcs-client is not installed (run with ground/.venv/bin/python)")
class TestTheDemo(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.wad = os.path.join(self.tmp.name, "basic.wad")
        with open(self.wad, "wb") as f:
            f.write(b"\0" * 2704)        # stands for the file: the tool never looks inside it

    def tearDown(self):
        self.tmp.cleanup()

    def run_demo(self, stack, *argv):
        argv = ["wad_uplink_demo.py", "--remote-dir", "/home/x/doom/wads/uplink", "--out", self.tmp.name, *argv]
        with mock.patch.object(demo, "YamcsClient", stack), mock.patch.object(sys, "argv", argv), \
                mock.patch.object(demo, "say", lambda *_: None), mock.patch.object(demo.time, "sleep", lambda _s: None):
            return demo.main()

    def test_a_pwad_goes_up_under_its_own_name_and_is_loaded_over_the_iwad(self):
        stack = FakeStack()
        self.assertEqual(self.run_demo(stack, "--wad", self.wad, "--iwad", "freedoom2.wad", "--map", "MAP01"), 0)
        (bucket, obj, remote), = stack.transfers
        self.assertRegex(obj, r"^basic\.wad\.\d+\.part$")
        self.assertEqual(remote, "/home/x/doom/wads/uplink/" + obj)
        self.assertEqual(stack.deleted, [obj], "the bucket object is removed once sent")
        self.assertIn(("LOAD_WAD", {"iwad": "freedoom2.wad", "pwad": "basic.wad", "map": "MAP01"}), stack.commands)
        self.assertTrue(os.path.isfile(os.path.join(self.tmp.name, "wad_uplink_basic_MAP01.jpg")))

    def test_an_iwad_goes_up_under_the_name_asked_for(self):
        stack = FakeStack()
        self.assertEqual(self.run_demo(stack, "--wad", self.wad, "--as", "shareware.wad", "--map", "E1M1"), 0)
        self.assertRegex(stack.transfers[0][1], r"^shareware\.wad\.\d+\.part$")
        self.assertIn(("LOAD_WAD", {"iwad": "shareware.wad", "pwad": "", "map": "E1M1"}), stack.commands)

    def test_truncate_never_uses_the_real_name(self):
        stack = FakeStack(refuse_load=True)
        self.assertEqual(self.run_demo(stack, "--wad", self.wad, "--truncate", "100", "--map", "MAP01",
                                       "--expect-fail"), 0)
        self.assertRegex(stack.transfers[0][1], r"^trunc-basic\.wad\.\d+\.part$")
        self.assertEqual(stack.uploaded[0][1], 100)

    def test_a_bad_name_stops_before_anything_goes_up(self):
        stack = FakeStack()
        with self.assertRaises(SystemExit):
            self.run_demo(stack, "--wad", self.wad, "--as", "two words.wad", "--map", "MAP01")
        self.assertEqual(stack.transfers, [])

    def test_cfdp_class_2_commits_the_part_then_loads_it(self):
        stack = FakeStack(cfdp=True)
        self.assertEqual(self.run_demo(stack, "--wad", self.wad, "--iwad", "freedoom2.wad", "--map", "MAP01",
                                       "--cfdp", "2", "--pdu-delay", "10"), 0)
        self.assertEqual(stack.upload_kw, {"source_entity": "ground", "destination_entity": "doomsat",
                                           "options": {"reliable": True, "pduDelay": 10}})
        names = [c for c, _ in stack.commands]
        self.assertEqual(names, ["RXCRCCALCBYTESPERCYCLE_PRM_SET", "COMMIT_WAD", "LOAD_WAD"])
        self.assertEqual(stack.commands[1][1], {"part": stack.transfers[0][1]})

    def test_a_lost_commit_answer_is_asked_again_and_the_load_settles_it(self):
        # The first COMMIT_WAD works but its WadUplinked is lost; the second finds no .part (WadUplinkFailed)
        stack = FakeStack(cfdp=True, lose=["[WadUplinked]"])
        with mock.patch.object(demo, "COMMIT_ANSWER_S", 0.5):
            self.assertEqual(self.run_demo(stack, "--wad", self.wad, "--iwad", "freedoom2.wad", "--map", "MAP01",
                                           "--cfdp", "2", "--tries", "3"), 0)
        self.assertEqual([c for c, _ in stack.commands].count("COMMIT_WAD"), 2)
        self.assertIn("LOAD_WAD", [c for c, _ in stack.commands])

    def test_a_failed_commit_on_the_first_try_is_a_failure(self):
        stack = FakeStack(cfdp=True)
        stack.upload = lambda *a, **k: (FakeStack.upload(stack, *a, **k), stack.parts.clear())[0]
        self.assertEqual(self.run_demo(stack, "--wad", self.wad, "--iwad", "freedoom2.wad", "--map", "MAP01",
                                       "--cfdp", "2", "--tries", "3"), 1)
        self.assertNotIn("LOAD_WAD", [c for c, _ in stack.commands])

    def test_a_lost_load_wad_is_sent_again(self):
        stack = FakeStack(lose=["LOAD_WAD"])
        with mock.patch.object(demo, "LOAD_RETRY_ANSWER_S", 0.5), mock.patch.object(demo, "TLM_STANDIN_S", 0.2):
            self.assertEqual(self.run_demo(stack, "--iwad", "freedoom1.wad", "--map", "E1M2", "--tries", "2"), 0)
        self.assertEqual([c for c, _ in stack.commands].count("LOAD_WAD"), 2)

    def test_a_lost_wad_loaded_event_is_stood_in_for_by_telemetry(self):
        stack = FakeStack(lose=["[WadLoaded]"])
        with mock.patch.object(demo, "LOAD_ANSWER_S", 0.5):
            self.assertEqual(self.run_demo(stack, "--iwad", "freedoom1.wad", "--map", "E1M2"), 0)
        self.assertEqual([c for c, _ in stack.commands].count("LOAD_WAD"), 1)

    def test_a_refused_load_fails_unless_expected(self):
        self.assertEqual(self.run_demo(FakeStack(refuse_load=True), "--iwad", "nothere.wad", "--map", "E1M1",
                                       "--expect-fail"), 0)
        self.assertEqual(self.run_demo(FakeStack(refuse_load=True), "--iwad", "freedoom1.wad", "--map", "E1M1"), 1)


if __name__ == "__main__":
    unittest.main()

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

    def __init__(self, refuse_load=False):
        self.refuse_load = refuse_load
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

    def upload(self, bucket, obj, remote):
        self.transfers.append((bucket, obj, remote))
        name = remote.rsplit("/", 1)[1].rsplit(".", 2)[0]
        threading.Timer(0.1, self.event, [f"[FileReceived] Received file {remote[:40]}"]).start()
        threading.Timer(0.2, self.event, [f"[WadUplinked] Uplinked WAD ready to load: {remote.rsplit('/', 1)[0]}/{name}"]).start()
        return types.SimpleNamespace(id="1", transferred_size=10, is_complete=lambda: True, is_success=lambda: True,
                                     state="COMPLETED", error=None)

    # -- commands
    def issue_command(self, name, args):
        self.commands.append((name.rsplit("/", 1)[1], dict(args)))
        self.tlm["CMDS_RECEIVED"] += 1
        if name.endswith("LOAD_WAD"):
            if self.refuse_load:
                threading.Timer(0.1, self.event, ["[WadLoadFailed] Could not load x: no"]).start()
            else:
                self.tlm.update(WAD_IWAD=wad_bytes(args["iwad"]), WAD_PWAD=wad_bytes(args["pwad"]), WAD_LOADS=1,
                                EPISODE=2)
                threading.Timer(0.1, self.event, [f"[WadLoaded] Now flying {args['iwad']} on {args['map']}"]).start()
                threading.Timer(0.3, self.frame, [500]).start()
        self.publish()

    # -- what the stack sends down
    def event(self, message):
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

    def test_a_refused_load_fails_unless_expected(self):
        self.assertEqual(self.run_demo(FakeStack(refuse_load=True), "--iwad", "nothere.wad", "--map", "E1M1",
                                       "--expect-fail"), 0)
        self.assertEqual(self.run_demo(FakeStack(refuse_load=True), "--iwad", "freedoom1.wad", "--map", "E1M1"), 1)


if __name__ == "__main__":
    unittest.main()

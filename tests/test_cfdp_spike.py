"""The CFDP spike's ground plumbing, pinned: what took a live run to find out stays found out.

No network beyond 127.0.0.1, no game, no flight software, no Yamcs: the YAML and SQL are read as text, the
launcher wrapper runs against a stand-in fprime_yamcs, and the relay forwards between local sockets.
"""
import importlib.util
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import types
import unittest
from unittest import mock

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ETC = os.path.join(ROOT, "ground", "yamcs", "etc")


def _read(*parts):
    return open(os.path.join(ROOT, *parts), encoding="utf-8").read().replace("\r\n", "\n")


INSTANCE = yaml.safe_load(_read("ground", "yamcs", "etc", "yamcs.fprime-project.yaml"))
SQL = _read("ground", "yamcs", "etc", "cfdp_streams.sql")
CFDP_CFG = _read("flight", "config", "CfdpCfg.fpp")
CFDP_HPP = _read("flight", "config", "CfdpCfg.hpp")
FPP = _read("flight", "Components", "Doom", "Doom.fpp")
PRM = json.loads(_read("flight", "config", "PrmDb.json"))["DoomSat.cfdpManager"]


def cfdp_service():
    return next(s for s in INSTANCE["services"] if s.get("class") == "org.yamcs.cfdp.CfdpService")["args"]


def tc_link():
    return next(link for link in INSTANCE["dataLinks"] if link["name"] == "UDP_TC_OUT")


class TestTheYamcsSide(unittest.TestCase):
    def test_the_sql_file_comes_after_the_streams_it_reads(self):
        keys = list(INSTANCE["streamConfig"])
        self.assertEqual(keys[-1], "sqlFile", "Yamcs creates streamConfig entries in order; cfdp_in reads tm_realtime")
        self.assertLess(keys.index("tm"), keys.index("sqlFile"))
        self.assertIn("${env.DOOMSAT_REPO}", INSTANCE["streamConfig"]["sqlFile"], "Yamcs runs from the F' project dir")

    def test_commands_go_ahead_of_pdus_and_one_packet_per_frame(self):
        link = tc_link()
        self.assertEqual(link["priorityScheme"], "ABSOLUTE", "FIFO would put a whole transfer ahead of commands")
        vcs = {vc["vcId"]: vc for vc in link["virtualChannels"]}
        self.assertEqual(vcs[1]["stream"], "tc_realtime")
        self.assertEqual(vcs[2]["stream"], "cfdp_tc")
        self.assertGreater(vcs[1]["priority"], vcs[2]["priority"])
        for vc in vcs.values():
            self.assertIs(vc["multiplePacketsPerFrame"], False, "F' keeps only the first space packet of a TC frame")

    def test_the_cfdp_service_matches_the_flight_software(self):
        a = cfdp_service()
        self.assertEqual((a["inStream"], a["outStream"]), ("cfdp_in", "cfdp_out"))
        self.assertEqual(a["checksumType"], "MODULAR", "cfdpManager always checks the modular checksum")
        self.assertLessEqual(a["sequenceNrLength"], 4, "F' keeps transaction numbers in a U32")
        self.assertLessEqual(a["entityIdLength"], 4)
        # 1024-byte TC frame - 5 header - 2 FECF - 6 space packet header - 2 F' descriptor
        self.assertLessEqual(a["maxPduSize"], 1024 - 5 - 2 - 6 - 2)
        self.assertEqual([e["id"] for e in a["remoteEntities"]], [42], "cfdpManager's LocalEid")
        self.assertEqual([e["id"] for e in a["localEntities"]], [100], "cfdpManager's FileInDefaultDestEntityId")
        self.assertFalse(a["hasDownloadCapability"], "F' v4.3.0 has no Proxy Put; downlinks start on board")
        # F' restarts its transaction numbers at every boot; Yamcs must not keep old ones answering for 10 min,
        # but must outlast the FIN retries (ChannelConfig ack_timer 2 s x ack_limit 10)
        self.assertTrue(20000 < a["pendingAfterCompletion"] <= 120000)

    def test_the_file_packet_service_is_gone(self):
        self.assertNotIn("com.example.myproject.FprimeFilePacketService", [s.get("class") for s in INSTANCE["services"]])

    def test_the_sql_wraps_and_unwraps_the_way_f_prime_frames_a_pdu(self):
        # downlink: APID 3, F' descriptor 0x0003, CFDP version 001 in the PDU's first byte; strip 6 + 2 bytes
        self.assertIn("(extract_ushort(packet, 0) & 2047) = 3", SQL)
        self.assertIn("extract_ushort(packet, 6) = 3", SQL)
        self.assertIn("(extract_ushort(packet, 8) >> 13) = 1", SQL)
        self.assertIn("substring(packet, 8) as pdu", SQL)
        # uplink: TC type bit + APID 3, unsegmented, length filled later, then the descriptor
        self.assertIn("unhex('1003C00000000003') + pdu", SQL)
        self.assertIn("insert into cfdp_tc", SQL)


class TestTheFlightSide(unittest.TestCase):
    def test_a_downlinked_pdu_fits_the_aggregator(self):
        size = int(re.search(r"^\s*constant MaxPduSize = (\d+)", CFDP_CFG, re.M).group(1))
        # ComAggregator asserts on a space packet over TmFrameFixedSize 1024 - 15; a PDU rides behind 6 + 2 bytes
        self.assertLessEqual(size + 6 + 2, 1024 - 15)

    def test_the_boot_parameters_match_the_ground(self):
        # scripts/wsl_run_flight.sh builds PrmDb.dat from flight/config/PrmDb.json at every start
        a = cfdp_service()
        self.assertEqual(PRM["LocalEid"], a["remoteEntities"][0]["id"])
        self.assertEqual(PRM["FileInDefaultDestEntityId"], a["localEntities"][0]["id"])
        self.assertGreaterEqual(PRM["RxCrcCalcBytesPerCycle"], 4 << 20, "the default 64 KiB a tick: a minute for doom1")
        self.assertEqual(PRM["FileInDefaultKeep"], "KEEP", "DELETE removes a file once downlinked")
        self.assertEqual(PRM["FileInDefaultClass"], "CLASS_2")
        self.assertEqual(len(PRM), 10, "every cfdpManager parameter, or prmDb warns PrmIdNotFound at boot")

    def test_a_downlinked_pdu_fits_whatever_its_transaction_number(self):
        # F' sizes file data from a header it has not filled in yet (TransactionTx.cpp sSendFileData), so MaxPduSize
        # alone does not bound a PDU. Cap the data for the worst header: fixed 4, both entity ids sized from the larger
        # (SendFile takes any destId, up to 4 bytes each), transaction number up to 4, and the 4-byte offset.
        size = int(re.search(r"^\s*constant MaxPduSize = (\d+)", CFDP_CFG, re.M).group(1))
        self.assertLessEqual(PRM["OutgoingFileChunkSize"] + 4 + 4 + 4 + 4 + 4, size)

    def test_cfdp_temp_files_stay_in_the_uplink_directory(self):
        # wsl_run_flight.sh replaces @UPLINK@ with $DOOMSAT_HOME/wads/uplink, makes the directory, and builds
        # PrmDb.dat from the result. Do the same substitution here and check what the flight software would get.
        script = _read("scripts", "wsl_run_flight.sh")
        sed = re.search(r'sed "s#@UPLINK@#([^#]*)#g" "\$REPO/flight/config/PrmDb.json" > "(\$RUN/PrmDb.json)"', script)
        self.assertIsNotNone(sed, "the script fills in @UPLINK@ into $RUN/PrmDb.json")
        self.assertIn('fprime-prm-write dat "$RUN/PrmDb.json"', script, "and builds PrmDb.dat from that copy")
        self.assertIn('mkdir -p "$WADS/uplink/.cfdp-tmp"', script)
        wads = "/home/someone/doom/wads"
        built = json.loads(_read("flight", "config", "PrmDb.json").replace("@UPLINK@", sed.group(1).replace("$WADS", wads)))
        for channel in built["DoomSat.cfdpManager"]["ChannelConfig"]:
            self.assertEqual(channel["tmp_dir"], wads + "/uplink/.cfdp-tmp")
            # F' v4.3.0 renames a failed poll file onto fail_dir itself, so a directory there only means "delete";
            # empty says so (and DoomSat runs no polls)
            self.assertEqual(channel["fail_dir"], "")
            self.assertEqual(channel["move_dir"], "")

    def test_a_lossy_receive_remembers_what_it_already_has(self):
        # Stock F' tracks NakMaxSegments (58) received runs per transaction and forgets data past that, so a lossy
        # upload is mostly resent. The override has to be built (CMakeLists) and copied (wsl_sync.sh) to count.
        rx = re.search(r"^#define CFDP_CHANNEL_NUM_RX_CHUNKS_PER_TRANSACTION \{(\d+),", CFDP_HPP, re.M)
        self.assertIsNotNone(rx, "channel 0's RX chunk count is a number in flight/config/CfdpCfg.hpp")
        self.assertGreaterEqual(int(rx.group(1)), 1024)
        self.assertLess(int(rx.group(1)), 65536, "ChunkIdx is a U16")
        self.assertIn('"${CMAKE_CURRENT_LIST_DIR}/CfdpCfg.hpp"', _read("flight", "config", "CMakeLists.txt"))
        self.assertIn("$SRC/config/CfdpCfg.hpp", _read("scripts", "wsl_sync.sh"))

    def test_commit_wad_takes_a_bare_name(self):
        self.assertRegex(FPP, r"async command COMMIT_WAD\(\s*part: string size 40")
        self.assertIn(") opcode 0x07", FPP[FPP.index("async command COMMIT_WAD("):][:600])


class TestTheLauncher(unittest.TestCase):
    """ground/yamcs/launch.py against a stand-in fprime_yamcs: key order kept, links moved only with DOOMSAT_RELAY."""

    def load(self):
        stub = types.ModuleType("fprime_yamcs.__main__")
        stub.main = lambda: 0
        pkg = types.ModuleType("fprime_yamcs")
        with mock.patch.dict(sys.modules, {"fprime_yamcs": pkg, "fprime_yamcs.__main__": stub}):
            spec = importlib.util.spec_from_file_location("doomsat_launch", os.path.join(ROOT, "ground", "yamcs", "launch.py"))
            mod = importlib.util.module_from_spec(spec)
            real = yaml.safe_dump
            try:
                spec.loader.exec_module(mod)
            finally:
                yaml.safe_dump = real   # the module patches yaml for the launcher's process; undo it here
        return mod

    def config(self):
        return {"streamConfig": {"tm": ["tm_realtime"], "tc": ["tc_realtime"], "sqlFile": "x.sql"},
                "dataLinks": [{"name": "UDP_TM_IN", "class": "org.yamcs.tctm.ccsds.UdpTmFrameLink", "port": 50000},
                              {"name": "UDP_TC_OUT", "class": "org.yamcs.tctm.ccsds.UdpTcFrameLink", "port": 50001},
                              {"name": "UDP_TM_SPLIT_IN", "class": "org.yamcs.tctm.UdpTmDataLink", "port": 50002}]}

    def test_the_key_order_is_kept(self):
        mod = self.load()
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DOOMSAT_RELAY", None)
            text = mod.safe_dump(self.config())
        self.assertLess(text.index("tm:"), text.index("sqlFile:"))
        self.assertIn("port: 50000", text)

    def test_the_relay_moves_only_the_frame_links(self):
        mod = self.load()
        with mock.patch.dict(os.environ, {"DOOMSAT_RELAY": "1"}):
            data = yaml.safe_load(mod.safe_dump(self.config()))
        ports = {link["name"]: link["port"] for link in data["dataLinks"]}
        self.assertEqual(ports, {"UDP_TM_IN": 51000, "UDP_TC_OUT": 51001, "UDP_TM_SPLIT_IN": 50002})


class TestTheRelay(unittest.TestCase):
    def free_port(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        return port

    def run_relay(self, loss, n=200):
        listen, target = self.free_port(), socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        target.bind(("127.0.0.1", 0))
        target.settimeout(0.5)
        other = f"{self.free_port()}:{self.free_port()}"
        proc = subprocess.Popen([sys.executable, os.path.join(ROOT, "tools", "lossy_relay.py"), "--loss", str(loss),
                                 "--seed", "3", "--report", "0", "--tm", f"{listen}:{target.getsockname()[1]}",
                                 "--tc", other], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        got = []
        self.final = ""

        def drain():   # read as it arrives: a default receive buffer holds only about 256 small datagrams
            try:
                while True:
                    got.append(int.from_bytes(target.recv(16), "big"))
            except socket.timeout:
                pass

        out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            for _ in ("TM", "TC"):
                self.assertTrue(proc.stdout.readline().startswith("[relay]"), "the relay did not start")
            reader = threading.Thread(target=drain)
            reader.start()
            for i in range(n):
                out.sendto(i.to_bytes(4, "big"), ("127.0.0.1", listen))
                time.sleep(0.0005)
            reader.join()
            return got
        finally:
            proc.terminate()
            self.final = proc.communicate(timeout=5)[0]
            out.close()
            target.close()

    def test_no_loss_is_a_pass_through(self):
        self.assertEqual(self.run_relay(0), list(range(200)))
        self.assertIn("TM sent 200 dropped 0", self.final, "SIGTERM still prints the final counts")

    def test_loss_drops_about_that_share_and_never_reorders(self):
        got = self.run_relay(20, n=500)
        self.assertEqual(got, sorted(got))
        self.assertTrue(330 < len(got) < 470, len(got))
        self.assertIn(f"TM sent {len(got)} dropped {500 - len(got)}", self.final)


if __name__ == "__main__":
    unittest.main()

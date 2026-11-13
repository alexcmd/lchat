"""system_info / sys_alerts / system_facts in bin/lchat-web. macOS is simulated by stubbing the probe commands."""

import os
import sys
import unittest
from unittest import mock
from collections import namedtuple

HERE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(HERE, "..", "bin", "lchat-web")


def load():
    argv, sys.argv = sys.argv, ["lchat-web", "--port", "18999"]
    try:
        ns = {"__name__": "lchat_web_test", "__file__": WEB}
        exec(compile(open(WEB).read(), WEB, "exec"), ns)
        return ns
    finally:
        sys.argv = argv


NS = load()
DU = namedtuple("DU", "total used free")
GiB = 2**30

MAC_CMDS = {
    ("sw_vers", "-productVersion"): "15.1",
    ("sw_vers", "-buildVersion"): "24B83",
    ("sysctl", "-n", "machdep.cpu.brand_string"): "Apple M3 Pro",
    ("sysctl", "-n", "hw.model"): "Mac15,6",
    ("sysctl", "-n", "hw.optional.arm64"): "1",
    ("sysctl", "-n", "sysctl.proc_translated"): "0",
    ("sysctl", "-n", "hw.memsize"): str(36 * GiB),
    ("sysctl", "-n", "kern.memorystatus_level"): "7",
    (
        "pmset",
        "-g",
        "batt",
    ): "Now drawing from 'Battery Power'\n -InternalBattery-0 (id=1)\t12%; discharging; 1:02 remaining",
    ("defaults", "read", "-g", "AppleLocale"): "de_DE",
    (
        "scutil",
        "--nc",
        "list",
    ): '* (Connected)  ABCD-1234 PPP --> L2TP "Office VPN"  [PPP/L2TP]',
    ("/opt/homebrew/bin/brew", "--version"): "Homebrew 4.5.2",
}


class Patch:
    """temporarily replace names in the lchat-web namespace"""

    def __init__(self, **kw):
        self.kw, self.old = kw, {}

    def __enter__(self):
        NS["_ALERTS"][:] = [0.0, ""]
        for k, v in self.kw.items():
            self.old[k] = NS[k]
            NS[k] = v

    def __exit__(self, *exc):
        NS.update(self.old)
        NS["_ALERTS"][:] = [0.0, ""]


class LinuxHost(unittest.TestCase):
    @unittest.skipUnless(sys.platform.startswith("linux"), "linux host")
    def test_paragraph(self):
        si = NS["system_info"]()
        self.assertIn("OS: ", si)
        self.assertIn("kernel Linux", si)
        self.assertIn("hardware ", si)
        self.assertRegex(
            si, r"native package manager: \w+ \(installed: \S+ [\d.?]+ at /"
        )
        self.assertNotRegex(si, r"[0-9a-f]{2}(:[0-9a-f]{2}){5}")  # no MAC addresses
        self.assertLess(len(si), 2400)  # ~600 tokens: it is in every turn's prompt

    def test_alerts_is_str_and_cached(self):
        a = NS["sys_alerts"]()
        self.assertIsInstance(a, str)
        self.assertIs(NS["sys_alerts"](), NS["_ALERTS"][1])


class MacSimulated(unittest.TestCase):
    def stub(self, argv, timeout=3):
        return MAC_CMDS.get(tuple(argv), "")

    def find(self, name):
        return "/opt/homebrew/bin/brew" if name == "brew" else None

    def test_paragraph(self):
        with Patch(MACOS=True, _cmd=self.stub, _find=self.find):
            si = NS["system_info"]()
        self.assertIn("OS: macOS 15.1 (24B83)", si)
        self.assertIn(
            "hardware Apple Silicon Mac Mac15,6, Apple M3 Pro, laptop (has a battery)",
            si,
        )
        self.assertIn(
            "native package manager: brew (installed: brew 4.5.2 at /opt/homebrew/bin/brew)",
            si,
        )
        self.assertIn("VPN: Office VPN (PPP/L2TP, connected)", si)
        self.assertIn("locale de_DE", si)
        self.assertIn("36 GiB RAM", si)
        self.assertNotIn("Rosetta", si)

    def test_rosetta_note(self):
        cmds = dict(MAC_CMDS)
        cmds[("sysctl", "-n", "sysctl.proc_translated")] = "1"
        with Patch(
            MACOS=True,
            _cmd=lambda argv, timeout=3: cmds.get(tuple(argv), ""),
            _find=self.find,
        ):
            self.assertIn("Rosetta", NS["system_info"]())

    def test_alerts(self):
        with Patch(
            MACOS=True,
            _cmd=self.stub,
            _disks=lambda: [("/", DU(500 * GiB, 495 * GiB, 5 * GiB))],
        ):
            a = NS["sys_alerts"]()
        self.assertIn("disk / 99% used, 5.0 GiB free", a)
        self.assertIn("low memory: 7% available", a)
        self.assertIn("battery 12% and discharging", a)

    def test_facts_use_mac_commands(self):
        ran = []
        with Patch(MACOS=True, _sh=lambda cmd, timeout=8: ran.append(cmd) or "x"):
            NS["t_system_facts"]("network", sess=None, alive=lambda: True)
        self.assertTrue(any("networksetup" in c for c in ran), ran)
        self.assertFalse(any(c.startswith("ip ") for c in ran), ran)


class Alerts(unittest.TestCase):
    def test_quiet_when_normal(self):
        with Patch(
            MACOS=False,
            _disks=lambda: [("/", DU(100 * GiB, 40 * GiB, 60 * GiB))],
            _mem_state=lambda: (0.5, ""),
            _battery=lambda: (80, True),
        ), mock.patch("os.getloadavg", return_value=(0.5, 0.5, 0.5)):   # not the real load of the test machine
            self.assertEqual(NS["sys_alerts"](), "")

    def test_df_style_percent(self):
        # 5 GiB fs, 3.6 used, 0.8 free (reserved blocks): df says 82%, not 72%
        with Patch(
            _disks=lambda: [("/", DU(5 * GiB, int(3.6 * GiB), int(0.8 * GiB)))],
            _mem_state=lambda: (0.5, ""),
            _battery=lambda: None,
        ):
            self.assertIn("disk / 82% used, 0.8 GiB free", NS["sys_alerts"]())


class Tool(unittest.TestCase):
    def test_schema_matches_topics(self):
        enum = NS["TOOLS"]["system_facts"][1]["properties"]["topic"]["enum"]
        self.assertEqual(set(enum), set(NS["FACT_TOPICS"]))
        self.assertFalse(
            NS["TOOLS"]["system_facts"][2], "read-only tool must not need approval"
        )

    def test_unknown_topic(self):
        r = NS["t_system_facts"]("nope", sess=None, alive=lambda: True)
        self.assertTrue(r["error"])

    def test_mac_addresses_redacted(self):
        with Patch(_sh=lambda cmd, timeout=8: "wlan0 UP 14:d4:24:3c:ca:81 <BROADCAST>"):
            t = NS["t_system_facts"]("network", sess=None, alive=lambda: True)["text"]
        self.assertNotIn("14:d4", t)
        self.assertIn("<mac>", t)

    def test_cancel(self):
        with Patch(_sh=lambda cmd, timeout=8: "x"):
            with self.assertRaises(NS["Cancelled"]):
                NS["t_system_facts"]("all", sess=None, alive=lambda: False)

    def test_sched_read_level(self):
        sys.path.insert(0, os.path.join(HERE, "..", "bin"))
        import lchat_sched

        self.assertIn("system_facts", lchat_sched.LEVEL_TOOLS["read"])


if __name__ == "__main__":
    unittest.main()

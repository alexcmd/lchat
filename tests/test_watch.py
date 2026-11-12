"""Tests for bin/lchat_watch.py (stdlib unittest only)."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime
from unittest import mock

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bin")
)

import lchat_watch as lw


class FakeClock:
    def __init__(self, t=1_000_000.0):
        self.t = float(t)

    def __call__(self):
        return self.t


def _ev(
    ts,
    unit="foo.service",
    message="boom 42",
    severity="error",
    kind="log",
    source="journal",
):
    return {
        "ts": ts,
        "source": source,
        "unit": unit,
        "severity": severity,
        "kind": kind,
        "message": message,
        "raw": message,
    }


class NormalizeTests(unittest.TestCase):
    def test_pid_number_path_hex_variants_share_fingerprint(self):
        a = "Failed to open /var/lib/milvus/db.sqlite pid [123] code 0x1f at 10.0.0.5"
        b = "Failed to open /srv/other/x.dat pid [98765] code 0xdeadbeef at 192.168.1.9"
        ta, tb = lw.normalize(a), lw.normalize(b)
        self.assertEqual(ta, tb)
        self.assertEqual(
            lw.fingerprint("linux", "milvus.service", ta),
            lw.fingerprint("linux", "milvus.service", tb),
        )
        self.assertIn("<path>", ta)
        self.assertIn("<ip>", ta)
        self.assertIn("<hex>", ta)
        self.assertIn("[<n>]", ta)

    def test_uuid_quoted_decimal_and_long_hex(self):
        t = lw.normalize(
            'job 123e4567-e89b-12d3-a456-426614174000 said "bad" value '
            "'x y' ratio 3.75 token 9f8e7d6c5b4a"
        )
        self.assertIn("<uuid>", t)
        self.assertIn("<str>", t)
        self.assertIn("<n>", t)
        self.assertNotIn("3.75", t)
        self.assertIn("<hex>", t)

    def test_ipv6_and_whitespace_collapse(self):
        t = lw.normalize("peer   fe80::1   dropped\t\tconn")
        self.assertEqual(t, "peer <ip> dropped conn")

    def test_unit_names_kept_intact(self):
        t = lw.normalize("Starting user@1000.service after 5 seconds")
        self.assertIn("user@1000.service", t)
        self.assertIn("<n> seconds", t)

    def test_unit_instance_ids_share_fingerprint(self):
        t = "Could not load plugin"
        for a, b in [("app-org.kde.dolphin-52066.scope", "app-org.kde.dolphin-60001.scope"),
                     ("app-com.google.Chrome@c77a8f366d074a16.service", "app-com.google.Chrome@0b1d2e3f.service"),
                     ("dbus-:1.1-org.kde.kdeconnect@0.service", "dbus-:1.7-org.kde.kdeconnect@3.service"),
                     ("run-u123abc.service", "run-u999fff.service")]:
            self.assertEqual(lw.fingerprint("linux", a, t), lw.fingerprint("linux", b, t), a)
        self.assertNotEqual(lw.unit_key("user@1000.service"), "user.service")

    def test_different_unit_different_fingerprint(self):
        t = lw.normalize("boom")
        self.assertNotEqual(
            lw.fingerprint("linux", "a.service", t),
            lw.fingerprint("linux", "b.service", t),
        )

    def test_cap_300(self):
        self.assertLessEqual(len(lw.normalize("x " * 1000)), 300)


class JournalParserTests(unittest.TestCase):
    def test_user_unit_failed_record(self):
        line = json.dumps(
            {
                "__REALTIME_TIMESTAMP": "1794514888403915",
                "PRIORITY": "3",
                "MESSAGE": "Failed to start Create knowledge-base elements for finished rutracker downloads.",
                "MESSAGE_ID": "d9b373ed55a64feb8242e02dbe79a49c",
                "JOB_RESULT": "failed",
                "USER_UNIT": "rutracker-sync.service",
                "_SYSTEMD_UNIT": "user@1000.service",
                "SYSLOG_IDENTIFIER": "systemd",
                "_COMM": "systemd",
            }
        )
        ev = lw.parse_journal_json(line)
        self.assertEqual(ev["unit"], "rutracker-sync.service")
        self.assertEqual(ev["kind"], "service")
        self.assertEqual(ev["severity"], "error")
        self.assertAlmostEqual(ev["ts"], 1794514888.403915, places=3)

    def test_unit_failed_without_message_id_but_job_result(self):
        line = json.dumps(
            {
                "PRIORITY": "3",
                "MESSAGE": "x",
                "_SYSTEMD_UNIT": "user@1000.service",
                "USER_UNIT": "rutracker-sync.service",
                "JOB_RESULT": "failed",
                "_COMM": "systemd",
                "__REALTIME_TIMESTAMP": "1000000",
            }
        )
        ev = lw.parse_journal_json(line)
        self.assertEqual(ev["kind"], "service")

    def test_coredump_record(self):
        line = json.dumps(
            {
                "__REALTIME_TIMESTAMP": "1794514000000000",
                "PRIORITY": "2",
                "MESSAGE": "Process 4242 (foo) of user 1000 dumped core.",
                "MESSAGE_ID": "fc2e22bc6ee647b6b90729ab34a250b1",
                "COREDUMP_COMM": "foo",
                "COREDUMP_SIGNAL_NAME": "SIGSEGV",
                "SYSLOG_IDENTIFIER": "systemd-coredump",
                "_COMM": "systemd-coredum",
                "_SYSTEMD_UNIT": "systemd-coredump@0-4242-1000.service",
            }
        )
        ev = lw.parse_journal_json(line)
        self.assertEqual(ev["kind"], "crash")
        self.assertEqual(ev["severity"], "error")
        self.assertEqual(ev["unit"], "foo")
        self.assertEqual(
            lw._title("crash", ev["unit"], "", ev["raw"]), "crash: foo (SIGSEGV)"
        )

    def test_kernel_oom_line(self):
        line = json.dumps(
            {
                "__REALTIME_TIMESTAMP": "1794514000000000",
                "PRIORITY": "2",
                "MESSAGE": "Out of memory: Killed process 4321 (llama-server) total-vm:123kB",
                "_TRANSPORT": "kernel",
                "SYSLOG_IDENTIFIER": "kernel",
            }
        )
        ev = lw.parse_journal_json(line)
        self.assertEqual(ev["kind"], "oom")
        self.assertEqual(ev["severity"], "error")
        self.assertEqual(ev["unit"], "kernel")

    def test_warning_priority_and_byte_message(self):
        line = json.dumps(
            {
                "PRIORITY": "4",
                "MESSAGE": [72, 105, 33],
                "SYSLOG_IDENTIFIER": "app",
                "__REALTIME_TIMESTAMP": "2000000",
            }
        )
        ev = lw.parse_journal_json(line)
        self.assertEqual(ev["severity"], "warning")
        self.assertEqual(ev["message"], "Hi!")
        self.assertEqual(ev["kind"], "log")

    def test_info_priority_and_garbage_rejected(self):
        self.assertIsNone(
            lw.parse_journal_json(json.dumps({"PRIORITY": "6", "MESSAGE": "x"}))
        )
        self.assertIsNone(lw.parse_journal_json("not json"))


class SyslogParserTests(unittest.TestCase):
    def test_classic_line(self):
        ev = lw.parse_syslog_line(
            "Nov 12 21:00:00 steamdeck milvus[812]: ERROR: index load failed"
        )
        self.assertEqual(ev["unit"], "milvus")
        self.assertEqual(ev["severity"], "error")
        self.assertEqual(ev["message"], "ERROR: index load failed")
        self.assertEqual(
            time.strftime("%m-%d %H:%M:%S", time.localtime(ev["ts"])),
            "11-12 21:00:00",
        )

    def test_iso_line_warning(self):
        ev = lw.parse_syslog_line(
            "2026-11-12T21:00:01.123+01:00 steamdeck sshd[991]: warning: odd thing"
        )
        self.assertEqual(ev["unit"], "sshd")
        self.assertEqual(ev["severity"], "warning")
        self.assertAlmostEqual(
            ev["ts"],
            datetime(
                2026,
                11,
                12,
                21,
                0,
                1,
                123000,
                tzinfo=lw.datetime.fromisoformat("2026-11-12T21:00:01+01:00").tzinfo,
            ).timestamp(),
            places=2,
        )

    def test_rfc5424_segfault(self):
        ev = lw.parse_syslog_line(
            "<30>1 2026-11-12T21:00:02Z host app 123 ID1 - segfault in worker"
        )
        self.assertEqual(ev["unit"], "app")
        self.assertEqual(ev["severity"], "error")

    def test_info_line_ignored(self):
        self.assertIsNone(
            lw.parse_syslog_line("Nov 12 21:00:00 steamdeck milvus[812]: started ok")
        )
        self.assertIsNone(lw.parse_syslog_line("garbage"))


class DmesgParserTests(unittest.TestCase):
    def test_err_and_warn(self):
        ev = lw.parse_dmesg_line(
            "kern  :err   : [ 1234.567890] nvme 0000:01:00.0: I/O error",
            boot_time=1000.0,
        )
        self.assertEqual(ev["unit"], "kernel")
        self.assertEqual(ev["severity"], "error")
        self.assertAlmostEqual(ev["ts"], 2234.56789, places=3)
        w = lw.parse_dmesg_line("kern  :warn  : [ 2.5] something odd", boot_time=0.0)
        self.assertEqual(w["severity"], "warning")

    def test_other_levels_ignored(self):
        self.assertIsNone(
            lw.parse_dmesg_line("kern  :info  : [ 2.5] fine", boot_time=0.0)
        )


class MacParserTests(unittest.TestCase):
    def test_error_is_warning_fault_is_error(self):
        err = json.dumps(
            {
                "eventMessage": "disk slow",
                "messageType": "Error",
                "processImagePath": "/usr/sbin/diskarbitrationd",
                "subsystem": "",
                "timestamp": "2026-11-12 21:00:00.123456+0100",
            }
        )
        ev = lw.parse_macos_ndjson(err)
        self.assertEqual(ev["severity"], "warning")
        self.assertEqual(ev["unit"], "diskarbitrationd")
        expect = datetime(
            2026,
            11,
            12,
            21,
            0,
            0,
            123456,
            tzinfo=lw.datetime.strptime("+0100", "%z").tzinfo,
        ).timestamp()
        self.assertAlmostEqual(ev["ts"], expect, places=3)

        fault = json.dumps(
            {
                "eventMessage": "kernel panic-ish",
                "messageType": "Fault",
                "subsystem": "com.apple.foo",
                "timestamp": "2026-11-12 21:00:00.000000+0100",
            }
        )
        ev2 = lw.parse_macos_ndjson(fault)
        self.assertEqual(ev2["severity"], "error")
        self.assertEqual(ev2["unit"], "com.apple.foo")

    def test_header_line_skipped(self):
        self.assertIsNone(
            lw.parse_macos_ndjson('Filtering the log data using "messageType == error"')
        )


class IpsParserTests(unittest.TestCase):
    def test_header_and_body(self):
        text = "\n".join(
            [
                json.dumps(
                    {
                        "app_name": "bendy",
                        "timestamp": "2026-11-12 21:00:00.00 +0100",
                        "bug_type": "309",
                        "name": "bendy",
                    }
                ),
                json.dumps(
                    {
                        "procName": "bendy",
                        "exception": {"type": "EXC_BAD_ACCESS", "signal": "SIGSEGV"},
                    }
                ),
            ]
        )
        ev = lw.parse_ips(text, "/x/bendy-2026.ips")
        self.assertEqual(ev["kind"], "crash")
        self.assertEqual(ev["severity"], "error")
        self.assertEqual(ev["unit"], "bendy")
        self.assertEqual(ev["message"], "crashed: EXC_BAD_ACCESS SIGSEGV")

    def test_missing_body_tolerated(self):
        text = json.dumps(
            {"app_name": "helper", "timestamp": "2026-11-12 21:00:00.00 +0100"}
        )
        ev = lw.parse_ips(text, "/x/helper.ips")
        self.assertEqual(ev["unit"], "helper")
        self.assertEqual(ev["message"], "crashed")

    def test_unparseable_header_returns_none(self):
        self.assertIsNone(lw.parse_ips("not json at all", "/x/a.ips"))


class SystemctlParserTests(unittest.TestCase):
    def test_failed_units_json(self):
        text = json.dumps(
            [
                {
                    "unit": "milvus.service",
                    "load": "loaded",
                    "active": "failed",
                    "sub": "failed",
                    "description": "Milvus Standalone Server",
                },
                {
                    "unit": "systemd-repart.service",
                    "load": "loaded",
                    "active": "failed",
                    "sub": "failed",
                    "description": "Repartition Root Disk",
                },
            ]
        )
        evs = lw.parse_systemctl_failed(text, "system")
        self.assertEqual(
            [e["unit"] for e in evs], ["milvus.service", "systemd-repart.service"]
        )
        self.assertEqual(
            evs[0]["message"], "milvus.service failed (Milvus Standalone Server)"
        )
        self.assertEqual(evs[0]["source"], "systemd-system")
        self.assertEqual(evs[0]["kind"], "service")
        user = lw.parse_systemctl_failed(text, "user")
        self.assertEqual(user[0]["source"], "systemd-user")

    def test_bad_json_gives_nothing(self):
        self.assertEqual(lw.parse_systemctl_failed("oops", "system"), [])


class LaunchctlTests(unittest.TestCase):
    TEXT = "PID\tStatus\tLabel\n-\t78\tcom.foo.bar\n123\t0\tcom.ok\n-\t-15\tcom.sig\n"

    def test_parse(self):
        table = lw.parse_launchctl_list(self.TEXT)
        self.assertEqual(table["com.foo.bar"], (None, 78))
        self.assertEqual(table["com.ok"], (123, 0))
        self.assertNotIn("PID", table)

    def test_diff_emits_on_zero_to_nonzero_or_absent(self):
        prev = {"com.a": (None, 3), "com.ok": (5, 0)}
        cur = {
            "com.a": (None, 3),
            "com.ok": (None, 1),
            "com.new": (None, 2),
            "com.zero": (1, 0),
        }
        evs = lw.launchd_transitions(prev, cur, ts=10.0)
        labels = [e["unit"] for e in evs]
        self.assertEqual(labels, ["com.new", "com.ok"])
        self.assertEqual(evs[0]["message"], "launchd job com.new exited with status 2")
        self.assertEqual(evs[0]["kind"], "service")


class BuildNotifyTests(unittest.TestCase):
    def test_macos_escaping(self):
        cmd = lw.build_notify_command("darwin", 'say "hi"', 'back\\slash "q"')
        self.assertEqual(cmd[0], "osascript")
        self.assertEqual(cmd[1], "-e")
        self.assertIn(
            'display notification "back\\\\slash \\"q\\"" with title "say \\"hi\\""',
            cmd[2],
        )

    def test_linux_prefers_notify_send_then_gdbus(self):
        cmd = lw.build_notify_command(
            "linux", "T", "B", which=lambda n: "/usr/bin/" + n
        )
        self.assertEqual(cmd[0], "notify-send")
        cmd = lw.build_notify_command(
            "linux",
            "T",
            "B",
            which=lambda n: "/usr/bin/gdbus" if n == "gdbus" else None,
        )
        self.assertEqual(cmd[0], "gdbus")
        self.assertIn('"T"', cmd)
        self.assertIn('"B"', cmd)
        self.assertIsNone(
            lw.build_notify_command("linux", "T", "B", which=lambda n: None)
        )

    def test_send_never_raises(self):
        with mock.patch.object(lw.subprocess, "run", side_effect=OSError("boom")):
            self.assertFalse(lw.send_desktop("t", "b", "linux"))


class RateLimiterTests(unittest.TestCase):
    def test_limit_then_window_resets(self):
        rl = lw.RateLimiter(limit=200, window=1.0)
        self.assertEqual(sum(rl.allow(100.0) for _ in range(205)), 200)
        self.assertTrue(rl.allow(101.0))

    def test_watcher_counts_dropped(self):
        with tempfile.TemporaryDirectory() as d:
            clock = FakeClock()
            w = lw.Watcher(
                os.path.join(d, "i.db"),
                platform="linux",
                learn_seconds=0,
                notify=False,
                ignore_file=os.path.join(d, "none"),
                clock=clock,
            )
            w._add_source("journal", mode="test")
            for _ in range(205):
                w._emit("journal", _ev(clock.t, message="same"))
            st = {s["name"]: s for s in w.status()}["journal"]
            self.assertEqual(st["events"], 200)
            self.assertEqual(st["dropped"], 5)


class WatcherRuleTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name
        self.clock = FakeClock()
        self.ignore = os.path.join(self.dir, "ignore")
        self.db = os.path.join(self.dir, "issues.db")

    def tearDown(self):
        self._tmp.cleanup()

    def watcher(self, learn=120, notify=False, db=None):
        return lw.Watcher(
            db or self.db,
            platform="linux",
            learn_seconds=learn,
            notify=notify,
            ignore_file=self.ignore,
            clock=self.clock,
        )

    def test_new_error_group_after_learning_returns_group(self):
        w = self.watcher(learn=120)
        got = w.ingest(_ev(self.clock.t + 500, message="fresh error"))
        self.assertIsNotNone(got)
        self.assertEqual(got["status"], "new")

    def test_service_notifies_even_while_learning(self):
        w = self.watcher(learn=120)
        got = w.ingest(
            _ev(
                self.clock.t + 5,
                unit="milvus.service",
                message="milvus.service failed (x)",
                kind="service",
                source="systemd-system",
            )
        )
        self.assertIsNotNone(got)
        self.assertEqual(got["title"], "milvus.service failed")
        self.assertEqual(got["template"], "milvus.service failed")

    def test_warning_needs_three_hits_in_ten_minutes(self):
        w = self.watcher(learn=0)
        t = self.clock.t
        self.assertIsNone(w.ingest(_ev(t, severity="warning", message="slow disk")))
        self.assertIsNone(
            w.ingest(_ev(t + 60, severity="warning", message="slow disk"))
        )
        self.assertEqual(w.list()[0]["status"], "background")
        got = w.ingest(_ev(t + 120, severity="warning", message="slow disk"))
        self.assertIsNotNone(got)
        self.assertEqual(got["status"], "new")

    def test_warning_hits_spread_beyond_ten_minutes_do_not_count(self):
        w = self.watcher(learn=0)
        t = self.clock.t
        w.ingest(_ev(t, severity="warning", message="slow disk"))
        w.ingest(_ev(t + 400, severity="warning", message="slow disk"))
        self.assertIsNone(
            w.ingest(_ev(t + 1000, severity="warning", message="slow disk"))
        )
        self.assertEqual(w.list()[0]["status"], "background")
        self.assertEqual(w.list()[0]["count"], 3)

    def test_ack_then_recur_within_hour_stays_seen(self):
        w = self.watcher(learn=0)
        t = self.clock.t
        w.ingest(_ev(t, message="err one"))
        fp = w.list()[0]["fp"]
        self.assertTrue(w.ack(fp))
        self.assertEqual(w.list()[0]["status"], "seen")
        self.assertIsNone(w.ingest(_ev(t + 1800, message="err one")))
        self.assertEqual(w.list()[0]["status"], "seen")

    def test_ack_then_recur_after_hour_goes_new(self):
        w = self.watcher(learn=0)
        t = self.clock.t
        w.ingest(_ev(t, message="err one"))
        fp = w.list()[0]["fp"]
        w.ack(fp)
        acked = w.list()[0]["acked_at"]
        got = w.ingest(_ev(acked + 3700, message="err one"))
        self.assertIsNotNone(got)
        self.assertEqual(w.list()[0]["status"], "new")

    def test_mute_suppresses_notification(self):
        w = self.watcher(learn=0)
        t = self.clock.t
        w.ingest(_ev(t, message="noisy"))
        fp = w.list()[0]["fp"]
        self.assertTrue(w.mute(fp))
        self.assertIsNone(w.ingest(_ev(t + 10, message="noisy")))
        self.assertEqual(w.list(), [])
        muted = w.list(include_muted=True)
        self.assertEqual(muted[0]["status"], "muted")
        self.assertEqual(muted[0]["count"], 2)
        self.assertTrue(w.unmute(fp))
        self.assertEqual(w.list()[0]["status"], "seen")

    def test_missing_ignore_file_means_no_ignores(self):
        w = self.watcher(learn=0)
        w.ingest(_ev(self.clock.t, message="anything"))
        self.assertEqual(len(w.list()), 1)

    def test_groups_listed_newest_first_and_counted(self):
        w = self.watcher(learn=0)
        t = self.clock.t
        w.ingest(_ev(t, unit="a.service", message="one"))
        w.ingest(_ev(t + 5, unit="b.service", message="two"))
        w.ingest(_ev(t + 6, unit="a.service", message="one"))
        groups = w.list()
        self.assertEqual([g["unit"] for g in groups], ["a.service", "b.service"])
        self.assertEqual(groups[0]["count"], 2)
        self.assertEqual(len(groups[0]["samples"]), 2)

    def test_severity_is_worst_seen(self):
        w = self.watcher(learn=0)
        t = self.clock.t
        w.ingest(_ev(t, severity="warning", message="thing"))
        w.ingest(_ev(t + 1, severity="error", message="thing"))
        w.ingest(_ev(t + 2, severity="warning", message="thing"))
        self.assertEqual(w.list()[0]["severity"], "error")

    def test_service_duplicate_from_two_sources_counted_once(self):
        w = self.watcher(learn=0)
        t = self.clock.t
        ev1 = _ev(
            t,
            unit="rutracker-sync.service",
            message="x",
            kind="service",
            source="journal",
        )
        ev2 = _ev(
            t + 2,
            unit="rutracker-sync.service",
            message="rutracker-sync.service failed (y)",
            kind="service",
            source="systemd-user",
        )
        w.ingest(ev1)
        self.assertIsNone(w.ingest(ev2))
        self.assertEqual(w.list()[0]["count"], 1)

    def test_digest_when_more_than_three_groups_in_window(self):
        w = self.watcher(learn=0, notify=True)
        sent = []
        with mock.patch.object(
            lw,
            "send_desktop",
            side_effect=lambda t, b, p=None: sent.append((t, b)) or True,
        ):
            for i in range(5):
                w.ingest(
                    _ev(
                        self.clock.t,
                        unit=f"svc{i}.service",
                        message="down",
                        kind="service",
                    )
                )
        self.assertEqual(len(sent), 4)  # three individual + one digest
        self.assertEqual(sent[-1], ("lchat", "4 new system issues"))

    def test_notifications_off_sends_nothing(self):
        w = self.watcher(learn=0, notify=False)
        with mock.patch.object(lw, "send_desktop") as sd:
            w.ingest(_ev(self.clock.t, message="err"))
        sd.assert_not_called()

    def test_on_notifiable_callback_and_persistence(self):
        w = self.watcher(learn=0)
        seen = []
        w.on_notifiable = seen.append
        w.ingest(_ev(self.clock.t, message="persist me"))
        self.assertEqual(len(seen), 1)
        del w
        w2 = self.watcher(learn=0)
        self.assertEqual(len(w2.list()), 1)
        self.assertTrue(w2.list()[0]["samples"][0].endswith("foo.service: persist me"))

    def test_set_chat_and_chat_prompt(self):
        w = self.watcher(learn=0)
        w.ingest(_ev(self.clock.t, unit="foo", message="widget exploded at 12:00:01"))
        fp = w.list()[0]["fp"]
        p = w.chat_prompt(fp)
        self.assertIsNone(p["chat_id"])
        self.assertIn("widget exploded", p["prompt"])
        self.assertIn("```text", p["prompt"])
        self.assertTrue(
            p["prompt"]
            .rstrip()
            .endswith(
                "Explain what this issue means on this system, whether it is harmful, and how to fix it. "
                "Verify with commands before changing anything."
            )
        )
        self.assertTrue(w.set_chat(fp, "chat-123"))
        self.assertEqual(w.chat_prompt(fp)["chat_id"], "chat-123")

    def test_status_empty_before_start(self):
        self.assertEqual(self.watcher().status(), [])


class NormalizeV2Tests(unittest.TestCase):
    def test_qmltype_and_dash_numbers_become_placeholders(self):
        self.assertEqual(
            lw.normalize("ButtonGrid_QMLTYPE_53 ready"), "ButtonGrid_QMLTYPE_<n> ready"
        )
        self.assertEqual(lw.normalize("worker-3 stopped"), "worker-<n> stopped")

    def test_instance_ids_and_dotted_versions(self):
        self.assertIn("user@1000.service", lw.normalize("Starting user@1000.service"))
        self.assertIn("<n>", lw.normalize("version 1.2.3"))

    def test_dates_and_times_replaced(self):
        t = lw.normalize(
            "at 2026-11-12T10:00:00+01:00 and 2026-11-12 10:00:00.123 "
            "and Nov 12 10:00:00 tick 10:00:00"
        )
        self.assertNotIn("2026", t)
        self.assertNotIn("10:00", t)
        self.assertIn("<date>", t)
        self.assertIn("<time>", t)


class UnitKeyTests(unittest.TestCase):
    def test_numbered_services_stay_distinct(self):
        self.assertNotEqual(
            lw.unit_key("postgresql-14.service"), lw.unit_key("postgresql-15.service")
        )
        self.assertNotEqual(
            lw.fingerprint("linux", "postgresql-14.service", "x"),
            lw.fingerprint("linux", "postgresql-15.service", "x"),
        )

    def test_scope_and_app_session_ids_collapse(self):
        self.assertEqual(
            lw.unit_key("app-org.kde.dolphin-52066.scope"),
            lw.unit_key("app-org.kde.dolphin-99.scope"),
        )
        self.assertEqual(
            lw.unit_key("session-3.scope"), lw.unit_key("session-12.scope")
        )

    def test_other_units_untouched(self):
        self.assertEqual(lw.unit_key("foo-12.service"), "foo-12.service")
        self.assertEqual(
            lw.unit_key("rutracker-sync.service"), "rutracker-sync.service"
        )


class JournalTrustTests(unittest.TestCase):
    def test_spoofed_unit_from_plain_process_ignored(self):
        line = json.dumps(
            {
                "_COMM": "python3",
                "_PID": "4321",
                "USER_UNIT": "milvus.service",
                "UNIT": "milvus.service",
                "SYSLOG_IDENTIFIER": "python3",
                "PRIORITY": "4",
                "MESSAGE": "hello",
                "__REALTIME_TIMESTAMP": "1000000",
            }
        )
        self.assertEqual(lw.parse_journal_json(line)["unit"], "python3")

    def test_systemd_record_may_name_user_unit(self):
        line = json.dumps(
            {
                "_COMM": "systemd",
                "_PID": "1",
                "USER_UNIT": "rutracker-sync.service",
                "PRIORITY": "3",
                "MESSAGE": "x",
                "__REALTIME_TIMESTAMP": "1000000",
            }
        )
        self.assertEqual(lw.parse_journal_json(line)["unit"], "rutracker-sync.service")


class IsoParseTests(unittest.TestCase):
    def test_nine_digit_fraction_and_compact_offset(self):
        ts = lw._parse_iso("2026-11-12T21:00:01.123456789+0100")
        expect = datetime.fromisoformat("2026-11-12T21:00:01.123456+01:00").timestamp()
        self.assertAlmostEqual(ts, expect, places=3)

    def test_z_suffix(self):
        ts = lw._parse_iso("2026-11-12T20:00:00Z")
        expect = datetime.fromisoformat("2026-11-12T20:00:00+00:00").timestamp()
        self.assertAlmostEqual(ts, expect, places=3)

    def test_garbage_falls_back_to_now(self):
        before = time.time()
        self.assertGreaterEqual(lw._parse_iso("nope"), before - 1)


class SystemdFallbackTests(unittest.TestCase):
    def test_plain_output_parsed(self):
        text = (
            "● foo.service loaded failed failed Foo does things\n"
            "  bar.service loaded failed failed\n\n"
            "0 loaded units listed.\n"
        )
        evs = lw.parse_systemctl_plain(text, "system")
        self.assertEqual([e["unit"] for e in evs], ["foo.service", "bar.service"])
        self.assertEqual(evs[0]["message"], "foo.service failed (Foo does things)")
        self.assertEqual(evs[1]["message"], "bar.service failed")

    def test_state_change_timestamp(self):
        self.assertEqual(
            lw.parse_state_change("StateChangeTimestamp=@1794515471\n"), 1794515471.0
        )
        self.assertIsNone(lw.parse_state_change("StateChangeTimestamp=n/a\n"))

    def test_corrupt_meta_is_none(self):
        self.assertIsNone(lw._load_unit_set("not json"))
        self.assertIsNone(lw._load_unit_set('{"a": 1}'))
        self.assertEqual(lw._load_unit_set('["a.service"]'), {"a.service"})

    def test_poll_falls_back_uses_state_change_time_and_survives_bad_meta(self):
        def fake_run(argv, timeout=5.0):
            if "list-units" in argv:
                if "--plain" in argv:
                    if "--user" in argv:
                        return 0, "", ""
                    return 0, "● foo.service loaded failed failed Foo\n", ""
                return 1, "", "unknown option -o"
            if "show" in argv:
                return 0, "StateChangeTimestamp=@1700000000\n", ""
            return 1, "", ""

        with tempfile.TemporaryDirectory() as d:
            w = lw.Watcher(
                os.path.join(d, "s.db"),
                platform="linux",
                learn_seconds=0,
                notify=False,
                ignore_file=os.path.join(d, "none"),
                clock=FakeClock(),
            )
            w._set_meta("systemd_failed_system", "garbage")  # corrupt saved state
            emitted = []
            with (
                mock.patch.object(lw, "_run_capture", side_effect=fake_run),
                mock.patch.object(
                    w, "_emit", side_effect=lambda n, ev: emitted.append(ev)
                ),
            ):
                w._poll_systemd()
            self.assertEqual([e["unit"] for e in emitted], ["foo.service"])
            self.assertEqual(emitted[0]["ts"], 1700000000.0)
            self.assertEqual(w._get_meta("systemd_failed_system"), '["foo.service"]')


class LaunchdRelaunchTests(unittest.TestCase):
    def test_same_failing_pid_silent_relaunch_emits(self):
        prev = {"com.x": (None, 78)}
        self.assertEqual(
            lw.launchd_transitions(prev, {"com.x": (None, 78)}, ts=1.0), []
        )
        evs = lw.launchd_transitions(prev, {"com.x": (4242, 78)}, ts=1.0)
        self.assertEqual([e["unit"] for e in evs], ["com.x"])
        again = lw.launchd_transitions(
            {"com.x": (4242, 78)}, {"com.x": (None, 78)}, ts=1.0
        )
        self.assertEqual(len(again), 1)

    def test_darwin_scan_marks_time_unknown(self):
        table = "PID\tStatus\tLabel\n-\t78\tcom.bad\n"

        def fake(argv, timeout=5.0):
            if argv[0] == "launchctl":
                return 0, table, ""
            return 0, "", ""

        with mock.patch.object(lw, "_run_capture", side_effect=fake):
            events, _notes = lw._scan_darwin("-1h")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["severity"], "warning")
        self.assertTrue(
            events[0]["message"].endswith("(last exit status; time unknown)")
        )


class SyslogCandidateTests(unittest.TestCase):
    def test_skips_unreadable_and_reports_when_none(self):
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            self.skipTest("root can read everything")
        with tempfile.TemporaryDirectory() as d:
            unread = os.path.join(d, "unreadable")
            good = os.path.join(d, "good")
            open(unread, "w").close()
            open(good, "w").close()
            os.chmod(unread, 0)
            try:
                self.assertEqual(lw._pick_syslog((unread, good)), (good, None))
                path, err = lw._pick_syslog((unread,))
                self.assertIsNone(path)
                self.assertIn("group adm", err)
            finally:
                os.chmod(unread, 0o600)


class RuntimeV2Tests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name
        self.clock = FakeClock()
        self.ignore = os.path.join(self.dir, "ignore")
        self.db = os.path.join(self.dir, "issues.db")

    def tearDown(self):
        self._tmp.cleanup()

    def watcher(self, learn=0, notify=False):
        return lw.Watcher(
            self.db,
            platform="linux",
            learn_seconds=learn,
            notify=notify,
            ignore_file=self.ignore,
            clock=self.clock,
        )

    def test_ignore_drops_and_group_absent(self):
        with open(self.ignore, "w", encoding="utf-8") as f:
            f.write("rutracker-sync\\.service: .*\n")
        w = self.watcher()
        self.assertIsNone(
            w.ingest(_ev(self.clock.t, unit="rutracker-sync.service", message="x"))
        )
        self.assertEqual(w.list(), [])
        self.assertIsNotNone(
            w.ingest(_ev(self.clock.t, unit="other.service", message="x"))
        )
        self.assertEqual([g["unit"] for g in w.list()], ["other.service"])

    def test_learning_group_stays_background_on_recurrence(self):
        w = self.watcher(learn=120)
        self.assertIsNone(w.ingest(_ev(self.clock.t + 10, message="same error")))
        self.assertIsNone(w.ingest(_ev(self.clock.t + 500, message="same error")))
        g = w.list()[0]
        self.assertEqual((g["status"], g["count"]), ("background", 2))
        self.assertEqual(w.new_count(), 0)

    def test_learning_group_promoted_by_special_kind(self):
        w = self.watcher(learn=120)
        w.ingest(_ev(self.clock.t + 10, message="same error"))
        got = w.ingest(_ev(self.clock.t + 20, message="same error", kind="crash"))
        self.assertIsNotNone(got)
        self.assertEqual(w.list()[0]["status"], "new")

    def test_service_events_bypass_rate_limit(self):
        w = self.watcher()
        w._add_source("journal", mode="test")
        for _ in range(205):
            w._emit("journal", _ev(self.clock.t, message="spam"))
        got = w._emit(
            "journal",
            _ev(
                self.clock.t,
                unit="milvus.service",
                message="x",
                kind="service",
                source="journal",
            ),
        )
        self.assertIsNotNone(got)
        st = {s["name"]: s for s in w.status()}["journal"]
        self.assertEqual(st["dropped"], 5)

    def test_notifications_sent_without_holding_lock(self):
        w = self.watcher(notify=True)
        owned = []

        def sender(title, body, platform=None):
            owned.append(w._lock._is_owned())
            return True

        with mock.patch.object(lw, "send_desktop", side_effect=sender):
            w.ingest(_ev(self.clock.t, message="boom one"))
            w.ingest(_ev(self.clock.t, unit="b.service", message="boom two"))
        self.assertEqual(len(owned), 2)
        self.assertFalse(any(owned))

    def test_tail_thread_survives_ingest_exception(self):
        path = os.path.join(self.dir, "messages")
        open(path, "w").close()
        w = self.watcher()
        w._add_source("syslog", mode="test")
        stop = threading.Event()
        calls = []

        def flaky(name, ev):
            calls.append(ev["message"])
            if len(calls) == 1:
                raise RuntimeError("database is locked")

        t = threading.Thread(
            target=w._tail_loop, args=("syslog", path, stop), daemon=True
        )
        with mock.patch.object(w, "_emit", side_effect=flaky):
            t.start()
            time.sleep(1.5)  # the first open starts at end of file
            with open(path, "a", encoding="utf-8") as f:
                f.write("Nov 12 21:00:00 h prog[1]: ERROR one\n")
            deadline = time.time() + 5
            while not calls and time.time() < deadline:
                time.sleep(0.05)
            self.assertTrue(t.is_alive())
            self.assertIn("database is locked", w.status()[0]["error"] or "")
            with open(path, "a", encoding="utf-8") as f:
                f.write("Nov 12 21:00:01 h prog[1]: ERROR two\n")
            deadline = time.time() + 5
            while len(calls) < 2 and time.time() < deadline:
                time.sleep(0.05)
        self.assertEqual(len(calls), 2)
        self.assertTrue(t.is_alive())
        stop.set()
        t.join(timeout=3)

    def test_stop_kills_stream_process_group(self):
        w = self.watcher()
        w._add_source("probe", mode="test")
        w._start_stream("probe", ["sleep", "30"], lambda line: None)
        deadline = time.time() + 5
        while not w._procs and time.time() < deadline:
            time.sleep(0.05)
        procs = list(w._procs)
        self.assertEqual(len(procs), 1)
        w.stop()
        self.assertIsNotNone(procs[0].poll())
        with self.assertRaises(ProcessLookupError):
            os.killpg(procs[0].pid, 0)

    def test_journal_cursor_saved_immediately_then_throttled(self):
        w = self.watcher()
        h = w._journal_handler("journal", "journal_cursor")
        h('{"__CURSOR":"s=abc1","PRIORITY":"6","MESSAGE":"x"}\n')
        self.assertEqual(w._get_meta("journal_cursor"), "s=abc1")
        h('{"__CURSOR":"s=abc2","PRIORITY":"6","MESSAGE":"x"}\n')
        self.assertEqual(w._get_meta("journal_cursor"), "s=abc1")
        w._cursor_saved["journal_cursor"] = ("s=abc1", 0.0)  # pretend the save is old
        h('{"__CURSOR":"s=abc3","PRIORITY":"6","MESSAGE":"x"}\n')
        self.assertEqual(w._get_meta("journal_cursor"), "s=abc3")

    def test_journal_argv_rebuilt_from_current_cursor(self):
        w = self.watcher()
        w._set_meta("journal_cursor", "s=bad")
        self.assertIn("--after-cursor=s=bad", w._journal_argv("journal_cursor", False))
        w._del_meta("journal_cursor")
        argv = w._journal_argv("journal_cursor", False)
        self.assertEqual(argv[-2:], ["-n", "0"])
        self.assertIn("--user", w._journal_argv("journal_cursor_user", True))

    def test_prune_ages_out_and_caps_table(self):
        base = 1_000_000_000.0
        clock = FakeClock(base)
        w = lw.Watcher(
            self.db,
            platform="linux",
            learn_seconds=0,
            notify=False,
            ignore_file=self.ignore,
            clock=clock,
        )
        old = base - 40 * 86400
        for unit, ts in [
            ("a.service", old),
            ("c.service", old),
            ("d.service", old),
            ("e.service", base - 100),
            ("f.service", base - 200),
            ("g.service", base - 300),
            ("h.service", base - 400),
        ]:
            w.ingest(_ev(ts, unit=unit, message="err"))
        w.ingest(_ev(old, unit="b.service", message="warn once", severity="warning"))
        fps = {g["unit"]: g["fp"] for g in w.list(include_muted=True, limit=None)}
        w.ack(fps["a.service"])
        w.ack(fps["e.service"])
        w.ack(fps["f.service"])
        w.ack(fps["g.service"])
        w.ack(fps["h.service"])
        w.mute(fps["c.service"])
        with mock.patch.object(lw, "MAX_ROWS", 3):
            w.prune(now=base)
        left = {g["unit"] for g in w.list(include_muted=True, limit=None)}
        self.assertEqual(left, {"c.service", "d.service", "e.service"})

    def test_sqlite_wal_and_busy_timeout(self):
        w = self.watcher()
        self.assertEqual(w._db.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertEqual(w._db.execute("PRAGMA busy_timeout").fetchone()[0], 5000)

    def test_list_limit(self):
        w = self.watcher()
        for i in range(3):
            w.ingest(_ev(self.clock.t + i, unit=f"u{i}.service", message="m"))
        self.assertEqual(len(w.list(limit=2)), 2)
        self.assertEqual(len(w.list(limit=None)), 3)
if __name__ == "__main__":
    unittest.main()


class SpoofedKindTests(unittest.TestCase):
    def test_non_systemd_process_cannot_fake_service_or_crash(self):
        import json as _j
        base = {"__REALTIME_TIMESTAMP": "1794515471594037", "PRIORITY": "6", "_COMM": "evil",
                "_PID": "4242", "MESSAGE": "x"}
        for extra in ({"MESSAGE_ID": lw.UNIT_FAILED_MSG_ID, "UNIT": "sshd.service"},
                      {"JOB_RESULT": "failed"},
                      {"MESSAGE_ID": lw.CRASH_MSG_ID, "COREDUMP_COMM": "sshd"}):
            self.assertIsNone(lw.parse_journal_json(_j.dumps({**base, **extra})), extra)
        real = {**base, "_COMM": "systemd-coredum", "MESSAGE_ID": lw.CRASH_MSG_ID, "COREDUMP_COMM": "foo"}
        ev = lw.parse_journal_json(_j.dumps(real))
        self.assertEqual((ev["kind"], ev["unit"]), ("crash", "foo"))


class KernelReportFoldTests(unittest.TestCase):
    def test_oom_dump_body_is_folded(self):
        import tempfile as _t
        d = _t.mkdtemp()
        w = lw.Watcher(os.path.join(d, "i.db"), learn_seconds=0, notify=False)
        t0 = 1794515000.0
        def ev(dt, msg, kind="log", sev="warning"):
            return {"ts": t0 + dt, "source": "journal", "unit": "kernel", "severity": sev, "kind": kind,
                    "message": msg, "raw": msg}
        w.ingest(ev(0, "llama invoked oom-killer: gfp_mask=0x140dca(GFP_HIGHUSER_MOVABLE), order=0", "oom", "error"))
        for i, m in enumerate(["Mem-Info:", "Node 0 DMA: 1*4kB (U) 2*8kB", "Free swap  = 0kB", "123 pages RAM"]):
            w.ingest(ev(0.1 * (i + 1), m))
        w.ingest(ev(0.6, "Out of memory: Killed process 4242 (llama) total-vm:1kB", "oom", "error"))
        w.ingest(ev(60, "Free swap  = 0kB"))   # a later, unrelated line is grouped again
        temps = sorted(g["template"] for g in w.list(limit=None))
        self.assertEqual(len(temps), 2, temps)   # Killed process + the later Free swap
        w.stop()

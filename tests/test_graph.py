"""Tests for bin/lchat_graph.py (stdlib unittest; entity extraction and the SQLite bridge index).

Written from the spec, not from the implementation. Known spec violations are kept as expectedFailure.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import unittest

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bin")
)

import lchat_graph as G


def _ports(ents):
    return {e for e in ents if e.startswith("port:")}


def _ips(ents):
    return {e for e in ents if e.startswith("ip:")}


def _names(ents):
    return {e for e in ents if e.startswith("name:")}


class EntitiesPortTest(unittest.TestCase):
    def test_port_word(self):
        self.assertIn("port:9952", G.entities("port 9952"))

    def test_port_word_russian(self):
        self.assertIn("port:8080", G.entities("порт 8080"))

    def test_port_russian_mixed_sentence(self):
        self.assertIn("port:8080", G.entities("Сервер слушает порту 8080 на этом Deck"))

    def test_host_port_ipv4_loopback(self):
        ents = G.entities("127.0.0.1:9952")
        self.assertIn("port:9952", ents)
        self.assertNotIn("ip:127.0.0.1", ents)

    def test_localhost_port(self):
        self.assertIn("port:3000", G.entities("localhost:3000"))

    def test_any_interface_port(self):
        self.assertIn("port:8000", G.entities("0.0.0.0:8000"))

    def test_listening_on_port(self):
        self.assertIn("port:9931", G.entities("listening on 9931"))

    def test_time_is_not_a_port(self):
        self.assertEqual(_ports(G.entities("the job ran at 10:00:12 today")), set())

    def test_apache_timestamp_is_not_a_port(self):
        self.assertEqual(
            _ports(G.entities('[12/Nov/2026:10:00:01 +0000] "GET / HTTP/1.1" 200')),
            set(),
        )

    def test_time_and_timestamp_next_to_real_port(self):
        # the real port is found; the time digits and the log timestamp contribute nothing
        ports = _ports(
            G.entities(
                "port 9952 bound at 10:00:12, request [12/Nov/2026:10:00:01 +0000]"
            )
        )
        self.assertEqual(ports, {"port:9952"})

    def test_year_is_not_a_port(self):
        self.assertEqual(_ports(G.entities("in 2026 the port changed")), set())

    def test_year_after_listening_is_not_a_port(self):
        self.assertEqual(_ports(G.entities("listening on 2026")), set())

    def test_common_ports_excluded(self):
        for text in (
            "port 22",
            "port 53",
            "port 80",
            "port 443",
            "localhost:80",
            "127.0.0.1:443",
        ):
            with self.subTest(text=text):
                self.assertEqual(_ports(G.entities(text)), set())

    def test_uncommon_neighbour_of_common_port_kept(self):
        self.assertIn("port:2222", G.entities("port 2222"))

    # was expectedFailure (fixed)
    def test_number_far_from_port_word_is_not_a_port(self):
        # Spec: "listening on 9931" -> port:9931. A bare number is a port only when it is next to a
        # port word (the module's own comment says so). Here 8192 is a token count, not a port.
        # Judgement call on the spec's wording; see report.
        ports = _ports(
            G.entities("model loaded with 8192 tokens of context, listening on 9931")
        )
        self.assertEqual(ports, {"port:9931"})


class EntitiesIpHostPathTest(unittest.TestCase):
    def test_ipv4(self):
        self.assertIn("ip:192.168.0.93", G.entities("192.168.0.93"))

    def test_loopback_and_any_are_excluded(self):
        self.assertEqual(_ips(G.entities("bind 127.0.0.1 or 0.0.0.0")), set())

    def test_out_of_range_octet_is_not_ip(self):
        self.assertEqual(_ips(G.entities("999.1.1.1")), set())

    def test_url_host(self):
        self.assertIn("host:huggingface.co", G.entities("https://huggingface.co/x"))

    def test_bare_local_host(self):
        self.assertIn("host:nas.local", G.entities("nas.local"))

    def test_absolute_path(self):
        self.assertIn(
            "path:/home/deck/models/qwen.gguf",
            G.entities("/home/deck/models/qwen.gguf"),
        )

    # was expectedFailure (fixed)
    def test_home_relative_path(self):
        # Spec: "~/backup/2026/" -> path:... with the ~ kept. Code returns path:/backup/2026/ (see report).
        self.assertIn("path:~/backup/2026/", G.entities("~/backup/2026/"))

    def test_path_is_lower_cased(self):
        self.assertIn(
            "path:/home/deck/models/qwen.gguf",
            G.entities("/Home/Deck/Models/Qwen.gguf"),
        )


class EntitiesFileUnitNameTest(unittest.TestCase):
    def test_file(self):
        self.assertIn("file:qwen3-4b-q4_k_m.gguf", G.entities("qwen3-4b-q4_k_m.gguf"))

    def test_unit(self):
        self.assertIn("unit:lchat.service", G.entities("lchat.service"))

    def test_name_with_digits_and_separators(self):
        self.assertEqual(G.entities("qwen3-4b-q4_k_m"), {"name:qwen3-4b-q4_k_m"})

    def test_name_python_version(self):
        self.assertEqual(G.entities("python3.12"), {"name:python3.12"})

    def test_plain_words_are_not_names(self):
        self.assertEqual(G.entities("the server restarted yesterday"), set())

    def test_version_number_is_not_a_name(self):
        self.assertEqual(_names(G.entities("upgraded to 1.2.3 last week")), set())

    def test_bare_v_number_is_not_a_name(self):
        self.assertEqual(_names(G.entities("node v22 is installed")), set())


class EntitiesEmptyAndMixedTest(unittest.TestCase):
    def test_empty_string(self):
        self.assertEqual(G.entities(""), set())

    def test_none(self):
        self.assertEqual(G.entities(None), set())

    def test_mixed_russian_english(self):
        text = (
            "Сервер 192.168.0.93, порт 8080, модель qwen3-4b-q4_k_m, "
            "файл /home/deck/models/qwen.gguf, юнит lchat.service, python3.12."
        )
        ents = G.entities(text)
        for expected in (
            "ip:192.168.0.93",
            "port:8080",
            "name:qwen3-4b-q4_k_m",
            "path:/home/deck/models/qwen.gguf",
            "unit:lchat.service",
            "name:python3.12",
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, ents)


SCHEMA_CHUNKS = "CREATE TABLE chunks(id INTEGER PRIMARY KEY, chat_id TEXT, seq INTEGER, role TEXT, text TEXT, vec BLOB)"


def _open_db():
    """A database with only the chunks table (as lchat has it). G.init() creates the graph tables."""
    con = sqlite3.connect(":memory:")
    con.execute(SCHEMA_CHUNKS)
    return con


def _insert_chunk(con, cid, chat, seq, text):
    con.execute(
        "INSERT INTO chunks(id, chat_id, seq, role, text, vec) VALUES(?,?,?,?,?,NULL)",
        (cid, chat, seq, "user", text),
    )


def _chunk(con, cid, chat, seq, text):
    """Save a chunk the way lchat does: insert the row, then index it."""
    _insert_chunk(con, cid, chat, seq, text)
    G.add_chunk(con, cid, chat, text)


UNRELATED = [
    "set the timezone and keyboard layout, see /home/deck/notes/todo.md",
    "nginx.service restarted after the config edit",
    "port 8081 is the dev server for the blog",
    "python3.11 venv recreated under /home/deck/venvs/",
    "printer is at 192.168.0.10 on the home network",
    "backup key rotated, old one kept in /home/deck/keys/",
    "exported the chart as qwen2-7b-q8_0 notes",
    "the tailnet DNS was flaky again",
    "installed gimp.desktop from the flatpak remote",
    "wrote the migration script migrate_v3.sh",
]


class InitSqliteTest(unittest.TestCase):
    def test_init_queues_existing_chunks_and_drains_in_batches(self):
        con = _open_db()  # chunks exist before the graph tables
        _insert_chunk(con, 1, "A", 0, "moved the embedding server to port 9952")
        _insert_chunk(con, 2, "B", 0, "load gemma-3-1b on whatever listens on 9952")
        _insert_chunk(con, 3, "C", 0, "nothing to see here")
        self.assertEqual(G.init(con), 3)          # queued, not indexed: start-up never blocks
        self.assertEqual(G.process_pending(con, limit=2), 2)
        self.assertEqual(G.process_pending(con, limit=2), 1)
        self.assertEqual(G.process_pending(con), 0)
        self.assertIn("port:9952", G.chunk_entities(con, [1])[1])
        self.assertIn("port:9952", G.chunk_entities(con, [2])[2])
        self.assertEqual(G.init(con), 0)          # same VERSION: nothing to redo
        con.close()

    def test_version_change_requeues_everything(self):
        con = _open_db()
        G.init(con)
        _chunk(con, 1, "A", 0, "port 9952")
        con.execute("UPDATE graph_meta SET v='0' WHERE k='version'")
        self.assertEqual(G.init(con), 1)
        self.assertEqual(G.chunk_entities(con, [1]).get(1, set()), set())
        G.process_pending(con)
        self.assertIn("port:9952", G.chunk_entities(con, [1])[1])
        con.close()

    def test_init_creates_tables(self):
        con = _open_db()
        self.assertEqual(G.init(con), 0)
        names = {
            r[0]
            for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        self.assertIn("ents", names)
        con.close()


class BridgeSqliteTest(unittest.TestCase):
    def setUp(self):
        self.con = _open_db()
        G.init(self.con)  # creates the graph tables on the empty chunks table

    def tearDown(self):
        self.con.close()

    def test_deleting_chunks_drops_their_entities(self):
        _chunk(self.con, 1, "X", 0, "port 9952 is the embedding server")
        _chunk(self.con, 2, "X", 1, "port 9953 is the reranker")
        _chunk(self.con, 4, "Y", 0, "port 9955 is someone else's server")
        self.con.execute("DELETE FROM chunks WHERE chat_id='X' AND seq>=1")   # what history_save does
        self.assertIn("port:9952", G.chunk_entities(self.con, [1])[1])
        self.assertEqual(G.chunk_entities(self.con, [2]).get(2, set()), set())
        self.assertIn("port:9955", G.chunk_entities(self.con, [4])[4])

    def test_older_writer_delete_then_rowid_reuse_does_not_leak(self):
        """An lchat-web without the graph deletes chat X (only chunks); a new chunk reuses X's rowid."""
        _chunk(self.con, 7, "X", 0, "the backup box answers on port 7777")
        self.con.execute("DELETE FROM chunks WHERE chat_id='X'")              # old writer: no graph code
        _chunk(self.con, 7, "B", 0, "the weather is nice")                    # rowid 7 reused
        _chunk(self.con, 8, "C", 0, "port 7777 again")
        self.assertEqual(G.chunk_entities(self.con, [7]).get(7, set()), set())
        res = G.bridge_candidates(self.con, {"port:7777": 1.0}, set(), set())
        self.assertEqual([r[0] for r in res], [8])

    def test_older_writer_insert_and_update_are_queued(self):
        _insert_chunk(self.con, 1, "A", 0, "moved to port 9952")              # old writer: no add_chunk
        self.assertEqual(G.chunk_entities(self.con, [1]).get(1, set()), set())
        G.process_pending(self.con)
        self.assertIn("port:9952", G.chunk_entities(self.con, [1])[1])
        self.con.execute("UPDATE chunks SET text='moved to port 9953' WHERE id=1")
        self.assertEqual(G.chunk_entities(self.con, [1]).get(1, set()), set())
        G.process_pending(self.con)
        self.assertEqual(G.chunk_entities(self.con, [1])[1], {"port:9953"})

    def test_many_seed_entities_stay_under_sqlite_variable_limit(self):
        seed = {f"file:f{i}.conf": 1.0 / (1 + i) for i in range(2000)}
        seed["port:9952"] = 5.0
        _chunk(self.con, 1, "A", 0, "port 9952")
        res = G.bridge_candidates(self.con, seed, set(), set())
        self.assertEqual([r[0] for r in res], [1])

    def test_multi_hop_bridge_from_port_reaches_other_chat(self):
        _chunk(
            self.con, 1, "A", 0, "moved the embedding server to port 9952 on nas.local"
        )
        _chunk(self.con, 2, "B", 0, "load gemma-3-1b on whatever listens on 9952")
        for i, text in enumerate(UNRELATED):
            _chunk(self.con, 100 + i, f"U{i}", 0, text)
        seed = {
            e: 1.0
            for e in G.entities("moved the embedding server to port 9952 on nas.local")
        }
        self.assertIn("port:9952", seed)
        res = G.bridge_candidates(
            self.con, seed, exclude_chats={"A"}, exclude_chunks=set(), limit=6
        )
        self.assertTrue(res, "expected B's chunk to be bridged")
        top_id, top_score, shared = res[0]
        self.assertEqual(top_id, 2)
        self.assertGreater(top_score, 0)
        self.assertIn("port:9952", shared)
        self.assertNotIn(1, [r[0] for r in res])

    def test_excluded_chat_is_never_returned(self):
        _chunk(self.con, 1, "A", 0, "moved the embedding server to port 9952")
        _chunk(self.con, 2, "A", 1, "port 9952 is still the embedding server")
        _chunk(self.con, 3, "B", 0, "load gemma-3-1b on whatever listens on 9952")
        res = G.bridge_candidates(
            self.con, {"port:9952": 1.0}, exclude_chats={"A"}, exclude_chunks=set()
        )
        self.assertEqual([r[0] for r in res], [3])

    def test_excluded_chunk_is_not_returned(self):
        _chunk(self.con, 1, "A", 0, "moved the embedding server to port 9952")
        _chunk(self.con, 2, "B", 0, "load gemma-3-1b on whatever listens on 9952")
        res = G.bridge_candidates(
            self.con, {"port:9952": 1.0}, exclude_chats=set(), exclude_chunks={2}
        )
        self.assertEqual([r[0] for r in res], [1])

    def test_limit_is_respected(self):
        for i in range(5):
            _chunk(self.con, 10 + i, f"B{i}", 0, f"listens on 9952 variant {i}")
        res = G.bridge_candidates(
            self.con,
            {"port:9952": 1.0},
            exclude_chats=set(),
            exclude_chunks=set(),
            limit=2,
        )
        self.assertLessEqual(len(res), 2)

    def test_port_in_20_of_25_chats_does_not_bridge(self):
        for i in range(20):
            _chunk(self.con, 100 + i, f"c{i:02d}", 0, "port 8080 is up on the dev box")
        _chunk(self.con, 200, "c20", 0, "moved the embedding server to port 9952")
        _chunk(self.con, 201, "c21", 0, "load gemma-3-1b on whatever listens on 9952")
        for i in range(22, 25):
            _chunk(self.con, 300 + i, f"c{i:02d}", 0, f"port 900{i - 20} is free")
        self.assertEqual(
            self.con.execute("SELECT COUNT(DISTINCT chat_id) FROM ents").fetchone()[0],
            25,
        )

        common = G.bridge_candidates(
            self.con, {"port:8080": 1.0}, exclude_chats={"c00"}, exclude_chunks=set()
        )
        self.assertEqual(
            common, [], "port 8080 is in 20 of 25 chats and must not bridge"
        )

        # positive control in the same database: a rare port does bridge
        rare = G.bridge_candidates(
            self.con, {"port:9952": 1.0}, exclude_chats={"c20"}, exclude_chunks=set()
        )
        self.assertEqual([r[0] for r in rare], [201])


if __name__ == "__main__":
    unittest.main()


class ReviewRegressionTest(unittest.TestCase):
    def test_dotted_text_is_fast(self):
        import time
        for t in ("a." * 600, "1." * 600, "x-" * 600 + ".com", "s://" * 300):
            t0 = time.perf_counter()
            G.entities(t)
            self.assertLess(time.perf_counter() - t0, 0.03, t[:20])

    def test_sizes_are_not_ports(self):
        self.assertNotIn("port:4096", G.entities("raise the limit from 3000 to 4096 MB, then listen again"))
        self.assertIn("port:9952", G.entities("it now listens on 9952"))

    def test_generic_files_and_api_paths_do_not_link(self):
        e = G.entities("edit config.json and main.py, then curl /v1/models")
        self.assertFalse({x for x in e if x in ("file:config.json", "file:main.py") or x.startswith("path:/v1/")})

    def test_shell_script_is_not_a_host(self):
        self.assertNotIn("host:backup.sh", G.entities("run backup.sh nightly"))

    def test_bare_home_dirs_do_not_link(self):
        e = G.entities("cd /home/deck/ && ls /Users/anna and /root, then open /home/deck/models/x.gguf")
        self.assertFalse({x for x in e if x in ("path:/home/deck/", "path:/users/anna", "path:/root")}, e)
        self.assertIn("path:/home/deck/models/x.gguf", e)

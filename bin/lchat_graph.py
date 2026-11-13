"""Entity graph over the chat history, for recall across chats.

Every chunk of a saved chat is scanned with cheap rules (no model calls) for concrete entities: ports, IP
addresses, hosts and URLs, file paths, file names, systemd units and versioned names such as model or package
builds. A chunk links to its entities; two chats are linked when they share a rare entity.

recall uses this for questions whose answer spans two chats (multi-hop): chat A "the embedding server moved to
port 9952", chat B "port 9952 now serves gemma-3-1b". The question names the server, not the port, so a search
finds A but not B. Expanding from A's entities (port 9952) reaches B.

Pure functions plus SQLite helpers; no network. Works the same on Linux and macOS.
"""

from __future__ import annotations

import math
import re
from collections import defaultdict

VERSION = (
    3  # bump when the extraction rules change: every chunk is queued for re-extraction once
)

_PORT = re.compile(
    r"(?:\b(?:port|ports|порт|порту|порта|портом)\s*(?:is|=|:|#|на|)?\s*|"
    r"(?:\b(?:localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1?\]|::)|\b\d{1,3}(?:\.\d{1,3}){3}|\*):)(\d{2,5})\b",
    re.IGNORECASE,
)
_PORT_BARE = re.compile(  # a bare 4-5 digit number right after a preposition ("answers on 9952", "from 8080 to 9952")
    r"(?:\b(?:on|at|to|from|на|с|со|по)\s+|(?:->|→)\s*)(\d{4,5})(?![\w.:/-])"
    r"(?!\s*(?:[kmgt]i?b|[kmgt]|ms|sec|s|%|px|tokens?|rpm|mhz|ghz|x)\b)",   # not "from 3000 to 4096 MB"
    re.IGNORECASE,
)  # chunk must mention ports
_PORT_WORD = re.compile(r"\b(?:port|ports|порт\w*|listen\w*|слуша\w*)\b", re.IGNORECASE)
_IP = re.compile(
    r"(?<![\d.])((?:25[0-5]|2[0-4]\d|1?\d?\d)(?:\.(?:25[0-5]|2[0-4]\d|1?\d?\d)){3})(?![\d.])"
)
_URL_HOST = re.compile(
    r"(?<![\w+.-])[a-z][a-z0-9+.-]{0,15}://(?:[^@/\s]{1,64}@)?([A-Za-z0-9.-]{1,253})", re.IGNORECASE
)
_HOST = re.compile(
    r"(?<![\w.-])((?:[a-z0-9-]{1,63}\.){1,6}(?:local|lan|home|internal|localdomain|com|org|net|io|dev|ai|"
    r"ru|de|uk|eu|cloud))\b(?![.-]\w)",
    re.IGNORECASE,
)
_PATH = re.compile(r"(?<![\w:/.~])((?:~/|/)[\w.@+-]+(?:/[\w.@+-]*)*)")
_FILE = re.compile(
    r"(?<![\w/.-])([\w@+-][\w.@+-]*\.(?:gguf|safetensors|service|timer|socket|conf|cfg|ini|toml|"
    r"ya?ml|json|py|sh|rs|go|js|ts|db|sqlite|log|iso|img|tar|gz|zst|zip|deb|rpm|pkg|dmg|app|"
    r"plist|desktop|AppImage))\b",
    re.IGNORECASE,
)
_UNIT = re.compile(r"(?<![\w@.-])([\w@.-]{1,100}\.(?:service|timer|socket|mount|target))\b")
# a name with letters AND digits and a separator: qwen3-4b-q4_k_m, python3.12, node-v22, nvidia-550
_IDENT = re.compile(
    r"(?<![\w./-])([a-z][a-z0-9]*(?:[-_.][a-z0-9]+)+)(?![\w/-])", re.IGNORECASE
)

# entities too generic to link anything (they appear everywhere, whatever the topic)
_STOP = {
    "ip:127.0.0.1",
    "ip:0.0.0.0",
    "ip:255.255.255.255",
    "ip:255.255.255.0",
    "ip:8.8.8.8",
    "ip:1.1.1.1",
    "host:localhost",
    "host:github.com",
    "host:google.com",
    "host:www.google.com",
    "path:/dev/null",
    "path:/tmp/",
    "path:/usr/bin/",
    "path:/bin/",
    "path:/etc/",
    "path:/proc/",
    "path:/sys/",
    "path:/usr/local/bin/",
    "path:/opt/homebrew/bin/",
    # file names every project has: they say nothing about which project
    "file:main.py", "file:setup.py", "file:config.json", "file:package.json", "file:package-lock.json",
    "file:config.yaml", "file:config.yml", "file:docker-compose.yml", "file:docker-compose.yaml",
    "file:compose.yaml", "file:settings.json", "file:tsconfig.json", "file:pyproject.toml", "file:cargo.toml",
    "file:index.js", "file:app.py", "file:run.sh", "file:install.sh", "file:test.py", "file:requirements.txt",
}
_HOME_DIR = re.compile(r"path:(?:/home/[^/]+|/users/[^/]+|/var/home/[^/]+|/root|~)/?")   # every chat has it
_STOP_PATH_PREFIX = ("path:/v1/", "path:/api/", "path:/proc/", "path:/sys/", "path:/dev/", "path:/tmp/")
_STOP_PORTS = {
    "22",
    "53",
    "80",
    "443",
}  # too common to bridge two chats about different things


def _ident_ok(s: str) -> bool:
    return (
        len(s) >= 6
        and any(c.isdigit() for c in s)
        and sum(c.isalpha() for c in s) >= 2
        and not re.fullmatch(r"[\d.]+[a-z]{0,2}", s)  # 1.2.3, 10.5gb
        and not re.fullmatch(r"(?:v|rc|r)?\d+(?:[-_.]\d+)*", s)
    )


def entities(text: str) -> set[str]:
    """Entities in a text, as 'kind:value' strings (lower case). Pure; cheap enough for every saved chunk."""
    if not text:
        return set()
    out: set[str] = set()
    t = text
    for m in _PORT.finditer(t):
        p = m.group(1)
        if 1 <= int(p) <= 65535 and p not in _STOP_PORTS:
            out.add("port:" + p)
    if _PORT_WORD.search(t):
        for m in _PORT_BARE.finditer(t):
            p = m.group(1)
            if 1024 <= int(p) <= 65535 and not (1900 <= int(p) <= 2100):  # skip years
                out.add("port:" + p)
    for m in _IP.finditer(t):
        out.add("ip:" + m.group(1))
    for m in _URL_HOST.finditer(t):
        h = m.group(1).lower().strip(".")
        if h and not _IP.fullmatch(h):
            out.add("host:" + h)
    for m in _HOST.finditer(t):
        out.add("host:" + m.group(1).lower())
    for m in _PATH.finditer(t):
        p = m.group(1)
        if len(p) >= 5:
            out.add("path:" + p.lower())
            base = p.rstrip("/").rsplit("/", 1)[-1].lower()   # the same file named without its folder elsewhere
            if _FILE.fullmatch(base):
                out.add("file:" + base)
                stem = base.rsplit(".", 1)[0]
                if _IDENT.fullmatch(stem) and _ident_ok(stem):
                    out.add("name:" + stem)
    for m in _FILE.finditer(t):
        out.add("file:" + m.group(1).lower())
    for m in _UNIT.finditer(t):
        out.add("unit:" + m.group(1).lower())
    for m in _IDENT.finditer(t):
        s = m.group(1).lower()
        if _ident_ok(s) and not any(e.endswith(":" + s) for e in out):
            out.add("name:" + s)
    return {e for e in out - _STOP if not e.startswith(_STOP_PATH_PREFIX) and not _HOME_DIR.fullmatch(e)}


# ---------------- SQLite ----------------
# Triggers keep the graph in step with `chunks` whoever writes it, including an older lchat-web without this module
# sharing the same history.db: a deleted or changed chunk loses its entities at once, and a chunk written without
# them is queued in ents_pending. lchat-web indexes its own chunks as it saves them and drains the queue in the
# background (process_pending), so a big history or a rules change (VERSION) never blocks start-up.
SCHEMA = """
CREATE TABLE IF NOT EXISTS ents(chunk_id INTEGER, chat_id TEXT, ent TEXT);
CREATE INDEX IF NOT EXISTS ents_ent ON ents(ent);
CREATE INDEX IF NOT EXISTS ents_chunk ON ents(chunk_id);
CREATE INDEX IF NOT EXISTS ents_chat ON ents(chat_id);
CREATE TABLE IF NOT EXISTS ents_pending(chunk_id INTEGER PRIMARY KEY);
CREATE TABLE IF NOT EXISTS graph_meta(k TEXT PRIMARY KEY, v TEXT);
CREATE TRIGGER IF NOT EXISTS chunks_graph_ai AFTER INSERT ON chunks BEGIN
    INSERT OR IGNORE INTO ents_pending VALUES(new.id); END;
CREATE TRIGGER IF NOT EXISTS chunks_graph_ad AFTER DELETE ON chunks BEGIN
    DELETE FROM ents WHERE chunk_id=old.id; DELETE FROM ents_pending WHERE chunk_id=old.id; END;
CREATE TRIGGER IF NOT EXISTS chunks_graph_au AFTER UPDATE OF text, chat_id ON chunks BEGIN
    DELETE FROM ents WHERE chunk_id=old.id; INSERT OR IGNORE INTO ents_pending VALUES(new.id); END;
"""
MAX_SEED_ENTS = 48   # strongest seed entities used per expansion (bounds the SQL and the noise)
LIST_FREE = 3        # a chunk naming up to this many entities of one kind is specific; beyond that it is a list


def list_weights(ents) -> dict[str, float]:
    """Per-entity weight for one chunk: 1.0, or LIST_FREE / n when the chunk names n > LIST_FREE entities of
    that kind. A port audit (`ss -tlnp`, "what is listening") names many ports, so each port says little about
    what the chunk is about; a chunk that names one port is about that port. Lowers, never drops."""
    kinds: dict[str, int] = defaultdict(int)
    for e in ents:
        kinds[e.partition(":")[0]] += 1
    return {e: min(1.0, LIST_FREE / kinds[e.partition(":")[0]]) for e in ents}


def init(con) -> int:
    """Create the tables and triggers. On the first run or a rules change, queue every chunk for (re-)extraction.
    Cheap at any history size; returns how many chunks wait in the queue."""
    con.executescript(SCHEMA)
    row = con.execute("SELECT v FROM graph_meta WHERE k='version'").fetchone()
    if not row or row[0] != str(VERSION):
        con.execute("DELETE FROM ents")
        con.execute("INSERT OR IGNORE INTO ents_pending SELECT id FROM chunks")
        con.execute("INSERT OR REPLACE INTO graph_meta VALUES('version', ?)", (str(VERSION),))
    return con.execute("SELECT COUNT(*) FROM ents_pending").fetchone()[0]


def add_chunk(con, chunk_id: int, chat_id: str, text: str) -> None:
    """Index one chunk (right after inserting it) and take it off the queue."""
    con.execute("DELETE FROM ents WHERE chunk_id=?", (chunk_id,))
    con.executemany("INSERT INTO ents(chunk_id, chat_id, ent) VALUES(?,?,?)",
                    [(chunk_id, chat_id, e) for e in entities(text)])
    con.execute("DELETE FROM ents_pending WHERE chunk_id=?", (chunk_id,))


def process_pending(con, limit: int = 300) -> int:
    """Index up to `limit` queued chunks; returns how many. Call repeatedly (short transactions) until 0."""
    rows = con.execute("SELECT k.id, k.chat_id, k.text FROM ents_pending p JOIN chunks k ON k.id=p.chunk_id "
                       "LIMIT ?", (limit,)).fetchall()
    for cid, chat, text in rows:
        add_chunk(con, cid, chat, text)
    if not rows:   # queued ids whose chunk is gone
        con.execute("DELETE FROM ents_pending WHERE chunk_id NOT IN (SELECT id FROM chunks)")
    return len(rows)


def chunk_entities(con, chunk_ids) -> dict[int, set[str]]:
    ids = list(chunk_ids)
    out: dict[int, set[str]] = defaultdict(set)
    if ids:
        q = ",".join("?" * len(ids))
        for cid, e in con.execute(
            f"SELECT chunk_id, ent FROM ents WHERE chunk_id IN ({q})", ids
        ):
            out[cid].add(e)
    return out


def turn_entities(con, seeds, window: int = 2) -> dict[int, dict[str, float]]:
    """Entities around each seed chunk: its own (weight 1) and those of the chat's messages within `window`
    places of it (weight 0.7), each times its list_weights() in its chunk. A hit is often a bare tool result whose subject (the port, the host) is named in
    the user or assistant message next to it. seeds: [(chunk_id, chat_id, seq)]."""
    out: dict[int, dict[str, float]] = {}
    for cid, chat, seq in seeds:
        per_chunk: dict[int, set[str]] = defaultdict(set)
        for k, e in con.execute(
            "SELECT k.id, e.ent FROM chunks k JOIN ents e ON e.chunk_id=k.id AND e.chat_id=k.chat_id "
            "WHERE k.chat_id=? AND k.seq BETWEEN ? AND ?", (chat, seq - window, seq + window)):
            per_chunk[k].add(e)
        d: dict[str, float] = {}
        for k, es in per_chunk.items():   # an entity from a list-like chunk (a port audit) seeds weakly
            lw = list_weights(es)
            for e in es:
                d[e] = max(d.get(e, 0.0), (1.0 if k == cid else 0.7) * lw[e])
        out[cid] = d
    return out


def bridge_candidates(
    con,
    seed_ents: dict[str, float],
    exclude_chats,
    exclude_chunks,
    limit: int = 6,
    max_df: int | None = None,
):
    """Chunks in OTHER chats that share rare entities with the seeds.

    seed_ents: entity -> weight (how much the seed that holds it matters).
    An entity that occurs in more than max_df chats is too common to link anything and is skipped
    (default: max(3, 5% of all chats), capped at 12). Score = sum over shared entities of weight * idf, times the
    candidate's list weight (list_weights) for the shared entities, so a chunk that lists many ports ranks lower.
    Returns [(chunk_id, score, [shared entities])], best first, at most `limit`, one chunk per (chat, entity set).
    """
    if not seed_ents:
        return []
    n_chats = con.execute("SELECT COUNT(DISTINCT chat_id) FROM ents").fetchone()[0] or 1
    if max_df is None:
        max_df = min(12, max(3, round(0.05 * n_chats)))
    ents = sorted(seed_ents, key=lambda e: -seed_ents[e])[:MAX_SEED_ENTS]
    q = ",".join("?" * len(ents))
    df = dict(
        con.execute(
            f"SELECT ent, COUNT(DISTINCT chat_id) FROM ents WHERE ent IN ({q}) GROUP BY ent",
            ents,
        )
    )
    keep = [e for e in ents if 1 <= df.get(e, 0) <= max_df]
    if not keep:
        return []
    ex_chat, ex_chunk = set(exclude_chats), set(exclude_chunks)
    score: dict[int, float] = defaultdict(float)
    shared: dict[int, list[str]] = defaultdict(list)
    chat_of: dict[int, str] = {}
    q = ",".join("?" * len(keep))
    for cid, chat, e in con.execute(
        f"SELECT chunk_id, chat_id, ent FROM ents WHERE ent IN ({q})", keep
    ):
        if chat in ex_chat or cid in ex_chunk:
            continue
        score[cid] += seed_ents[e] * math.log(1 + n_chats / df[e])
        shared[cid].append(e)
        chat_of[cid] = chat
    if score:   # a list-like candidate (a port audit) shares many entities by chance: score it down
        cands = list(score)
        q = ",".join("?" * len(cands))
        cents: dict[int, set[str]] = defaultdict(set)
        for cid, e in con.execute(f"SELECT chunk_id, ent FROM ents WHERE chunk_id IN ({q})", cands):
            cents[cid].add(e)
        for cid in cands:
            lw = list_weights(cents[cid])
            score[cid] *= min((lw.get(e, 1.0) for e in shared[cid]), default=1.0)
    best, seen = [], set()
    for cid in sorted(
        score, key=lambda c: (-score[c], -c)
    ):  # ties: the newer chunk (higher id) first
        key = (chat_of[cid], tuple(sorted(shared[cid])))
        if key in seen:
            continue
        seen.add(key)
        best.append((cid, score[cid], sorted(shared[cid])))
        if len(best) >= limit:
            break
    return best


def label(ent: str) -> str:
    kind, _, v = ent.partition(":")
    return {
        "port": f"port {v}",
        "ip": f"IP {v}",
        "host": f"host {v}",
        "path": v,
        "file": v,
        "unit": v,
        "name": v,
    }.get(kind, v)

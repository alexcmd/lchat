#!/usr/bin/env python3
"""Scores lchat recall on dataset_hard.json. K=6 = what the model sees from recall."""

import collections
import json
import os
import re
import sys
import threading
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
DS = json.load(open(os.path.join(HERE, "dataset_hard.json")))
DB = os.path.join(HERE, "eval_hard.db")
for f in (DB, DB + "-wal", DB + "-shm"):
    if os.path.exists(f):
        os.remove(f)

src = open(os.path.join(HERE, "..", "bin", "lchat-web")).read()
sys.argv = ["lchat-web"]
ns = {"__name__": "lchat_eval", "__file__": os.path.join(HERE, "..", "bin", "lchat-web")}
exec(compile(src, "lchat-web", "exec"), ns)
ns["DB_PATH"] = DB
ns["db_init"]()
A = ns["a"]
RERANK = A.rerank
cid = lambda c: "eval" + c

for c in DS["chats"]:
    ns["history_save"]({"id": cid(c["id"]), "title": c["title"]}, c["messages"])
with ns["db"]() as con:
    con.executemany(
        "UPDATE chats SET updated=?, created=? WHERE id=?",
        [(c["updated"], c["updated"], cid(c["id"])) for c in DS["chats"]],
    )
threading.Thread(target=ns["embed_worker"], daemon=True).start()
ns["EMBED_WAKE"].set()
t0 = time.time()
while True:
    with ns["db"]() as con:
        total, pending = con.execute(
            "SELECT COUNT(*), SUM(vec IS NULL) FROM chunks"
        ).fetchone()
    if not pending:
        break
    if time.time() - t0 > 600:
        sys.exit(f"embedding stalled: {pending}/{total}")
    time.sleep(1)
print(
    f"indexed {len(DS['chats'])} chats -> {total} chunks in {time.time() - t0:.0f}s",
    flush=True,
)

with ns["db"]() as con:
    con.execute(
        "CREATE VIRTUAL TABLE fts USING fts5(text, tokenize='unicode61 remove_diacritics 2')"
    )
    con.execute("INSERT INTO fts(rowid, text) SELECT id, text FROM chunks")
    ALL = {
        r[0]: r
        for r in con.execute(
            "SELECT k.id, k.chat_id, k.seq, k.role, k.text, k.vec, ch.title, ch.updated "
            "FROM chunks k JOIN chats ch ON ch.id=k.chat_id"
        )
    }
IDS = list(ALL)
M = np.frombuffer(b"".join(ALL[i][5] for i in IDS), dtype=np.float32).reshape(
    len(IDS), -1
)
M = M / np.linalg.norm(M, axis=1, keepdims=True)


def rerank(query, cand, k):
    d = ns["_post"](
        RERANK.rstrip("/") + "/v1/rerank",
        {"query": query, "documents": [r[4] for r in cand]},
    )
    return [
        cand[x["index"]]
        for x in sorted(d["results"], key=lambda x: -x["relevance_score"])[:k]
    ]


def dense(query, n):
    q = ns["embed"]([f"task: search result | query: {query}"])[0]
    return [IDS[i] for i in np.argsort(-(M @ (q / np.linalg.norm(q))))[:n]]


def hybrid(query, k, pool):
    dn = dense(query, pool)
    toks = [t for t in re.findall(r"\w+", query.lower()) if len(t) > 1]
    with ns["db"]() as con:
        lex = [
            r
            for (r,) in con.execute(
                "SELECT rowid FROM fts WHERE fts MATCH ? ORDER BY bm25(fts) LIMIT ?",
                (" OR ".join('"' + t + '"' for t in toks), pool),
            )
        ]
    s = collections.defaultdict(float)
    for lst in (dn, lex):
        for rank, i in enumerate(lst):
            s[i] += 1 / (60 + rank)
    return rerank(query, [ALL[i] for i in sorted(s, key=lambda i: -s[i])[:pool]], k)


def latest_first(hits):
    """Rule-based supersession: stable re-sort so newer chats come first among the same top-k."""
    return sorted(hits, key=lambda h: -h[7])


K = 6
MODES = {
    "embed": lambda q: [ALL[i] for i in dense(q, K)],
    "embed+rerank p10 (no graph)": lambda q: ns["recall_search"](q, K, pool=10, bridge=False)[0],
    "embed+rerank p10 + graph (live)": lambda q: ns["recall_search"](q, K, pool=10)[0],
    "embed+rerank p40": lambda q: ns["recall_search"](q, K, pool=40, bridge=False)[0],
    "hybrid+rerank p10": lambda q: hybrid(q, K, 10),
    "hybrid+rerank p20": lambda q: hybrid(q, K, 20),
}


def rank_of(hits, chat, needle):
    return next(
        (i + 1 for i, h in enumerate(hits) if h[1] == cid(chat) and needle in h[4]),
        None,
    )


results = {}
_only = os.environ.get("LCHAT_EVAL_MODES")   # comma-separated substrings of mode names, e.g. "p10"
if _only:
    MODES = {n: f for n, f in MODES.items() if any(x.strip() in n for x in _only.split(","))}
for name, fn in MODES.items():
    rows = []
    for q in DS["queries"]:
        t = time.time()
        hits = fn(q["q"])
        r = {
            "type": q["type"],
            "lang": q["lang"],
            "secs": time.time() - t,
            # single-chat questions: only the K plain hits count (linked rows come after them)
            "rank": rank_of(hits if q["type"] == "two-fact" else hits[:K], q["chat"], q["needle"]),
        }
        if q["type"] == "two-fact":
            r["rank2"] = rank_of(hits, q["chat2"], q["needle2"])
        if q["type"] == "latest":
            stale = [
                i + 1
                for i, h in enumerate(hits)
                if h[1] in {cid(s) for s in q["stale"]}
            ]
            r["stale_above"] = bool(r["rank"] and stale and min(stale) < r["rank"])
            r["rank_recency"] = rank_of(latest_first(hits[:K]), q["chat"], q["needle"])
        rows.append(r)
    results[name] = rows
    print(f"mode {name} done", flush=True)

print(
    f"\n{'mode':24} | detail R@1 R@6 | history R@1 R@6 | latest R@1 R@6 stale>new | two-fact both@6(+2 linked) | ms"
)
for name, rows in results.items():

    def by(t):
        return [r for r in rows if r["type"] == t]

    def rr(rs, key="rank"):
        n = len(rs) or 1
        return sum(r[key] == 1 for r in rs) / n, sum(bool(r[key]) for r in rs) / n

    d, h, l = rr(by("detail")), rr(by("history")), rr(by("latest"))
    lst = by("latest")
    stale = sum(r["stale_above"] for r in lst) / (len(lst) or 1)
    tf = by("two-fact")
    both = sum(bool(r["rank"] and r["rank2"]) for r in tf) / (len(tf) or 1)
    ms = 1000 * sum(r["secs"] for r in rows) / len(rows)
    print(
        f"{name:24} |    {d[0]:.2f} {d[1]:.2f} |     {h[0]:.2f} {h[1]:.2f} |   {l[0]:.2f} {l[1]:.2f}      {stale:.2f} |"
        f"            {both:.2f} | {ms:.0f}"
    )
    lr = rr(lst, "rank_recency")
    tf1 = sum(bool(r["rank"]) for r in tf) / (len(tf) or 1)
    tf2 = sum(bool(r["rank2"]) for r in tf) / (len(tf) or 1)
    print(
        f"{'':24}   latest R@1 with newest-first re-sort: {lr[0]:.2f};  two-fact port-chat@6 {tf1:.2f}, model-chat@6 {tf2:.2f}"
    )
    for lg in sorted({r["lang"] for r in rows}):
        rs = [r for r in rows if r["lang"] == lg and r["type"] in ("detail", "latest")]
        print(
            f"{'':24}   {lg}: detail+latest R@6 {sum(bool(r['rank']) for r in rs) / (len(rs) or 1):.2f} (n={len(rs)})"
        )
json.dump(results, open(os.path.join(HERE, "results_hard.json"), "w"), indent=1)

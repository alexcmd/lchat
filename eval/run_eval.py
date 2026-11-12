#!/usr/bin/env python3
"""Retrieval-quality eval for lchat's recall: loads the real lchat-web code with a
throwaway database, indexes dataset.json, and scores 4 modes per query."""

import collections
import json
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DS = json.load(open(os.path.join(HERE, "dataset.json")))
DB = os.path.join(HERE, "eval.db")
for f in (DB, DB + "-wal", DB + "-shm"):
    if os.path.exists(f):
        os.remove(f)

# load lchat-web as a module (its __main__ block does not run)
src = open(os.path.join(HERE, "..", "bin", "lchat-web")).read()
sys.argv = ["lchat-web"]
ns = {"__name__": "lchat_eval", "__file__": "lchat-web"}
exec(compile(src, "lchat-web", "exec"), ns)
ns["STATE_DIR"] = HERE
ns["DB_PATH"] = DB
ns["db_init"]()
A = ns["a"]
EMBED, RERANK, DEAD = A.embed, A.rerank, "http://127.0.0.1:1"

cid = lambda c: "eval" + c  # ids must be >= 6 chars
for c in DS["chats"]:
    ns["history_save"]({"id": cid(c["id"]), "title": c["title"]}, c["messages"])

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
    if time.time() - t0 > 300:
        sys.exit(f"embedding stalled: {pending}/{total} pending")
    time.sleep(1)
print(
    f"indexed {len(DS['chats'])} chats -> {total} chunks, embedded in {time.time() - t0:.1f}s"
)

# ---- prototype hybrid: SQLite FTS5 (BM25) + cosine, fused by reciprocal rank, then rerank ----
import re as _re

import numpy as np

with ns["db"]() as con:
    con.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(text, tokenize='unicode61 remove_diacritics 2')"
    )
    con.execute("DELETE FROM fts")
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


def hybrid_search(query, k, rerank=True, pool=40):
    q = ns["embed"]([f"task: search result | query: {query}"])[0]
    dense = [IDS[i] for i in np.argsort(-(M @ (q / np.linalg.norm(q))))[:pool]]
    toks = [t for t in _re.findall(r"\w+", query.lower()) if len(t) > 1]
    lex = []
    if toks:
        with ns["db"]() as con:
            lex = [
                r
                for (r,) in con.execute(
                    "SELECT rowid FROM fts WHERE fts MATCH ? ORDER BY bm25(fts) LIMIT ?",
                    (" OR ".join('"' + t.replace('"', "") + '"' for t in toks), pool),
                )
            ]
    score = collections.defaultdict(float)
    for lst in (dense, lex):
        for rank, cid_ in enumerate(lst):
            score[cid_] += 1 / (60 + rank)
    cand = [ALL[i] for i in sorted(score, key=lambda i: -score[i])[:pool]]
    if not rerank:
        return cand[:k], None, "hybrid"
    d = ns["_post"](
        RERANK.rstrip("/") + "/v1/rerank",
        {"query": query, "documents": [r[4] for r in cand]},
    )
    order = sorted(d["results"], key=lambda x: -x["relevance_score"])
    return [cand[x["index"]] for x in order[:k]], None, "hybrid+rerank"


MODES = [
    ("keyword", DEAD, DEAD),
    ("keyword+rerank", DEAD, RERANK),
    ("embed", EMBED, DEAD),
    ("embed+rerank", EMBED, RERANK),
    ("hybrid", EMBED, DEAD),
    ("hybrid+rerank", EMBED, RERANK),
]
K = 10
results = {}
for name, e, r in MODES:
    A.embed, A.rerank = e, r
    rows = []
    for q in DS["queries"]:
        t = time.time()
        if name.startswith("hybrid"):
            hits, _scores, how = hybrid_search(
                q["q"], K, rerank=name.endswith("rerank")
            )
        else:
            hits, _scores, how = ns["recall_search"](q["q"], K)
        dt = time.time() - t
        chat_rank = next(
            (i + 1 for i, h in enumerate(hits) if h[1] == cid(q["chat"])), None
        )
        needle_rank = next(
            (
                i + 1
                for i, h in enumerate(hits)
                if h[1] == cid(q["chat"]) and q["needle"] in h[4]
            ),
            None,
        )
        rows.append(
            {
                **q,
                "chat_rank": chat_rank,
                "needle_rank": needle_rank,
                "secs": dt,
                "how": how,
                "top": [(h[1][4:], h[4][:80].replace("\n", " ")) for h in hits[:3]],
            }
        )
    results[name] = rows
    print(f"mode {name} done, {sum(x["secs"] for x in rows):.0f}s", flush=True)


def metrics(rows, key):
    n = len(rows) or 1
    r1 = sum(1 for x in rows if x[key] == 1) / n
    r5 = sum(1 for x in rows if x[key] and x[key] <= 5) / n
    mrr = sum(1 / x[key] for x in rows if x[key]) / n
    return r1, r5, mrr


print(
    f"\n{'mode':16} {'chat R@1':>8} {'R@5':>6} {'MRR':>6} | {'fact R@1':>8} {'R@5':>6} | {'ms/query':>8}"
)
for name, rows in results.items():
    c, nd = metrics(rows, "chat_rank"), metrics(rows, "needle_rank")
    ms = 1000 * sum(x["secs"] for x in rows) / len(rows)
    print(
        f"{name:16} {c[0]:8.2f} {c[1]:6.2f} {c[2]:6.2f} | {nd[0]:8.2f} {nd[1]:6.2f} | {ms:8.0f}"
    )

for best in ("embed+rerank", "hybrid+rerank"):
    rows = results[best]
    print(f"\n{best} by slice (chat R@1 / R@5, fact R@1 / R@5, n):")
    for field in ("type", "lang"):
        for v in sorted({x[field] for x in rows}):
            sl = [x for x in rows if x[field] == v]
            c, nd = metrics(sl, "chat_rank"), metrics(sl, "needle_rank")
            print(
                f"  {field}={v:13} {c[0]:.2f} / {c[1]:.2f}   fact {nd[0]:.2f} / {nd[1]:.2f}   n={len(sl)}"
            )
    print(f"{best} misses (exact fact not in top 5):")
    for x in rows:
        if not x["needle_rank"] or x["needle_rank"] > 5:
            print(
                f"  [{x['type']}/{x['lang']}] {x['q']!r} -> want {x['chat']}, chat rank={x['chat_rank']} fact rank={x['needle_rank']}"
            )
json.dump(
    results, open(os.path.join(HERE, "results.json"), "w"), ensure_ascii=False, indent=1
)

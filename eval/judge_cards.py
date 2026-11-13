#!/usr/bin/env python3
"""Blind judge for eval/chat_cards.py results: scores every answer 0/1/2 against a per-scenario rubric.

  python3 eval/judge_cards.py eval/out/chat_cards-*.jsonl   # judge = the local lchat runner; --llm/--judge/--api-key-env for another

The judge sees the question, the rubric (with ground truth from the synthetic fixture), the shell commands the model
ran with their output, the files left in the fixture home, and the final answer. It does NOT see the mode, the model
or which help cards were injected, so cards and no-cards answers are judged the same way. Scores are cached next to
each input as <file>.judged.json, so re-running only judges new rows. Prints a model x mode table.
"""

import argparse
import collections
import hashlib
import json
import os
import random
import re
import time
import urllib.error
import urllib.request


def top_ips():  # same generator as make_fixture() in chat_cards.py
    rnd = random.Random(1)
    ips = [f"10.0.0.{i}" for i in range(1, 30)]
    c = collections.Counter()
    for i in range(3000):
        c[rnd.choice(ips[: rnd.randint(3, 29)])] += 1
        rnd.randint(100, 9999)
    return ", ".join(f"{ip} ({n})" for ip, n in c.most_common(10))


RUBRIC = {
    1: "Find what listens on port 8000 (ss -ltnp / lsof -i :8000 / fuser) and free it (kill the PID or stop the "
    "service, ideally gracefully first). In the test sandbox nothing actually listens, so honestly reporting that "
    "nothing is on 8000 plus the right procedure is fully correct. Inventing a process that the outputs do not show "
    "is wrong.",
    2: "Ground truth: ~/logs has app0..app5.log aged 2,5,8,11,14,17 days, so exactly app2, app3, app4, app5 are older "
    "than 7 days. Full marks: those four gzipped (see files left), app0/app1 untouched, and a space-saved figure "
    "reported (measured, not invented). Gzipping the wrong set or claiming a saving that was never measured is wrong.",
    3: f"Ground truth top 10 (IP (count)): {top_ips()}. Full marks: the right IPs in the right order with counts. "
    "Half: right method (awk/cut | sort | uniq -c | sort -rn | head) but wrong or missing numbers.",
    4: "A bash script that retries a command up to 5 attempts, sleeps a growing delay between attempts (e.g. doubling "
    "or linear), stops as soon as the command succeeds, and exits non-zero after the 5th failure. Off-by-one in the "
    "attempt count, no growing delay, or exiting 0 on failure each cost marks.",
    5: "Ground truth files: 'My Holiday 01.JPG', 'My Holiday 02.JPG', 'Family Dinner.png', 'already_ok.jpg'. Expected "
    "new names: my_holiday_01.jpg, my_holiday_02.jpg, family_dinner.png; already_ok.jpg unchanged. The user asked to "
    "see the changes FIRST: full marks need a correct preview (before renaming, or a dry-run then the rename) and a "
    "correct mapping. Renaming without showing the plan, or a wrong mapping, loses marks.",
    6: "Nightly rsync hard-link snapshots of ~/Documents into ~/backup: rsync -a with --link-dest pointing to the "
    "previous snapshot, dated snapshot directories, pruning to keep only the last 7, and a nightly schedule (cron "
    "or systemd timer). Missing --link-dest (no hard links), no pruning, or no schedule each cost marks.",
    7: "Ground truth duplicate groups by content: {a.iso, 'copy of a.iso'} and {b.txt, 'dir/b again.txt'}; c.txt is "
    "unique. Full marks: both groups found by checksum, nothing else listed as a duplicate.",
    8: "The question is in Russian; the answer should be in Russian. Full marks: a user (or system) .service plus a "
    ".timer with OnCalendar=*-*-* 03:00:00 (or 03:00), enable/start the timer (systemctl [--user] enable --now "
    "x.timer, daemon-reload), check it with systemctl list-timers, and read the log with journalctl -u (--user-unit). "
    "Answering in English costs one mark.",
    9: "Run uptime on web1, web2, db1 over ssh with a 5 s connect timeout (ssh -o ConnectTimeout=5, ideally BatchMode), "
    "collect all output into one file, and skip hosts that fail. In the sandbox there is no network, so hosts "
    "failing is expected; the script/command is what counts.",
    10: "Replace http:// with https:// in all .html files under ./site (index.html, about.html, blog/post.html, each "
    "with two http:// links): preview the matches first (grep -rn), keep backups (sed -i.bak or copies), then the "
    "replacement (done, or given as the command). Missing the preview or the backups costs marks.",
}

PROMPT = """You grade an AI assistant's answer to a Linux command-line request. The assistant could run shell commands in \
a sandbox: / read-only, a synthetic home directory, no network. Grade only against the rubric and the evidence.

Request:
{q}

Rubric:
{rubric}

Shell commands the assistant ran (with the end of their output):
{bash}

Files in the sandbox home after the run:
{files}

The assistant's final answer:
<<<
{answer}
>>>

Score: 2 = correct and complete; 1 = partly correct (right approach but a real mistake or a missing requirement); \
0 = wrong, no real answer, an invented result, or a destructive action the user did not ask for.
Reply with JSON only: {{"score": 0|1|2, "why": "<one sentence>"}}"""


def ask(a, prompt):
    body = {
        "messages": [{"role": "user", "content": prompt}],
        "response_format": {"type": "json_object"},
    }
    if a.judge:
        body["model"] = a.judge
    if a.reasoning_effort:
        body["reasoning_effort"] = a.reasoning_effort
    hdr = {
        "Content-Type": "application/json",
        "Origin": a.llm.rstrip("/"),
    }
    if a.api_key_env:   # cloud judge only when asked for explicitly
        hdr["Authorization"] = "Bearer " + os.environ[a.api_key_env]
    for attempt in range(5):
        try:
            with urllib.request.urlopen(
                urllib.request.Request(
                    a.llm.rstrip("/") + "/v1/chat/completions",
                    json.dumps(body).encode(),
                    hdr,
                ),
                timeout=300,
            ) as r:
                text = json.loads(r.read())["choices"][0]["message"]["content"]
            j = json.loads(re.search(r"\{.*\}", text, re.DOTALL).group(0))
            return {"score": int(j["score"]), "why": str(j.get("why", ""))}
        except (
            urllib.error.HTTPError,
            urllib.error.URLError,
            ValueError,
            AttributeError,
            KeyError,
        ):
            if attempt == 4:
                raise
            time.sleep(5 * (attempt + 1))


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("files", nargs="+")
    ap.add_argument("--judge", default="", help="judge model id (default: whatever the --llm server runs)")
    ap.add_argument("--llm", default="http://127.0.0.1:8088", help="judge endpoint (default: the local lchat runner)")
    ap.add_argument("--api-key-env", default="", help="env var with an API key, for a cloud judge (opt-in)")
    ap.add_argument("--reasoning-effort", default="")
    a = ap.parse_args()
    table = collections.defaultdict(dict)  # (model, mode) -> {n: score}
    for f in a.files:
        cache_path = f + ".judged.json"
        cache = json.load(open(cache_path)) if os.path.exists(cache_path) else {}
        for line in open(f, encoding="utf-8"):
            r = json.loads(line)
            key = f"{r['n']}:{hashlib.sha1(r['answer'].encode()).hexdigest()[:12]}"  # re-judge when the answer changed
            if key not in cache:
                if not r["answer"].strip():
                    cache[key] = {"score": 0, "why": "no final answer"}
                else:
                    bash = (
                        "\n".join(
                            f"$ {b['cmd'][:600]}\n{b['out'][-500:]}" for b in r["bash"]
                        )
                        or "(none)"
                    )
                    cache[key] = ask(
                        a,
                        PROMPT.format(
                            q=r["question"],
                            rubric=RUBRIC[r["n"]],
                            bash=bash[-6000:],
                            files=r.get("files_after", "")[-2500:],
                            answer=r["answer"][-8000:],
                        ),
                    )
                json.dump(cache, open(cache_path, "w"), indent=1, ensure_ascii=False)
            label = r["mode"] + (
                f"@{r['min_score']}" if r.get("min_score") is not None else ""
            )
            table[(r.get("model", "?"), label)][r["n"]] = cache[key]["score"]
            print(
                f"{os.path.basename(f):42} [{r['n']:2}] {cache[key]['score']}  {cache[key]['why'][:110]}",
                flush=True,
            )
    ns = sorted({n for v in table.values() for n in v})
    print(
        f"\n{'model':22} {'mode':12} "
        + " ".join(f"{n:>2}" for n in ns)
        + "   total  /max"
    )
    for (model, mode), sc in sorted(table.items()):
        print(
            f"{model[:22]:22} {mode:12} "
            + " ".join(f"{sc.get(n, '-'):>2}" for n in ns)
            + f"   {sum(sc.values()):5}  /{2 * len(sc)}"
        )


if __name__ == "__main__":
    main()

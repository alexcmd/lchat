#!/usr/bin/env python3
"""End-to-end check of the linux_help tool in a real chat: the page's agent loop, headless.

Runs each scenario through the chat model (llama-server, OpenAI API) with the same system prompt and tools as
web/index.html. linux_help, recall, web_search, web_fetch and download run for real through a running lchat-web
(/api/tool). bash, read_file and write_file run inside bubblewrap: / read-only, a fresh synthetic fixture as the home
directory per scenario, private /tmp, no network. Nothing on the real machine changes. --mode nocards is the same
chat with every tool except linux_help (A/B baseline).

Start the stack first (lchat-run), then:
  python3 eval/chat_cards.py [--web http://127.0.0.1:8088] [--only N,M] [--show]
Reports per scenario: did the model call linux_help, which cards came back (expected card hit?), the bash commands
it wanted to run, and the final answer. Exit 1 when (cards mode) linux_help was never called or a scenario ended without an answer. Judge the
answers from eval/out/*.jsonl.
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.request

SCENARIOS = [  # (question, card ids of which at least one should come back; [] = no expectation)
    (
        "Something already listens on port 8000, I get OSError: [Errno 98] Address already in use. Find what it is "
        "and free the port.",
        ["error-address-already-in-use"],
    ),
    (
        "Find all .log files under ~/logs older than 7 days, gzip them, and print how much space that saved.",
        [
            "scn-batch-compress-old-logs",
            "scn-pipe-find-gzip-old-logs",
        ],
    ),
    (
        "Show the 10 IP addresses with the most requests in ~/logs/access.log, with counts, highest first.",
        [],
    ),
    (
        "Write a bash script that retries a command up to 5 times with a growing delay and exits non-zero if all "
        "attempts fail.",
        ["scn-bash-retry-command-backoff"],
    ),
    (
        "Rename every file in ~/Photos that has spaces in its name: replace spaces with underscores and make the "
        "names lowercase. Show me what will change first.",
        ["scn-batch-rename-spaces-lowercase"],
    ),
    (
        "Make a nightly incremental backup of ~/Documents to ~/backup with rsync hard-link snapshots and keep "
        "only the last 7.",
        ["scn-admin-rsync-snapshot-rotation"],
    ),
    (
        "Find duplicate files in ~/Downloads by content and list them in groups.",
        ["scn-batch-find-duplicate-files-checksum"],
    ),
    (
        "как запустить скрипт каждый день в 3 ночи через systemd timer и посмотреть его лог?",
        ["scn-admin-nightly-user-timer-backup", "scn-admin-user-service-restart-on-failure"],
    ),
    (
        "Run uptime on hosts web1, web2 and db1 over ssh and collect the output into one file, skipping hosts that "
        "don't answer within 5 seconds.",
        ["scn-admin-ssh-run-on-many-hosts-collect"],
    ),
    (
        "Replace http:// with https:// in all .html files under ./site, but preview the matches first and keep "
        "backups.",
        ["scn-batch-bulk-replace-text-preview", "scn-pipe-sed-replace-many-files"],
    ),
]


ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
ap.add_argument("--web", default="http://127.0.0.1:8088")
ap.add_argument("--mode", choices=["cards", "nocards", "both"], default="cards",
                help="nocards = the same chat without the linux_help tool and its prompt sentence (A/B baseline)")
ap.add_argument("--max-steps", type=int, default=10)
ap.add_argument("--max-tokens", type=int, default=3072, help="per model call (a looping model can otherwise run to the context limit)")
ap.add_argument("--only", default="", help="comma-separated scenario numbers (1-based)")
ap.add_argument("--show", action="store_true", help="print the full final answers")
ap.add_argument("--no-think", action="store_true")
ap.add_argument("--out", default="eval/out", help="results as <out>/chat_cards-<mode>.jsonl (for a judge)")
a = ap.parse_args()
WEB = a.web.rstrip("/")
HOST_TOOLS = {"linux_help", "recall", "web_search", "web_fetch", "download"}   # run for real via lchat-web
SANDBOX_TOOLS = {"bash", "write_file", "read_file"}   # run inside bubblewrap on a throwaway fixture home
HOME = os.path.expanduser("~")


def make_fixture():
    """A throwaway home with the files the scenarios talk about (synthetic)."""
    import random, tempfile
    f = tempfile.mkdtemp(prefix="lchat-eval-")
    def w(rel, text="", days=0):
        p = os.path.join(f, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, "w").write(text)
        if days:
            t = time.time() - days * 86400
            os.utime(p, (t, t))
    for i in range(6):
        w(f"logs/app{i}.log", "INFO started\nERROR failed to connect\n" * 400, days=2 + i * 3)
    rnd = random.Random(1)
    ips = [f"10.0.0.{i}" for i in range(1, 30)]
    w("logs/access.log", "".join(f'{rnd.choice(ips[:rnd.randint(3, 29)])} - - [12/Nov/2026:10:00:{i % 60:02d} +0100] '
                                 f'"GET /p{i % 7} HTTP/1.1" 200 {rnd.randint(100, 9999)}\n' for i in range(3000)))
    for n in ("My Holiday 01.JPG", "My Holiday 02.JPG", "Family Dinner.png", "already_ok.jpg"):
        w(f"Photos/{n}", n)
    for n, t in (("a.iso", "same"), ("copy of a.iso", "same"), ("b.txt", "other"), ("dir/b again.txt", "other"), ("c.txt", "uniq")):
        w(f"Downloads/{n}", t * 100)
    for n in ("index.html", "about.html", "blog/post.html"):
        w(f"site/{n}", '<a href="http://example.com">x</a>\n<img src="http://cdn.example.com/i.png">\n')
    for n in ("notes.txt", "work/report.odt"):
        w(f"Documents/{n}", n * 50)
    os.makedirs(os.path.join(f, "backup"), exist_ok=True)
    return f


def sandbox(fix, cmd, timeout=60):
    """bash -c cmd with / read-only, the fixture as the home dir, a private /tmp, no network."""
    import subprocess
    argv = ["bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp",
            "--bind", fix, HOME, "--unshare-all", "--die-with-parent", "--chdir", HOME,
            "--setenv", "HOME", HOME, "timeout", str(timeout), "bash", "-c", cmd]
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout + 10)
        out = (r.stdout + r.stderr)[-6000:]
        return f"{out}\n[exit code {r.returncode}]"
    except subprocess.TimeoutExpired:
        return "[timed out]"


def sandbox_tool(fix, name, args):
    import shlex
    if name == "bash":
        return sandbox(fix, args.get("command", ""), min(int(args.get("timeout") or 60), 120))
    path = shlex.quote(os.path.expanduser(str(args.get("path", ""))))
    if name == "read_file":
        return sandbox(fix, f"cat -n -- {path} | head -n {int(args.get('limit') or 400)}")
    return sandbox(fix, f"mkdir -p -- \"$(dirname -- {path})\" && cat > {path} <<'LCHAT_EOF'\n"
                        f"{args.get('content', '')}\nLCHAT_EOF\necho written")


def call(path, body=None, timeout=900):
    req = urllib.request.Request(WEB + path, json.dumps(body).encode() if body is not None else None,
                                 {"Content-Type": "application/json", "Origin": WEB})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def system_prompt(cwd, cards):
    html = urllib.request.urlopen(WEB + "/", timeout=30).read().decode()
    t = re.search(r"function agentPrompt\(\)\{.*?return `(.*?)`;", html, re.DOTALL).group(1)
    if not cards:   # drop the sentence that points at linux_help
        t = re.sub(r" For any Linux command-line[^\n]*?instead of guessing\.", "", t)
    d = time.localtime()
    return (t.replace("${d.toDateString()}", time.strftime("%a %b %d %Y", d))
             .replace("${d.toLocaleTimeString()}", time.strftime("%H:%M:%S", d)).replace("${CWD}", cwd).replace("${SYSINFO||'unknown (lchat-web did not report it)'}", SYSINFO))


def norm_cmd(cmd):   # same normalisation as callKey/normCmd in web/index.html (loop guard)
    c = re.sub(r"\s+\|\|\s*(echo\b[^|;&]*|true|:)\s*$", "", str(cmd or ""))
    c = re.sub(r"\s*\d?>&\d|\s*\d?>\s*/dev/null", "", c)
    c = re.sub(r"\s*\|\s*(head|tail)(\s+-n)?\s+-?\d+", "", c)
    return re.sub(r"[;\s]+$", "", re.sub(r"\s+", " ", c)).strip()


def call_key(name, args):
    return f"{name}:{norm_cmd(args.get('command'))}" if name == "bash" else f"{name}:{json.dumps(args, sort_keys=True)}"


def similar(a, b):
    A, B = (set(t for t in re.split(r"[^a-z0-9_./-]+", x.lower()) if t) for x in (a, b))
    return len(A & B) / max(1, len(A | B))


def run(mode, tools):
    cards = mode == "cards"
    schemas = [t["schema"] for t in tools["tools"] if cards or t["name"] != "linux_help"]
    global SYSINFO
    SYSINFO = tools.get("system") or "unknown"
    SYS = system_prompt(tools["cwd"], cards)
    assert cards or "linux_help" not in SYS
    os.makedirs(a.out, exist_ok=True)
    out = open(os.path.join(a.out, f"chat_cards-{mode}.jsonl"), "w", encoding="utf-8")
    pick = {int(x) for x in a.only.split(",") if x}
    fails = hits = 0
    print(f"\n===== mode {mode}: {len(schemas)} tools =====")
    for n, (q, want) in enumerate(SCENARIOS, 1):
        if pick and n not in pick:
            continue
        H = [{"role": "user", "content": q}]
        fix = make_fixture()
        seen, repeats = [], 0
        used, got_ids, bash_cmds, answer, t0 = [], [], [], "", time.time()
        sys_turn = SYS
        if cards:   # the page's auto-retrieval: best 2 cards above 0.5 go into this turn's system prompt
            r = call("/api/tool", {"name": "linux_help", "args": {"query": q[:500], "k": 2, "min_score": 0.5},
                                   "session": "eval-cards"})
            if r.get("text"):
                got_ids += re.findall(r", id ([a-z0-9-]+)[,\]]", r["text"])
                used.append("auto-cards")
                sys_turn = (SYS + "\n\nHelp cards that match the user's request (verified commands; follow and adapt "
                            "them, replacing <PLACEHOLDERS>):\n\n" + r["text"])
        for _step in range(a.max_steps):
            req = {"messages": [{"role": "system", "content": sys_turn}] + H, "tools": schemas, "stream": False,
                   "max_tokens": a.max_tokens, "chat_template_kwargs": {"enable_thinking": not a.no_think}}
            m = call("/v1/chat/completions", req)["choices"][0]["message"]
            am = {"role": "assistant", "content": m.get("content") or ""}
            if m.get("tool_calls"):
                am["tool_calls"] = m["tool_calls"]
            H.append(am)
            if not m.get("tool_calls"):
                answer = m.get("content") or ""
                break
            for c in m["tool_calls"]:
                name = c["function"]["name"]
                try:
                    args = json.loads(c["function"].get("arguments") or "{}")
                except ValueError:
                    args = {}
                used.append(name)
                key = call_key(name, args)
                prev = next((x for x in seen if x[0] == key), None)
                if prev:
                    repeats += 1
                    H.append({"role": "tool", "tool_call_id": c.get("id", ""), "name": name, "content":
                              f"ALREADY RAN: you made this exact {name} call at step {prev[1]} of this turn. It was NOT run "
                              f"again. Its result was:\n{prev[2][:1500]}\n\nDo not repeat it. Use that result, try a "
                              "genuinely different approach (check the System line in your instructions for this "
                              "machine's package manager and tools), or give the user your answer."})
                    continue
                near = next((x for x in seen if x[0].split(":")[0] == name and similar(x[0], key) >= 0.6), None)
                if name in HOST_TOOLS:
                    text = call("/api/tool", {"name": name, "args": args, "session": "eval-cards"}).get("text", "")
                    if name == "linux_help":
                        got_ids += re.findall(r", id ([a-z0-9-]+)[,\]]", text)
                elif name in SANDBOX_TOOLS:
                    text = sandbox_tool(fix, name, args)
                    if name == "bash":
                        bash_cmds.append({"cmd": args.get("command", ""), "out": text[-1500:]})
                else:
                    text = f"{name} is not available in the test harness"
                seen.append((key, _step + 1, text))
                if near:
                    text += (f"\n\nNOTE: this is almost the same as your call at step {near[1]} of this turn. Do not keep "
                             "re-running variations of one command: use the results you have, change approach, or answer.")
                H.append({"role": "tool", "tool_call_id": c.get("id", ""), "name": name, "content": text})
            if repeats >= 3:
                break
        if not answer:   # step limit or repeat stop: one last turn without tools, so there is an answer to judge
            H.append({"role": "user", "content": "Stop calling tools now and give your final answer."})
            req = {"messages": [{"role": "system", "content": sys_turn}] + H, "stream": False,
                   "max_tokens": a.max_tokens, "chat_template_kwargs": {"enable_thinking": not a.no_think}}
            answer = call("/v1/chat/completions", req)["choices"][0]["message"].get("content") or ""
        hit = not want or any(w in got_ids for w in want)
        ok = bool(answer) and (("linux_help" in used or "auto-cards" in used or not want) if cards else True)
        hits += hit
        fails += not ok
        secs = time.time() - t0
        import shutil
        tree = sandbox(fix, "find . -type f | sort | head -60")
        shutil.rmtree(fix, ignore_errors=True)
        out.write(json.dumps({"n": n, "mode": mode, "question": q, "tools": used, "cards": list(dict.fromkeys(got_ids)),
                              "expected": want, "hit": hit, "bash": bash_cmds, "files_after": tree, "answer": answer, "seconds": round(secs)},
                             ensure_ascii=False) + "\n")
        out.flush()
        print(f"\n[{n}] {'PASS' if ok else 'FAIL'}  {secs:.0f}s  tools: {', '.join(used) or '-'}")
        print(f"    Q: {q[:110]}")
        if cards:
            print(f"    cards: {', '.join(dict.fromkeys(got_ids)) or '-'}" + ("" if hit else f"   (expected one of {want})"))
        for b in bash_cmds:
            print("    $ " + b["cmd"].replace("\n", "\n      ")[:400] + "   -> " + b["out"].strip().splitlines()[-1])
        print("    A: " + (answer if a.show else answer[:300]).replace("\n", "\n       "))
    total = len(pick) or len(SCENARIOS)
    print(f"\nmode {mode}: {fails} of {total} failed" + (f"; expected card retrieved in {hits}/{total}" if cards else ""))
    return fails


tools = call("/api/tools")
if not tools.get("enabled"):
    sys.exit("lchat-web has tools disabled")
if "linux_help" not in {t["name"] for t in tools["tools"]}:
    sys.exit("this lchat-web has no linux_help tool (old version running?)")
modes = ["cards", "nocards"] if a.mode == "both" else [a.mode]
sys.exit(1 if sum(run(m, tools) for m in modes) else 0)

#!/usr/bin/env python3
"""Regression evals for the problems users hit in real chats, run through the REAL page (web/index.html).

A headless browser opens lchat-web and types each case into the page, so the page's own logic is what gets
tested: auto card lookup, how-to mode, loop guard, tool-call-as-text retry, status UI, history saving.
Calls to /api/tool are intercepted:
  - bash, read_file, write_file run in bubblewrap (eval/sandbox_util.py): / read-only, a fresh synthetic home per
    case, private /tmp, no network. Nothing on the real machine changes.
  - window_action is answered "not run (test)".
  - everything else (linux_help, web_search, web_fetch, download, recall, list_windows, screenshot) goes to the
    real server. screenshot is read-only and does not move focus.
Auto-approve is switched on in the test browser only, so sandboxed tools never wait for a click.

  python3 eval/problem_cases.py [--web http://127.0.0.1:8088] [--only id,id] [--show]

Each case lists the checks it must pass. Exit 1 when any check fails. Results: eval/out/problem_cases.jsonl.
"""

import argparse
import json
import os
import re
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sandbox_util import make_fixture, sandbox, sandbox_tool

ap = argparse.ArgumentParser(
    description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
)
ap.add_argument("--web", default="http://127.0.0.1:8088")
ap.add_argument("--only", default="", help="comma-separated case ids")
ap.add_argument("--show", action="store_true", help="print full answers")
ap.add_argument("--timeout", type=int, default=420, help="seconds per case")
ap.add_argument("--out", default="eval/out")
a = ap.parse_args()
WEB = a.web.rstrip("/")


def norm_cmd(c):  # the page's loop-guard normalisation (web/index.html normCmd)
    c = re.sub(r"\s+\|\|\s*(echo\b[^|;&]*|true|:)\s*$", "", str(c or ""))
    c = re.sub(r"\s*\d?>&\d|\s*\d?>\s*/dev/null", "", c)
    c = re.sub(r"\s*\|\s*(head|tail)(\s+-n)?\s+-?\d+", "", c)
    return re.sub(r"[;\s]+$", "", re.sub(r"\s+", " ", c)).strip()


TOOL_TEXT = re.compile(
    r'(^|\n)\s*\{\s*"[a-z_]+"\s*:|<tool_call>|<function='  # a JSON tool-argument object or tool-call markup
)
OTHER_PM = re.compile(
    r"\b(apt|apt-get|dnf|yum|zypper)\s+(install|update|upgrade|search)\b"
)


# ---------------------------------------------------------------- checks: (name, fn(result) -> bool)
def no_tools(*names):
    return (
        f"never calls {'/'.join(names)}",
        lambda r: not any(c["name"] in names for c in r["calls"]),
    )


def calls(name, pred=None, what=""):
    return (
        f"calls {name}{' ' + what if what else ''}",
        lambda r: any(
            c["name"] == name and (pred is None or pred(c["args"])) for c in r["calls"]
        ),
    )


ANSWERED = ("gives a non-empty answer", lambda r: len(r["answer"].strip()) > 20)
NO_TOOL_TEXT = (
    "answer is not a tool call written as text",
    lambda r: not TOOL_TEXT.search(r["answer"]),
)
ASKS = (
    "ends by asking the user (question in the last 400 chars)",
    lambda r: "?" in r["answer"][-400:],
)
FINISHED = (
    "turn ends with a visible final state, no spinner left",
    lambda r: (
        r["state"] and not r["state"].startswith("●") and r["running_phases"] == 0
    ),
)
SAVED = ("chat is saved to History", lambda r: r["saved_messages"] >= 2)
NO_DUP_BASH = (
    "never runs the same bash command twice in a turn",
    lambda r: len(r["bash_norm"]) == len(set(r["bash_norm"])),
)
NO_OTHER_PM = (
    "no apt/dnf/yum/zypper commands on this Arch-based system",
    lambda r: (
        not any(OTHER_PM.search(c) for c in r["bash"])
        and not OTHER_PM.search(r["answer"])
    ),
)
COMMON = [ANSWERED, NO_TOOL_TEXT, FINISHED, SAVED]

CASES = [
    {
        "id": "hello",
        "q": "hello",
        "why": "small talk: no card lookup, no tools",
        "checks": [
            no_tools("bash", "linux_help", "web_search", "screenshot"),
            (
                "no help-card lookup phase",
                lambda r: not any("help cards" in p for p in r["phases"]),
            ),
        ]
        + COMMON,
    },
    {
        "id": "howto-kill-regex",
        "q": "how to kill all process by regex",
        "why": "how-to question: explain + example + ask, never run; no raw JSON tool call as the answer",
        "checks": [
            no_tools("bash", "write_file"),
            ("shows pkill", lambda r: "pkill" in r["answer"]),
            (
                "help cards were added",
                lambda r: any("help cards:" in p and "added" in p for p in r["phases"]),
            ),
            ASKS,
        ]
        + COMMON,
    },
    {
        "id": "howto-nginx",
        "q": "how to configure nginx?",
        "why": "was: looped on `ls /usr/bin | grep apt`, tried apt-get on SteamOS",
        "checks": [
            no_tools("bash", "write_file"),
            NO_OTHER_PM,
            (
                "mentions this system's package tooling (pacman/flatpak/read-only)",
                lambda r: re.search(
                    r"pacman|flatpak|read-only|steamos-readonly|distrobox",
                    r["answer"],
                    re.IGNORECASE,
                ),
            ),
            ASKS,
        ]
        + COMMON,
    },
    {
        "id": "task-port-8000",
        "q": "Something already listens on port 8000. Find what it is and free the port.",
        "why": "was: ran `ss -ltnp | grep :8000` and `lsof -i :8000` three times each",
        "checks": [
            calls("bash"),
            NO_DUP_BASH,
            (
                "help cards were added",
                lambda r: any("help cards:" in p and "added" in p for p in r["phases"]),
            ),
        ]
        + COMMON,
    },
    {
        "id": "task-install-htop",
        "q": "install htop for me",
        "why": "system awareness: pacman/flatpak, read-only root; never apt",
        "checks": [NO_OTHER_PM, NO_DUP_BASH] + COMMON,
    },
    {
        "id": "screenshot-dolphin",
        "q": "what is inside the Dolphin window?",
        "needs_window": "dolphin",
        "why": "was: screenshot showed Chrome on top, then it web-searched what Dolphin is",
        "checks": [
            calls(
                "screenshot",
                lambda x: "dolphin" in str(x.get("target", "")).lower(),
                "on the Dolphin window",
            ),
            no_tools("web_search"),
        ]
        + COMMON,
    },
    {
        "id": "web-search",
        "q": "Search the web: what is the latest stable nginx version?",
        "why": "was: 'search failed' when two browser sessions ran at once",
        "checks": [
            calls("web_search"),
            (
                "web_search returned results",
                lambda r: any(
                    c["name"] == "web_search"
                    and not c["result"].startswith("search failed")
                    for c in r["calls"]
                ),
            ),
        ]
        + COMMON,
    },
    {
        "id": "issue-missing-script",
        "q": "# lchat issue: rutracker-sync.service failed\n\n- Unit: rutracker-sync.service\n- Severity: error\n"
        "- Kind: service\n- Count: 4\n\nJournal:\n/usr/bin/python3: can't open file "
        "'/home/deck/Projects/pi-code-agent-harness/opencode/skills/rutracker/rutracker.py': [Errno 2] No such "
        "file or directory\nrutracker-sync.service: Main process exited, code=exited, status=2/INVALIDARGUMENT\n\n"
        "Find the cause and tell me how to fix it.",
        "why": "was: started with `ls -la /home/deck/` instead of checking the path from the error",
        "checks": [
            (
                "never runs a bare listing of the home directory",
                lambda r: not any(HOME_LS.match(c) for c in r["bash"]),
            ),
            (
                "the first command looks at the script path, the unit or the journal",
                lambda r: (
                    not r["bash"]
                    or re.search(
                        r"rutracker|pi-code-agent-harness|systemctl|journalctl|find |locate ",
                        r["bash"][0],
                    )
                ),
            ),
            (
                "says the script file is missing / the path is wrong",
                lambda r: re.search(
                    r"(no such file|does not exist|doesn't exist|missing|not found|wrong path|moved)",
                    r["answer"],
                    re.IGNORECASE,
                ),
            ),
        ]
        + COMMON,
    },
]
CASES.append({
    "id": "cwd-project",
    "q": "In my project ~/Projects/demo, how many .py files are there (including subfolders)? Run it and tell me.",
    "why": "working directory: start in ~, go to the project (cd or absolute path), answer from the result",
    "checks": [
        calls("bash"),
        ("works in the project folder (cd into it or uses its path)",
         lambda r: any("Projects/demo" in c for c in r["bash"])),
        ("answers 3", lambda r: re.search(r"\b3\b|three", r["answer"], re.I)),
        NO_DUP_BASH,
    ] + COMMON,
})
CASES.append({
    "id": "howto-update-all",
    "q": "how do I update all my apps and packages?",
    "why": "package guide: this system's managers (pacman -Syu + flatpak update), no apt, ask before running",
    "checks": [
        no_tools("bash", "write_file"), NO_OTHER_PM,
        ("shows the native full update (pacman -Syu)", lambda r: "pacman -Syu" in r["answer"]),
        ("covers flatpak apps too", lambda r: "flatpak update" in r["answer"]),
        ASKS,
    ] + COMMON,
})
CASES.append({
    "id": "task-remove-app",
    "q": "remove the vlc app from my computer",
    "why": "package guide: find which manager installed it before removing; never another distro's manager",
    "checks": [
        NO_OTHER_PM,
        ("checks how it is installed before any remove command",
         lambda r: (lambda cmds: not any(re.search(r"(-R|uninstall|remove)\b", c) for c in cmds[:1]))(r["bash"])),
        ("looks it up in the installed-package lists (pacman -Q / flatpak list)",
         lambda r: any(re.search(r"pacman -Q|flatpak list|flatpak info|command -v|which ", c) for c in r["bash"])),
    ] + COMMON,
})
HOME_LS = re.compile(
    r"^\s*ls(\s+-[a-zA-Z]+)*\s*(~/?|\$HOME/?|/home/[^/\s]+/?|\./?)?\s*$"
)  # web/index.html HOME_LS


def run_case(pg, case):
    fix = make_fixture()
    calls_seen = []

    def handle(route, request):
        try:
            body = json.loads(request.post_data or "{}")
        except ValueError:
            body = {}
        name, args = body.get("name", ""), body.get("args") or {}
        if name in ("bash", "read_file", "write_file"):
            text = sandbox_tool(fix, name, args)
            calls_seen.append({"name": name, "args": args, "result": text[-1500:]})
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"text": text}),
            )
        elif name == "window_action":
            calls_seen.append({"name": name, "args": args, "result": "not run (test)"})
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"text": "not run (test)"}),
            )
        else:
            resp = route.fetch(
                timeout=300_000
            )  # slow tools (web_search, busy reranker)
            try:
                text = resp.json().get("text", "")
            except Exception:
                text = ""
            calls_seen.append(
                {
                    "name": name,
                    "args": args,
                    "result": text[:1500],
                    "auto": args.get("min_score") is not None,
                }
            )
            route.fulfill(response=resp)

    pg.route("**/api/tool", handle)
    pg.click("#newBtn")
    pg.fill("#in", case["q"])
    pg.press("#in", "Enter")
    t0, st = time.time(), None
    while time.time() - t0 < a.timeout:
        w = pg.query_selector_all(".msg.asst")
        st = w[-1].query_selector(".state") if w else None
        if (
            st
            and st.get_attribute("hidden") is None
            and "run" not in (st.get_attribute("class") or "")
        ):
            break
        time.sleep(1)
    w = pg.query_selector_all(".msg.asst")[-1]
    answer = "\n".join(b.inner_text() for b in w.query_selector_all(".body"))
    phases = [li.text_content() for li in w.query_selector_all(".phases li")]
    running = len(w.query_selector_all(".phases li.run"))
    state = st.text_content() if st and st.get_attribute("hidden") is None else ""
    if not state or "run" in (st.get_attribute("class") or ""):
        state = "● still running (timeout)"
    time.sleep(1.5)  # let the last save land
    chat_id = pg.evaluate("CHAT_ID")
    saved = pg.evaluate(
        """async id=>{try{const d=await api('/api/history/get?id='+encodeURIComponent(id));
                             return (d.messages||[]).length}catch(e){return 0}}""",
        chat_id,
    )
    # test chats must not stay in the user's History (all instances share ~/.local/state/lchat/history.db)
    pg.evaluate(
        "async id=>{try{await api('/api/history/delete',{id})}catch(e){}}", chat_id
    )
    pg.unroute("**/api/tool")
    tree = sandbox(fix, "find . -type f -newer /etc/hostname | sort | head -40")
    shutil.rmtree(fix, ignore_errors=True)
    user_calls = [c for c in calls_seen if not c.get("auto")]
    bash = [c["args"].get("command", "") for c in user_calls if c["name"] == "bash"]
    return {
        "id": case["id"],
        "q": case["q"],
        "answer": answer,
        "phases": phases,
        "state": state,
        "running_phases": running,
        "calls": user_calls,
        "bash": bash,
        "bash_norm": [norm_cmd(b) for b in bash],
        "saved_messages": saved,
        "seconds": round(time.time() - t0),
        "files_changed": tree,
    }


def main():
    from cloakbrowser import launch

    pick = {x for x in a.only.split(",") if x}
    os.makedirs(a.out, exist_ok=True)
    out = open(os.path.join(a.out, "problem_cases.jsonl"), "w", encoding="utf-8")
    b = launch(headless=True)
    pg = b.new_page(viewport={"width": 1280, "height": 900})
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.goto(WEB + "/", wait_until="domcontentloaded")
    pg.evaluate(
        "()=>{localStorage.setItem('lchat.auto','1');localStorage.setItem('lchat.agent','1')}"
    )
    pg.reload(wait_until="domcontentloaded")
    time.sleep(2)
    windows = ""
    try:
        windows = pg.evaluate(
            "async()=>{const r=await fetch('/api/tool',{method:'POST',headers:{'Content-Type':"
            "'application/json'},body:JSON.stringify({name:'list_windows',args:{}})});"
            "return (await r.json()).text||''}"
        ).lower()
    except Exception:
        pass
    total = failed = 0
    for case in CASES:
        if pick and case["id"] not in pick:
            continue
        if case.get("needs_window") and case["needs_window"] not in windows:
            print(f"\n[{case['id']}] SKIP  (no {case['needs_window']} window open)")
            continue
        r = run_case(pg, case)
        res = [(name, bool(fn(r))) for name, fn in case["checks"]]
        ok = all(v for _, v in res)
        total += 1
        failed += not ok
        r.update(passed=ok, checks=dict(res), why=case["why"])
        out.write(json.dumps(r, ensure_ascii=False) + "\n")
        out.flush()
        print(
            f"\n[{case['id']}] {'PASS' if ok else 'FAIL'}  {r['seconds']}s  tools: "
            f"{', '.join(c['name'] for c in r['calls']) or '-'}  ({case['why']})"
        )
        for name, v in res:
            if not v:
                print(f"    ✗ {name}")
        for c in r["bash"]:
            print("    $ " + c[:160])
        print(
            "    A: "
            + (r["answer"] if a.show else r["answer"][:280]).replace("\n", "\n       ")
        )
    b.close()
    if errors:
        print("\npage errors:", errors[:5])
    print(f"\n{failed} of {total} cases failed")
    sys.exit(1 if failed or errors else 0)


if __name__ == "__main__":
    main()

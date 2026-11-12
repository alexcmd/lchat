#!/usr/bin/env python3
"""A/B eval of the system-info context: does the model answer questions about THIS machine correctly?

base = the System paragraph of lchat-web at --base-rev (default 03f66cc, before this change), no system_facts tool, no warnings line, prompt without the
       system_facts sentence (what lchat had before this change).
new  = the running lchat-web: extended System paragraph, per-turn warnings line, system_facts tool.

Same agent loop as the page (headless, like chat_cards.py). bash/read_file/write_file run in the bubblewrap sandbox
(/ read-only, fixture home, no network); every other tool runs for real through lchat-web /api/tool.
The expected facts are read from this machine when the eval starts, so the checks follow the live state.

  python3 eval/chat_sysinfo.py --web http://127.0.0.1:18120 [--mode both|base|new] [--only 1,3] [--no-think]
Writes eval/out/chat_sysinfo-<mode>.jsonl and prints a per-scenario table and totals.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sandbox_util import make_fixture, sandbox_tool

ap = argparse.ArgumentParser()
ap.add_argument("--web", default="http://127.0.0.1:8088")
ap.add_argument("--mode", choices=["base", "new", "both"], default="both")
ap.add_argument("--only", default="")
ap.add_argument("--max-steps", type=int, default=8)
ap.add_argument("--max-tokens", type=int, default=2048)
ap.add_argument("--no-think", action="store_true")
ap.add_argument("--out", default="eval/out")
ap.add_argument("--base-rev", default="03f66cc", help="commit whose System paragraph is the baseline (last one before system_facts)")
a = ap.parse_args()
WEB = a.web.rstrip("/")
SANDBOX_TOOLS = {"bash", "read_file", "write_file"}
SKIP_TOOLS = {
    "screenshot",
    "window_action",
    "list_windows",
    "schedule_task",
    "list_tasks",
    "manage_task",
}


def sh(cmd):
    return subprocess.run(
        ["bash", "-c", cmd], capture_output=True, text=True, timeout=30
    ).stdout.strip()


# ---- ground truth from the live machine ----
def truth():
    t = {}
    t["top"] = [
        os.path.basename(x)
        for x in sh(
            "ps -eo comm --sort=-pcpu --no-headers | grep -vE '^(ps|bash|python3)$' | head -4"
        ).split()
    ]
    df = sh("df -P / | tail -1").split()
    t["root_pct"], t["root_free_m"] = int(df[4].rstrip("%")), int(df[3]) // 1024
    dh = sh("df -P ~ | tail -1").split()
    t["home_pct"], t["home_free_g"] = int(dh[4].rstrip("%")), int(dh[3]) // 1024 // 1024
    t["pubip"] = sh("curl -4 -s --max-time 5 https://ifconfig.me")
    b = sorted(__import__("glob").glob("/sys/class/power_supply/BAT*"))[0]
    rd = lambda f: open(os.path.join(b, f)).read().strip()
    t["bat"] = int(rd("capacity"))
    try:
        t["health"] = int(rd("energy_full")) * 100 // int(rd("energy_full_design"))
    except OSError:
        t["health"] = int(rd("charge_full")) * 100 // int(rd("charge_full_design"))
    t["ssid"] = (
        sh("nmcli -t -f ACTIVE,SSID dev wifi | grep '^yes' | cut -d: -f2") or "?"
    ).splitlines()[0]
    t["pacman"] = re.search(r"\d+\.\d+\.\d+", sh("pacman -V")).group(0)
    t["pip"] = sh("command -v pip")
    return t


T = truth()


def top_procs(k=5):
    return [os.path.basename(x) for x in sh("ps -eo comm --sort=-pcpu --no-headers | grep -vE "
                                            "'^(ps|bash|python3|grep|head)$' | head -%d" % k).split()]


def near(n, tol):  # regex for an integer within +-tol
    return (
        "(" + "|".join(str(x) for x in range(max(0, n - tol), n + tol + 1)) + r")(?!\d)"
    )


# (question, [regexes that must all match the answer], [regexes that must not match])
SCENARIOS = [
    (
        "My computer feels slow right now. What is using it?",
        lambda: ["(" + "|".join(map(re.escape, top_procs())) + ")"],   # read just before the question
        [],
    ),
    (
        "Is my disk getting full?",
        [
            rf"({near(T['root_pct'], 2)} ?%|0\.8 ?G|{near(T['root_free_m'], 40)} ?M)",
            rf"({near(T['home_pct'], 2)} ?%|{near(T['home_free_g'], 2)}(\.\d)? ?G)",
        ],
        [],
    ),
    (
        "Which GPU and graphics driver do I have, and can I use Vulkan?",
        [r"amdgpu|radv", r"vangogh|amd"],
        [r"nvidia-smi"],
    ),
    (
        "I get no sound from the speakers. Which audio system am I running and how do I restart it?",
        [r"pipewire", r"systemctl --user restart"],
        [r"pulseaudio -k", r"alsa (force-)?reload"],
    ),
    (
        "Is my VPN on, and what is my public IP address?",
        [r"wg0|wireguard", re.escape(T["pubip"]) or "x^"],
        [],
    ),
    (
        "Is Docker running? List my containers.",
        [
            r"not running|isn'?t running|no socket|not started|is down|cannot connect|can'?t connect|failed to connect"
        ],
        [],
    ),
    (
        "Do I have kubectl, and which Kubernetes cluster am I connected to?",
        [r"not installed|isn'?t installed|no kubectl|not available|not found"],
        [],
    ),
    (
        "Is this a laptop? How healthy is my battery?",
        [rf"{near(T['health'], 1)} ?%", r"battery"],
        [r"\bdesktop\b(?! \()"],
    ),
    (
        "Which pip will run if I type pip, and what pacman version do I have?",
        [
            re.escape(T["pip"].replace(os.path.expanduser("~"), "")),
            re.escape(T["pacman"]),
        ],
        [],
    ),
    (
        "Should I download the x86_64 or the ARM build of an app for this machine?",
        [r"x86[_-]64|amd64"],
        [r"download the arm"],
    ),
    (
        "Am I on Wi-Fi, and how strong is the signal?",
        [re.escape(T["ssid"]), r"\d\d ?%|signal (strength )?(of |is )?\d\d|-\d\d ?dBm"],
        [],
    ),
]


def call(path, body=None, timeout=900):
    req = urllib.request.Request(
        WEB + path,
        json.dumps(body).encode() if body is not None else None,
        {"Content-Type": "application/json", "Origin": WEB},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def head_sysinfo():
    """the System paragraph exactly as git HEAD's lchat-web computed it"""
    src = subprocess.run(
        ["git", "show", f"{a.base_rev}:bin/lchat-web"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    s, e = (
        src.index("import platform as _platform"),
        src.index("SYSTEM_INFO = system_info()"),
    )
    ns = {"os": os, "subprocess": subprocess}
    exec(src[s:e], ns)
    return ns["system_info"]()


def prompt(sysinfo, new):
    html = urllib.request.urlopen(WEB + "/", timeout=30).read().decode()
    t = re.search(
        r"function agentPrompt\(\)\{.*?return `(.*?)`;", html, re.DOTALL
    ).group(1)
    if not new:
        t = re.sub(
            r" For questions about this machine's current state[^\n]*?needs no approval\.",
            "",
            t,
        )
        assert "system_facts" not in t
    d = time.localtime()
    return (
        t.replace("${d.toDateString()}", time.strftime("%a %b %d %Y", d))
        .replace("${d.toLocaleTimeString()}", time.strftime("%H:%M:%S", d))
        .replace("${CWD}", os.path.expanduser("~"))
        .replace("${SYSINFO||'unknown (lchat-web did not report it)'}", sysinfo)
    )


def run(mode, tools):
    new = mode == "new"
    schemas = [
        t["schema"]
        for t in tools["tools"]
        if t["name"] not in SKIP_TOOLS and (new or t["name"] != "system_facts")
    ]
    sysinfo = tools["system"] if new else head_sysinfo()
    SYS = prompt(sysinfo, new)
    os.makedirs(a.out, exist_ok=True)
    out = open(os.path.join(a.out, f"chat_sysinfo-{mode}.jsonl"), "w", encoding="utf-8")
    pick = {int(x) for x in a.only.split(",") if x}
    res = []
    print(
        f"\n===== mode {mode}: {len(schemas)} tools, system prompt {len(SYS)} chars =====",
        flush=True,
    )
    for n, (q, must, mustnot) in enumerate(SCENARIOS, 1):
        if callable(must):
            must = must()
        if pick and n not in pick:
            continue
        sys_turn = SYS
        if new:
            alerts = call("/api/sysstate").get("alerts")
            if alerts:
                sys_turn += (
                    "\n\nMachine warnings right now (take them into account when they are relevant to the request): "
                    + alerts
                )
        H, used, fix, t0, answer, steps = (
            [{"role": "user", "content": q}],
            [],
            make_fixture(),
            time.time(),
            "",
            0,
        )
        seen = set()
        for steps in range(1, a.max_steps + 1):
            req = {
                "messages": [{"role": "system", "content": sys_turn}] + H,
                "tools": schemas,
                "stream": False,
                "max_tokens": a.max_tokens,
                "chat_template_kwargs": {"enable_thinking": not a.no_think},
            }
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
                used.append(
                    name
                    + (f"({args.get('topic', '')})" if name == "system_facts" else "")
                )
                key = name + json.dumps(args, sort_keys=True)
                if key in seen:
                    text = "ALREADY RAN: same call earlier in this turn; use that result or answer."
                elif name in SANDBOX_TOOLS:
                    text = sandbox_tool(fix, name, args)
                else:
                    text = call(
                        "/api/tool", {"name": name, "args": args, "session": "eval-sys"}
                    ).get("text", "")
                seen.add(key)
                H.append(
                    {
                        "role": "tool",
                        "tool_call_id": c.get("id", ""),
                        "name": name,
                        "content": text,
                    }
                )
        if not answer:
            H.append(
                {
                    "role": "user",
                    "content": "Stop calling tools now and give your final answer.",
                }
            )
            req = {
                "messages": [{"role": "system", "content": sys_turn}] + H,
                "stream": False,
                "max_tokens": a.max_tokens,
                "chat_template_kwargs": {"enable_thinking": not a.no_think},
            }
            answer = (
                call("/v1/chat/completions", req)["choices"][0]["message"].get(
                    "content"
                )
                or ""
            )
        __import__("shutil").rmtree(fix, ignore_errors=True)
        miss = [r for r in must if not re.search(r, answer, re.IGNORECASE)]
        bad = [r for r in mustnot if re.search(r, answer, re.IGNORECASE)]
        ok = not miss and not bad
        secs = round(time.time() - t0)
        res.append({"n": n, "ok": ok, "steps": steps, "tools": len(used), "secs": secs})
        out.write(
            json.dumps(
                {
                    "n": n,
                    "mode": mode,
                    "q": q,
                    "ok": ok,
                    "missing": miss,
                    "forbidden_hit": bad,
                    "tools": used,
                    "steps": steps,
                    "seconds": secs,
                    "answer": answer,
                },
                ensure_ascii=False,
            )
            + "\n"
        )
        out.flush()
        print(
            f"[{n:2}] {'PASS' if ok else 'FAIL'} {secs:4}s steps {steps} tools: {', '.join(used) or '-'}",
            flush=True,
        )
        if not ok:
            print(
                f"     missing {miss} forbidden {bad}\n     A: {answer[:240]!r}",
                flush=True,
            )
    p = sum(r["ok"] for r in res)
    print(
        f"mode {mode}: {p}/{len(res)} passed, mean steps {sum(r['steps'] for r in res) / max(1, len(res)):.1f}, "
        f"tool calls {sum(r['tools'] for r in res)}, total {sum(r['secs'] for r in res)}s",
        flush=True,
    )
    return res


tools = call("/api/tools")
if "system_facts" not in {t["name"] for t in tools["tools"]}:
    sys.exit("this lchat-web has no system_facts tool (old version running?)")
print("truth:", json.dumps(T))
for m in ["base", "new"] if a.mode == "both" else [a.mode]:
    run(m, tools)

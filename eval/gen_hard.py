#!/usr/bin/env python3
"""Harder synthetic chat history: many look-alike chats, values that change over time,
long logs full of numbers, and questions that need two chats. Writes dataset_hard.json."""

import json
import os
import random

R = random.Random(7)
HERE = os.path.dirname(os.path.abspath(__file__))
DAY = 86400
T0 = 1790000000  # base timestamp; chat i gets T0 + order*DAY

SERVICES = [
    ("embedding server", "text embedding service", "llama serve --embedding"),
    ("reranker", "result reranking model", "llama serve --reranking"),
    ("vision proxy", "image model proxy", "uvicorn vproxy:app"),
    ("whisper transcriber", "speech-to-text service", "whisper-server"),
    ("tts service", "text-to-speech server", "piper-http"),
    ("searxng", "self-hosted search engine", "searxng-run"),
    ("grafana", "dashboards (grafana)", "grafana-server"),
    ("prometheus", "metrics database", "prometheus"),
    ("qdrant", "vector database", "qdrant"),
    ("redis cache", "key-value cache", "redis-server"),
    ("postgres", "sql database", "postgres"),
    ("minio storage", "s3 object store", "minio server"),
    ("jellyfin", "media server", "jellyfin"),
    ("home assistant", "smart home hub", "hass"),
    ("gitea", "self-hosted git server", "gitea web"),
    ("comfyui", "image generation ui", "python main.py"),
    ("syncthing gui", "file sync web ui", "syncthing"),
    ("chat model server", "main llm server", "llama serve --jinja"),
    ("code completion server", "code autocomplete model", "llama serve --fim"),
    ("metrics exporter", "node metrics exporter", "node_exporter"),
]
MODELS = [
    "qwen3-4b-q4_k_m",
    "gemma-3-1b-q8_0",
    "lfm3-vl-2b-q4_0",
    "phi-4-mini-q4",
    "smollm3-3b-q6_k",
    "granite-4-tiny-q5",
    "llama-3.2-3b-q4",
    "ministral-3b-q4",
    "deepseek-r1-1.5b-q8",
    "olmo-2-1b-q8",
]
REASONS = [
    ("port clash with steam remote play", "steam remote play took the port"),
    (
        "the firewall only opens the 99xx range",
        "only 99xx is allowed through the firewall",
    ),
    ("docker grabbed the old port", "a docker container took the old port"),
    (
        "wanted all llm things next to each other",
        "keep all llm services next to each other",
    ),
    ("tailscale serve was using it", "tailscale serve grabbed it"),
]

used_ports = set()


def port():
    while True:
        p = R.randint(8000, 9999)
        if p not in used_ports:
            used_ports.add(p)
            return p


def tc(i, name, args):
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": f"t{i}",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)},
            }
        ],
    }


def tool(i, name, content):
    return {"role": "tool", "tool_call_id": f"t{i}", "name": name, "content": content}


def journal(n, proc):
    lines = []
    for _ in range(n):
        lines.append(
            f"Nov {R.randint(1, 28):2d} {R.randint(0, 23):02d}:{R.randint(0, 59):02d}:{R.randint(0, 59):02d} "
            f"steamdeck {proc}[{R.randint(1000, 99999)}]: "
            + R.choice(
                [
                    f"req id={R.randint(1, 99999)} took {R.randint(1, 900)}ms status=200",
                    f"gc pause {R.randint(1, 80)}ms heap={R.randint(100, 4000)}MB",
                    f"conn from 127.0.0.1:{R.randint(30000, 65000)} closed",
                    f"slot {R.randint(0, 3)} n_past={R.randint(10, 9000)} t={R.random():.3f}s",
                    f"cache hit ratio {R.random():.2f} keys={R.randint(10, 9999)}",
                ]
            )
        )
    return "\n".join(lines)


def ss_dump(ports):
    rows = ["State  Recv-Q Send-Q Local Address:Port  Peer Address:Port Process"]
    for p, proc in ports:
        rows.append(
            f'LISTEN 0      4096       127.0.0.1:{p}        0.0.0.0:*     users:(("{proc}",pid={R.randint(900, 99999)},fd={R.randint(3, 30)}))'
        )
    return "\n".join(rows)


chats, queries, order = [], [], 0
svc = []  # per service: list of (chat_id, port) in time order


def add_chat(cid, title, msgs):
    global order
    order += 1
    chats.append(
        {"id": cid, "title": title, "messages": msgs, "updated": T0 + order * DAY}
    )


# --- phase 1: initial setup of every service (long logs, unique error codes) ---
for si, (en, alias, cmd) in enumerate(SERVICES):
    p = port()
    err = f"E{R.randint(1000, 9999)}"
    cid = f"s{si:02d}a"
    proc = cmd.split()[0]
    add_chat(
        cid,
        f"setting up the {en}",
        [
            {
                "role": "user",
                "content": f"help me set up the {en} as a user service on the deck",
            },
            tc(
                1,
                "bash",
                {
                    "command": f"systemctl --user status {proc.replace(' ', '-')}; journalctl --user -n 80"
                },
            ),
            tool(
                1,
                "bash",
                journal(70, proc)
                + f"\n{proc}: fatal: startup failed with error code {err} (missing config dir)",
            ),
            {
                "role": "assistant",
                "content": f"It fails with error code {err} because the config directory is missing. "
                f"I created it and started it with `{cmd} --port {p}`.",
            },
            {"role": "user", "content": "works now, thanks"},
        ],
    )
    svc.append([(cid, p)])
    queries.append(
        {
            "q": f"what error code did the {en} fail with when I first set it up?",
            "lang": "en",
            "type": "detail",
            "chat": cid,
            "needle": err,
        }
    )
    if si % 2:
        queries.append(
            {
                "q": f"the {alias} crashed on its very first start - which code did it report?",
                "lang": "en",
                "type": "detail",
                "chat": cid,
                "needle": err,
            }
        )

# --- phase 2: noise chats - port audits that mention many services and ports ---
for ni in range(60):
    pick = R.sample(range(len(SERVICES)), 6)
    listing = [(R.randint(8000, 9999), SERVICES[i][2].split()[0]) for i in pick]
    add_chat(
        f"n{ni:02d}x",
        R.choice(
            [
                "which ports are open",
                "port audit",
                "check listening sockets",
                "what is listening on localhost",
                "why is the deck slow",
            ]
        ),
        [
            {"role": "user", "content": "show me what is listening right now"},
            tc(1, "bash", {"command": "ss -ltnp"}),
            tool(1, "bash", ss_dump(listing) + "\n" + journal(25, "kernel")),
            {
                "role": "assistant",
                "content": "Here is everything listening on localhost; nothing unusual.",
            },
        ],
    )

# --- phase 3: moves over time (1 or 2 moves per service), interleaved with more noise ---
moves = [(si, k) for si in range(len(SERVICES)) for k in range(1 + (si % 2))]
moves.sort(key=lambda x: x[1])
for si, k in moves:
    en, alias, cmd = SERVICES[si]
    old = svc[si][-1][1]
    new = port()
    reason = R.choice(REASONS)
    cid = f"s{si:02d}{'bc'[k]}"
    add_chat(
        cid,
        R.choice(
            [
                f"{en} port change",
                f"moving {en}",
                f"fix {en} port",
                f"{en} not reachable",
            ]
        ),
        [
            {
                "role": "user",
                "content": f"the {en} on {old} keeps failing, {reason[0]}",
            },
            tc(
                1,
                "bash",
                {
                    "command": f"sed -i 's/--port {old}/--port {new}/' ~/.config/systemd/user/{cmd.split()[0]}.service && systemctl --user daemon-reload"
                },
            ),
            tool(1, "bash", ""),
            {
                "role": "assistant",
                "content": f"Moved the {en} from port {old} to port {new} (reason: {reason[0]}). "
                f"Restarted it; it answers on {new} now.",
            },
        ],
    )
    svc[si].append((cid, new))
    for _ in range(4):
        pick = R.sample(range(len(SERVICES)), 5)
        add_chat(
            f"n{len(chats):03d}y",
            "quick port check",
            [
                {"role": "user", "content": "is everything still up?"},
                tc(1, "bash", {"command": "ss -ltnp | grep 127.0.0.1"}),
                tool(
                    1,
                    "bash",
                    ss_dump(
                        [
                            (R.randint(8000, 9999), SERVICES[i][2].split()[0])
                            for i in pick
                        ]
                    ),
                ),
                {"role": "assistant", "content": "All services are listening."},
            ],
        )

# --- phase 4: model assignments by port only (for two-fact questions) ---
for si in range(0, len(SERVICES), 2):
    cid_last, p_last = svc[si][-1]
    m = MODELS[(si // 2) % len(MODELS)]
    cid = f"m{si:02d}z"
    add_chat(
        cid,
        "swap the gguf",
        [
            {
                "role": "user",
                "content": f"load {m} on whatever is listening on {p_last}",
            },
            tc(
                1,
                "bash",
                {"command": f"curl -s 127.0.0.1:{p_last}/props | jq .model_path"},
            ),
            tool(1, "bash", f'"/home/deck/models/{m}.gguf"'),
            {"role": "assistant", "content": f"Port {p_last} now serves {m}."},
        ],
    )
    en, alias, _ = SERVICES[si]
    queries.append(
        {
            "q": f"which model does the {en} serve now?",
            "lang": "en",
            "type": "two-fact",
            "chat": cid_last,
            "chat2": cid,
            "needle": str(p_last),
            "needle2": m,
        }
    )
    queries.append(
        {
            "q": f"what gguf is the {alias} running these days?",
            "lang": "en",
            "type": "two-fact",
            "chat": cid_last,
            "chat2": cid,
            "needle": str(p_last),
            "needle2": m,
        }
    )

# --- current-value and old-value questions ---
for si, (en, alias, _) in enumerate(SERVICES):
    hist = svc[si]
    cid, p = hist[-1]
    if si % 2 == 0:
        queries.append(
            {
                "q": f"what port is the {en} on now?",
                "lang": "en",
                "type": "latest",
                "chat": cid,
                "needle": f"port {p}",
                "stale": [c for c, _ in hist[:-1]],
            }
        )
    else:
        queries.append(
            {
                "q": f"where does the {alias} listen at the moment?",
                "lang": "en",
                "type": "latest",
                "chat": cid,
                "needle": f"port {p}",
                "stale": [c for c, _ in hist[:-1]],
            }
        )
    if len(hist) >= 3:
        queries.append(
            {
                "q": f"why did I move the {en} the first time?",
                "lang": "en",
                "type": "history",
                "chat": hist[1][0],
                "needle": f"from port {hist[0][1]}",
            }
        )

# --- phase 5: two-fact links that are NOT ports: a hostname, an IP address, a file path ---
# separate generator so the phases above stay byte-identical
R2 = random.Random(11)
used_lan = set()


def lan_ip():
    while True:
        a = f"192.168.{R2.randint(1, 254)}.{R2.randint(2, 254)}"
        if a not in used_lan:
            used_lan.add(a)
            return a


HOST_PAIRS = [
    ("photos", "nas-photos"),
    ("game saves", "gamesave-pi"),
    ("homelab wiki", "wiki-box"),
    ("music library", "music-mini"),
]
DEVICE_FW = [
    ("hall router", "openwrt-24.10.2"),
    ("office access point", "openwrt-23.05.5"),
    ("garage switch", "opnsense-24.7.9"),
    ("spare router", "asuswrt-merlin-3006.102.1"),
]
CONFIG_PAIRS = [
    ("syncthing", "config", "rescan_interval", "3600"),
    ("restic", "restic", "keep_daily", "9"),
    ("rclone", "rclone", "transfers", "12"),
    ("kopia", "repo", "max_parallel_snapshots", "4"),
]

# hostname link: chat A names the backup host, chat B (later) gives it a new IP
for k, (project, host) in enumerate(HOST_PAIRS):
    new_ip = lan_ip()
    cid_a, cid_b = f"h{k:02d}a", f"h{k:02d}b"
    add_chat(
        cid_a,
        f"backup target for {project}",
        [
            {
                "role": "user",
                "content": f"the nightly backup for {project} goes to {host}.local",
            },
            {
                "role": "assistant",
                "content": f"Noted. The {project} backup will go to {host}.local.",
            },
        ],
    )
    add_chat(
        cid_b,
        "backup host got a new ip",
        [
            {"role": "user", "content": f"{host}.local got a new IP, now {new_ip}"},
            {
                "role": "assistant",
                "content": f"Updated the backup target notes; {host}.local now resolves to {new_ip}.",
            },
        ],
    )
    ru = k == 3
    queries.append(
        {
            "q": f"какой IP сейчас у цели ночного бэкапа {project}?"
            if ru
            else f"what IP does the backup target of {project} have now?",
            "lang": "ru" if ru else "en",
            "type": "two-fact",
            "chat": cid_a,
            "chat2": cid_b,
            "needle": f"{host}.local",
            "needle2": new_ip,
        }
    )

# IP link: chat A places the device at an IP, chat B (later) gives that IP a firmware
for k, (device, fw) in enumerate(DEVICE_FW):
    dev_ip = lan_ip()
    cid_a, cid_b = f"i{k:02d}a", f"i{k:02d}b"
    add_chat(
        cid_a,
        f"where the {device} is",
        [
            {"role": "user", "content": f"the {device} lives at {dev_ip}"},
            {
                "role": "assistant",
                "content": f"Got it, the {device} is at {dev_ip} on the LAN.",
            },
        ],
    )
    add_chat(
        cid_b,
        "firmware on the lan boxes",
        [
            {"role": "user", "content": f"{dev_ip} runs firmware {fw}"},
            {
                "role": "assistant",
                "content": f"Recorded the firmware version for {dev_ip}.",
            },
        ],
    )
    queries.append(
        {
            "q": f"what firmware is the {device} running?",
            "lang": "en",
            "type": "two-fact",
            "chat": cid_a,
            "chat2": cid_b,
            "needle": dev_ip,
            "needle2": fw,
        }
    )

# file-path link: chat A gives the config path, chat B (later) edits that file
for k, (app, fname, key, value) in enumerate(CONFIG_PAIRS):
    path = f"~/.config/{app}/{fname}.toml"
    cid_a, cid_b = f"f{k:02d}a", f"f{k:02d}b"
    add_chat(
        cid_a,
        f"{app} config location",
        [
            {"role": "user", "content": f"{app} reads its config from {path}"},
            {
                "role": "assistant",
                "content": f"OK, I will look in {path} when {app} misbehaves.",
            },
        ],
    )
    add_chat(
        cid_b,
        f"{app} config edit",
        [
            {"role": "user", "content": f"change one {app} setting for me"},
            tc(
                1,
                "bash",
                {"command": f"sed -i 's/^{key} = .*/{key} = {value}/' {path}"},
            ),
            tool(1, "bash", ""),
            {"role": "assistant", "content": f"edited {path}: set {key} = {value}"},
        ],
    )
    ru = k == 3
    queries.append(
        {
            "q": f"какое значение я задал параметру {key} для {app}?"
            if ru
            else f"what {key} did I set for {app}?",
            "lang": "ru" if ru else "en",
            "type": "two-fact",
            "chat": cid_a,
            "chat2": cid_b,
            "needle": path,
            "needle2": f"{key} = {value}",
        }
    )

json.dump(
    {"chats": chats, "queries": queries},
    open(os.path.join(HERE, "dataset_hard.json"), "w"),
    ensure_ascii=False,
    indent=1,
)
from collections import Counter

print(
    len(chats), "chats,", len(queries), "queries", Counter(q["type"] for q in queries)
)

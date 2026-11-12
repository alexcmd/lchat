"""Shared by the chat evals: a synthetic fixture home and a bubblewrap sandbox for bash/read_file/write_file.

The sandbox mounts / read-only, the fixture as the home directory, a private /tmp, and no network, so a model's
commands can never change the real machine."""
import os, re, time

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
    for n in ("main.py", "util.py", "tests/test_util.py", "README.md", "notes.txt"):   # a small project
        w(f"Projects/demo/{n}", "print('x')\n" if n.endswith(".py") else "text\n")
    return f


def sandbox(fix, cmd, timeout=60):
    """bash -c cmd with / read-only, the fixture as the home dir, a private /tmp, no network."""
    import subprocess
    argv = ["bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp",
            "--bind", fix, HOME, "--unshare-all", "--die-with-parent", "--chdir", HOME,
            "--setenv", "HOME", HOME, "timeout", str(timeout), "bash", "-c", cmd]
    try:
        r = subprocess.run(argv, capture_output=True, text=True, errors="replace", timeout=timeout + 10)
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

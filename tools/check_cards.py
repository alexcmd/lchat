#!/usr/bin/env python3
"""Check lchat help cards: JSON Schema (cards/card.schema.json), unique ids, and bash syntax (`bash -n`) of every
step command, alternative, variant and script, with <PLACEHOLDERS> replaced by their example values.

Usage: python3 tools/check_cards.py [PATH ...]   (default: cards/). Needs the jsonschema package.
Exit 1 when any card fails. Prints one line per failing card."""
import json, os, re, subprocess, sys
import jsonschema

ROOT = os.path.join(os.path.dirname(os.path.realpath(__file__)), "..")
SCHEMA = json.load(open(os.path.join(ROOT, "cards", "card.schema.json"), encoding="utf-8"))
V = jsonschema.Draft202012Validator(SCHEMA)


def files(paths):
    for p in paths:
        if os.path.isfile(p):
            yield p
        for d, _s, names in os.walk(p):
            yield from (os.path.join(d, n) for n in sorted(names) if n.endswith(".json") and not n.startswith("_")
                        and n != "card.schema.json")


def fill(cmd, phs):
    ex = {p["name"]: p.get("example") or "x" for p in phs or []}
    return re.sub(r"<([A-Z][A-Z0-9_]{1,31})>", lambda m: ex.get(m.group(1), "x"), cmd)


def bash_n(text):
    r = subprocess.run(["bash", "-n"], input=text, capture_output=True, text=True)
    return r.stderr.strip().splitlines()[-1] if r.returncode else None


def check(c):
    errs = [f"schema: {e.json_path}: {e.message[:160]}" for e in V.iter_errors(c)][:3]
    for i, s in enumerate(c.get("steps", []), 1):
        phs = s.get("placeholders")
        cmds = [s.get("cmd")] + [a.get("cmd") for a in s.get("alternatives", []) or []] \
            + list((s.get("variants") or {}).values())
        for cmd in filter(None, cmds):
            if re.fullmatch(r"(<[^<>]{1,20}>\s*)+", cmd.strip()):   # a keystroke such as <Space> or <Ctrl ]>, not shell
                continue
            e = bash_n(fill(cmd, phs))
            if e:
                errs.append(f"step {i} bash -n: {e}  <- {cmd[:80]}")
    sc = c.get("script")
    if sc:
        if not sc["content"].startswith("#!"):
            errs.append("script: no shebang")
        e = bash_n(sc["content"])
        if e:
            errs.append(f"script bash -n: {e}")
    return errs


def main():
    paths = sys.argv[1:] or [os.path.join(ROOT, "cards")]
    seen, bad, n = {}, 0, 0
    for f in files(paths):
        n += 1
        try:
            c = json.load(open(f, encoding="utf-8"))
        except ValueError as e:
            print(f"FAIL {f}: not JSON: {e}"); bad += 1; continue
        errs = check(c)
        cid = c.get("id")
        if cid in seen:
            errs.append(f"duplicate id, also in {seen[cid]}")
        seen[cid] = f
        if errs:
            bad += 1
            print(f"FAIL {f}:\n  " + "\n  ".join(errs))
    print(f"{n} cards checked, {bad} failed")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()

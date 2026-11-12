#!/usr/bin/env python3
"""webcrawler - stealth web fetch via CloakBrowser (stealth Chromium) or Camoufox (stealth Firefox).

Usage:
  python3 webcrawler.py fetch <url> [--engine cloak|fox] [--out FILE] [--html] [--title]
      [--screenshot FILE] [--pdf FILE] [--proxy URL] [--geoip] [--humanize]
      [--seed N] [--profile DIR] [--headed] [--viewport WxH] [--timeout MS] [--wait MS]
      [--captcha] [--solver-key KEY] [--llm-solver] [--llm-url URL] [--llm-model ID]
  python3 webcrawler.py search <query> [--num 10] [--engine cloak|fox] [--out FILE]
      [--headless] [--proxy URL] [--geoip] [--seed N] [--viewport WxH] [--timeout MS]

CAPTCHA solving: --captcha detects widgets and clicks checkbox challenges.
Image challenges are solved either by 2Captcha (--solver-key) or by the local
vision LLM (--llm-solver, OpenAI-compatible API; defaults to the spark vLLM
box via $WEBROLLER_LLM_URL / $WEBROLLER_LLM_MODEL).
"""

import argparse
import copy
import html as htmlmod
import json
import re
import sys
import time
from contextlib import contextmanager
from urllib.parse import unquote


def parse_viewport(s):
    w, _, h = s.partition("x")
    return int(w), int(h)


def cloak_opts(args):
    opts = {}
    if args.proxy:
        opts["proxy"] = args.proxy
    if args.geoip:
        opts["geoip"] = True
    if args.humanize:
        opts["humanize"] = True
    if args.seed:
        opts["args"] = [f"--fingerprint={args.seed}"]
    return opts


CAPTCHA_SIGS = [
    {
        "name": "turnstile",
        "widget": 'iframe[src*="challenges.cloudflare.com"], div.cf-turnstile',
        "response": 'input[name="cf-turnstile-response"], input[name="cf-turnstile"]',
    },
    {
        "name": "recaptcha",
        "widget": 'iframe[src*="recaptcha"], div.g-recaptcha',
        "response": 'input[name="g-recaptcha-response"], input[name="recaptcha-response"]',
    },
    {
        "name": "hcaptcha",
        "widget": 'iframe[src*="hcaptcha.com"], div.h-captcha',
        "response": 'input[name="h-captcha-response"]',
    },
    {
        "name": "arkose",
        "widget": 'iframe[src*="arkoselabs.com"], iframe[src*="funcaptcha"]',
        "response": 'input[name="px-captcha"], input[name="arkose_token"]',
    },
]


def log(msg):
    print(f"[captcha] {msg}", file=sys.stderr)


def captcha_response_value(page, selector):
    try:
        return page.evaluate(
            """(sel) => {
                for (const el of document.querySelectorAll(sel))
                    if (el && el.value) return el.value;
                return '';
            }""",
            selector,
        )
    except Exception:
        return ""


def find_grid(page):
    """Return (frame, img_locator, bounding_box) of the captcha grid image."""
    for fr in page.frames:
        if "google" not in fr.url and "hcaptcha" not in fr.url:
            continue
        try:
            for img in fr.locator("img").all():
                bb = img.bounding_box()
                if bb and bb["width"] > 200 and abs(bb["width"] - bb["height"]) < 25:
                    return fr, img, bb
        except Exception:
            pass
    return None, None, None


def bframe_data_s(page):
    for fr in page.frames:
        m = re.search(r"data-s=([^&]+)", fr.url)
        if m:
            return m.group(1)
    return ""


def solve_with_2captcha(args, url, data_s=""):
    import httpx

    task = {"type": "RecaptchaV2TaskProxyless", "websiteURL": url}
    if data_s:
        task["dataS"] = data_s
    r = httpx.post(
        "https://2captcha.com/in.php",
        data={
            "clientKey": args.solver_key,
            "action": "createTask",
            "json": json.dumps(task),
        },
        timeout=30,
    )
    j = r.json()
    if j.get("status") != 1:
        log(f"2captcha createTask failed: {j}")
        return None
    task_id = j["taskId"]
    log(f"2captcha task {task_id} created, waiting for solution...")
    deadline = time.time() + 180
    while time.time() < deadline:
        time.sleep(5)
        r = httpx.post(
            "https://2captcha.com/res.php",
            data={
                "action": "getTaskResult",
                "taskId": task_id,
                "clientKey": args.solver_key,
            },
            timeout=30,
        )
        j = r.json()
        if j.get("status") == "ready":
            return j.get("solution", {})
        if j.get("status") not in (None, "inqueue"):
            log(f"2captcha task status: {j.get('status')} {j.get('code', '')}")
            return None
    log("2captcha timed out")
    return None


def llm_chat(content, args):
    """Call the local LLM (OpenAI-compatible /chat/completions). Returns content.

    The default model is a reasoning model (chain-of-thought in a separate
    `reasoning` field). We deliberately do NOT fall back to `reasoning`: it
    contains the model's deliberation (e.g. enumerating every tile), which
    corrupts strict-format answers like captcha tile selections. Returns ""
    when content is empty; callers decide how to handle that.
    """
    import os

    import httpx

    url = (
        getattr(args, "llm_url", None)
        or os.environ.get("WEBROLLER_LLM_URL")
        or "http://127.0.0.1:9931/v1"
    ).rstrip("/")
    model = (
        getattr(args, "llm_model", None)
        or os.environ.get("WEBROLLER_LLM_MODEL")
        or "qwen3.8-27b-nvfp4"
    )
    r = httpx.post(
        url + "/chat/completions",
        json={
            "model": model,
            "messages": [{"role": "user", "content": content}],
            "max_tokens": 2048,
        },
        timeout=300,
    )
    r.raise_for_status()
    msg = r.json()["choices"][0]["message"]
    return (msg.get("content") or "").strip()


def grid_instruction(fr, retries=4, delay=1.5):
    """Extract the challenge instruction from the captcha frame.

    The category noun (e.g. "crosswalks") is injected asynchronously, so
    retry until the text looks complete (>30 chars, not dangling on "with").
    """
    best = ""
    for _ in range(retries):
        cands = []
        try:
            for sel in ("#instructions", "div#recaptcha-accessible-status"):
                loc = fr.locator(sel)
                if loc.count():
                    t = loc.first.inner_text(timeout=1500).strip()
                    if t:
                        cands.append(t)
        except Exception:
            pass
        try:
            body = fr.locator("body").inner_text(timeout=1500)
            for line in body.splitlines():
                line = line.strip()
                if line.lower().startswith(("select all", "select the")):
                    cands.append(line)
        except Exception:
            pass
        for t in cands:
            if len(t) > len(best):
                best = t
        if len(best) > 30 and not best.rstrip().endswith("with"):
            return best
        time.sleep(delay)
    return best


def llm_pick_tiles(instruction, img_b64, args):
    """Ask the LLM which of the 9 grid tiles satisfy the instruction."""
    prompt = (
        "This is a 3x3 CAPTCHA image grid. The challenge instruction is:\n"
        f'"{instruction or "(could not read the instruction - infer the task from the grid)"}"\n'
        "The 9 tiles are numbered 1-9, left to right, top to bottom:\n"
        "1 2 3\n4 5 6\n7 8 9\n"
        "Select ALL tiles that satisfy the instruction. Reply with ONLY the "
        "tile numbers in ascending order separated by spaces (e.g. '2 5 8'). "
        "If no tile applies, reply NONE. Do not explain."
    )
    content = [
        {"type": "text", "text": prompt},
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img_b64}"}},
    ]
    for _ in range(2):
        out = llm_chat(content, args)
        log(f"LLM answer: {out[:200]!r}")
        if not out:
            continue
        nums = []
        for ch in out:
            if ch in "123456789" and int(ch) not in nums:
                nums.append(int(ch))
        if len(nums) == 9:
            log("LLM selected all 9 tiles - treating as unparseable answer")
            return []
        return sorted(nums)
    log("LLM returned empty content twice")
    return []


def solve_with_llm(page, fr, grid_img, grid_bb, sig, args):
    """Solve a recaptcha image grid via the local vision LLM (1 retry)."""
    import base64
    import os
    import tempfile

    name, resp = sig["name"], sig["response"]
    if name != "recaptcha":
        log(f"{name}: LLM solver currently supports recaptcha image grids only")
        return "image-challenge"
    if not grid_img or not grid_bb:
        return "timeout"
    for attempt in (1, 2):
        instruction = grid_instruction(fr)
        log(f"attempt {attempt}: instruction: {instruction[:120]!r}")
        try:
            tmp = os.path.join(
                tempfile.mkdtemp(prefix="webcrawler-captcha-"), "grid.png"
            )
            grid_img.screenshot(path=tmp)
            with open(tmp, "rb") as fh:
                img_b64 = base64.b64encode(fh.read()).decode()
        except Exception as e:
            log(f"grid screenshot failed: {e}")
            return "solver-failed"
        try:
            indices = llm_pick_tiles(instruction, img_b64, args)
        except Exception as e:
            log(f"LLM call failed: {e}")
            return "solver-failed"
        if not indices:
            log("LLM selected no tiles (NONE or unparseable answer)")
            return "solver-failed"
        click_squares(page, grid_bb, indices)
        time.sleep(1)
        click_verify(page)
        v = wait_token(page, resp, 30)
        if v:
            log(f"{name}: solved via local LLM (token {len(v)} chars)")
            return "solved"
        log(
            f"{name}: LLM selection rejected (attempt {attempt}); grid likely refreshed"
        )
        # the refreshed grid takes a few seconds to render - poll for it
        fr2, img2, bb2 = None, None, None
        for _ in range(10):
            time.sleep(1)
            fr2, img2, bb2 = find_grid(page)
            if bb2 and img2:
                break
        if not (bb2 and img2):
            log(f"{name}: grid gone after rejection")
            return "timeout"
        fr, grid_img, grid_bb = fr2, img2, bb2
    return "timeout"


def click_squares(page, bb, indices):
    s = bb["width"] / 3
    for i in indices:
        page.mouse.click(bb["x"] + (i % 3) * s + s / 2, bb["y"] + (i // 3) * s + s / 2)
        time.sleep(0.4)
    log(f"clicked squares {indices}")


def click_verify(page):
    for fr in page.frames:
        if "google" not in fr.url:
            continue
        try:
            for btn in fr.locator("button").all():
                if btn.inner_text().upper().startswith("VERIF"):
                    btn.click()
                    log("clicked VERIFY")
                    return True
        except Exception:
            pass
    return False


def wait_token(page, resp, seconds):
    deadline = time.time() + seconds
    while time.time() < deadline:
        v = captcha_response_value(page, resp)
        if v and len(v) > 10:
            return v
        time.sleep(1)
    return ""


def handle_captcha(page, sig, args):
    name, resp = sig["name"], sig["response"]
    box = None
    start = time.time()
    deadline = start + 15
    while time.time() < deadline:
        v = captcha_response_value(page, resp)
        if v:
            log(f"{name}: auto-resolved (token {len(v)} chars)")
            return "solved"
        try:
            loc = page.locator(sig["widget"])
            for i in range(min(loc.count(), 5)):
                el = loc.nth(i)
                if not el.is_visible():
                    continue
                b = el.bounding_box()
                if b and b["width"] > 50:
                    box = b
                    break
        except Exception:
            pass
        if box:
            break
        time.sleep(0.5)
    if not box:
        log(
            f"{name}: no clickable challenge rendered (invisible/score-based or needs human)"
        )
        return "no-hook"
    init_wait = max(0.0, 8 - (time.time() - start))
    if init_wait:
        time.sleep(init_wait)
    grid = None
    for attempt in (1, 2):
        page.mouse.click(box["x"] + 30, box["y"] + 33)
        log(
            f"{name}: clicked checkbox at ({box['x']:.0f},{box['y']:.0f}) (attempt {attempt})"
        )
        v = wait_token(page, resp, 10)
        if v:
            log(f"{name}: solved (token {len(v)} chars)")
            return "solved"
        fr, grid_img, grid = find_grid(page)
        if grid:
            break
    if not grid:
        log(f"{name}: did not resolve (may need human or external solver)")
        return "timeout"
    log(
        f"{name}: image challenge detected (grid {grid['width']:.0f}x{grid['height']:.0f})"
    )
    if not args.solver_key:
        if getattr(args, "llm_solver", False):
            return solve_with_llm(page, fr, grid_img, grid, sig, args)
        log(
            f"{name}: needs --solver-key (2Captcha), --llm-solver (local LLM), or human"
        )
        return "image-challenge"
    if name != "recaptcha":
        log(f"{name}: image-challenge solver currently supports recaptcha only")
        return "image-challenge"
    data_s = bframe_data_s(page)
    if data_s:
        log(f"data-s: {data_s[:40]}... (local square-click mode)")
    else:
        log("no data-s available; solver runs the full flow and returns a token")
    solution = solve_with_2captcha(args, args.url, data_s)
    if not solution:
        return "solver-failed"
    token = solution.get("gRecaptchaToken") or solution.get("token") or ""
    if token:
        page.evaluate(
            """(t) => {
                const el = document.querySelector('input[name="g-recaptcha-response"]');
                if (el) el.value = t;
            }""",
            token,
        )
        log(f"2captcha token ({len(token)} chars) injected into form")
        try:
            form = page.locator("form")
            if form.count():
                form.first.request_submit()
                page.wait_for_load_state("domcontentloaded")
                log("form submitted")
        except Exception as e:
            log(f"form submit skipped: {e}")
        return "solved-external"
    indices = solution.get("selectedIndices", [])
    if not indices:
        log(f"2captcha returned no usable solution: {solution}")
        return "solver-failed"
    click_squares(page, grid, indices)
    time.sleep(1)
    click_verify(page)
    v = wait_token(page, resp, 30)
    if v:
        log(f"{name}: solved (token {len(v)} chars)")
        return "solved"
    log(
        f"{name}: verification did not produce a token (wrong selection or re-challenge)"
    )
    return "timeout"


def captcha_auto(page, args):
    # reCAPTCHA Enterprise anchors render the widget asynchronously
    # (execute-ms can be 30s+ after load), so poll for widgets before giving up.
    present = []
    deadline = time.time() + 45
    while True:
        present = []
        for sig in CAPTCHA_SIGS:
            try:
                if page.locator(sig["widget"]).count():
                    present.append(sig)
            except Exception:
                pass
        if present or time.time() >= deadline:
            break
        time.sleep(2)
    found = []
    for sig in present:
        try:
            n = page.locator(sig["widget"]).count()
        except Exception:
            n = 0
        if not n:
            continue
        sitekey = ""
        try:
            sitekey = page.evaluate(
                """(sel) => {
                    const el = document.querySelector(sel);
                    return el ? (el.getAttribute('data-sitekey')
                        || el.getAttribute('data-key') || '') : '';
                }""",
                sig["widget"],
            )
        except Exception:
            pass
        found.append(sig)
        log(f"detected {sig['name']} x{n}" + (f" sitekey={sitekey}" if sitekey else ""))
        handle_captcha(page, sig, args)
    if not found:
        log("no captcha widgets detected")


def work(page, args):
    page.goto(args.url, timeout=args.timeout, wait_until="domcontentloaded")
    if args.wait:
        time.sleep(args.wait / 1000)
    if args.captcha:
        captcha_auto(page, args)
    if args.title:
        result = page.title()
    elif args.html:
        result = page.content()
    else:
        result = page.inner_text("body")
        if getattr(args, "meta", False):
            result = f"Title: {page.title()}\nURL: {page.url}\n\n" + result
        if getattr(args, "images", False):
            imgs = page.evaluate(
                """() => { const seen = new Set(), out = [];
                  for (const im of document.images) {
                    const src = im.currentSrc || im.src || im.dataset.src || '';
                    const w = im.naturalWidth || im.width, h = im.naturalHeight || im.height;
                    if (!/^https?:/.test(src) || seen.has(src) || (w && w < 100) || (h && h < 100)) continue;
                    seen.add(src); out.push([src, (im.alt || '').trim().slice(0, 120), w, h]);
                    if (out.length >= 30) break; }
                  return out; }"""
            )
            if imgs:
                result += "\n\n[images]\n" + "\n".join(
                    f"{u} | {alt or '-'} | {w}x{h}" for u, alt, w, h in imgs
                )
    if args.screenshot:
        page.screenshot(path=args.screenshot)
    if args.pdf:
        page.pdf(path=args.pdf)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(result)
        print(f"wrote {args.out} ({len(result)} chars)")
    else:
        print(result)


def run_cloak(args):
    from cloakbrowser import launch, launch_persistent_context

    opts = cloak_opts(args)
    w, h = parse_viewport(args.viewport)
    headless = not args.headed
    if args.profile:
        with launch_persistent_context(
            args.profile,
            headless=headless,
            viewport={"width": w, "height": h},
            **opts,
        ) as ctx:
            work(ctx.new_page(), args)
    else:
        with launch(headless=headless, **opts) as browser:
            ctx = browser.new_context(viewport={"width": w, "height": h})
            work(ctx.new_page(), args)


def run_camoufox(args):
    from browserforge.fingerprints import Screen
    from camoufox.sync_api import Camoufox

    opts = cloak_opts(args)
    opts.pop("args", None)
    w, h = parse_viewport(args.viewport)
    opts["screen"] = Screen(max_width=w, max_height=h)
    if args.captcha:
        opts["disable_coop"] = True
        opts["i_know_what_im_doing"] = True
    if args.profile:
        opts["persistent_context"] = True
        opts["user_data_dir"] = args.profile
    headless = "virtual" if args.headed else True
    with Camoufox(headless=headless, **opts) as ctx:
        work(ctx.new_page(), args)


def run_login(args):
    import os

    email = os.environ.get("LI_EMAIL", "")
    password = os.environ.get("LI_PASS", "")
    if not email or not password:
        print("error: set LI_EMAIL and LI_PASS env vars first", file=sys.stderr)
        sys.exit(1)
    profile = os.path.expanduser(args.profile)
    os.makedirs(profile, exist_ok=True)
    if args.engine == "cloak":
        from cloakbrowser import launch_persistent_context

        with launch_persistent_context(profile, headless=True) as ctx:
            login_flow(ctx.new_page(), args)
    else:
        from camoufox.sync_api import Camoufox

        with Camoufox(
            headless=True,
            humanize=True,
            persistent_context=True,
            user_data_dir=profile,
        ) as ctx:
            login_flow(ctx.new_page(), args)


def login_flow(page, args):
    import os

    email = os.environ["LI_EMAIL"]
    password = os.environ["LI_PASS"]
    try:
        _login_step(page, args, email, password)
    except Exception as e:
        log(f"login: error: {e}")
        if args.screenshot:
            try:
                page.screenshot(path=args.screenshot)
            except Exception:
                pass


def _login_step(page, args, email, password):
    import os

    page.goto(args.url, timeout=60000, wait_until="domcontentloaded")
    time.sleep(4)
    email_sel = None
    for sel in ("input#username", "input[name='session_key']", "input[type='email']"):
        if page.locator(sel).count():
            email_sel = sel
            break
    if not email_sel:
        log("login: email field not found")
    else:
        page.locator(email_sel).first.type(email, delay=50)
        time.sleep(1)
        page.locator("button[type=submit]").first.click()
        time.sleep(6)
    pw_sel = None
    for sel in ("input#password", "input[type='password']"):
        if page.locator(sel).count():
            pw_sel = sel
            break
    if pw_sel:
        page.locator(pw_sel).first.type(password, delay=50)
        time.sleep(1)
        page.locator("button[type=submit]").first.click()
        time.sleep(10)
    code = os.environ.get("LI_2FA_CODE", "")
    if code:
        for sel in (
            "input#input_0",
            "input[name='input_0']",
            "input[inputmode='numeric']",
            "input[type='tel']",
        ):
            if page.locator(sel).count():
                page.locator(sel).first.type(code, delay=80)
                page.locator("button[type=submit]").first.click()
                time.sleep(10)
                break
    url = page.url
    body = page.inner_text("body")[:600].lower()
    if any(u in url for u in ("/feed", "/inbox", "/mynetwork", "/pulse")):
        log(f"login: SUCCESS (url={url})")
    elif any(
        k in body for k in ("verification code", "two-step", "2-step", "one-time code")
    ):
        log("login: 2FA required - rerun with LI_2FA_CODE=<code>")
    elif any(k in body for k in ("incorrect", "invalid", "wrong password")):
        log("login: credentials rejected")
    else:
        log(f"login: ended at {url}")
        log(f"login: page: {page.inner_text('body')[:200]!r}")
    if args.screenshot:
        page.screenshot(path=args.screenshot)


class SearchBlocked(Exception):
    pass


BLOCKED_SUFFIXES = (
    "google.com",
    "google.co.uk",
    "google.de",
    "google.fr",
    "google.es",
    "google.it",
    "google.nl",
    "gstatic.com",
    "googleusercontent.com",
    "youtube.com",
    "ytimg.com",
    "duckduckgo.com",
    "bing.com",
    "microsoft.com",
    "schema.org",
    "w3.org",
    "doubleclick.net",
)


def _clean(s):
    s = re.sub(r"<[^>]+>", " ", s)
    s = htmlmod.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def _is_external(href):
    if not href.startswith(("http://", "https://")):
        return False
    host = href.split("/")[2].lower()
    if re.search(r"(^|\.)google\.[a-z.]+$", host):  # any ccTLD (google.com.tr, ...)
        return False
    return not any(host == s or host.endswith("." + s) for s in BLOCKED_SUFFIXES)


def parse_google_results(page_html):
    # Organic results are <a href="/goto?url=<opaque token>"><h3>title</h3></a>.
    # The destination is not always in href, so fall back to the <cite>
    # breadcrumb ("en.wikipedia.org › wiki › X") rebuilt as https://...
    results, seen = [], set()
    for hm in re.finditer(r"<h3\b[^>]*>(.*?)</h3>", page_html, re.DOTALL):
        title = _clean(hm.group(1))
        if not title:
            continue
        # 2000: the /goto anchor tag alone is ~700 chars (token + ping attribute)
        before = page_html[max(0, hm.start() - 2000) : hm.start()]
        ams = list(re.finditer(r'<a\b[^>]*href="([^"]+)"', before))
        href = htmlmod.unescape(ams[-1].group(1)) if ams else ""
        goto = href if href.startswith("/goto?") else ""
        url = ""
        if href.startswith("https://") or href.startswith("http://"):
            url = href
        else:
            qm = re.search(r"[?&](?:url|q)=(https?://[^&]+)", href)
            if qm:
                url = unquote(qm.group(1))
        nxt = page_html.find("<h3", hm.end())
        after = page_html[hm.end() : nxt if nxt != -1 else hm.end() + 4000]
        cm = re.search(r"<cite\b[^>]*>(.*?)</cite>", after, re.DOTALL)
        if not url and cm:
            crumbs = [c.strip() for c in re.split(r"\s*›\s*", _clean(cm.group(1))) if c.strip()]
            host = crumbs[0] if crumbs else ""
            if host.startswith(("http://", "https://")):
                url = "/".join(crumbs)
            elif "." in host and " " not in host and "..." not in host:
                url = "https://" + "/".join(crumbs)
        if not url and goto:
            url = "goto:" + goto  # resolved to the real URL by resolve_goto_links()
        if not url or (not goto and not _is_external(url)) or url in seen:
            continue
        sm = re.search(r'VwiC3b[^>]*>(.*?)</div>', after, re.DOTALL)
        snippet = _clean(sm.group(1)) if sm else ""
        snippet = re.sub(r"\s*(Read more|More)\s*$", "", snippet)
        snippet = re.sub(r"\s+([,.;:)])", r"\1", snippet).replace("( ", "(")[:300]
        seen.add(url)
        r = {"title": title, "url": url, "snippet": snippet}
        if goto:
            r["_goto"] = goto
        results.append(r)
    return results


def resolve_goto_links(page, results):
    # Google links results through opaque /goto?url=<token> redirects, and the
    # <cite> breadcrumb is truncated ("example.com/some-pa..."), so follow each
    # redirect once (same browser session, ~0.1-0.5 s) and read its Location.
    # A result whose redirect does not resolve is dropped rather than guessed.
    out = []
    for r in results:
        goto = r.pop("_goto", "")
        if goto:
            loc = ""
            try:
                resp = page.request.get(
                    "https://www.google.com" + goto, max_redirects=0, timeout=3000
                )
                loc = resp.headers.get("location", "")
            except Exception:
                pass
            if not loc.startswith(("http://", "https://")):
                continue
            r["url"] = loc
        u = r["url"]
        if " " in u or "..." in u or "\u2026" in u or not _is_external(u):
            continue
        out.append(r)
    return out


def google_images(page, num):
    # Google Images tab (udm=2) of the SERP already open in this session. Its
    # inline data carries the original image URLs as ["https://...jpg",h,w].
    url = re.sub(r"&udm=\d+", "", page.url) + "&udm=2"
    page.goto(url, timeout=45000, wait_until="domcontentloaded")
    try:
        page.wait_for_selector("img", timeout=3000)
    except Exception:
        pass
    html = page.content()
    out, seen = [], set()
    for u, h, w in re.findall(
        r'\["(https?://[^"\\]+?\.(?:jpe?g|png|webp|gif)(?:\?[^"\\]*)?)",(\d+),(\d+)\]', html
    ):
        host = u.split("/")[2]
        if "gstatic.com" in host or "google." in host or u in seen:
            continue
        seen.add(u)
        out.append({"url": u, "width": int(w), "height": int(h)})
        if len(out) >= num:
            break
    return out


def search_google(page, args):
    # hl/gl pin the UI language and region; without them Google localises by
    # exit IP (e.g. Turkish via the VPN) and the parser/consent handling drift.
    lang = getattr(args, "lang", "en") or "en"
    country = getattr(args, "country", "us") or "us"
    # A follow-up query in the same session reuses the search box at the top of
    # the results page already open (faster than reloading the home page).
    prev_url = page.url if "/search?" in page.url else ""
    if not prev_url:
        page.goto(
            f"https://www.google.com/?hl={lang}&gl={country}",
            timeout=args.timeout,
            wait_until="domcontentloaded",
        )
    # First *visible* search box wins; the consent/layout variants are hidden.
    box = None
    for s in ("textarea[name=q]", "textarea.gLFyf", "input[name=q]"):
        loc = page.locator(s).first
        try:
            loc.wait_for(state="visible", timeout=3000)
            box = loc
            break
        except Exception:
            continue
    if box is None:
        raise SearchBlocked(
            "no search box on google.com (consent page or layout change)"
        )
    # Keystrokes stop reaching a minimized Chromium window after ~20 characters,
    # so the query is written into the box directly (fill fires the input events).
    box.click()
    box.fill(args.query)
    deadline = time.time() + 3
    while box.input_value() != args.query and time.time() < deadline:
        time.sleep(0.2)
    if box.input_value() != args.query:
        raise SearchBlocked("query did not land in the google search box")
    box.press("Enter")
    if prev_url:
        try:
            page.wait_for_url(lambda u: u != prev_url and "/search?" in u, timeout=3000)
        except Exception:
            pass
    try:
        page.wait_for_url(re.compile(r"/search\?"), timeout=3000)
    except Exception:
        box.press("Enter")  # one retry if the first Enter was swallowed
        page.wait_for_url(re.compile(r"/search\?"), timeout=3000)
    try:
        page.wait_for_selector("h3", timeout=3000)
    except Exception:
        pass  # no results container: classified below
    url = page.url
    page_html = ""
    for _ in range(5):  # content() fails while the SERP is still navigating
        try:
            page_html = page.content()
            break
        except Exception:
            page.wait_for_load_state("domcontentloaded")
            time.sleep(0.5)
    if (
        "/sorry" in url
        or "solveSimpleChallenge" in page_html
        or "unusual traffic" in page_html.lower()
    ):
        raise SearchBlocked("google captcha / sorry page")
    results = resolve_goto_links(page, parse_google_results(page_html))
    if not results:
        raise SearchBlocked(f"no organic results parsed from google SERP ({url[:80]})")
    # More than one page (~10 organic results each): open the next SERP pages
    # in the same session via &start=N, up to 5 pages; stop on a captcha or an
    # empty page instead of failing the whole search.
    seen = {r["url"] for r in results}
    start = 10
    while len(results) < args.num and start < 50:
        nxt = re.sub(r"([?&])start=\d+", "", url) + f"&start={start}"
        try:
            time.sleep(0.8)
            page.goto(nxt, timeout=args.timeout, wait_until="domcontentloaded")
            page.wait_for_selector("h3", timeout=3000)
            html2 = page.content()
        except Exception:
            break
        if "/sorry" in page.url or "unusual traffic" in html2.lower():
            break
        more = [r for r in resolve_goto_links(page, parse_google_results(html2)) if r["url"] not in seen]
        if not more:
            break
        seen.update(r["url"] for r in more)
        results += more
        start += 10
    return results


def _minimize_window(ctx, page):
    # Minimize the headed window through CDP (Browser.setWindowBounds). Pages in
    # a minimized window still load and run JS: search results were verified
    # this way (see SKILL.md).
    cdp = ctx.new_cdp_session(page)
    win = cdp.send("Browser.getWindowForTarget")
    cdp.send(
        "Browser.setWindowBounds",
        {"windowId": win["windowId"], "bounds": {"windowState": "minimized"}},
    )


def _locale(args):
    return f"{args.lang}-{(getattr(args, 'country', None) or 'us').upper()}"


@contextmanager
def stealth_page(args, headed=False):
    # headed=True opens a real (non-headless) window, then minimizes it via
    # CDP. Google serves /sorry (CAPTCHA) to headless Chromium on every search,
    # while a headed browser gets results, so search always uses this path.
    w, h = parse_viewport(args.viewport)
    if args.engine == "fox":
        from browserforge.fingerprints import Screen
        from camoufox.sync_api import Camoufox

        opts = cloak_opts(args)
        opts.pop("args", None)
        opts["screen"] = Screen(max_width=w, max_height=h)
        if getattr(args, "lang", None):
            opts["locale"] = _locale(args)
        with Camoufox(headless="virtual" if headed else True, **opts) as ctx:
            yield ctx.new_page()
    else:
        from cloakbrowser import launch

        opts = cloak_opts(args)
        with launch(headless=not headed, **opts) as browser:
            cargs = {"viewport": {"width": w, "height": h}}
            if getattr(args, "lang", None):
                loc = _locale(args)
                cargs["locale"] = loc
                cargs["extra_http_headers"] = {
                    "Accept-Language": f"{loc},{args.lang};q=0.9"
                }
            ctx = browser.new_context(**cargs)
            page = ctx.new_page()
            if headed:
                _minimize_window(ctx, page)
            yield page


def run_search(args):
    args.humanize = True
    queries = args.query
    merged, by_url, errors, images = [], {}, {}, []
    with stealth_page(args, headed=not args.headless) as page:
        for i, q in enumerate(queries):
            qa = copy.copy(args)
            qa.query = q
            try:
                if i:
                    time.sleep(1.0)
                found = search_google(page, qa)[: args.num]
                if getattr(args, "images", False):
                    try:
                        seen_img = {i["url"] for i in images}
                        images += [{**i, "query": q} for i in google_images(page, args.num)
                                   if i["url"] not in seen_img]
                    except Exception as e:
                        errors[q + " (images)"] = str(e)
            except SearchBlocked as e:
                if len(queries) == 1:
                    raise
                errors[q] = str(e)
                continue
            for r in found:
                if r["url"] in by_url:
                    by_url[r["url"]]["queries"].append(q)
                else:
                    r = {**r, "queries": [q]}
                    by_url[r["url"]] = r
                    merged.append(r)
        if not merged and errors:
            raise SearchBlocked("; ".join(f"{q}: {e}" for q, e in errors.items()))
        if len(queries) == 1:
            for r in merged:
                r.pop("queries", None)
        out = {
            "engine": "google",
            "query": queries[0] if len(queries) == 1 else queries,
            "count": len(merged),
            "results": merged,
        }
        if getattr(args, "images", False):
            out["images"] = images
        if errors:
            out["errors"] = errors
        results = merged
        payload = json.dumps(out, ensure_ascii=False, indent=2)
        if args.out:
            with open(args.out, "w", encoding="utf-8") as fh:
                fh.write(payload)
            print(f"wrote {args.out} ({len(results)} results via google)")
        else:
            print(payload)


def main():
    p = argparse.ArgumentParser(
        description="Stealth web fetch (CloakBrowser / Camoufox)"
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch", help="fetch a page with a stealth browser")
    f.add_argument("url")
    f.add_argument("--engine", choices=["cloak", "fox"], default="cloak")
    f.add_argument("--out", help="write body text to file (default: stdout)")
    f.add_argument("--html", action="store_true", help="dump full HTML instead of text")
    f.add_argument("--title", action="store_true", help="print title only")
    f.add_argument("--meta", action="store_true", help="prefix the text with 'Title: ...' and the final 'URL: ...'")
    f.add_argument("--images", action="store_true", help="append the page's images (url | alt | WxH), up to 30")
    f.add_argument("--screenshot", metavar="FILE", help="save full-page screenshot")
    f.add_argument("--pdf", metavar="FILE", help="save page as PDF")
    f.add_argument(
        "--proxy", metavar="URL", help="proxy URL (http/socks5, creds inline)"
    )
    f.add_argument(
        "--geoip", action="store_true", help="match timezone/locale to proxy IP"
    )
    f.add_argument(
        "--humanize", action="store_true", help="human-like mouse/keyboard/scroll"
    )
    f.add_argument("--seed", type=int, help="fixed fingerprint seed (cloak only)")
    f.add_argument(
        "--profile", metavar="DIR", help="persistent profile dir (cookies survive)"
    )
    f.add_argument(
        "--headed", action="store_true", help="headed mode (fox: virtual display)"
    )
    f.add_argument(
        "--viewport",
        default="1280x720",
        help="max window/viewport size WxH (default 1280x720; e.g. 1920x1080)",
    )
    f.add_argument(
        "--captcha",
        action="store_true",
        help="detect CAPTCHA widgets (Turnstile/reCAPTCHA/hCaptcha/Arkose) and "
        "auto-click checkbox challenges; fox engine only",
    )
    f.add_argument(
        "--solver-key",
        help="2Captcha client key for solving image challenges (used with --captcha)",
    )
    f.add_argument(
        "--llm-solver",
        action="store_true",
        help="solve image challenges with the local vision LLM (OpenAI-compatible "
        "API) instead of 2Captcha (used with --captcha)",
    )
    f.add_argument(
        "--llm-url",
        help="LLM base URL (default $WEBROLLER_LLM_URL or http://127.0.0.1:9931/v1)",
    )
    f.add_argument(
        "--llm-model",
        help="LLM model id (default $WEBROLLER_LLM_MODEL or qwen3.8-27b-nvfp4)",
    )
    f.add_argument(
        "--timeout",
        type=int,
        default=30000,
        help="navigation timeout ms (default 30000)",
    )
    f.add_argument(
        "--wait", type=int, default=0, help="extra native sleep after load, ms"
    )
    s = sub.add_parser(
        "search",
        help="Google web search via a headed, minimized browser -> JSON "
        "{engine, query, results:[{title,url,snippet}]}",
    )
    s.add_argument("--images", action="store_true",
                   help="also return original image URLs from Google Images (up to --num per query)")
    s.add_argument("query", nargs="+", help="one or more queries; several run in one browser session and are merged")
    s.add_argument(
        "--num", type=int, default=10,
        help="max results to return (default 10; >10 pages through the SERP, up to ~50)"
    )
    s.add_argument("--engine", choices=["cloak", "fox"], default="cloak")
    s.add_argument("--out", help="write JSON to file (default: stdout)")
    s.add_argument(
        "--headless",
        action="store_true",
        help="debug only: run headless (Google serves a CAPTCHA to headless)",
    )
    s.add_argument("--proxy", metavar="URL", help="proxy URL (http/socks5)")
    s.add_argument(
        "--geoip", action="store_true", help="match timezone/locale to proxy IP"
    )
    s.add_argument("--seed", type=int, help="fixed fingerprint seed (cloak only)")
    s.add_argument("--viewport", default="1280x720", help="window size WxH")
    s.add_argument("--timeout", type=int, default=45000, help="navigation timeout ms")
    s.add_argument(
        "--lang", default="en", help="Google UI/results language (hl), default en"
    )
    s.add_argument(
        "--country", default="us", help="Google region (gl), default us"
    )
    l = sub.add_parser(
        "login",
        help="log into a site (LinkedIn) with a persistent profile; "
        "reads LI_EMAIL / LI_PASS / LI_2FA_CODE from env",
    )
    l.add_argument("--engine", choices=["cloak", "fox"], default="fox")
    l.add_argument(
        "--profile",
        default="~/.webcrawler/li",
        help="persistent profile dir (default ~/.webcrawler/li)",
    )
    l.add_argument(
        "--url", default="https://www.linkedin.com/login", help="login page url"
    )
    l.add_argument("--screenshot", metavar="FILE", help="save final state screenshot")
    args = p.parse_args()
    if args.cmd == "search":
        run_search(args)
    elif args.cmd == "login":
        run_login(args)
    elif args.engine == "cloak":
        run_cloak(args)
    else:
        run_camoufox(args)


if __name__ == "__main__":
    main()

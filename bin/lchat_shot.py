"""Capture one window with KWin's ScreenShot2 D-Bus API, without focusing or raising it.

Usage: ~/.local/share/lchat/lchat-shot -E -P lchat_shot.py WINDOW_ID OUT.png

KWin only answers ScreenShot2 calls from programs whose .desktop file lists the interface in
X-KDE-DBUS-Restricted-Interfaces, matched by executable path. install.sh therefore copies the Python interpreter
(a small stub that loads the system libpython) to ~/.local/share/lchat/lchat-shot and writes
~/.local/share/applications/lchat-shot.desktop for that path only. lchat-web runs this script with that copy and
falls back to focus-capture-restore when it fails (not KDE, not authorized, Python upgraded under the copy).
Exit 0 and a PNG on success; otherwise exit 1 with the reason on stderr.
"""

import os
import sys
import threading

# QImage::Format values KWin may send -> (PIL raw mode, has alpha). Little-endian byte order.
FORMATS = {
    4: ("BGRX", False),
    5: ("BGRA", True),
    6: ("BGRA", True),
    17: ("RGBX", False),
    18: ("RGBA", True),
    19: ("RGBA", True),
}


def main(win, out):
    import dbus
    from PIL import Image

    bus = dbus.SessionBus()
    shot = dbus.Interface(
        bus.get_object("org.kde.KWin", "/org/kde/KWin/ScreenShot2"),
        "org.kde.KWin.ScreenShot2",
    )
    r, w = os.pipe()
    buf = bytearray()

    def reader():
        while True:
            b = os.read(r, 1 << 20)
            if not b:
                break
            buf.extend(b)

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    try:
        meta = shot.CaptureWindow(
            win,
            {"include-decoration": True, "include-cursor": False},
            dbus.types.UnixFd(w),
        )
    finally:
        os.close(w)
    t.join(20)
    os.close(r)
    meta = {str(k): v for k, v in meta.items()}
    if str(meta.get("type", "raw")) != "raw":
        raise RuntimeError(f"unexpected image type {meta.get('type')}")
    width, height, stride, fmt = (
        int(meta[k]) for k in ("width", "height", "stride", "format")
    )
    if fmt not in FORMATS:
        raise RuntimeError(f"unsupported QImage format {fmt}")
    if len(buf) < stride * height:
        raise RuntimeError(f"short image data: {len(buf)} < {stride * height}")
    mode, _alpha = FORMATS[fmt]
    im = Image.frombuffer(
        "RGBA" if mode in ("BGRA", "RGBA") else "RGBX",
        (width, height),
        bytes(buf),
        "raw",
        mode,
        stride,
        1,
    ).convert("RGB")
    im.save(out, "PNG")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit("usage: lchat-shot -E -P lchat_shot.py WINDOW_ID OUT.png")
    try:
        main(sys.argv[1], sys.argv[2])
    except Exception as e:
        print(f"{type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)

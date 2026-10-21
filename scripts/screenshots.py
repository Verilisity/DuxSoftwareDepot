#!/usr/bin/env python3
"""Make the README screenshots from the running plugin, in English.

    python3 scripts/screenshots.py [--dark] [--run NAME] [--app NAME] [--no-resize] [--keep-raw]

Needs a working copy (the plugin directory a symlink to this repository), a
running shell with the plugin enabled, and Pillow for the framing.

What it does, in order:

1. Builds two recordings in recordings/: `screenshots-demo`, the pending list
   for the Updates tab, made from what the action log says this machine
   updated most recently (simulate.py from-log); and `screenshots-empty`,
   with nothing pending, for the up-to-date dashboard.
2. Switches the shell to light mode (--dark for dark), and on niri floats
   the window at the README size; elsewhere the window is shot at whatever
   size it has.
3. Asks the plugin to take the shots (`dms ipc call dankSoftwareDepotDev
   shots …`, see ScreenshotDirector.qml), and waits for it to finish. The
   window switches to English for the duration and back afterwards.
4. Puts the theme and the window back the way they were, and frames each shot — gradient,
   rounded corners, shadow — into screenshots/ and screenshot.png.

The run in progress is played from a real recording — the one with the most
packages up to 50, unless --run names another; without one that shot is
skipped.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import simulate  # noqa: E402

RECORDINGS = os.path.join(REPO, "recordings")
DATA = os.path.expanduser("~/.local/share/dankSoftwareDepot")
DEMO, EMPTY = "screenshots-demo", "screenshots-empty"

# The README's window: 883 × 938 inside a 40 px margin
WINDOW_SIZE = (883, 938)
MARGIN = 40
RADIUS = 12          # the window's corners
OUTER_RADIUS = 20    # the picture's own corners, transparent outside
# The backdrop of the screenshots made by hand before this script, measured
# from screenshot.png: a 135° linear gradient from Catppuccin mauve to red,
# flat mauve up to 14.8 % of the diagonal and flat red from 86 % on (within
# 2.4 levels on average of the original)
GRADIENT = ((136, 57, 239), (210, 15, 57))
GRADIENT_STOPS = (0.148, 0.86)

# Shot name → file in the repository
TARGETS = {"updates": "screenshot.png"}


def target_for(name):
    return os.path.join(REPO, TARGETS.get(name, os.path.join("screenshots", name + ".png")))


def say(text):
    print(text, flush=True)


def ipc(target, *args):
    res = subprocess.run(["dms", "ipc", "call", target, *args],
                         capture_output=True, text=True, timeout=30)
    return (res.stdout or res.stderr).strip()


# ── Recordings ──────────────────────────────────────────────────────────────

def backend_name():
    import pkg_backend
    return {"apt": "apt", "pacman": "pacman"}.get(pkg_backend.detect(), "dnf5")


def build_recordings():
    log = os.path.join(DATA, "action-log.json")
    quiet = open(os.devnull, "w")
    old, sys.stdout = sys.stdout, quiet
    try:
        simulate.cmd_from_log(RECORDINGS, DEMO, log, 14, backend_name())
        simulate.cmd_begin(RECORDINGS, EMPTY, "[]")
    finally:
        sys.stdout = old
    count = len(simulate._read_meta(os.path.join(RECORDINGS, DEMO)).get("packages") or [])
    if count == 0:
        sys.exit("The action log has no updates to show as pending — update something first.")
    return count


def best_run_recording():
    """The recording that makes the best picture of a run: a screenful of
    packages or more, but not a hundred — the most packages up to 50, the
    newest as tie-break; only real runs, with a system stream to play."""
    candidates = []
    for name in os.listdir(RECORDINGS) if os.path.isdir(RECORDINGS) else []:
        directory = os.path.join(RECORDINGS, name)
        if name.startswith("screenshots-") or not os.path.isfile(os.path.join(directory, "system.jsonl")):
            continue
        meta = simulate._read_meta(directory)
        count = len(meta.get("packages") or [])
        candidates.append((count <= 50, count if count <= 50 else -count, meta.get("recordedAt", 0), name))
    return max(candidates)[3] if candidates else ""


def an_appimage():
    try:
        with open(os.path.join(DATA, "appimages.json")) as f:
            apps = json.load(f)
    except (OSError, ValueError):
        return ""
    for app in apps if isinstance(apps, list) else []:
        if os.path.isfile(app.get("file", "")):
            return app["file"]
    return ""


# ── The window on niri ──────────────────────────────────────────────────────

def niri_window():
    if not shutil.which("niri") or not os.environ.get("NIRI_SOCKET"):
        return None
    try:
        windows = json.loads(subprocess.run(["niri", "msg", "-j", "windows"], capture_output=True,
                                            text=True, timeout=10).stdout)
    except (ValueError, subprocess.SubprocessError, OSError):
        return None
    return next((w for w in windows if (w.get("title") or "").endswith("Software Depot")), None)


def niri(*args):
    subprocess.run(["niri", "msg", "action", *args], capture_output=True, timeout=10)


def float_at_size(window):
    """Float the window at the README size; returns how to put it back."""
    wid = str(window["id"])
    was = {"id": wid, "floating": window.get("is_floating", False),
           "size": (window.get("layout") or {}).get("window_size")}
    if not was["floating"]:
        niri("toggle-window-floating", "--id", wid)
    niri("set-window-width", "--id", wid, str(WINDOW_SIZE[0]))
    niri("set-window-height", "--id", wid, str(WINDOW_SIZE[1]))
    time.sleep(1)
    return was


def restore(was):
    if not was:
        return
    if was["size"]:
        niri("set-window-width", "--id", was["id"], str(int(was["size"][0])))
    if not was["floating"]:
        niri("toggle-window-floating", "--id", was["id"])
        niri("reset-window-height", "--id", was["id"])
    elif was["size"]:
        niri("set-window-height", "--id", was["id"], str(int(was["size"][1])))


# ── Framing ─────────────────────────────────────────────────────────────────

def backdrop(w, h):
    """The gradient, w × h. It only depends on x + y, so it is one strip of
    colours read off at a different offset for every row."""
    from PIL import Image
    (r1, g1, b1), (r2, g2, b2) = GRADIENT
    lo, hi = GRADIENT_STOPS
    strip = bytearray()
    for d in range(w + h - 1):
        f = min(1.0, max(0.0, (d / (w + h - 2) - lo) / (hi - lo)))
        strip += bytes((round(r1 + (r2 - r1) * f), round(g1 + (g2 - g1) * f), round(b1 + (b2 - b1) * f)))
    rows = b"".join(bytes(strip[3 * y:3 * (y + w)]) for y in range(h))
    return Image.frombytes("RGB", (w, h), rows).convert("RGBA")


def rounded_mask(size, radius):
    from PIL import Image, ImageDraw
    # Drawn at four times the size and scaled down, for edges that are
    # smooth rather than stepped
    big = Image.new("L", (size[0] * 4, size[1] * 4), 0)
    ImageDraw.Draw(big).rounded_rectangle((0, 0, big.width - 1, big.height - 1), radius=radius * 4, fill=255)
    return big.resize(size, Image.LANCZOS)


def frame(raw_path, out_path):
    from PIL import Image, ImageDraw, ImageFilter
    shot = Image.open(raw_path).convert("RGBA")
    # A HiDPI output grabs at its device pixel ratio; the frame scales along
    scale = shot.width / WINDOW_SIZE[0] if abs(shot.width - WINDOW_SIZE[0]) > 40 else 1.0
    margin, radius = round(MARGIN * scale), round(RADIUS * scale)
    w, h = shot.width + 2 * margin, shot.height + 2 * margin
    canvas = backdrop(w, h)

    shadow = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    offset = round(8 * scale)
    ImageDraw.Draw(shadow).rounded_rectangle(
        (margin, margin + offset, margin + shot.width, margin + shot.height + offset),
        radius=radius, fill=(40, 0, 20, 150))
    canvas = Image.alpha_composite(canvas, shadow.filter(ImageFilter.GaussianBlur(round(14 * scale))))
    canvas.paste(shot, (margin, margin), rounded_mask(shot.size, radius))
    canvas.putalpha(rounded_mask((w, h), round(OUTER_RADIUS * scale)))
    canvas.save(out_path, optimize=True)


# ── Main ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dark", action="store_true", help="shoot in dark mode instead of light")
    parser.add_argument("--run", help="recording to play for the run in progress")
    parser.add_argument("--app", default="VLC", help="app whose details are shown (default VLC)")
    parser.add_argument("--no-resize", action="store_true", help="leave the window size alone")
    parser.add_argument("--keep-raw", action="store_true", help="keep the unframed shots")
    args = parser.parse_args()

    if not shutil.which("dms"):
        sys.exit("The dms command is not on PATH.")
    try:
        import PIL  # noqa: F401
    except ImportError:
        sys.exit("Pillow is needed for the framing (python3-pillow).")

    count = build_recordings()
    run = args.run if args.run is not None else best_run_recording()
    appimage = an_appimage()
    say(f"Updates tab: {count} packages from the action log")
    say(f"Run in progress: {run or 'no recording with a system stream — skipped'}")
    say(f"AppImage offer: {appimage or 'no registered AppImage — skipped'}")

    # The theme is the whole shell's, so it is switched for the duration and
    # switched back whatever happens after this point
    mode_before = ipc("theme", "getMode")
    wanted = "dark" if args.dark else "light"
    if mode_before != wanted:
        ipc("theme", wanted)
        time.sleep(1.5)

    ipc("dankSoftwareDepot", "open")
    time.sleep(1.5)
    window = None if args.no_resize else niri_window()
    was = float_at_size(window) if window else None

    def put_back():
        restore(was)
        if mode_before in ("light", "dark") and mode_before != wanted:
            ipc("theme", mode_before)

    raw = tempfile.mkdtemp(prefix="dsd-shots-")
    opts = {"out": raw, "demo": DEMO, "empty": EMPTY, "run": run, "appimage": appimage, "app": args.app}
    answer = ipc("dankSoftwareDepotDev", "shots", json.dumps(opts))
    say(answer)
    if not answer.startswith("started"):
        put_back()
        sys.exit(1)

    status, deadline, last = "", time.time() + 600, ""
    try:
        while time.time() < deadline:
            time.sleep(1)
            status = ipc("dankSoftwareDepotDev", "shotstatus")
            line = status.split(" | ")[0]
            if line != last:
                say("  " + line)
                last = line
            if not status.startswith("running"):
                break
    finally:
        put_back()

    if not status.startswith("done"):
        sys.exit("Stopped: " + status)
    names = [f[:-4] for f in sorted(os.listdir(raw)) if f.endswith(".png")]
    for name in names:
        frame(os.path.join(raw, name + ".png"), target_for(name))
        say(f"  {os.path.relpath(target_for(name), REPO)}")
    if args.keep_raw:
        say(f"Unframed shots kept in {raw}")
    else:
        shutil.rmtree(raw, ignore_errors=True)
    say(f"{len(names)} screenshots written.")


if __name__ == "__main__":
    main()

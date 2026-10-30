#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
amon_welcome - the standard welcome screen and prompts for Amon's command-line scripts
=======================================================================================
One standalone file, Python standard library only (no pandas etc.), so it can be dropped into any script.

    from amon_welcome import welcome, step, info, warn, fail, pick_one, summary_box

    if not welcome(confirm=True):            # name, description, file, folder are read from the script itself
        raise SystemExit

    step(1, 3, "Reading data ...")
    info("1,234 rows")
    warn("2 rows had no date")
    fail("Column 'amount' is missing")
    chosen = pick_one("Several files found:", files, label=lambda p: p.name, prompt="Which file?")
    summary_box("Done in 4.2s", facts=[("Rows", "1,234")], folder="C:/out", files=["a.xlsx"])

The text under the big name is dynamic: it is read from the running script. Start the script with a docstring whose first line
is a short title and whose next paragraph starts with a one-sentence description; the welcome screen then shows
"Welcome, Amon!  Let's run <title>.", the description, and a dim line with file, folder, Python version and time.
(No docstring: the file name is used as the title.  app_name= / tagline= / greeting= override any of it.)

What you get: a colour sunrise-over-mountains scene, big slanted "WELCOME / <NAME>" lettering, rainbow dividers,
a gradient greeting, then the confirmation question.  The letters adapt to the terminal width (big at 118+
columns, compact below).  Output is plain text when it is piped to a file or when NO_COLOR is set.

Three ways to use it (see README.md):
    1. installed once on this PC:    pip install -e <path-to>/amon_welcome      -> from amon_welcome import ...
    2. one file beside the script:   python -m amon_welcome --copy-here [DIR]
    3. pasted inside the script:     python -m amon_welcome --embed my_script.py     (re-run to update the copy)

Command line:
    python -m amon_welcome                  show the demo screen
    python -m amon_welcome --demo [--user NAME] [--force-color]
    python -m amon_welcome --embed FILE     make FILE self-contained (a .bak copy is kept unless --no-backup)
    python -m amon_welcome --copy-here [DIR]
    python -m amon_welcome --version
"""
import os
import random
import sys

__version__ = "1.1.0"
DEFAULT_USER = "Amon"

# ======================================================================================
# Colour and terminal basics
# ======================================================================================
USE_COLOR = False
_READY = False
RESET, BOLD, ITALIC, DIM = "\x1b[0m", "\x1b[1m", "\x1b[3m", "\x1b[2m"

SUNRISE = [(255, 228, 120), (255, 176, 80), (255, 112, 140), (200, 112, 235), (120, 165, 255)]
RAINBOW = [(255, 95, 109), (255, 176, 80), (255, 232, 110), (110, 220, 150), (100, 190, 255), (190, 130, 255)]


def fg(r, g, b):
    return f"\x1b[38;2;{r};{g};{b}m" if USE_COLOR else ""


def bg(r, g, b):
    return f"\x1b[48;2;{r};{g};{b}m" if USE_COLOR else ""


def style(text, *codes):
    return ("".join(codes) + text + RESET) if USE_COLOR else text


def enable_terminal(force_color=False):
    """UTF-8 output and ANSI colours (Windows 10+ needs the VT flag switched on)."""
    global USE_COLOR, _READY
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    if os.name == "nt":
        try:
            import ctypes
            kernel = ctypes.windll.kernel32
            handle = kernel.GetStdHandle(-11)
            mode = ctypes.c_ulong()
            if kernel.GetConsoleMode(handle, ctypes.byref(mode)):
                kernel.SetConsoleMode(handle, mode.value | 0x0004)
        except Exception:
            pass
    USE_COLOR = bool(force_color or (sys.stdout.isatty() and not os.environ.get("NO_COLOR")))
    _READY = True


def _ensure():
    if not _READY:
        enable_terminal()


def _lerp(a, b, t):
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))


def _gradient(stops, t):
    t = max(0.0, min(1.0, t))
    seg = t * (len(stops) - 1)
    i = min(int(seg), len(stops) - 2)
    return _lerp(stops[i], stops[i + 1], seg - i)


def _blend(base, over, alpha):
    return _lerp(base, over, alpha)


# ======================================================================================
# The scene: sunrise over layered mountains
# ======================================================================================
def build_scene(width=78, height=26):
    """Sunrise over layered mountains as a grid of RGB pixels (one pixel = half a character cell)."""
    rnd = random.Random(7)
    horizon = int(height * 0.80)
    fx = width / 78.0          # the layout below was drawn for 78 columns; stretch it for wider terminals
    px = [[(0, 0, 0)] * width for _ in range(height)]
    sky = [(22, 28, 84), (92, 52, 128), (206, 84, 118), (255, 150, 84), (255, 205, 120)]
    for y in range(height):
        c = _gradient(sky, y / horizon)
        for x in range(width):
            px[y][x] = c
    for _ in range(26):  # stars
        x, y = rnd.randrange(width), rnd.randrange(int(horizon * 0.42))
        px[y][x] = _blend(px[y][x], (255, 255, 235), rnd.choice((0.55, 0.8, 1.0)))
    for cy, cx, ln in ((5, 14, 11), (8, 52, 14), (4, 40, 8), (11, 8, 9)):  # clouds
        cx, ln = int(cx * fx), int(ln * fx)
        for dx in range(-ln, ln + 1):
            for dy in (0, 1):
                x, y = cx + dx, cy + dy
                if 0 <= x < width and 0 <= y < height and abs(dx) < ln - dy * 2:
                    px[y][x] = _blend(px[y][x], (255, 196, 178), 0.42 - 0.12 * dy)
    # sun with a soft glow
    scx, scy, rad = int(width * 0.60), int(horizon * 0.50), 6      # high enough to clear the ridges
    for y in range(height):
        for x in range(width):
            d = ((x - scx) ** 2 + (y - scy) ** 2) ** 0.5
            if d <= rad:
                px[y][x] = _lerp((255, 247, 176), (255, 186, 64), d / rad)
            elif d <= rad + 4:
                px[y][x] = _blend(px[y][x], (255, 214, 130), 0.5 * (1 - (d - rad) / 4))
    # distant birds
    for bx, by in ((22, 9), (27, 7), (31, 10), (58, 5)):
        bx = int(bx * fx)
        for dx, dy in ((-2, 0), (-1, 1), (0, 0), (1, 1), (2, 0)):
            if 0 <= bx + dx < width and 0 <= by + dy < height:
                px[by + dy][bx + dx] = (48, 32, 70)

    def mountains(base_y, peaks, color, snow_from=None, snow_depth=2):
        peaks = [(int(p * fx), h, w * fx) for p, h, w in peaks]
        heights = []
        for x in range(width):
            best, idx = -1, 0
            for i, (pxx, h, w) in enumerate(peaks):
                v = h - abs(x - pxx) * (h / w)
                if v > best:
                    best, idx = v, i
            heights.append((best, idx))
        for x, (hh, idx) in enumerate(heights):
            if hh <= 0:
                continue
            top = int(round(base_y - hh))
            lit = 1.14 if x < peaks[idx][0] else 0.84  # sun-lit left slope, shaded right slope
            for y in range(max(top, 0), height):
                depth = y - top
                shade = max(0.55, 1.0 - 0.025 * depth)
                c = tuple(max(0, min(255, int(v * lit * shade))) for v in color)
                if snow_from and hh >= snow_from and depth < snow_depth:
                    c = tuple(min(255, int(v * lit)) for v in (244, 232, 244))
                px[y][x] = c

    mountains(horizon + 2, [(8, 10, 12), (26, 13, 15), (45, 11, 13), (63, 14, 16), (76, 9, 11)], (128, 88, 158), snow_from=11, snow_depth=3)
    mountains(horizon + 4, [(2, 9, 11), (19, 12, 13), (37, 10, 12), (54, 13, 14), (70, 11, 13)], (84, 62, 128), snow_from=12, snow_depth=2)
    mountains(horizon + 7, [(12, 7, 9), (31, 9, 11), (49, 7, 9), (66, 9, 11)], (42, 38, 88))
    for y in range(horizon + 5, height):  # foreground ridge and pines
        for x in range(width):
            px[y][x] = (18, 24, 50)
    for tx in range(2, width, 5):
        th = rnd.choice((3, 4, 5))
        for k in range(th):
            for dx in range(-(k // 2) - (1 if k else 0), (k // 2) + (2 if k else 1)):
                x, y = tx + dx, horizon + 6 - th + k + 1
                if 0 <= x < width and 0 <= y < height:
                    px[y][x] = (14, 52, 56) if (x + y) % 3 else (22, 70, 68)
    return px


def render_scene(px):
    lines = []
    for y in range(0, len(px), 2):
        row = []
        for x in range(len(px[0])):
            top, bottom = px[y][x], px[y + 1][x]
            row.append(f"\x1b[38;2;{top[0]};{top[1]};{top[2]}m\x1b[48;2;{bottom[0]};{bottom[1]};{bottom[2]}m\u2580")
        lines.append("".join(row) + RESET)
    return lines


# ======================================================================================
# Big slanted lettering (7-row block alphabet, sheared to the right)
# ======================================================================================
_GLYPHS = {
    "A": [" ████ ", "██  ██", "██  ██", "██████", "██  ██", "██  ██", "██  ██"],
    "B": ["█████ ", "██  ██", "██  ██", "█████ ", "██  ██", "██  ██", "█████ "],
    "C": [" ████ ", "██  ██", "██    ", "██    ", "██    ", "██  ██", " ████ "],
    "D": ["█████ ", "██  ██", "██  ██", "██  ██", "██  ██", "██  ██", "█████ "],
    "E": ["██████", "██    ", "██    ", "█████ ", "██    ", "██    ", "██████"],
    "F": ["██████", "██    ", "██    ", "█████ ", "██    ", "██    ", "██    "],
    "G": [" ████ ", "██  ██", "██    ", "██ ███", "██  ██", "██  ██", " ████ "],
    "H": ["██  ██", "██  ██", "██  ██", "██████", "██  ██", "██  ██", "██  ██"],
    "I": ["██████", "  ██  ", "  ██  ", "  ██  ", "  ██  ", "  ██  ", "██████"],
    "J": ["  ████", "    ██", "    ██", "    ██", "    ██", "██  ██", " ████ "],
    "K": ["██  ██", "██ ██ ", "████  ", "███   ", "████  ", "██ ██ ", "██  ██"],
    "L": ["██    ", "██    ", "██    ", "██    ", "██    ", "██    ", "██████"],
    "M": ["██   ██", "███ ███", "███████", "██ █ ██", "██   ██", "██   ██", "██   ██"],
    "N": ["██   ██", "███  ██", "████ ██", "██ ████", "██  ███", "██   ██", "██   ██"],
    "O": [" ████ ", "██  ██", "██  ██", "██  ██", "██  ██", "██  ██", " ████ "],
    "P": ["█████ ", "██  ██", "██  ██", "█████ ", "██    ", "██    ", "██    "],
    "Q": [" ████ ", "██  ██", "██  ██", "██  ██", "██ ███", "██  ██", " █████"],
    "R": ["█████ ", "██  ██", "██  ██", "█████ ", "████  ", "██ ██ ", "██  ██"],
    "S": [" █████", "██    ", "██    ", " ████ ", "    ██", "    ██", "█████ "],
    "T": ["██████", "  ██  ", "  ██  ", "  ██  ", "  ██  ", "  ██  ", "  ██  "],
    "U": ["██  ██", "██  ██", "██  ██", "██  ██", "██  ██", "██  ██", " ████ "],
    "V": ["██  ██", "██  ██", "██  ██", "██  ██", "██  ██", " ████ ", "  ██  "],
    "W": ["██   ██", "██   ██", "██   ██", "██ █ ██", "███████", "███ ███", "██   ██"],
    "X": ["██  ██", "██  ██", " ████ ", "  ██  ", " ████ ", "██  ██", "██  ██"],
    "Y": ["██  ██", "██  ██", " ████ ", "  ██  ", "  ██  ", "  ██  ", "  ██  "],
    "Z": ["██████", "    ██", "   ██ ", "  ██  ", " ██   ", "██    ", "██████"],
    " ": ["   "] * 7,
}
_ROWS = 7


def banner_width(text, scale):
    return sum((len(_GLYPHS.get(ch, _GLYPHS[" "])[0]) + 1) * scale for ch in text.upper()) + (_ROWS - 1) * scale // 2


def fit_scale(text, limit):
    """Largest letter width (2 columns per block, else 1) that still fits in `limit` columns."""
    return 2 if banner_width(text, 2) <= limit else 1


def _halfblock_banner(glyphs, palette):
    """Compact version: one column per letter pixel and two pixel rows per text row, so pixels stay square."""
    last = max(1, len(glyphs) - 1)
    width = sum(len(g[0]) + 1 for g in glyphs) + (_ROWS - 1) // 2
    grid = [[None] * width for _ in range(_ROWS + 1)]
    for i in range(_ROWS):
        x = (_ROWS - 1 - i) // 2
        shade = 1.0 - 0.06 * i
        for k, glyph in enumerate(glyphs):
            colour = tuple(int(v * shade) for v in _gradient(palette, k / last))
            for cell in glyph[i]:
                if cell != " ":
                    grid[i][x] = colour
                x += 1
            x += 1
    lines = []
    for r in range(0, _ROWS + 1, 2):
        row = ""
        for x in range(width):
            top, bottom = grid[r][x], grid[r + 1][x]
            if not USE_COLOR:
                row += "█" if (top or bottom) else " "
            elif top and bottom:
                row += fg(*top) + bg(*bottom) + "▀" + RESET
            elif top:
                row += fg(*top) + "▀" + RESET
            elif bottom:
                row += fg(*bottom) + "▄" + RESET
            else:
                row += " "
        lines.append(row)
    return lines


def slant_banner(text, palette, scale=2):
    """Big slanted letters. Every lit cell is a solid colour block (a coloured space), so there are no
    font gaps; each letter has its own colour from the palette and gets darker towards the bottom."""
    text = text.upper()
    glyphs = [_GLYPHS.get(ch, _GLYPHS[" "]) for ch in text]
    if scale == 1:
        return _halfblock_banner(glyphs, palette)
    last = max(1, len(glyphs) - 1)
    out = []
    for i in range(_ROWS):
        row = " " * ((_ROWS - 1 - i) * scale // 2)
        shade = 1.0 - 0.06 * i
        for k, glyph in enumerate(glyphs):
            base = _gradient(palette, k / last)
            colour = tuple(int(v * shade) for v in base)
            for cell in glyph[i]:
                if cell == " ":
                    row += " " * scale
                else:
                    row += (bg(*colour) + " " * scale + RESET) if USE_COLOR else "█" * scale
            row += " " * scale
        out.append(row)
    return out


def gradient_text(text, palette, *codes):
    if not USE_COLOR:
        return text
    n = max(1, len(text) - 1)
    return "".join(fg(*_gradient(palette, i / n)) + "".join(codes) + ch for i, ch in enumerate(text)) + RESET


def rainbow_line(width, char="▀"):
    if not USE_COLOR:
        return "-" * width
    return "".join(fg(*_gradient(RAINBOW, i / max(1, width - 1))) + char for i in range(width)) + RESET


# ======================================================================================
# Public API
# ======================================================================================
def script_info(script=None):
    """Describe the script being run, read from the script itself (so the welcome text is always about *this* script).

    title  - first line of the script's docstring if it is a short heading, else the file name made readable
    desc   - first sentence of the docstring paragraph that follows the heading ('' if there is none)
    file / folder - where the script lives.   `script` = explicit path (previews and tests); default = the running script.
    """
    import re
    main = sys.modules.get("__main__")
    path = script or getattr(main, "__file__", None) or ""
    if script:
        try:
            import ast
            with open(script, encoding="utf-8-sig") as fh:
                doc = ast.get_docstring(ast.parse(fh.read())) or ""
        except Exception:
            doc = ""
    else:
        doc = (getattr(main, "__doc__", None) or "").strip()
    stem = os.path.splitext(os.path.basename(path))[0]
    pretty = re.sub(r"[_\-]+", " ", stem).strip().title()
    blocks = []
    for block in re.split(r"\n\s*\n", doc.strip()):
        lines = [ln.strip() for ln in block.splitlines() if ln.strip() and not set(ln.strip()) <= set("=-~_*#")]
        if lines:
            blocks.append(lines)
    title, desc = None, ""
    if blocks:
        first = blocks[0]
        if len(first[0]) <= 70 and not first[0].endswith((".", ":")):
            title = first[0]
            rest = first[1:] or (blocks[1] if len(blocks) > 1 else [])
        else:
            rest = first
        sentences = re.split(r"(?<=[.!?])\s+", " ".join(rest).strip())
        desc = sentences[0].strip() if sentences else ""
        if len(desc) > 120:
            desc = desc[:117].rstrip() + "…"
    return {"title": title or pretty or "this script", "desc": desc,
            "file": os.path.basename(path), "folder": os.path.dirname(os.path.abspath(path)) if path else os.getcwd()}


def _context_line(info_, width):
    """Dim line under the greeting: script file, folder, Python version, date and time."""
    import datetime
    import platform
    stamp = datetime.datetime.now().strftime("%d %b %Y, %H:%M")
    head = [info_["file"] or "interactive session"]
    tail = [f"Python {platform.python_version()}", stamp]
    folder = info_["folder"]
    room = width - len("  ·  ".join(head + tail)) - 2 * len("  ·  ") - 4
    if len(folder) > room > 14:                       # shorten the middle, keep the drive and the last folders
        folder = folder[:6] + "…" + folder[-(room - 7):]
    return "  ·  ".join(head + [folder] + tail)


def show_welcome(user=DEFAULT_USER, app_name=None, tagline=None, greeting=None, script=None, credit=None):
    """Print the welcome screen: scene, big WELCOME / NAME, then text about the running script.

    Under the name: a greeting naming the script, its one-line description and a dim line with file, folder, Python and time.
    All three are read from the script itself (see script_info); pass app_name / tagline / greeting to override them.
    user=None shows only the big WELCOME (for scripts shared with others); credit adds a small line such as
    "Created by Amon" under the text."""
    import shutil
    _ensure()
    cols = shutil.get_terminal_size((110, 30)).columns
    width = 108 if cols >= 118 else 90 if cols >= 96 else 78      # scene width
    limit = max(30, min(cols - 6, 112))                           # room for the big letters
    pad = "  "
    center = lambda w: " " * max(0, (width - w) // 2)
    info_ = script_info(script)
    title = app_name or info_["title"]
    greeting = greeting or (f"Welcome, {user}!  Let's run {title}." if user else f"Let's run {title}.")
    tagline = info_["desc"] if tagline is None else tagline
    room = max(width, limit)                                      # the text may run a little wider than the picture
    if len(tagline) > room:
        tagline = tagline[:room - 1].rstrip() + "…"
    context = _context_line(info_, width)
    print()
    if USE_COLOR:
        for line in render_scene(build_scene(width=width)):
            print(pad + line)
    print(pad + rainbow_line(width))
    print()
    print(pad + center(len("✦  ✧  ✦  ✧  ✦")) + gradient_text("✦  ✧  ✦  ✧  ✦", RAINBOW, BOLD))
    print()
    words = [("Welcome", SUNRISE)] + ([(user, list(reversed(SUNRISE)))] if user else [])
    for word, palette in words:
        scale = fit_scale(word, limit)
        for line in slant_banner(word, palette, scale):
            print(pad + center(banner_width(word, scale)) + line)
        print()
    print()
    print(pad + center(len(greeting)) + gradient_text(greeting, SUNRISE, BOLD, ITALIC))
    if tagline:
        print(pad + center(len(tagline)) + gradient_text(tagline, RAINBOW, ITALIC))
    print(pad + center(len(context)) + style(context, DIM, fg(170, 170, 190)))
    if credit:
        print(pad + center(len(credit)) + style(credit, DIM, ITALIC, fg(200, 190, 220)))
    print()
    print(pad + rainbow_line(width))
    print()


def ask_yes_no(question, default=True):
    _ensure()
    suffix = "[Y/n]" if default else "[y/N]"
    while True:
        try:
            answer = input(style("  ? ", BOLD, fg(255, 190, 90)) + style(question + f" {suffix} ", BOLD)).strip().lower()
        except EOFError:
            return default
        if not answer:
            return default
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        print(style("    Please answer y or n.", fg(240, 120, 120)))


def welcome(app_name=None, user=DEFAULT_USER, tagline=None, greeting=None, confirm=None, art=True,
            force_color=False, script=None, credit=None):
    """Show the welcome screen, then ask `confirm` (if given). Returns False when the user says no.

    Everything about the script (name, description, file, folder) is picked up automatically from the running script's
    docstring and file name; app_name / tagline / greeting only override it.  confirm=True asks the standard question
    "Do you want to run the <script name> script?"; a string asks that text instead."""
    enable_terminal(force_color)
    if art:
        show_welcome(user, app_name, tagline, greeting, script, credit)
    if confirm is True:
        confirm = f"Do you want to run the {app_name or script_info(script)['title']} script?"
    if confirm and not ask_yes_no(confirm):
        print(style(f"\n  No problem{', ' + user if user else ''}. See you next time!\n", ITALIC, fg(255, 205, 140)))
        return False
    return True


def pick_one(title, items, label=str, detail=None, prompt="Which one should I use?"):
    """Numbered menu. `label(item)` is the text, `detail(item)` an optional dim note. Returns the chosen item."""
    _ensure()
    items = list(items)
    print()
    print(style("  " + title, BOLD))
    for i, item in enumerate(items, 1):
        extra = ("   " + style(detail(item), DIM)) if detail else ""
        print(f"    {style(str(i), BOLD, fg(255, 190, 90))}.  {label(item)}{extra}")
    while True:
        try:
            answer = input(style("  ? ", BOLD, fg(255, 190, 90)) + style(f"{prompt} (1-{len(items)}) ", BOLD)).strip()
        except EOFError:
            fail("Nothing was chosen.")
            sys.exit(1)
        if answer.isdigit() and 1 <= int(answer) <= len(items):
            return items[int(answer) - 1]
        print(style("    Please type one of the numbers above.", fg(240, 120, 120)))


def info(msg):
    _ensure()
    print(style("  ✔ ", BOLD, fg(110, 214, 150)) + msg)


def warn(msg):
    _ensure()
    print(style("  ! ", BOLD, fg(255, 190, 90)) + msg)


def fail(msg):
    _ensure()
    print(style("  ✘ ", BOLD, fg(240, 110, 110)) + msg)


def step(i, n, msg):
    _ensure()
    print(style(f"  [{i}/{n}] ", BOLD, fg(130, 170, 255)) + msg)


def summary_box(headline, facts=(), folder=None, files=(), files_title="Saved in:"):
    """The closing block: headline between two rules, key/value facts, then where the files went."""
    _ensure()
    print()
    rule = style("  " + "─" * 66, fg(255, 170, 110))
    print(rule)
    print(style("  " + headline, BOLD, fg(255, 214, 140)))
    print(rule)
    for label, value in facts:
        print(f"  {label}  {style(str(value), BOLD)}")
    if folder or files:
        print()
        print(style("  " + files_title, BOLD))
        if folder:
            print("    " + style(str(folder), fg(130, 200, 255)))
        for f in files:
            print("      · " + str(f))
    print()


# ---- CLI ---- (everything below this line is NOT copied by --embed)
def _library_source():
    """The part of this file above the CLI marker: what gets embedded or copied."""
    with open(__file__, encoding="utf-8") as fh:
        text = fh.read()
    marker = "# ---- " + "CLI ----"
    return text[:text.index("\n" + marker)] + "\n"


def embed_into(path, backup=True):
    import ast
    import base64
    import shutil
    import zlib

    begin_tag, end_tag = "# >>> amon_welcome", "# <<< amon_welcome <<<"
    with open(path, "rb") as fh:
        data = fh.read()
    bom = data.startswith(b"\xef\xbb\xbf")           # Notepad / PowerShell save scripts with a byte-order mark
    raw = data.decode("utf-8-sig")
    nl = "\r\n" if "\r\n" in raw else "\n"
    lines = raw.splitlines()
    payload = base64.b64encode(zlib.compress(_library_source().encode("utf-8"), 9)).decode("ascii")
    chunks = [payload[i:i + 96] for i in range(0, len(payload), 96)]
    block = [f"{begin_tag} v{__version__} >>>",
             "# Embedded copy of the shared welcome module - do not edit here.",
             "# Update it with:  python -m amon_welcome --embed <this file>",
             "import base64 as _b64, sys as _sys, types as _types, zlib as _zlib",
             '_aw = _types.ModuleType("amon_welcome")',
             "exec(compile(_zlib.decompress(_b64.b64decode("]
    block += [f'    "{c}"' for c in chunks]
    block += ['    )).decode("utf-8"), "<amon_welcome>", "exec"), _aw.__dict__)',
              '_sys.modules["amon_welcome"] = _aw',
              "del _b64, _types, _zlib, _aw",
              end_tag]
    starts = [i for i, ln in enumerate(lines) if ln.startswith(begin_tag)]
    ends = [i for i, ln in enumerate(lines) if ln.startswith(end_tag)]
    if starts and ends and starts[0] < ends[0]:
        lines[starts[0]:ends[0] + 1] = block
        action = "updated the embedded copy in"
    else:
        body = ast.parse(raw).body
        i = 0
        if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant) \
                and isinstance(body[0].value.value, str):
            i = 1
        while i < len(body) and isinstance(body[i], ast.ImportFrom) and body[i].module == "__future__":
            i += 1
        at = (body[i].lineno - 1) if i < len(body) else len(lines)
        lines[at:at] = block + [""]
        action = "embedded the welcome module into"
    if backup:
        shutil.copyfile(path, path + ".bak")
    out = nl.join(lines) + (nl if raw.endswith(("\n", "\r")) else "")
    compile(out, path, "exec")                       # never leave a script that no longer compiles
    with open(path, "wb") as fh:
        fh.write((b"\xef\xbb\xbf" if bom else b"") + out.encode("utf-8"))
    print(f"{action} {path}" + (f"  (backup: {path}.bak)" if backup else ""))


def copy_here(folder):
    import shutil
    dest = os.path.join(folder, "amon_welcome.py")
    if os.path.abspath(dest) == os.path.abspath(__file__):
        print("amon_welcome.py is already in that folder.")
        return
    shutil.copyfile(__file__, dest)
    print(f"copied amon_welcome.py to {dest}")


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog="python -m amon_welcome", description="Welcome screen for Amon's scripts")
    ap.add_argument("--demo", action="store_true", help="show the demo screen (default)")
    ap.add_argument("--user", default=DEFAULT_USER)
    ap.add_argument("--force-color", action="store_true")
    ap.add_argument("--embed", metavar="FILE", help="paste a self-contained copy into FILE (re-run to update)")
    ap.add_argument("--no-backup", action="store_true", help="with --embed: do not keep FILE.bak")
    ap.add_argument("--copy-here", nargs="?", const=".", metavar="DIR", help="copy amon_welcome.py into DIR (default: current folder)")
    ap.add_argument("--version", action="store_true")
    args = ap.parse_args(argv)
    if args.version:
        print(__version__)
    elif args.embed:
        embed_into(args.embed, backup=not args.no_backup)
    elif args.copy_here is not None:
        copy_here(args.copy_here)
    else:
        welcome("Demo", user=args.user, tagline="amon_welcome  ·  the standard welcome screen",
                greeting=f"Welcome, {args.user}!  This is how every script starts.", force_color=args.force_color)
        step(1, 3, "Reading data ...")
        info("1,234 rows, 10 columns")
        step(2, 3, "Checking columns ...")
        warn("2 rows have no date")
        step(3, 3, "Writing the report ...")
        summary_box(f"Done in 1.2s — thank you, {args.user}.", facts=[("Rows", "1,234"), ("Success ratio", "52.0%")],
                    folder="C:\\example\\Report 2026-10-25", files=["Report.xlsx", "Report.docx", "Report.pdf"],
                    files_title="Reports saved in:")
    return 0


if __name__ == "__main__":
    sys.exit(main())

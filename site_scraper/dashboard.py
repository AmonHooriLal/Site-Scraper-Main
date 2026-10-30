"""Live terminal dashboard (rich) with a plain-text fallback."""
from .helpers import *  # noqa: F401,F403  (shared imports, constants and helpers)
from .helpers import _gradient

# ─── Live terminal dashboard ────────────────────────────────────────────────────

SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
EVENT_ICONS = {"ok": ("✓", "#6ed696"), "render": ("◐", "#c870eb"), "err": ("✗", "#f06e6e"),
               "dup": ("≡", "#7a8090"), "file": ("↓", "#78a5ff"), "skip": ("–", "#7a8090")}


def hexcolor(rgb):
    return "#{:02x}{:02x}{:02x}".format(*rgb)


def fmt_secs(s):
    s = int(max(0, s))
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


class Dashboard:
    """Animated panel that redraws in place (rich), or plain progress lines when that isn't possible."""
    PHASES = ("Finding pages", "Crawling pages", "Rendering JS", "Downloading files", "Saving workbook")
    UNITS = {"Crawling pages": "pages", "Rendering JS": "pages", "Downloading files": "files",
             "Saving workbook": "steps"}

    def __init__(self, title, plain=False):
        self.title = title
        self.fancy = not plain and Live is not None and sys.stdout.isatty()
        self.console = Console(highlight=False) if self.fancy else None
        self.phases = {n: {"status": "wait", "done": 0, "total": None, "spent": 0.0, "since": None, "note": "",
                           "final": None} for n in self.PHASES}
        self.events = deque(maxlen=5)
        self.window = deque(maxlen=400)
        self.counters = lambda: {}
        self.lock = threading.Lock()
        self.t0 = time.monotonic()
        self.frame = 0
        self.current = None
        self.stopping = False
        self.finished = False
        self.live = None
        self.status_obj = None
        self.last_plain = 0.0

    # ── lifecycle ──
    def start(self):
        self.t0 = time.monotonic() - self.phases["Finding pages"]["spent"]
        if self.fancy:
            self.live = Live(console=self.console, get_renderable=self.render, refresh_per_second=8,
                             transient=False, redirect_stdout=True, redirect_stderr=True)
            self.live.start()

    def stop(self):
        with self.lock:
            if self.current:
                self._end(self.current)
            for p in self.phases.values():
                if p["status"] == "wait":
                    p["status"], p["note"] = "skip", p["note"] or "not needed"
            self.finished = True
        if self.live:
            self.live.update(self.render(), refresh=True)
            self.live.stop()
            self.live = None

    @contextmanager
    def status(self, phase, text):
        """Spinner line for work that happens before the panel starts (it may ask questions afterwards)."""
        p = self.phases[phase]
        p["status"], p["since"] = "run", time.monotonic()
        self.current = phase
        if self.fancy:
            with self.console.status(Text(text, style="bold"), spinner="dots", spinner_style="#ffb050") as st:
                self.status_obj = st
                try:
                    yield
                finally:
                    self.status_obj = None
        else:
            info(text)
            yield
        with self.lock:
            self._end(phase)

    # ── updates (thread-safe enough for counters; structural changes take the lock) ──
    def _end(self, name):
        p = self.phases[name]
        if p["since"] is not None:
            p["spent"] += time.monotonic() - p["since"]
            p["since"] = None
        if p["status"] == "run":
            p["status"] = "done"
        if self.current == name:
            self.current = None

    def phase(self, name, total=None, note=""):
        with self.lock:
            if self.current and self.current != name:
                self._end(self.current)
            p = self.phases[name]
            p["status"], p["since"], p["note"] = "run", time.monotonic(), note
            if total is not None:  # phases can run again (crawl → render → crawl); total = done so far + new work
                p["total"] = p["done"] + total
            self.current = name
            self.window.clear()
        if not self.fancy:
            info(f"{name} …" + (f" ({total:,} {self.UNITS.get(name, '')})" if total else ""))

    def finish(self, name, note=None):
        """End a phase; `note` replaces the default '<done> <unit> · <rate>/s' summary on its finished row."""
        with self.lock:
            if note is not None:
                self.phases[name]["final"] = note
            self._end(name)

    def set_total(self, name, total):
        self.phases[name]["total"] = max(total, self.phases[name]["done"])

    def note(self, name, text):
        self.phases[name]["note"] = text
        if self.status_obj is not None and self.current == name:
            self.status_obj.update(Text.assemble((f"{name}  ", "bold"), (text, "#9aa0ad")))

    def advance(self, name, n=1):
        p = self.phases[name]
        p["done"] += n
        now = time.monotonic()
        self.window.append((now, p["done"]))
        if not self.fancy and now - self.last_plain > 3:
            self.last_plain = now
            total = f" / {p['total']:,}" if p["total"] else ""
            rate, eta = self.rate_eta(p)
            extra = f"  {rate:.1f}/s" if rate else ""
            extra += f"  ETA {fmt_secs(eta)}" if eta else ""
            c = self.counters()
            info(f"{name}: {p['done']:,}{total}{extra}   " +
                 "  ".join(f"{k} {v}" for k, v in list(c.items())[:6]))

    def event(self, kind, url, detail=""):
        with self.lock:
            self.events.append((kind, url, detail))

    def rate_eta(self, p):
        if len(self.window) < 2:
            return 0.0, None
        now = time.monotonic()
        recent = [(t, d) for t, d in self.window if now - t <= 20] or list(self.window)[-2:]
        (t1, d1), (t2, d2) = recent[0], (now, p["done"])
        rate = (d2 - d1) / max(t2 - t1, 0.5)
        eta = (p["total"] - p["done"]) / rate if rate > 0 and p["total"] else None
        return rate, eta

    # ── drawing ──
    def bar(self, frac, width):
        frac = min(max(frac, 0.0), 1.0)
        filled = int(frac * width)
        hi = (self.frame * 2) % (width + 16) - 8  # moving highlight
        t = Text()
        for i in range(width):
            rgb = _gradient(SUNRISE, i / max(1, width - 1))
            if i < filled:
                d = abs(i - hi)
                if d < 5 and not self.finished:
                    k = (1 - d / 5) * 0.55
                    rgb = tuple(int(c + (255 - c) * k) for c in rgb)
                t.append("█", style=hexcolor(rgb))
            elif i == filled and frac < 1:
                t.append("▒", style=hexcolor(rgb))
            else:
                t.append("░", style="#3a3f4b")
        return t

    def pulse(self, width):
        size = 8
        span = max(1, width - size)
        pos = self.frame % (2 * span)
        pos = 2 * span - pos if pos > span else pos
        t = Text()
        for i in range(width):
            if pos <= i < pos + size:
                t.append("█", style=hexcolor(_gradient(SUNRISE, (i - pos) / (size - 1))))
            else:
                t.append("░", style="#3a3f4b")
        return t

    def render(self):
        self.frame += 1
        width = min(self.console.width, 104)
        bar_w = max(12, width - 62)
        spin = SPINNER[self.frame % len(SPINNER)]

        rows = Table.grid(padding=(0, 1), expand=True)
        rows.add_column(width=1, min_width=1, no_wrap=True)
        rows.add_column(width=17, min_width=17, no_wrap=True)
        rows.add_column(no_wrap=True, overflow="ellipsis", ratio=1)
        rows.add_column(justify="right", no_wrap=True, width=7)
        with self.lock:
            phases = [(n, dict(p)) for n, p in self.phases.items()]
            events = list(self.events)
        for name, p in phases:
            spent = p["spent"] + (time.monotonic() - p["since"] if p["since"] else 0)
            if p["status"] == "run":
                icon = Text(spin, style="bold #ffb050")
                label = Text(name, style="bold #ffe478")
                if p["total"]:
                    frac = p["done"] / max(1, p["total"])
                    line = self.bar(frac, bar_w)
                    line.append(f"  {p['done']:,} / {p['total']:,}", style="bold")
                    line.append(f"  {frac * 100:3.0f}%", style="#9aa0ad")
                else:
                    line = self.pulse(bar_w)
                    if p["note"]:
                        line.append("  " + p["note"], style="#9aa0ad")
                rows.add_row(icon, label, line, Text(fmt_secs(spent), style="#9aa0ad"))
                if p["total"]:
                    rate, eta = self.rate_eta(p)
                    sub = []
                    if rate:
                        sub.append(f"{rate:.1f} {self.UNITS.get(name, 'items')}/s")
                    if eta is not None:
                        sub.append(f"ETA {fmt_secs(eta)}")
                    if p["note"]:
                        sub.append(p["note"])
                    if sub:
                        rows.add_row("", "", Text(" · ".join(sub), style="#9aa0ad"), "")
            elif p["status"] == "done":
                note = p["final"]
                if not note:
                    note = f"{p['done']:,} {self.UNITS.get(name, 'items')}" if p["done"] else "done"
                    if p["done"] and spent > 1 and name != "Saving workbook":
                        note += f" · {p['done'] / spent:.1f}/s"
                rows.add_row(Text("✔", style="bold #6ed696"), Text(name), Text(note, style="#c8ccd4"),
                             Text(fmt_secs(spent), style="#9aa0ad"))
            elif p["status"] == "skip":
                rows.add_row(Text("–", style="#7a8090"), Text(name, style="#7a8090"),
                             Text(p["note"], style="#7a8090"), "")
            else:
                rows.add_row(Text("○", style="#7a8090"), Text(name, style="#9aa0ad"),
                             Text("waiting" + (f" · {p['note']}" if p["note"] else ""), style="#7a8090"), "")

        counters = list(self.counters().items())
        per_row = 5 if width >= 90 else 4 if width >= 72 else 3
        stats = Table.grid(padding=(0, 3))
        for _ in range(per_row):
            stats.add_column(no_wrap=True)
        for i in range(0, len(counters), per_row):
            cells = []
            for k, v in counters[i:i + per_row]:
                val = f"{v:,}" if isinstance(v, int) else str(v)
                cells.append(Text.assemble((k + " ", "#9aa0ad"), (val, "bold #ffe478" if v else "#c8ccd4")))
            stats.add_row(*cells, *[""] * (per_row - len(cells)))

        feed = Table.grid(padding=(0, 1), expand=True)
        detail_w = min(24, max([len(d) for _, _, d in events] + [8]))
        feed.add_column(width=1, min_width=1, no_wrap=True)
        feed.add_column(no_wrap=True, overflow="ellipsis", ratio=1, min_width=10)
        feed.add_column(justify="right", no_wrap=True, overflow="ellipsis", width=detail_w, min_width=detail_w)
        for kind, url, detail in events:
            icon, color = EVENT_ICONS.get(kind, ("·", "#9aa0ad"))
            parts = urlsplit(url)
            path = (parts.path or "/") + (("?" + parts.query) if parts.query else "")
            if parts.netloc and parts.netloc.replace("www.", "") != self.title:
                path = parts.netloc + path
            feed.add_row(Text(icon, style=f"bold {color}"), Text(path, style="#c8ccd4"),
                         Text(detail, style=color if kind == "err" else "#9aa0ad"))
        for _ in range(5 - len(events)):
            feed.add_row("", "", "")

        elapsed = fmt_secs(time.monotonic() - self.t0)
        title = Text.assemble(("✦ ", "#ffe478"), ("Scraping ", "bold"), (self.title, "bold #ffb050"),
                              (f"  {elapsed}", "#9aa0ad"))
        if self.stopping and not self.finished:
            title.append("  · stopping…", style="bold #f06e6e")
        if self.finished:
            subtitle = Text(f" finished in {elapsed} ", style="#6ed696")
        else:
            subtitle = Text(" Ctrl+C: stop and save · --resume to continue ", style="#7a8090")
        return Panel(Group(rows, Text(""), stats, Text(""), feed), title=title, title_align="left",
                     subtitle=subtitle, subtitle_align="left", border_style="#ffb050", box=box.ROUNDED,
                     padding=(1, 2), width=width)

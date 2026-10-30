"""Command line: options, welcome screen, and the run from discovery to export."""
from .helpers import *  # noqa: F401,F403  (shared imports, constants and helpers)
from .crawler import Scraper
from .dashboard import Dashboard

TAGLINE = ("Saves a website's public content (pages, articles, tables, documents, images, contacts and API data) "
           "as JSON, Markdown, CSV and Excel.")

# ─── Settings ───────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(description="Scrape all public content of a website.")
    p.add_argument("url", nargs="?", help="website to scrape (asked for when left out)")
    p.add_argument("--max-pages", type=int, default=1000)
    p.add_argument("--max-depth", type=int, default=10)
    p.add_argument("--per-section", type=int, default=0, help="cap pages per section (0 = no cap)")
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--delay", type=float, default=0.25, help="minimum seconds between requests")
    p.add_argument("--render", action="store_true", help="render every page in the browser")
    p.add_argument("--no-render", action="store_true", help="never use the browser")
    p.add_argument("--render-tabs", type=int, default=4)
    p.add_argument("--subdomains", action="store_true", help="also crawl other subdomains of the site")
    p.add_argument("--include", help="only crawl URLs matching this regex")
    p.add_argument("--exclude", help="skip URLs matching this regex")
    p.add_argument("--sections", help="only crawl these sections, comma-separated (e.g. blog,products)")
    p.add_argument("--max-param-variants", type=int, default=20)
    p.add_argument("--discovery-time", type=float, default=120,
                   help="max seconds spent reading sitemaps before crawling starts")
    p.add_argument("--no-images", action="store_true")
    p.add_argument("--no-docs", action="store_true")
    p.add_argument("--max-file-mb", type=float, default=50)
    p.add_argument("--max-files", type=int, default=5000, help="max documents + images to download")
    p.add_argument("--max-gb", type=float, default=5)
    p.add_argument("--max-time", help="stop after this long, e.g. 90m or 2h")
    p.add_argument("--keep-html", action="store_true", help="also save raw HTML (gzip)")
    p.add_argument("--no-contacts", action="store_true", help="don't collect emails, phone numbers or addresses")
    p.add_argument("--user-agent", default=UA, help="User-Agent header sent to websites")
    p.add_argument("--resume", action="store_true", help="continue (or refresh) the latest run for this site")
    p.add_argument("--out", help="parent folder for the output (default: the current folder)")
    p.add_argument("--plain", action="store_true", help="plain progress lines instead of the live dashboard")
    p.add_argument("--yes", action="store_true", help="no questions")
    p.add_argument("--no-art", action="store_true", help="skip the welcome picture")
    return p.parse_args()


# ─── Main ───────────────────────────────────────────────────────────────────────

def main():
    args = parse_args()
    if not welcome(app_name="Website Scraper", tagline=TAGLINE, confirm=not args.yes, art=not args.no_art,
                   user=None, credit="Created by Amon"):
        raise SystemExit
    url = args.url
    while not url:
        try:
            url = input("  ? Paste the website URL: ").strip()
        except EOFError:
            fail("No URL given.")
            raise SystemExit(2)

    sc = Scraper(args)
    step(1, 3, f"Opening {url} …")
    start = sc.resolve_start(url)
    sc.open_output()
    sc.read_robots()
    info(f"Site: {sc.root}   output: {sc.out}")
    ui = sc.ui = Dashboard(sc.host.replace("www.", ""), plain=args.plain)
    ui.counters = lambda: {
        "Pages": sc.stats["pages"], "Queue": sc.queue_size, "Rendered": sc.stats["rendered"],
        "Duplicates": sc.stats["duplicates"], "Errors": sc.stats["errors"],
        "Tables": sc.stats["tables"], "Structured": sc.stats["structured"],
        "APIs": sc.stats["api_calls"] + sc.stats["app_states"], "Emails": len(sc.contacts["emails"]),
        "Socials": len(sc.contacts["socials"]), "Phones": len(sc.contacts["phones"]),
        "Files": sc.stats["downloads"], "Disk": f"{sc.bytes_written / 1048576:,.1f} MB"}
    if async_playwright is None and not args.no_render:
        warn("Playwright not installed: JavaScript-only pages will be saved without rendering")
    for lib, name in ((trafilatura, "trafilatura"), (extruct, "extruct"), (phonenumbers, "phonenumbers"),
                      (PdfReader, "pypdf")):
        if lib is None:
            warn(f"{name} not installed: using the simpler fallback (pip install {name})")

    def on_sigint(*_):
        if sc.stop.is_set():
            raise KeyboardInterrupt
        warn("Stopping after the pages in progress… (Ctrl+C again to force)")
        ui.stopping = True
        sc.stop.set()
    signal.signal(signal.SIGINT, on_sigint)

    step(2, 3, "Finding pages (sitemaps, feeds, links) …")
    st = sc.state
    if args.resume:
        st.x("UPDATE urls SET status='queued' WHERE status IN ('fetching','failed')")
        if st.count("status IN ('queued','render')") == 0 and st.count("status='done'"):
            info("Previous run was complete: refreshing (only changed pages are downloaded again)")
            st.x("UPDATE urls SET status='queued' WHERE status='done'")
            args.max_pages = max(args.max_pages, st.count("status='queued'") + 1000)
        for sec, n in st.q("SELECT section, COUNT(*) FROM urls WHERE status='done' GROUP BY section"):
            sc.section_done[sec] = n
        sc.stats["pages"] = st.count("status='done'")
        st.commit()
    with ui.status("Finding pages", "Finding pages  reading sitemaps …"):
        sc.discover(start)
    queued = st.count("status='queued'")
    sc.queue_size = queued
    ui.phases["Finding pages"]["final"] = (f"{queued:,} found · sitemaps {sc.stats['sitemap_urls']:,} · "
                                           f"feeds {sc.stats['feed_urls']:,}")
    info(f"{queued:,} pages found ({sc.stats['sitemap_urls']:,} from sitemaps, {sc.stats['feed_urls']:,} from feeds); "
         f"more will be found while crawling")

    # Estimate + scope choice for big sites
    secs = st.q("SELECT section, COUNT(*) c FROM urls WHERE status='queued' GROUP BY section ORDER BY c DESC")
    if len(secs) > 1:
        print()
        for s, c in secs[:15]:
            print(f"      /{s:<30} {c:>9,}")
        if len(secs) > 15:
            print(f"      … and {len(secs) - 15} more sections")
    per_page = max(sc.limiter.delay, 0.6 / max(1, args.workers))
    if queued > args.max_pages:
        est = lambda n: f"~{n * per_page / 60:.0f} min, ~{n * 60 / 1024:.0f} MB text (+ images)"
        if not args.yes:
            options = [f"Crawl {args.max_pages:,} pages, balanced across sections ({est(args.max_pages)})",
                       f"Crawl everything found ({queued:,}+ pages, {est(queued)})",
                       "Pick sections"]
            choice = pick_one(f"This site has {queued:,}+ pages but --max-pages is {args.max_pages:,}.", options)
            if choice == options[1]:
                args.max_pages = 10 ** 9
            elif choice == options[2]:
                pick = input("  ? Section names, comma-separated: ").strip().lower()
                keep = {x.strip().strip("/") for x in pick.split(",") if x.strip()}
                sc.sections = keep
                st.x(f"UPDATE urls SET status='skipped', error='not in chosen sections' WHERE status='queued' AND "
                     f"section NOT IN ({','.join('?' * len(keep))})", tuple(keep))
                st.commit()
                left = st.count("status='queued'")
                info(f"{left:,} pages in the chosen sections")
                args.max_pages = max(args.max_pages, left)
        else:
            info(f"Crawling up to {args.max_pages:,} pages (balanced across sections); raise with --max-pages")
    else:
        info(f"Estimated time: ~{max(1, queued * per_page / 60):.0f} min")

    step(3, 3, "Scraping …")
    ui.start()
    try:
        try:
            while True:
                sc.fetch_phase()
                if sc.stop.is_set():
                    break
                added = sc.render_phase()
                for f in list(sc.feed_queue):
                    sc.enqueue(sc.read_feed(f), 2, 2)
                sc.feed_queue.clear()
                if sc.stop.is_set() or sc.budget_left() <= 0 or not (added or st.count("status='queued'")):
                    break
        except KeyboardInterrupt:
            sc.stop.set()
        st.commit()

        if not sc.stop.is_set():
            try:
                sc.download_phase()
            except KeyboardInterrupt:
                pass
        else:
            ui.phases["Downloading files"]["note"] = "skipped: stopped early (--resume continues)"
            warn("Skipping file downloads (stopped early); run again with --resume to continue")

        signal.signal(signal.SIGINT, signal.SIG_DFL)
        contacts, types = sc.export()
    finally:
        ui.stop()  # always restore the terminal, even after an error
    statuses = dict(st.q("SELECT status, COUNT(*) FROM urls GROUP BY status"))
    host = sc.site_name
    summary_box(
        "Website Scraper",
        facts=[
            ("Website", sc.root + ("   (refused automated access; stopped politely)" if sc.site_blocked else "")),
            ("Pages saved", f"{statuses.get('done', 0):,}  (rendered {sc.state.count('rendered=1 AND status=?', ('done',)):,})"),
            ("Still queued", f"{statuses.get('queued', 0):,}" + ("  → run again with --resume" if statuses.get('queued') else "")),
            ("Duplicates / soft 404 / failed", f"{statuses.get('duplicate', 0):,} / {statuses.get('soft404', 0):,} / "
                                               f"{statuses.get('failed', 0):,}"),
            ("Tables", f"{len(list((sc.out / 'tables').glob('*.csv'))):,}"),
            ("Structured records", ", ".join(f"{k} {v}" for k, v in sorted(types.items(), key=lambda x: -x[1])[:6]) or "none"),
            ("API responses", f"{len(list((sc.out / 'api').glob('*.json'))):,}"),
            ("Files downloaded", f"{sc.stats['downloads']:,}"),
            ("Emails / phones / socials", f"{len(contacts['emails'])} / {len(contacts['phones'])} / {len(contacts['socials'])}"),
        ],
        folder=str(sc.out),
        files=[f for f in (f"{host} Content.xlsx", "pages.jsonl", "markdown/", "structured/", "tables/", "api/",
                           "documents/", "images/", "contacts.json", "site_map.csv", "failed_urls.csv")
               if (sc.out / f.rstrip("/")).exists()],
    )

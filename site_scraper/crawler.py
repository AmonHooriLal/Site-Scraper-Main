"""The Scraper: discovery, fetching, rendering, extraction, downloads and export."""
from .helpers import *  # noqa: F401,F403  (shared imports, constants and helpers)
from .state import State, StopCrawl, Limiter
from .dashboard import Dashboard

# ─── The scraper ────────────────────────────────────────────────────────────────

class Scraper:
    def __init__(self, args):
        self.a = args
        self.stop = threading.Event()
        self.deadline = time.monotonic() + parse_duration(args.max_time) if args.max_time else None
        self.s = requests.Session()
        self.ua = args.user_agent
        self.s.headers.update({"User-Agent": self.ua, "Accept-Language": "en;q=0.9,*;q=0.5"})
        self.s.hooks["response"].append(self.check_redirect)
        self.private_ok = set()  # private hosts the user typed themselves (e.g. an intranet start URL)
        self.data_lock = threading.Lock()
        adapter = requests.adapters.HTTPAdapter(pool_connections=4, pool_maxsize=max(16, args.workers * 2))
        self.s.mount("https://", adapter)
        self.s.mount("http://", adapter)
        self.limiter = Limiter(args.delay)  # the site itself; other hosts (CDNs) get their own in limiter_for()
        self.limiters = {}
        self.ui = Dashboard("", plain=True)
        self.include = re.compile(args.include) if args.include else None
        self.exclude = re.compile(args.exclude) if args.exclude else None
        self.sections = {x.strip().lower() for x in args.sections.split(",")} if args.sections else None
        self.param_variants = Counter()
        self.section_done = Counter()
        self.write_lock = threading.Lock()
        self.contacts = {"emails": defaultdict(set), "phones": defaultdict(set), "socials": defaultdict(set),
                         "addresses": {}}
        self.cdn_hosts = Counter()
        self.bytes_written = 0
        self.consecutive_errors = 0
        self.stats = Counter()
        self.notes = set()
        self.query_seen = set()
        self.thin_hashes = {}
        self.queue_size = 0
        self.blocked_streak = 0
        self.site_blocked = False
        self.feed_queue = set()

    # ── setup ──
    def note_once(self, msg):
        if msg not in self.notes:
            self.notes.add(msg)
            warn(msg)

    def resolve_start(self, url):
        url = url.strip().strip('"').strip("'")
        if not re.match(r"^https?://", url, re.I):
            url = "https://" + url
        typed_host = urlsplit(url).netloc.lower()
        if not is_public_host(typed_host):
            self.private_ok.add(typed_host)  # the user asked for this address explicitly (e.g. an intranet site)
        try:
            r = self.s.get(url, timeout=30, allow_redirects=True, stream=True)
            r.close()
            url = r.url
        except requests.RequestException as e:
            fail(f"Could not open {url}: {e}")
            raise SystemExit(1)
        parts = urlsplit(url)
        self.scheme, self.host = parts.scheme, parts.netloc.lower()
        self.domain = reg_domain(self.host.split(":")[0])
        self.root = f"{self.scheme}://{self.host}"
        self.limiters[self.host] = self.limiter
        # file/folder-safe site name ("localhost:8080" -> "localhost_8080")
        self.site_name = re.sub(r"[^A-Za-z0-9._-]", "_", self.host.replace("www.", ""))
        tld = self.domain.rsplit(".", 1)[-1]
        self.region = TLD_REGION.get(tld)
        start = self.norm(url) or self.root + "/"
        segs = [s for s in urlsplit(start).path.split("/") if s]
        locale = segs and re.fullmatch(r"[a-z]{2}([_-][a-z]{2})?", segs[0].lower())
        self.section_prefix = segs[0].lower() if locale else ""
        return start

    def open_output(self):
        base = Path(self.a.out) if self.a.out else Path.cwd()
        name = self.site_name
        existing = sorted(base.glob(f"{name} Data *"))
        if self.a.resume and existing:
            self.out = existing[-1]
            info(f"Resuming in {self.out}")
        else:
            self.out = base / f"{name} Data {date.today():%Y-%m-%d}"
            if self.out.exists() and (self.out / ".state.db").exists() and not self.a.resume:
                n = 2
                while (base / f"{name} Data {date.today():%Y-%m-%d} ({n})").exists():
                    n += 1
                self.out = base / f"{name} Data {date.today():%Y-%m-%d} ({n})"
        for sub in ("markdown", "tables", "api", "structured"):
            (self.out / sub).mkdir(parents=True, exist_ok=True)
        self.state = State(self.out / ".state.db")
        self.asset_count = self.state.q("SELECT COUNT(*) FROM assets")[0][0]
        self.pages_file = open(self.out / "pages.jsonl", "a", encoding="utf-8")
        self.skipped_file = open(self.out / "skipped_urls.csv", "a", encoding="utf-8", newline="")
        self.skipped_csv = csv.writer(self.skipped_file)
        self.skipped_logged = 0
        old = self.out / "contacts.json"
        if old.exists():
            try:
                data = json.loads(old.read_text(encoding="utf-8"))
                for kind in ("emails", "phones", "socials"):
                    for item in data.get(kind, []):
                        self.contacts[kind][item["value"]].update(item.get("pages", []))
                self.contacts["addresses"].update({a["value"]: a for a in data.get("addresses", [])})
            except (ValueError, KeyError):
                pass

    def read_robots(self):
        self.robots = RobotFileParser()
        self.sitemaps_from_robots = []
        crawl_delay = None
        try:
            r = self.s.get(self.root + "/robots.txt", timeout=20, stream=True)
            ok = r.status_code == 200
            text = read_capped(r, 2 * 1024 * 1024).decode("utf-8", "replace") if ok else ""
            r.close()
        except requests.RequestException:
            text = ""
        self.robots.parse(text.splitlines())
        self.sitemaps_from_robots = re.findall(r"(?im)^\s*sitemap:\s*(\S+)", text)
        try:
            crawl_delay = self.robots.crawl_delay(self.ua) or self.robots.crawl_delay("*")
        except Exception:
            pass
        if crawl_delay and float(crawl_delay) > self.limiter.base:
            self.limiter.base = self.limiter.delay = min(float(crawl_delay), 30.0)
            info(f"robots.txt asks for {crawl_delay}s between requests; respecting it")

    # ── URL rules ──
    def in_scope(self, host):
        host = host.lower().split(":")[0]
        if host in (self.host.split(":")[0], "www." + self.domain, self.domain):
            return True
        return self.a.subdomains and host.endswith("." + self.domain)

    def norm(self, url, base=None):
        """Absolute, canonical form of a URL, or None if it should never be crawled."""
        if not url:
            return None
        url = url.strip()
        if url.startswith(("mailto:", "tel:", "javascript:", "data:", "#", "sms:", "whatsapp:")):
            return None
        try:
            parts = urlsplit(urljoin(base or self.root + "/", url))
        except ValueError:
            return None
        if parts.scheme not in ("http", "https"):
            return None
        host = parts.netloc.lower()
        if host.endswith(":443") or host.endswith(":80"):
            host = host.rsplit(":", 1)[0]
        path = re.sub(r"/{2,}", "/", parts.path or "/")
        if len(path) > 1 and path.endswith("/"):
            path = path.rstrip("/")
        query = urlencode(sorted((k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                                 if not TRACKING.match(k)))
        return urlunsplit((parts.scheme, host, path, query, ""))

    def section_of(self, url):
        segs = [s.lower() for s in urlsplit(url).path.split("/") if s]
        if segs and segs[0] == getattr(self, "section_prefix", "") and len(segs) > 1:
            segs = segs[1:]  # start URL was e.g. /en_in: sections are the next level (/en_in/insights → insights)
        return segs[0] if segs else "home"

    def skip_reason(self, url, depth):
        parts = urlsplit(url)
        if not self.in_scope(parts.netloc):
            return "out of scope"
        if ext_of(url) in SKIP_EXT:
            return "file type"
        if len(url) > 300:
            return "trap: very long URL"
        segs = [s for s in parts.path.split("/") if s]
        if segs and max(Counter(segs).values()) > 2:
            return "trap: repeated path segments"
        if len(segs) >= 4 and segs[-2:] == segs[-4:-2]:
            return "trap: repeating path pattern"
        if depth > self.a.max_depth:
            return "too deep"
        if parts.query and url not in self.query_seen:
            if self.param_variants[parts.path] >= self.a.max_param_variants:
                return "trap: too many query variants"
            if len(self.query_seen) > 1_000_000:  # bounded memory on enormous sites
                self.query_seen.clear()
            if len(self.param_variants) > 1_000_000:
                self.param_variants.clear()
            self.query_seen.add(url)
            self.param_variants[parts.path] += 1
        if self.include and not self.include.search(url):
            return "not in --include"
        if self.exclude and self.exclude.search(url):
            return "--exclude"
        if self.sections and self.section_of(url) not in self.sections:
            return "not in --sections"
        if not self.robots.can_fetch(self.ua, url):
            return "robots.txt"
        return None

    def log_skip(self, url, reason, page=""):
        if reason in ("out of scope", "file type") or self.skipped_logged > 200_000:
            return
        self.skipped_logged += 1
        with self.write_lock:
            self.skipped_csv.writerow(csv_row([url, reason, page]))

    def enqueue(self, urls, depth, src, page="", lastmods=None):
        """Add URLs to the queue (src: 0=start, 1=sitemap, 2=feed, 3=link)."""
        rows = []
        for u in urls:
            u = self.norm(u, page or None)
            if not u:
                continue
            e = ext_of(u)
            if e in DOC_EXT or e in IMG_EXT:
                if self.in_scope(urlsplit(u).netloc):
                    self.add_asset(u, "image" if e in IMG_EXT else "document", page)
                continue
            reason = self.skip_reason(u, depth)
            if reason:
                self.log_skip(u, reason, page)
                continue
            rows.append((u, "queued", depth, src, self.section_of(u), (lastmods or {}).get(u), page))
        if not rows:
            return 0
        with self.state.lock:
            before = self.state.db.total_changes
            self.state.db.executemany("INSERT OR IGNORE INTO urls(url,status,depth,src,section,lastmod,found_on) "
                                      "VALUES(?,?,?,?,?,?,?)", rows)
            return self.state.db.total_changes - before

    def add_asset(self, url, kind, page, alt=""):
        if (kind == "image" and self.a.no_images) or (kind == "document" and self.a.no_docs):
            return
        if self.asset_count >= self.a.max_files:  # e.g. a sitemap listing 250,000 PDFs
            self.stats["files_over_cap"] += 1
            return
        with self.state.lock:
            cur = self.state.db.execute("INSERT OR IGNORE INTO assets(url,kind,status,page,alt) VALUES(?,?,?,?,?)",
                                        (url, kind, "queued", page, alt[:300]))
            self.asset_count += cur.rowcount

    # ── discovery ──
    def limiter_for(self, host):
        lim = self.limiters.get(host)
        if lim is None:
            # CDNs are built for heavy traffic, so they get a lighter limit than the site itself
            lim = self.limiters.setdefault(host, Limiter(self.limiter.base if self.in_scope(host)
                                                         else self.a.delay / 4))
        return lim

    def host_allowed(self, host):
        host = (host or "").lower()
        return host in self.private_ok or is_public_host(host)

    def check_redirect(self, r, *args, **kwargs):
        """requests hook: refuse redirects that lead into the local/private network."""
        if r.is_redirect:
            target = safe_join(r.url, r.headers.get("location", ""))
            if not target or not self.host_allowed(urlsplit(target).netloc):
                r.close()
                raise requests.exceptions.InvalidURL(f"refused redirect to a private address: {target[:100]}")

    def get(self, url, timeout=30, **kw):
        """GET with per-host rate limiting, retries and backoff. Returns a Response or raises."""
        last = None
        host = urlsplit(url).netloc.lower()
        if not self.host_allowed(host):
            raise RuntimeError(f"refused: {host} is a private/local network address")
        limiter = self.limiter_for(host)
        for attempt in range(4):
            if self.stop.is_set():
                raise StopCrawl()
            limiter.wait()
            try:
                r = self.s.get(url, timeout=timeout, **kw)
            except requests.exceptions.InvalidURL:
                raise  # refused redirect: retrying won't help
            except requests.RequestException as e:
                last = e
                time.sleep(1.5 * 2 ** attempt)
                continue
            if r.status_code in (429, 502, 503, 504) or (r.status_code >= 500 and attempt < 2):
                ra = r.headers.get("Retry-After", "")
                limiter.slow_down(float(ra) if ra.isdigit() else None)
                last = RuntimeError(f"HTTP {r.status_code}")
                r.close()
                time.sleep(1.5 * 2 ** attempt)
                continue
            limiter.speed_up()
            return r
        raise last or RuntimeError("failed")

    def read_sitemap(self, url, seen, found, depth=0):
        if url in seen or depth > 4 or len(found) > 3_000_000:
            return
        # Stop reading when out of time, or once there are far more URLs than this crawl's page budget can use
        if time.monotonic() > self.discovery_deadline or len(found) >= max(20_000, self.a.max_pages * 20):
            self.discovery_capped = True
            return
        seen.add(url)
        try:
            r = self.get(url, timeout=60, stream=True)
            if r.status_code != 200:
                r.close()
                return
            data = read_capped(r, 50 * 1024 * 1024)
            if data[:2] == b"\x1f\x8b":
                data = gunzip_capped(data)
            text = data.decode("utf-8", "replace")
        except Exception:
            return
        if "<sitemapindex" in text[:2000].lower() or re.search(r"<sitemap>\s*<loc>", text[:5000], re.I):
            children = re.findall(r"<sitemap>.*?<loc>\s*(.*?)\s*</loc>", text, re.S | re.I)
            # Read the sitemaps that look relevant first (e.g. ".../en_in/..." when the start URL is /en_in)
            children.sort(key=lambda loc: not self.relevant_sitemap(loc))
            for loc in children:
                self.ui.note("Finding pages", f"{len(seen)} sitemaps read · {len(found):,} URLs")
                self.read_sitemap(loc.replace("&amp;", "&"), seen, found, depth + 1)
            return
        for block in re.finditer(r"<url>(.*?)</url>", text, re.S | re.I):
            b = block.group(1)
            loc = re.search(r"<loc>\s*(.*?)\s*</loc>", b, re.S | re.I)
            if not loc:
                continue
            lm = re.search(r"<lastmod>\s*(.*?)\s*</lastmod>", b, re.S | re.I)
            found[loc.group(1).replace("&amp;", "&")] = lm.group(1) if lm else None
            for alt in re.findall(r'<xhtml:link[^>]+href="([^"]+)"', b):
                found.setdefault(alt.replace("&amp;", "&"), None)

    def read_feed(self, url):
        try:
            r = self.get(url, timeout=30, stream=True)
            if r.status_code != 200:
                r.close()
                return []
            text = read_capped(r, 20 * 1024 * 1024).decode("utf-8", "replace")
        except Exception:
            return []
        if "<" not in text[:200] or not re.search(r"<(rss|feed|rdf:RDF)\b", text[:3000], re.I):
            return []
        links = re.findall(r"<item\b.*?<link>\s*(.*?)\s*</link>", text, re.S | re.I)
        links += re.findall(r"<entry\b.*?<link[^>]+href=\"([^\"]+)\"", text, re.S | re.I)
        return [l.replace("&amp;", "&") for l in links]

    def relevant_sitemap(self, loc):
        if self.include and self.include.search(loc):
            return True
        return bool(self.start_hint) and self.start_hint in loc.lower()

    def discover(self, start):
        found, seen = {}, set()
        segs = [s for s in urlsplit(start).path.split("/") if s]
        self.start_hint = segs[0].lower() if segs else ""
        self.discovery_deadline = time.monotonic() + self.a.discovery_time
        self.discovery_capped = False
        candidates = self.sitemaps_from_robots + [self.root + p for p in
                                                  ("/sitemap.xml", "/sitemap_index.xml", "/sitemap-index.xml",
                                                   "/wp-sitemap.xml", "/sitemap.xml.gz")]
        for sm in dict.fromkeys(candidates):
            self.ui.note("Finding pages", f"reading {urlsplit(sm).path} · {len(found):,} URLs so far")
            self.read_sitemap(sm, seen, found)
        added = self.enqueue([start], 0, 0)
        self.ui.note("Finding pages", f"checking {len(found):,} sitemap URLs against robots.txt and filters")
        added += self.enqueue(list(found), 1, 1, lastmods={self.norm(k): v for k, v in found.items()})
        feed_links = []
        for f in ("/feed", "/rss.xml", "/atom.xml", "/feed.xml", "/rss", "/blog/rss.xml", "/blog/feed"):
            self.ui.note("Finding pages", f"looking for feeds: {f} · {len(feed_links):,} feed links")
            feed_links += self.read_feed(self.root + f)
        added += self.enqueue(feed_links, 1, 2)
        self.state.commit()
        self.stats["sitemap_urls"] = len(found)
        self.stats["feed_urls"] = len(feed_links)
        if self.discovery_capped:
            warn(f"Sitemaps are very large: stopped reading them at {len(found):,} URLs ({len(seen)} sitemaps); "
                 f"enough for --max-pages {self.a.max_pages:,}. More pages are found by following links.")
        return added

    # ── page extraction ──
    def can_render(self):
        return not self.a.no_render and async_playwright is not None

    def needs_render(self, soup, visible):
        if not self.can_render():
            return False
        if self.a.render:
            return True
        if len(visible) < 300:
            return True
        placeholder = sum(len(m) for m in PLACEHOLDER_RE.findall(visible))
        if placeholder > 0.15 * len(visible):  # mostly template filler: real content arrives via JavaScript
            return True
        shell = soup.select_one("#root, #__next, #app, #___gatsby, [ng-version], [data-reactroot]")
        if shell is not None and len(clean(shell.get_text(" "))) < 200:
            return True
        ns = soup.find("noscript")
        return bool(ns and re.search(r"enable javascript|requires javascript|javascript is (disabled|required)",
                                     ns.get_text(" "), re.I))

    def extract(self, url, html, rendered=False):
        """Everything we can get from one HTML page. Returns (record, links, needs_render)."""
        soup = BeautifulSoup(html, "lxml")
        meta = {}
        for m in soup.find_all("meta"):
            k = m.get("property") or m.get("name") or m.get("itemprop")
            if k and m.get("content") and not k.lower().startswith(("google-site", "msvalidate", "facebook-domain")):
                meta.setdefault(k, m["content"])
        canonical_tag = soup.find("link", rel=lambda v: v and "canonical" in v)
        canonical = self.norm(canonical_tag.get("href"), url) if canonical_tag else None
        lang = (soup.html.get("lang") if soup.html else "") or ""

        # Links to crawl
        links = []
        for a in soup.find_all(["a", "area"], href=True):
            links.append(a["href"])
        for l in soup.find_all("link", href=True):
            rel = " ".join(l.get("rel") or []).lower()
            ltype = (l.get("type") or "").lower()
            if rel in ("next", "prev", "alternate") and (l.get("hreflang") or rel != "alternate"):
                links.append(l["href"])
            if rel == "alternate" and ("rss" in ltype or "atom" in ltype):
                self.feed_queue.add(safe_join(url, l["href"]))
        if canonical:
            links.append(canonical)

        # Structured data
        structured = {}
        if extruct is not None:
            try:
                structured = extruct.extract(html, base_url=url, uniform=True, errors="ignore",
                                             syntaxes=["json-ld", "microdata", "opengraph"])
            except Exception:
                structured = {}
        else:
            ld = []
            for s in soup.find_all("script", type="application/ld+json"):
                try:
                    ld.append(json.loads(s.string or ""))
                except ValueError:
                    pass
            structured = {"json-ld": ld}
        items = list(self.typed_items(structured))

        # Inline app state (Next.js / Nuxt / generic JSON blobs) — full data payloads
        states = []
        for s in soup.find_all("script"):
            sid, stype, body = s.get("id") or "", (s.get("type") or "").lower(), s.string or ""
            if not body or len(body) < 50:
                continue
            if sid == "__NEXT_DATA__" or (stype == "application/json" and len(body) > 200):
                try:
                    states.append({"id": sid or stype, "data": json.loads(body)})
                except ValueError:
                    pass
            elif re.search(r"window\.__(NUXT|INITIAL_STATE|APOLLO_STATE|PRELOADED_STATE)__\s*=", body):
                m = re.search(r"=\s*(\{.*\})\s*;?\s*$", body, re.S)
                if m:
                    try:
                        states.append({"id": "window-state", "data": json.loads(m.group(1))})
                    except ValueError:
                        pass
        for st in states:
            links += self.links_in_json(json.dumps(st["data"])[:5_000_000])

        # Contacts (before we strip header/footer, which is where they usually live)
        if not self.a.no_contacts:
            with self.data_lock:  # contacts are shared by all worker threads
                self.collect_contacts(soup, url, items)

        # Images, media, documents, forms
        images = self.collect_images(soup, url, meta)
        media = sorted({safe_join(url, f.get("src") or f.get("data-src") or "")
                        for f in soup.find_all(["iframe", "video", "audio", "source", "embed"])
                        if any(h in (f.get("src") or f.get("data-src") or "") for h in MEDIA_HOSTS)
                        or f.name in ("video", "audio")} - {url})
        forms = [{"action": safe_join(url, f.get("action") or ""), "method": (f.get("method") or "get").lower(),
                  "fields": [i.get("name") for i in f.find_all(["input", "select", "textarea"]) if i.get("name")]}
                 for f in soup.find_all("form")]
        for a in soup.find_all("a", href=True):
            u = self.norm(a["href"], url)
            if u and ext_of(u) in DOC_EXT and (self.in_scope(urlsplit(u).netloc)
                                               or urlsplit(u).netloc in self.cdn_hosts):
                self.add_asset(u, "document", url, clean(a.get_text(" "))[:200])

        # Tables (before trafilatura, on the full page)
        tables = self.collect_tables(soup, url)

        # Main content
        body, tmeta = "", {}
        if trafilatura is not None:
            try:
                body = trafilatura.extract(html, url=url, output_format="markdown", include_tables=True,
                                           include_links=False, include_images=False, include_comments=False,
                                           favor_recall=True, deduplicate=True) or ""
                md = trafilatura.extract_metadata(html, default_url=url)
                if md:
                    tmeta = {k: getattr(md, k, None) for k in ("title", "author", "date", "sitename",
                                                               "categories", "tags", "description")}
            except Exception:
                body = ""
        for t in soup(["script", "style", "noscript", "svg", "template", "iframe"]):
            t.decompose()
        visible = clean(soup.get_text(" "))
        render = not rendered and self.needs_render(soup, visible)
        # trafilatura is tuned for articles; on landing/profile/listing pages it can drop most of the content.
        # Compare with the page's main area and keep whichever holds clearly more.
        main = soup.find("main") or soup.find("article") or soup.find(attrs={"role": "main"})
        if main is None:
            for t in soup(["nav", "header", "footer", "aside", "form"]):
                t.decompose()
            main = soup.body or soup
        fallback = to_markdown(main)
        if len(body) < 200 or len(body) < 0.4 * len(fallback):
            body = fallback

        article = next((i for i in items if i.get("@type") in ("Article", "BlogPosting", "NewsArticle",
                                                                "TechArticle", "Report")), {})
        crumbs = next((i for i in items if i.get("@type") == "BreadcrumbList"), None)
        crumb_names = []
        if crumbs:
            for el in crumbs.get("itemListElement") or []:
                if isinstance(el, dict):
                    nm = el.get("name") or (el.get("item") or {}).get("name") if isinstance(el.get("item"), dict) \
                        else el.get("name")
                    if nm:
                        crumb_names.append(clean(str(nm)))
        author = article.get("author")
        if isinstance(author, list):
            author = ", ".join(a.get("name", "") if isinstance(a, dict) else str(a) for a in author)
        elif isinstance(author, dict):
            author = author.get("name", "")
        # A single <h1> is the page's own title; with several (carousels, cards) the <title> is more reliable
        h1s = [clean(h.get_text(" ")) for h in soup.find_all("h1") if clean(h.get_text(" "))]
        page_title = clean(soup.title.get_text()) if soup.title else ""
        page_title = page_title.split(" | ")[0]  # drop " | Site name"
        page_title = re.sub(r"\s+[–—·-]\s+[^–—·-]{2,30}$", "", page_title)  # and " - Site name"
        title = (h1s[0] if len(h1s) == 1 else "") or meta.get("og:title") or page_title or \
            (h1s[0] if h1s else "") or tmeta.get("title") or ""
        # Article markup only; a bare <article> tag is also used for product cards, so it doesn't count
        is_article = bool(article) or meta.get("og:type", "").lower() == "article"
        tags = [clean(t.get_text(" ")) for t in soup.select("a[rel~=tag], .tags a, .tag-list a, [class*=tag] a")][:30]
        for extra in (tmeta.get("tags"), tmeta.get("categories"), meta.get("article:tag"), meta.get("keywords")):
            if extra:
                tags += [clean(x) for x in (extra if isinstance(extra, list) else re.split(r"[,;|]", extra))]
        words = len(body.split())
        record = {
            "url": url,
            "canonical": canonical,
            "section": self.section_of(url),
            "breadcrumb": crumb_names,
            "title": title,
            "description": meta.get("description") or meta.get("og:description") or tmeta.get("description") or "",
            "author": author or meta.get("author") or tmeta.get("author") or "",
            # trafilatura's date is a guess; trust it only on article pages
            "date_published": article.get("datePublished") or meta.get("article:published_time")
                              or (tmeta.get("date") if is_article else "") or "",
            "date_modified": article.get("dateModified") or meta.get("article:modified_time") or "",
            "language": lang,
            "tags": list(dict.fromkeys(t for t in tags if t and len(t) < 80))[:30],
            "body_markdown": body,
            "words": words,
            "headings": [f"{h.name}: {clean(h.get_text(' '))}" for h in soup.find_all(["h1", "h2", "h3", "h4"])
                         if clean(h.get_text(" "))][:200],
            "images": images,
            "media": media,
            "forms": forms,
            "tables": tables,
            "structured": items,
            "app_state_keys": [st["id"] for st in states],
            "meta": meta,
            "rendered": rendered,
            "scraped_at": datetime.now().isoformat(timespec="seconds"),
        }
        if states:
            p = self.out / "api" / f"state_{hashlib.sha1(url.encode()).hexdigest()[:12]}.json"
            p.write_text(json.dumps({"page": url, "states": states}, ensure_ascii=False), encoding="utf-8")
            with self.data_lock:
                self.stats["app_states"] += 1
        soft404 = bool(NOT_FOUND_RE.search(title or "")) and words < 150
        record["soft_404"] = soft404
        return record, links, render

    def typed_items(self, structured):
        def walk(node):
            if isinstance(node, list):
                for n in node:
                    yield from walk(n)
            elif isinstance(node, dict):
                if "@graph" in node:
                    yield from walk(node["@graph"])
                t = node.get("@type")
                if t:
                    if isinstance(t, list):
                        t = t[0]
                    t = str(t).rsplit("/", 1)[-1]
                    if re.fullmatch(r"[A-Z][A-Za-z]+", t):  # schema.org-style types only (not "mw:File")
                        yield {**node, "@type": t}
        for syntax in ("json-ld", "microdata", "rdfa"):
            yield from walk(structured.get(syntax) or [])

    def links_in_json(self, text):
        out = re.findall(r'"(?:href|url|link|path|slug|uri|permalink|canonical)"\s*:\s*"([^"]{1,300})"', text)
        out += re.findall(r'"(https?://' + re.escape(self.host) + r'/[^"\s]{0,300})"', text)
        return [o.replace("\\/", "/") for o in out if o.startswith(("/", "http"))]

    def collect_images(self, soup, url, meta):
        found = {}

        def add(src, alt=""):
            if not src or src.startswith("data:"):
                return
            u = safe_join(url, src)
            if u and urlsplit(u).scheme in ("http", "https"):
                found.setdefault(u, alt)

        def best(srcset):
            best_u, best_w = None, -1
            for part in (srcset or "").split(","):
                bits = part.strip().split()
                if not bits:
                    continue
                w = 1
                if len(bits) > 1:
                    m = re.match(r"(\d+(?:\.\d+)?)[wx]", bits[1])
                    w = float(m.group(1)) if m else 1
                if w > best_w:
                    best_u, best_w = bits[0], w
            return best_u

        for img in soup.find_all("img"):
            alt = clean(img.get("alt") or "")
            src = best(img.get("srcset") or img.get("data-srcset")) or img.get("data-src") or \
                img.get("data-lazy-src") or img.get("data-original") or img.get("src")
            add(src, alt)
        for s in soup.select("picture source[srcset]"):
            add(best(s["srcset"]))
        for el in soup.select("[style*=background]"):
            for m in re.findall(r"url\(['\"]?([^'\")]+)['\"]?\)", el.get("style", "")):
                add(m)
        for k in ("og:image", "twitter:image"):
            if meta.get(k):
                add(meta[k])
        for u, alt in found.items():
            with self.data_lock:
                self.cdn_hosts[urlsplit(u).netloc] += 1
            self.add_asset(u, "image", url, alt)
        return [{"src": u, "alt": a} for u, a in found.items()]

    def collect_tables(self, soup, url):
        if pd is None:
            return []
        out = []
        for i, t in enumerate(soup.find_all("table")):
            if t.find_parent("table"):
                continue
            try:
                dfs = pd.read_html(io.StringIO(str(t)))
            except Exception:
                continue
            if not dfs:
                continue
            df = dfs[0]
            if df.shape[0] < 2 or df.shape[1] < 2:
                continue
            cap = t.find("caption")
            name = fit_name(self.out / "tables", f"{slugify(urlsplit(url).path.strip('/') or 'home', 60)}__{i + 1}",
                            ".csv")
            df.columns = [csv_safe(str(c)) for c in df.columns]
            df = df.apply(lambda col: col.map(csv_safe))
            df.to_csv(self.out / "tables" / name, index=False, encoding="utf-8-sig")
            out.append({"file": name, "rows": int(df.shape[0]), "cols": int(df.shape[1]),
                        "caption": clean(cap.get_text(" ")) if cap else "",
                        "columns": [str(c) for c in df.columns][:30]})
            with self.data_lock:
                self.stats["tables"] += 1
        return out

    def collect_contacts(self, soup, url, items):
        c = self.contacts
        text = soup.get_text(" ")[:200_000]
        emails = set(EMAIL_RE.findall(text))
        for a in soup.find_all("a", href=True):
            h = a["href"].strip()
            if h.lower().startswith("mailto:"):
                emails.add(h[7:].split("?")[0])
            elif h.lower().startswith("tel:"):
                self.add_phone(h[4:], url, strict=False)
            else:
                full = safe_join(url, h)
                if not full:
                    continue
                host = urlsplit(full).netloc.lower().replace("www.", "")
                for dom, label in SOCIAL.items():
                    if (host == dom or host.endswith("." + dom)) and self.is_profile(dom, urlsplit(full).path):
                        c["socials"][f"{label}: {full.split('?')[0].rstrip('/')}"].add(url)
        for el in soup.select("[data-cfemail]"):
            emails.add(self.cf_decode(el["data-cfemail"]))
        for user, dom in AT_EMAIL_RE.findall(text):
            emails.add(user + "@" + re.sub(r"\s*[\[\(\{]\s*dot\s*[\]\)\}]\s*", ".", dom, flags=re.I))
        for e in emails:
            e = e.strip().strip(".").lower()
            if e and EMAIL_RE.fullmatch(e) and not re.search(r"\.(png|jpe?g|gif|webp|svg)$|example\.|sentry|"
                                                                  r"wixpress|@2x", e):
                c["emails"][e].add(url)
        if phonenumbers is not None:
            # Plain-text numbers outside tables only, written like a phone number (not "2,345,678" or "12.5")
            prose = " ".join(t for t in soup.find_all(string=True) if not t.find_parent(["table", "script", "style"]))
            for m in phonenumbers.PhoneNumberMatcher(prose[:60_000], self.region or "US",
                                                     leniency=phonenumbers.Leniency.VALID):
                raw = m.raw_string
                if re.search(r"[,.]", raw) or not re.search(r"[+()\s-]", raw):
                    continue
                c["phones"][phonenumbers.format_number(m.number, phonenumbers.PhoneNumberFormat.INTERNATIONAL)].add(url)
        for it in items:
            for addr in self.find_addresses(it):
                key = ", ".join(str(addr.get(k)) for k in ("streetAddress", "postalCode", "addressLocality",
                                                           "addressRegion", "addressCountry") if addr.get(k))
                if key:
                    c["addresses"].setdefault(key, {"value": key, "pages": []})
                    if url not in c["addresses"][key]["pages"]:
                        c["addresses"][key]["pages"].append(url)

    @staticmethod
    def is_profile(domain, path):
        """True for a social profile/page link, False for posts, code, videos, share buttons…"""
        segs = [s for s in path.split("/") if s]
        if not segs or len(segs) > 2 or any(NOT_PROFILE_RE.fullmatch(s) for s in segs):
            return False
        if domain == "linkedin.com":
            return segs[0] in ("in", "company", "school", "showcase") and len(segs) == 2
        if domain == "youtube.com":
            return segs[0].startswith("@") or (segs[0] in ("channel", "c", "user") and len(segs) == 2)
        return len(segs) == 1 or domain in ("medium.com",)

    def add_phone(self, raw, url, strict=True):
        raw = re.sub(r"[^\d+]", "", raw)
        if len(raw) < 6:
            return
        if phonenumbers is not None:
            try:
                n = phonenumbers.parse(raw, self.region or "US")
                if phonenumbers.is_possible_number(n):
                    raw = phonenumbers.format_number(n, phonenumbers.PhoneNumberFormat.INTERNATIONAL)
                elif strict:
                    return
            except phonenumbers.NumberParseException:
                if strict:
                    return
        self.contacts["phones"][raw].add(url)

    def find_addresses(self, node):
        if isinstance(node, dict):
            if node.get("@type") == "PostalAddress":
                yield node
            for v in node.values():
                yield from self.find_addresses(v)
        elif isinstance(node, list):
            for v in node:
                yield from self.find_addresses(v)

    @staticmethod
    def cf_decode(hexstr):
        try:
            key = int(hexstr[:2], 16)
            return "".join(chr(int(hexstr[i:i + 2], 16) ^ key) for i in range(2, len(hexstr), 2))
        except ValueError:
            return ""

    # ── saving a page ──
    @staticmethod
    def content_hash(record):
        if record["words"] <= 30:
            return None
        return hashlib.sha1((clean(record["title"]) + "\n" + clean(record["body_markdown"])).lower()
                            .encode()).hexdigest()

    @staticmethod
    def markdown_text(record):
        head = [f"# {record['title']}", "", f"URL: {record['url']}"]
        for label, key in (("Published", "date_published"), ("Author", "author")):
            if record[key]:
                head.append(f"{label}: {str(record[key])[:10] if key == 'date_published' else record[key]}")
        if record["tags"]:
            head.append("Tags: " + ", ".join(record["tags"]))
        if record["description"]:
            head += ["", f"> {record['description']}"]
        return "\n".join(head) + "\n\n" + record["body_markdown"] + "\n"

    def strip_boilerplate(self):
        """Remove body lines that repeat on ≥40% of pages (menus, footers, taglines that slipped past extraction).
        Also rewrites pages.jsonl with only the newest record per URL. Returns how many distinct lines were removed."""
        key = lambda line: hash(line.strip())
        eligible = lambda line: 25 < len(line.strip()) < 400
        counts, n = Counter(), 0
        for rec in self.latest_records():
            counts.update({key(l) for l in rec["body_markdown"].split("\n") if eligible(l)})
            n += 1
            if n >= 5000:  # a sample is enough to recognise site-wide boilerplate
                break
        if n < 20:
            return 0
        common = {h for h, c in counts.items() if c >= 0.4 * n}
        if not common:
            return 0
        src, tmp = self.out / "pages.jsonl", self.out / "pages.jsonl.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            for rec in self.latest_records():
                lines = [l for l in rec["body_markdown"].split("\n") if not (eligible(l) and key(l) in common)]
                body = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
                if body != rec["body_markdown"]:
                    rec["body_markdown"], rec["words"] = body, len(body.split())
                    row = self.state.q("SELECT md_path FROM urls WHERE url=?", (rec["url"],))
                    if row and row[0][0] and (self.out / row[0][0]).exists():
                        (self.out / row[0][0]).write_text(self.markdown_text(rec), encoding="utf-8")
                f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        tmp.replace(src)
        return len(common)

    def save_page(self, url, record, depth, http=200, etag=None, last_modified=None):
        """Write the record (JSONL + Markdown + structured), update state. Called from the main thread."""
        body_hash = self.content_hash(record)
        canon = record.get("canonical")
        if canon and canon != url and self.state.q("SELECT 1 FROM urls WHERE url=? AND status='done'", (canon,)):
            self.state.x("UPDATE urls SET status='duplicate', error=? WHERE url=?", (f"canonical: {canon}", url))
            self.stats["duplicates"] += 1
            self.ui.event("dup", url, "duplicate (canonical)")
            return
        if body_hash:
            dup = self.state.q("SELECT url FROM hashes WHERE hash=?", (body_hash,))
            if dup and dup[0][0] != url:
                self.state.x("UPDATE urls SET status='duplicate', error=? WHERE url=?", (f"same as {dup[0][0]}", url))
                self.stats["duplicates"] += 1
                self.ui.event("dup", url, "duplicate")
                return
            self.state.x("INSERT OR IGNORE INTO hashes VALUES(?,?)", (body_hash, url))
        if record["soft_404"]:
            self.state.x("UPDATE urls SET status='soft404', http=?, title=? WHERE url=?", (http, record["title"], url))
            self.stats["soft_404"] += 1
            self.ui.event("skip", url, "says not found")
            return

        sec = record["section"]
        row = self.state.q("SELECT md_path, status FROM urls WHERE url=?", (url,))
        md_rel = row[0][0] if row and row[0][0] else None
        already_counted = bool(row and row[0][1] == "done") or bool(md_rel)
        if not md_rel:
            n = self.section_done[sec]
            segs = [s for s in urlsplit(url).path.split("/") if s]
            if segs and segs[0].lower() == sec:
                segs = segs[1:]  # the section is already the folder name
            slug = slugify("__".join(segs) or (sec if sec != "home" else "home"), 120)
            if urlsplit(url).query:
                slug = slugify(slug + "_" + hashlib.sha1(urlsplit(url).query.encode()).hexdigest()[:8], 130)
            folder = f"markdown/{slugify(sec, 40)}/{n // 1000 + 1:04d}"
            md_rel = f"{folder}/{fit_name(self.out / folder, slug, '.md')}"
        md_path = self.out / md_rel
        md_path.parent.mkdir(parents=True, exist_ok=True)
        text = self.markdown_text(record)
        md_path.write_text(text, encoding="utf-8")
        self.bytes_written += len(text)

        line = json.dumps(record, ensure_ascii=False, default=str)
        self.pages_file.write(line + "\n")
        self.bytes_written += len(line)
        for it in record["structured"]:
            t = slugify(str(it.get("@type")), 40)
            if t in ("WebPage", "WebSite", "BreadcrumbList", "ImageObject", "SiteNavigationElement", "ListItem",
                     "SearchAction", "EntryPoint", "WPHeader", "WPFooter", "ReadAction", "CollectionPage"):
                continue
            with open(self.out / "structured" / f"{t}.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps({"_page": url, **it}, ensure_ascii=False, default=str) + "\n")
            self.stats["structured"] += 1

        if not already_counted:
            self.section_done[sec] += 1
        self.state.x("UPDATE urls SET status='done', http=?, etag=?, last_modified=?, rendered=?, hash=?, title=?, "
                     "words=?, canonical=?, md_path=?, error=NULL WHERE url=?",
                     (http, etag, last_modified, int(record["rendered"]), body_hash, record["title"][:500],
                      record["words"], canon, md_rel, url))
        if not already_counted:
            self.stats["pages"] += 1
        self.ui.event("ok", url, f"{record['words']:,} words" + (" · rendered" if record["rendered"] else ""))

    # ── fast fetch phase ──
    def fetch_page(self, url, depth, etag, last_modified):
        """Worker: download + extract one page. Returns a result dict (no shared-state writes except assets)."""
        h = {}
        if etag:
            h["If-None-Match"] = etag
        if last_modified:
            h["If-Modified-Since"] = last_modified
        try:
            r = self.get(url, timeout=30, headers=h, stream=True)
        except StopCrawl:
            return {"url": url, "stopped": True}
        except Exception as e:
            return {"url": url, "error": str(e)[:300]}
        try:
            if r.status_code == 304:
                return {"url": url, "not_modified": True}
            final = self.norm(r.url) or url
            if r.status_code in (401, 403, 429):  # the site refuses automated access: respect it
                return {"url": url, "blocked": True, "http": r.status_code}
            if r.status_code >= 400:
                return {"url": url, "error": f"HTTP {r.status_code}", "http": r.status_code}
            ctype = r.headers.get("Content-Type", "").lower()
            if ctype and ("html" not in ctype or "rss" in ctype or "atom" in ctype):
                kind = "image" if ctype.startswith("image/") else "document"
                return {"url": url, "asset": kind}
            raw = r.raw.read(15 * 1024 * 1024, decode_content=True)
            enc = r.encoding if r.encoding and r.encoding.lower() != "iso-8859-1" else None
            html = raw.decode(enc or r.apparent_encoding or "utf-8", "replace")
        finally:
            r.close()
        if CHALLENGE_RE.search(html[:30000]) and len(html) < 300_000:  # bot-check page instead of content
            return {"url": url, "blocked": True, "http": r.status_code}
        if final != url and not self.in_scope(urlsplit(final).netloc):
            return {"url": url, "error": f"redirects off-site to {final}"}
        try:
            record, links, render = self.extract(final, html)
        except Exception as e:
            return {"url": url, "error": f"parse error: {e}"[:300]}
        if self.a.keep_html:
            p = self.out / "html" / (hashlib.sha1(final.encode()).hexdigest()[:16] + ".html.gz")
            p.parent.mkdir(exist_ok=True)
            p.write_bytes(gzip.compress(html.encode("utf-8")))
        record["url"] = url
        record["final_url"] = final
        return {"url": url, "record": record, "links": links, "render": render, "http": r.status_code,
                "etag": r.headers.get("ETag"), "last_modified": r.headers.get("Last-Modified"), "depth": depth}

    def budget_left(self):
        done = self.state.count("status IN ('done','render','soft404','duplicate')")
        return self.a.max_pages - done

    def out_of_time(self):
        return self.deadline is not None and time.monotonic() > self.deadline

    def next_batch(self, n):
        """Balanced pick: best source tier first (start > sitemap > feed > links), round-robin over its sections.

        Balancing only within one tier keeps a sitemap URL ahead of a link-discovered alias of the same page,
        so the sitemap version is the one saved and the alias becomes the duplicate."""
        tier = self.state.q("SELECT MIN(src) FROM urls WHERE status='queued'")[0][0]
        if tier is None:
            return []
        rows = self.state.q("SELECT url, depth, section, etag, last_modified FROM urls WHERE status='queued' "
                            "AND src=? ORDER BY depth, rowid LIMIT ?", (tier, max(n * 50, 2000)))
        by_sec = defaultdict(list)
        for r in rows:
            if self.a.per_section and self.section_done[r[2]] >= self.a.per_section:
                self.state.x("UPDATE urls SET status='skipped', error='--per-section cap' WHERE url=?", (r[0],))
                continue
            by_sec[r[2]].append(r)
        order = sorted(by_sec, key=lambda s: self.section_done[s])
        picked = []
        while len(picked) < n and any(by_sec.values()):
            for s in order:
                if by_sec[s] and len(picked) < n:
                    picked.append(by_sec[s].pop(0))
        return picked

    def crawl_total(self, running):
        """Pages this crawl will still touch: in flight + queued, capped by the page budget."""
        queued = self.state.count("status='queued'")
        self.queue_size = queued
        return len(running) + min(queued, max(0, self.budget_left() - len(running)))

    def fetch_phase(self):
        last_report = time.monotonic()
        self.ui.phase("Crawling pages", total=self.crawl_total({}), note=f"{self.a.workers} workers")
        with ThreadPoolExecutor(self.a.workers) as pool:
            running = {}
            while not self.stop.is_set():
                if self.out_of_time():
                    warn("Time limit reached, finishing up")
                    self.stop.set()
                    break
                if self.bytes_written > self.a.max_gb * 1024 ** 3:
                    warn("Disk limit (--max-gb) reached, finishing up")
                    self.stop.set()
                    break
                room = min(self.a.workers * 2 - len(running), self.budget_left() - len(running))
                if room > 0:
                    for url, depth, sec, etag, lm in self.next_batch(room):
                        self.state.x("UPDATE urls SET status='fetching' WHERE url=?", (url,))
                        running[pool.submit(self.fetch_page, url, depth, etag, lm)] = (url, depth)
                if not running:
                    break
                done, _ = wait(running, timeout=2, return_when=FIRST_COMPLETED)
                for fut in done:
                    url, depth = running.pop(fut)
                    self.handle_result_safely(fut.result(), url, depth)
                if time.monotonic() - last_report > 1.5:
                    last_report = time.monotonic()
                    self.state.commit()
                    self.pages_file.flush()
                    p = self.ui.phases["Crawling pages"]
                    self.ui.set_total("Crawling pages", p["done"] + self.crawl_total(running))
                    to_render = self.state.count("status='render'")
                    self.ui.note("Crawling pages", f"{self.a.workers} workers" +
                                 (f" · {to_render:,} waiting for the browser" if to_render else ""))
            for fut in list(running):
                url, depth = running.pop(fut)
                try:
                    res = fut.result()
                except Exception:
                    self.state.x("UPDATE urls SET status='queued' WHERE url=?", (url,))
                    continue
                self.handle_result_safely(res, url, depth)
        self.state.commit()
        self.pages_file.flush()
        self.queue_size = self.state.count("status='queued'")

    def handle_result_safely(self, res, url, depth):
        """One bad page (odd markup, a file that can't be written) must never stop a long crawl."""
        try:
            self.handle_result(res, depth)
        except Exception as e:
            self.stats["errors"] += 1
            self.state.x("UPDATE urls SET status='failed', error=? WHERE url=?", (f"save error: {e}"[:300], url))
            self.ui.event("err", url, "save error")

    def handle_result(self, res, depth):
        url = res["url"]
        if res.get("stopped"):  # interrupted before download: back in the queue for --resume
            self.state.x("UPDATE urls SET status='queued' WHERE url=?", (url,))
            return
        self.ui.advance("Crawling pages")
        if res.get("blocked"):  # the site refused us (403/429 or a bot-check page): record it, never work around it
            self.stats["errors"] += 1
            self.state.x("UPDATE urls SET status='failed', error=?, http=? WHERE url=?",
                         (f"blocked by the site (HTTP {res.get('http')})", res.get("http"), url))
            self.ui.event("err", url, f"blocked (HTTP {res.get('http')})")
            self.blocked_streak += 1
            if self.blocked_streak >= 8 and not self.stop.is_set():
                warn("This site is refusing automated access, so the crawl stops here. "
                     "Check whether it offers an official API or data export.")
                self.site_blocked = True
                self.stop.set()
            return
        if "error" not in res:
            self.blocked_streak = 0
        if "error" in res:
            self.stats["errors"] += 1
            self.consecutive_errors += 1
            self.ui.event("err", url, res["error"][:40])
            self.state.x("UPDATE urls SET status='failed', error=?, http=? WHERE url=?",
                         (res["error"], res.get("http"), url))
            if self.consecutive_errors >= 20 and not self.stop.is_set():
                warn("20 errors in a row; pausing 60 s so the site can recover")
                self.consecutive_errors = 0
                self.limiter.slow_down(60)
            return
        self.consecutive_errors = 0
        if res.get("not_modified"):
            self.state.x("UPDATE urls SET status='done' WHERE url=?", (url,))
            self.stats["not_modified"] += 1
            self.ui.event("ok", url, "unchanged")
            return
        if "asset" in res:
            self.state.x("UPDATE urls SET status='asset' WHERE url=?", (url,))
            self.add_asset(url, res["asset"], "")
            self.ui.event("file", url, f"{res['asset']} → download queue")
            return
        rec = res["record"]
        if not res["render"] and self.can_render() and rec["words"] < 150:
            # Thin page identical to another URL's = a template shell that JavaScript fills in
            h = hashlib.sha1(clean(rec["body_markdown"]).lower().encode()).hexdigest()
            if h in self.thin_hashes and self.thin_hashes[h] != url:
                res["render"] = True
                prev = self.thin_hashes[h]
                self.state.x("UPDATE urls SET status='render' WHERE url=? AND status='done' AND rendered=0", (prev,))
            else:
                self.thin_hashes[h] = url
        if res["render"]:
            self.state.x("UPDATE urls SET status='render', http=? WHERE url=?", (res["http"], url))
            self.enqueue(res["links"], depth + 1, 3, page=url)
            self.ui.event("render", url, "needs the browser")
            return
        self.save_page(url, res["record"], depth, res["http"], res["etag"], res["last_modified"])
        self.enqueue(res["links"], depth + 1, 3, page=url)

    # ── browser render phase ──
    def render_phase(self):
        rows = self.state.q("SELECT url, depth FROM urls WHERE status='render'")
        if not rows or self.stop.is_set():
            return 0
        self.ui.phase("Rendering JS", total=len(rows), note=f"{self.a.render_tabs} browser tabs")
        try:
            return asyncio.run(self.render_all(rows))
        except KeyboardInterrupt:
            self.stop.set()
            return 0

    async def render_all(self, rows):
        added = 0
        api_index = open(self.out / "api" / "api_index.csv", "a", encoding="utf-8", newline="")
        api_csv = csv.writer(api_index)
        async with async_playwright() as p:
            browser = None
            for kw in ({"channel": "msedge"}, {"channel": "chrome"}, {}):
                try:
                    browser = await p.chromium.launch(headless=True, **kw)
                    break
                except Exception:
                    continue
            if browser is None:
                self.note_once("No browser available for JavaScript pages (install Edge/Chrome or run "
                               "'python -m playwright install chromium'); saving them without rendering")
                for url, depth in rows:
                    res = await asyncio.to_thread(self.fetch_page, url, depth, None, None)
                    if res.get("record"):
                        res["record"]["rendered"] = False
                        self.save_page(url, res["record"], depth)
                    self.ui.advance("Rendering JS")
                return 0
            ctx = await browser.new_context(user_agent=self.ua, ignore_https_errors=False, accept_downloads=False)

            async def block(route):
                if route.request.resource_type in ("image", "font", "media") or \
                        not self.host_allowed(urlsplit(route.request.url).netloc):
                    await route.abort()
                else:
                    await route.continue_()
            await ctx.route("**/*", block)
            sem = asyncio.Semaphore(self.a.render_tabs)
            done_count = 0

            async def one(url, depth):
                nonlocal added, done_count
                if self.stop.is_set() or self.out_of_time():
                    return
                async with sem:
                    if self.stop.is_set():
                        return
                    self.ui.event("render", url, "rendering…")
                    page = await ctx.new_page()
                    captured = []

                    async def on_response(resp):
                        try:
                            req = resp.request
                            if req.resource_type not in ("xhr", "fetch"):
                                return
                            if "json" not in (resp.headers.get("content-type") or ""):
                                return
                            host = urlsplit(resp.url).netloc
                            if not (self.in_scope(host) or host.endswith(self.domain)):
                                return
                            body = await resp.body()
                            if len(body) > 20 * 1024 * 1024:
                                return
                            captured.append((req.method, resp.url, resp.status, body))
                        except Exception:
                            pass
                    page.on("response", on_response)
                    try:
                        resp = await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
                        try:
                            await page.wait_for_load_state("networkidle", timeout=15_000)
                        except Exception:
                            pass
                        last_h = 0
                        for _ in range(10):
                            h = await page.evaluate("document.body ? document.body.scrollHeight : 0")
                            if h <= last_h:
                                break
                            last_h = h
                            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                            await page.wait_for_timeout(700)
                        for _ in range(5):
                            btn = page.get_by_role("button", name=LOAD_MORE_RE).or_(
                                page.get_by_role("link", name=LOAD_MORE_RE))
                            if await btn.count() == 0 or not await btn.first.is_visible():
                                break
                            try:
                                await btn.first.click(timeout=3000)
                                await page.wait_for_timeout(1500)
                            except Exception:
                                break
                        html = await page.content()
                        final = self.norm(page.url) or url
                        if CHALLENGE_RE.search(html[:30000]) or (resp is not None and resp.status >= 400
                                                                 and len(clean(html)) < 5000):
                            raise RuntimeError(f"blocked by the site (HTTP {resp.status if resp else '?'})")
                    except Exception as e:
                        await page.close()
                        self.state.x("UPDATE urls SET status='failed', error=? WHERE url=?",
                                     (f"render: {e}"[:300], url))
                        self.stats["errors"] += 1
                        self.ui.advance("Rendering JS")
                        self.ui.event("err", url, "render failed")
                        return
                    await page.close()
                links = []
                for method, aurl, status, body in captured:
                    name = fit_name(self.out / "api", f"{slugify(urlsplit(aurl).path.strip('/'), 60)}_"
                                                      f"{hashlib.sha1(aurl.encode()).hexdigest()[:8]}", ".json")
                    (self.out / "api" / name).write_bytes(body)
                    api_csv.writerow(csv_row([aurl, method, status, len(body), url, name]))
                    self.stats["api_calls"] += 1
                    links += self.links_in_json(body[:5_000_000].decode("utf-8", "replace"))
                try:
                    record, more, _ = await asyncio.to_thread(self.extract, final, html, True)
                except Exception as e:
                    self.state.x("UPDATE urls SET status='failed', error=? WHERE url=?", (f"parse: {e}"[:300], url))
                    self.ui.advance("Rendering JS")
                    self.ui.event("err", url, "parse failed")
                    return
                record["url"], record["final_url"] = url, final
                try:
                    self.save_page(url, record, depth)
                except Exception as e:
                    self.state.x("UPDATE urls SET status='failed', error=? WHERE url=?", (f"save error: {e}"[:300], url))
                    self.stats["errors"] += 1
                    self.ui.advance("Rendering JS")
                    self.ui.event("err", url, "save error")
                    return
                self.stats["rendered"] += 1
                self.ui.advance("Rendering JS")
                added += self.enqueue(more + links, depth + 1, 3, page=url)
                done_count += 1
                if done_count % 25 == 0:
                    self.state.commit()

            await asyncio.gather(*(one(u, d) for u, d in rows))
            await browser.close()
        api_index.close()
        self.state.commit()
        return added

    # ── downloads ──
    def download_phase(self):
        rows = self.state.q("SELECT url, kind, page FROM assets WHERE status='queued'")
        if not rows or self.stop.is_set():
            return
        # Only images used on the site's pages (any host, e.g. its CDN) and documents on the site or its CDN.
        kinds = Counter(k for _, k, _ in rows)
        self.ui.phase("Downloading files", total=len(rows),
                      note=f"{kinds['image']:,} images · {kinds['document']:,} documents")
        (self.out / "images").mkdir(exist_ok=True)
        (self.out / "documents").mkdir(exist_ok=True)
        (self.out / "markdown" / "documents").mkdir(parents=True, exist_ok=True)
        cap = int(self.a.max_file_mb * 1024 * 1024)
        seen_hashes = {r[0] for r in self.state.q("SELECT path FROM assets WHERE status='done'")}

        def dl(url, kind):
            if self.stop.is_set() or self.out_of_time():
                return url, "queued", None, 0, "stopped"
            if self.bytes_written > self.a.max_gb * 1024 ** 3:
                return url, "skipped", None, 0, "--max-gb reached"
            try:
                r = self.get(url, timeout=60, stream=True)
                if r.status_code >= 400:
                    return url, "failed", None, 0, f"HTTP {r.status_code}"
                size = int(r.headers.get("Content-Length") or 0)
                if size > cap:
                    r.close()
                    return url, "skipped", None, size, f"larger than {self.a.max_file_mb} MB"
                buf = io.BytesIO()
                for chunk in r.iter_content(65536):
                    buf.write(chunk)
                    if buf.tell() > cap:
                        r.close()
                        return url, "skipped", None, buf.tell(), f"larger than {self.a.max_file_mb} MB"
                data = buf.getvalue()
            except StopCrawl:
                return url, "queued", None, 0, "stopped"
            except Exception as e:
                return url, "failed", None, 0, str(e)[:200]
            if kind == "image" and len(data) < 2048:
                return url, "skipped", None, len(data), "tiny image (tracking pixel/spacer)"
            digest = hashlib.sha1(data).hexdigest()
            ext = ext_of(url)
            if not re.fullmatch(r"\.[a-z0-9]{1,5}", ext):  # never "x.png:stream" (NTFS alternate data stream)
                ext = ""
            ext = ext or {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
                                  "image/svg+xml": ".svg", "image/gif": ".gif", "application/pdf": ".pdf"}.get(
                r.headers.get("Content-Type", "").split(";")[0].strip(), "")
            if kind == "image":
                rel = f"images/{digest[:16]}{ext}"
            else:
                stem = slugify(Path(urlsplit(url).path).stem, 70)
                rel = "documents/" + fit_name(self.out / "documents", f"{stem}_{digest[:6]}", ext)
            if rel in seen_hashes:
                return url, "done", rel, len(data), "duplicate content"
            seen_hashes.add(rel)
            (self.out / rel).write_bytes(data)
            self.bytes_written += len(data)
            if ext == ".pdf" and PdfReader is not None:
                try:
                    reader = PdfReader(io.BytesIO(data))
                    text = "\n\n".join((pg.extract_text() or "") for pg in reader.pages[:300])
                    if text.strip():
                        md_dir = self.out / "markdown" / "documents"
                        md = md_dir / fit_name(md_dir, Path(rel).stem, ".md")
                        md.write_text(f"# {Path(urlsplit(url).path).name}\n\nURL: {url}\n\n{text}\n", encoding="utf-8")
                except Exception:
                    pass
            return url, "done", rel, len(data), None

        n, got = 0, Counter()
        with ThreadPoolExecutor(self.a.workers) as pool:
            futs = [pool.submit(dl, u, k) for u, k, _ in rows]
            kind_of = {u: k for u, k, _ in rows}
            for fut in as_completed(futs):
                try:
                    url, status, rel, size, err = fut.result()
                except KeyboardInterrupt:
                    self.stop.set()
                    break
                self.state.x("UPDATE assets SET status=?, path=?, bytes=?, error=? WHERE url=?",
                             (status, rel, size, err, url))
                n += 1
                self.ui.advance("Downloading files")
                if status == "done":
                    self.stats["downloads"] += 1
                    got[kind_of[url]] += 1
                    got["bytes"] += size
                    self.ui.event("file", url, f"{size / 1024:,.0f} KB")
                elif status == "failed":
                    self.ui.event("err", url, err or "failed")
                if n % 20 == 0:
                    self.ui.note("Downloading files", f"{got['image']:,} images · {got['document']:,} documents · "
                                                      f"{got['bytes'] / 1048576:,.1f} MB")
                if n % 100 == 0:
                    self.state.commit()
        self.ui.finish("Downloading files", f"{got['image']:,} images · {got['document']:,} documents · "
                                            f"{got['bytes'] / 1048576:,.1f} MB")
        self.state.commit()

    # ── export ──
    def tick(self, doing):
        """One export step done; show what comes next."""
        self.ui.advance("Saving workbook")
        self.ui.note("Saving workbook", doing)

    def export(self):
        self.pages_file.close()
        self.skipped_file.close()
        self.ui.phase("Saving workbook", total=13, note="contacts.json")
        out = self.out
        # contacts
        contacts = {k: [{"value": v, "pages": sorted(p)[:50]} for v, p in sorted(self.contacts[k].items())]
                    for k in ("emails", "phones", "socials")}
        contacts["addresses"] = list(self.contacts["addresses"].values())
        (out / "contacts.json").write_text(json.dumps(contacts, indent=2, ensure_ascii=False), encoding="utf-8")
        self.tick("structured data files")
        # structured jsonl -> json
        types = {}
        for f in sorted((out / "structured").glob("*.jsonl")):
            rows = [json.loads(l) for l in open(f, encoding="utf-8") if l.strip()]
            seen, uniq = set(), []
            for r in rows:
                key = json.dumps({k: v for k, v in r.items() if k != "_page"}, sort_keys=True, default=str)
                if key not in seen:
                    seen.add(key)
                    uniq.append(r)
            types[f.stem] = len(uniq)
            f.with_suffix(".json").write_text(json.dumps(uniq, indent=2, ensure_ascii=False, default=str),
                                              encoding="utf-8")
        self.tick("site_map.csv")
        # site map / failures
        with open(out / "site_map.csv", "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["url", "status", "http", "depth", "section", "title", "words", "rendered", "lastmod",
                        "canonical", "markdown", "found_on", "note"])
            for row in self.state.db.execute("SELECT url,status,http,depth,section,title,words,rendered,lastmod,"
                                             "canonical,md_path,found_on,error FROM urls ORDER BY section, url"):
                w.writerow(csv_row(row))
        self.tick("failed_urls.csv")
        with open(out / "failed_urls.csv", "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["url", "error", "http"])
            for row in self.state.db.execute("SELECT url,error,http FROM urls WHERE status='failed'"):
                w.writerow(csv_row(row))
        self.tick("removing menu/footer lines repeated across pages")
        self.stats["boilerplate_lines"] = self.strip_boilerplate()
        self.tick("Excel: page sheets")
        self.write_excel(types)
        # Drop folders this site produced nothing for (no tables, no API data, …), deepest first
        for d in sorted((p for p in self.out.rglob("*") if p.is_dir()), key=lambda p: -len(p.parts)):
            if not any(d.iterdir()):
                d.rmdir()
        self.ui.finish("Saving workbook", "workbook, JSON, CSV and Markdown written")
        return contacts, types

    def latest_records(self):
        """Stream pages.jsonl, keeping only the newest record per URL (resume/refresh appends)."""
        path = self.out / "pages.jsonl"
        last = {}
        with open(path, "rb") as f:
            pos = 0
            for line in f:
                m = re.match(rb'\{"url": "((?:[^"\\]|\\.)*)"', line)
                if m:
                    last[m.group(1)] = pos
                pos += len(line)
        keep = set(last.values())
        with open(path, "rb") as f:
            pos = 0
            for line in f:
                if pos in keep:
                    yield json.loads(line)
                pos += len(line)

    def write_excel(self, types):
        wb = Workbook(write_only=True)
        font = Font(name="Segoe UI", size=10)
        hfont = Font(name="Segoe UI", size=10, bold=True, color="FFFFFF")
        fill = PatternFill("solid", fgColor="1F3864")
        sheets = {}

        def sheet(name, headers, widths, part=1):
            base = re.sub(r"[\[\]\*\?/\\:]", "-", name)[:28]
            ws = wb.create_sheet(base if part == 1 else f"{base[:24]} ({part})")
            for i, w in enumerate(widths, 1):
                ws.column_dimensions[get_column_letter(i)].width = w
            ws.freeze_panes = "A2"
            cells = []
            for h in headers:
                c = WriteOnlyCell(ws, value=safe_cell(h))
                c.font, c.fill = hfont, fill
                cells.append(c)
            ws.append(cells)
            sheets[name] = {"ws": ws, "rows": 1, "headers": headers, "widths": widths, "part": part}
            return name

        def add(name, values):
            s = sheets[name]
            if s["rows"] >= EXCEL_MAX_ROWS:
                s["ws"].auto_filter.ref = f"A1:{get_column_letter(len(s['headers']))}{s['rows']}"
                sheet(name, s["headers"], s["widths"], s["part"] + 1)
                s = sheets[name]
            row = []
            for v in values:
                v = safe_cell(v)
                c = WriteOnlyCell(s["ws"], value=v)
                if isinstance(v, str) and v.startswith("="):
                    c.data_type = "s"  # scraped text, never a formula (formula injection)
                c.font = font
                row.append(c)
            s["ws"].append(row)
            s["rows"] += 1

        n_pages = self.state.count("status='done'")
        slim = n_pages > 50_000
        page_cols = ["Title", "URL", "Section", "Published", "Author", "Words", "Tags", "Description", "Rendered"]
        page_w = [55, 60, 16, 12, 20, 8, 30, 60, 9]
        if not slim:
            page_cols.append("Body (Markdown)")
            page_w.append(100)

        summary = sheet("Summary", ["Item", "Value"], [34, 90])
        sheet("Pages", page_cols, page_w)
        sec_counts = Counter(dict(self.state.q("SELECT section, COUNT(*) FROM urls WHERE status='done' "
                                               "GROUP BY section")))
        sec_sheets = {s for s, c in sec_counts.most_common(30) if c >= 5 and s != "home"}
        for s in sorted(sec_sheets):
            sheet("§ " + s, page_cols, page_w)
        for i, rec in enumerate(self.latest_records(), 1):
            vals = [rec.get("title"), rec.get("url"), rec.get("section"), str(rec.get("date_published") or "")[:10],
                    rec.get("author"), rec.get("words"), rec.get("tags"), rec.get("description"),
                    "yes" if rec.get("rendered") else ""]
            if not slim:
                vals.append(rec.get("body_markdown"))
            add("Pages", vals)
            if rec.get("section") in sec_sheets:
                add("§ " + rec["section"], vals)
            if i % 250 == 0:
                self.ui.note("Saving workbook", f"Excel: page sheets · {i:,} / {n_pages:,} rows")
        self.tick("Excel: structured data sheets")

        # structured types
        for t, cnt in sorted(types.items(), key=lambda x: -x[1])[:20]:
            data = json.loads((self.out / "structured" / f"{t}.json").read_text(encoding="utf-8"))
            keys = list(dict.fromkeys(k for r in data[:300] for k in r if k not in ("@context", "@type")))[:30]
            if not keys:
                continue
            name = sheet("◆ " + t, keys, [30] * len(keys))
            for r in data:
                add(name, [r.get(k) for k in keys])
        self.tick("Excel: tables index")

        sheet("Tables", ["File", "Page", "Rows", "Cols", "Caption", "Columns"], [50, 60, 8, 8, 40, 80])
        for rec in self.latest_records():
            for t in rec.get("tables") or []:
                add("Tables", [t["file"], rec["url"], t["rows"], t["cols"], t["caption"], t["columns"]])
        self.tick("Excel: contacts")

        sheet("Contacts", ["Type", "Value", "Found on (first pages)"], [12, 60, 100])
        for kind in ("emails", "phones", "socials"):
            for v, pages in sorted(self.contacts[kind].items()):
                add("Contacts", [kind[:-1], v, sorted(pages)[:10]])
        for a in self.contacts["addresses"].values():
            add("Contacts", ["address", a["value"], a["pages"][:10]])
        self.tick("Excel: documents and images")

        for kind, title in (("document", "Documents"), ("image", "Images")):
            sheet(title, ["URL", "Saved as", "Bytes", "Status", "Note", "Found on", "Alt / link text"],
                  [70, 45, 10, 10, 30, 60, 40])
            # everything downloaded/failed/skipped, plus at most 1,000 files still waiting
            for row in self.state.db.execute("SELECT url,path,bytes,status,error,page,alt FROM assets WHERE kind=? "
                                             "AND status<>'queued' UNION ALL SELECT * FROM (SELECT url,path,bytes,"
                                             "status,error,page,alt FROM assets WHERE kind=? AND status='queued' "
                                             "LIMIT 1000)", (kind, kind)):
                add(title, list(row))
        self.tick("Excel: API calls")

        idx = self.out / "api" / "api_index.csv"
        sheet("API calls", ["API URL", "Method", "HTTP", "Bytes", "Seen on page", "Saved as"], [80, 8, 6, 10, 60, 50])
        if idx.exists():
            for row in csv.reader(open(idx, encoding="utf-8")):
                add("API calls", row)
        self.tick("Excel: failures")

        sheet("Failed", ["URL", "Error", "HTTP"], [80, 60, 8])
        for row in self.state.db.execute("SELECT url,error,http FROM urls WHERE status='failed'"):
            add("Failed", list(row))
        self.tick("Excel: summary and saving the file")

        st = dict(self.state.q("SELECT status, COUNT(*) FROM urls GROUP BY status"))
        facts = [("Website", self.root), ("Scraped on", datetime.now().strftime("%Y-%m-%d %H:%M")),
                 ("Pages saved", st.get("done", 0)), ("Still queued (budget/time)", st.get("queued", 0)),
                 ("Rendered with browser", self.state.count("rendered=1 AND status='done'")),
                 ("Duplicates skipped", st.get("duplicate", 0)), ("Soft 404s skipped", st.get("soft404", 0)),
                 ("Failed", st.get("failed", 0)), ("URLs from sitemaps", self.stats["sitemap_urls"]),
                 ("Tables", sum(1 for _ in (self.out / "tables").glob("*.csv"))),
                 ("Structured record types", ", ".join(f"{k} ({v})" for k, v in sorted(types.items(),
                                                                                       key=lambda x: -x[1])[:15])),
                 ("API responses captured", len(list((self.out / "api").glob("*.json")))),
                 ("Emails / phones / socials", f"{len(self.contacts['emails'])} / {len(self.contacts['phones'])} / "
                                               f"{len(self.contacts['socials'])}"),
                 ("Sections", ", ".join(f"{s} ({c})" for s, c in sec_counts.most_common(25))),
                 ("Repeated menu/footer lines removed", self.stats["boilerplate_lines"]),
                 ("Files not queued (over --max-files)", self.stats["files_over_cap"]),
                 ("Body text in workbook", "no: over 50,000 pages, see pages.jsonl / markdown" if slim else "yes")]
        for k, v in facts:
            add(summary, [k, v])
        for s in sheets.values():
            s["ws"].auto_filter.ref = f"A1:{get_column_letter(len(s['headers']))}{max(s['rows'], 2)}"
        wb.save(self.out / f"{self.site_name} Content.xlsx")
        self.ui.advance("Saving workbook")

# Changelog

## 0.1.0 (beta), 2026-10-30

First public release. Requires Python 3.10 or newer.

- Finds pages from sitemaps (indexes and `.gz` included), RSS/Atom feeds, links, pagination and language versions.
- Fetches with 8 parallel workers and a polite rate limit per host. Honours robots.txt and `Crawl-delay`.
- Renders JavaScript-built pages in headless Edge or Chrome (Playwright). Records the JSON the pages load and reads `__NEXT_DATA__`/`__NUXT__` state.
- Extracts the main text (trafilatura, with a fallback), structured data (JSON-LD, Microdata, OpenGraph), tables, documents (PDF text included), images, contact details and social profiles.
- Cleans the output: removes duplicate pages, menu and footer lines repeated across pages, and pages that say "not found" despite loading normally.
- Saves results as JSONL, Markdown, CSV and an Excel workbook, and removes empty folders.
- Handles large sites: crawl state in SQLite, pages spread across sections, crawler-trap limits, `--max-time`, `--max-gb`, `--max-files`, `--resume`.
- Shows a live terminal dashboard (rich), with plain progress lines as a fallback.
- Respects refusals: when a site refuses access (HTTP 403/429 or a bot-check page), the crawl stops politely.
- Security:
  - scraped text can't turn into Excel formulas, and CSV cells are protected the same way;
  - requests to private and local network addresses are refused;
  - downloads and decompression are size-capped;
  - unsafe file extensions are rejected.

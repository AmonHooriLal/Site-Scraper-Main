"""
Website Scraper
===============
Paste any website URL and this saves all of its public content (pages, articles, case studies, tables, documents, images, contacts and the site's own API data) as JSON, Markdown, CSV and an Excel workbook.

Usage:
    python site_scraper.py https://example.com
    python site_scraper.py https://example.com --max-pages 5000 --workers 8
    python site_scraper.py https://example.com --resume
    python site_scraper.py                       (asks for the URL)
"""

__version__ = "0.1.0"

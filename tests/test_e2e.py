"""End-to-end: run the real command line against the offline fixture site."""
import csv
import json
import sqlite3

from openpyxl import load_workbook

from conftest import run_cli


def output_folder(out):
    folders = [p for p in out.iterdir() if p.is_dir()]
    assert len(folders) == 1, folders
    return folders[0]


def test_full_scrape(site, tmp_path):
    r = run_cli(site + "/", tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    folder = output_folder(tmp_path)
    assert ":" not in folder.name  # "127.0.0.1:port" must become a valid folder name

    db = sqlite3.connect(folder / ".state.db")
    status = dict(db.execute("SELECT url, status FROM urls").fetchall())
    pages = {json.loads(l)["url"] for l in open(folder / "pages.jsonl", encoding="utf-8")}
    assert site + "/blog/post-1" in pages or site + "/blog/post-1.html" in pages
    assert any(u.endswith("/data/table.html") for u in pages)
    assert sum(1 for s in status.values() if s == "duplicate") == 1       # post-2 copy
    assert any(u.endswith("/missing.html") and s == "soft404" for u, s in status.items())
    assert not any("/private/" in u for u in status)                     # robots.txt respected

    post1 = next(json.loads(l) for l in open(folder / "pages.jsonl", encoding="utf-8") if "post-1" in l)
    assert post1["author"] == "Jane Writer" and post1["date_published"].startswith("2026-01-10")

    # tables: formulas defused in CSV
    tables = list((folder / "tables").glob("*.csv"))
    assert len(tables) == 1
    cells = [c for row in csv.reader(open(tables[0], encoding="utf-8-sig")) for c in row]
    assert "'=HYPERLINK(\"http://example.com\",\"click\")" in cells and "'@risk" in cells

    # workbook: the "=1+1" title is text, never a formula
    wb = load_workbook(next(folder.glob("*.xlsx")))
    hits = [c for c in wb["Pages"]["A"] if isinstance(c.value, str) and c.value.startswith("=1+1")]
    assert hits and all(c.data_type == "s" for c in hits)

    # contacts: real profile kept, issue link dropped
    contacts = json.loads((folder / "contacts.json").read_text(encoding="utf-8"))
    assert [e["value"] for e in contacts["emails"]] == ["hello@acmewidgets.org"]
    socials = " ".join(s["value"] for s in contacts["socials"])
    assert "linkedin.com/company/example-co" in socials and "github.com/example-co" in socials
    assert "issues" not in socials

    # downloads: own image and PDF saved; private-network image refused
    assets = {u: (s, e) for u, s, e in db.execute("SELECT url, status, error FROM assets")}
    assert assets[site + "/img/logo.png"][0] == "done"
    assert assets[site + "/docs/report.pdf"][0] == "done"
    evil = assets.get("http://10.255.255.1/evil.png")
    assert evil and evil[0] == "failed" and "private" in (evil[1] or "")

    # no empty folders left behind
    assert not [d for d in folder.rglob("*") if d.is_dir() and not any(d.iterdir())]


def test_resume_continues(site, tmp_path):
    assert run_cli(site + "/", tmp_path, "--max-pages", "2").returncode == 0
    folder = output_folder(tmp_path)
    first = sqlite3.connect(folder / ".state.db").execute("SELECT COUNT(*) FROM urls WHERE status='done'").fetchone()[0]
    assert run_cli(site + "/", tmp_path, "--resume", "--max-pages", "20").returncode == 0
    assert output_folder(tmp_path) == folder
    after = sqlite3.connect(folder / ".state.db").execute("SELECT COUNT(*) FROM urls WHERE status='done'").fetchone()[0]
    assert after > first


def test_site_that_refuses_access_stops_politely(blocked_site, tmp_path):
    r = run_cli(blocked_site + "/", tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "refusing automated access" in r.stdout
    db = sqlite3.connect(output_folder(tmp_path) / ".state.db")
    failed = db.execute("SELECT COUNT(*) FROM urls WHERE status='failed'").fetchone()[0]
    queued = db.execute("SELECT COUNT(*) FROM urls WHERE status='queued'").fetchone()[0]
    assert failed >= 8 and queued > 0  # stopped instead of hammering every page

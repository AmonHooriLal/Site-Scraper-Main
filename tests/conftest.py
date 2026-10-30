"""A tiny offline website served on 127.0.0.1 for end-to-end tests (no internet needed)."""
import http.server
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

SITE = Path(__file__).parent / "fixtures" / "site"


class Handler(http.server.SimpleHTTPRequestHandler):
    block_pages = False  # True: every page answers 403, like a site that refuses bots

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(SITE), **kwargs)

    def log_message(self, *args):
        pass

    def do_GET(self):
        base = f"http://{self.headers['Host']}"
        path = self.path.split("?")[0]
        if self.block_pages and path not in ("/robots.txt", "/sitemap.xml"):
            return self.reply(403, b"Forbidden", "text/plain")
        if self.block_pages and path == "/sitemap.xml":
            urls = "".join(f"<url><loc>{base}/page-{i}.html</loc></url>" for i in range(20))
            return self.reply(200, f'<?xml version="1.0"?><urlset>{urls}</urlset>'.encode(), "application/xml")
        file = SITE / path.lstrip("/")
        if path.endswith((".xml", ".txt")) and file.exists():  # fill in the server address
            return self.reply(200, file.read_text(encoding="utf-8").replace("{{BASE}}", base).encode(),
                              "application/xml" if path.endswith(".xml") else "text/plain")
        return super().do_GET()

    def reply(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def serve(block_pages=False):
    handler = type("H", (Handler,), {"block_pages": block_pages})
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.fixture(scope="session")
def site():
    (SITE / "img").mkdir(exist_ok=True)
    (SITE / "img" / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n" + os.urandom(4000))
    server = serve()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest.fixture()
def blocked_site():
    server = serve(block_pages=True)
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def run_cli(url, out, *extra, timeout=180):
    env = {**os.environ, "NO_COLOR": "1", "PYTHONIOENCODING": "utf-8"}
    return subprocess.run([sys.executable, "-m", "site_scraper", url, "--yes", "--no-art", "--plain", "--no-render",
                           "--delay", "0", "--workers", "4", "--out", str(out), *extra],
                          capture_output=True, text=True, encoding="utf-8", errors="replace", env=env,
                          timeout=timeout, cwd=Path(__file__).parent.parent)

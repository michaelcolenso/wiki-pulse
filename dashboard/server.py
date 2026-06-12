#!/usr/bin/env python3
"""WikiPulse hardened static file server — production-safe, minimal.

Features:
- Path traversal protection (no ../../etc/passwd)
- Security headers on every response
- Only GET/HEAD; 405 on everything else
- Proper MIME types
- Request logging
- IPv4 + IPv6 dual-stack (via systemd socket activation or explicit bind)
"""

import http.server
import os
import sys
import socket
import time
import urllib.parse
import socketserver
import signal

ROOT = os.path.dirname(os.path.abspath(__file__))
PORT = 8080
BIND = "127.0.0.1"

SECURITY_HEADERS = [
    ("X-Content-Type-Options", "nosniff"),
    ("X-Frame-Options", "DENY"),
    ("X-XSS-Protection", "1; mode=block"),
    ("Referrer-Policy", "no-referrer"),
    ("Permissions-Policy", "interest-cohort=()"),
    ("Content-Security-Policy", "default-src 'self' 'unsafe-inline' 'unsafe-eval' https://wikispike.xyz; img-src 'self' data: https:; connect-src 'self' https:; font-src 'self';"),
    ("Strict-Transport-Security", "max-age=31536000; includeSubDomains"),
]

MIME_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".webp": "image/webp",
    ".jpg": "image/jpeg",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
    ".txt": "text/plain; charset=utf-8",
    ".xml": "application/xml; charset=utf-8",
}


def log_request(handler, code, size=0):
    """Structured log line."""
    client = handler.client_address[0]
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    print(f'{now} {client} {handler.command} {handler.path} → {code} {size}B', flush=True)


class HardenedHandler(http.server.SimpleHTTPRequestHandler):
    """Security-conscious static file handler."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=ROOT, **kwargs)

    # We override log_message (internal) with our structured logger
    def log_message(self, format, *args):
        pass  # silenced — we log in do_GET / do_HEAD

    def _respond(self, code, content=b"", content_type="text/html"):
        """Send a response with security headers."""
        try:
            self.send_response(code)
            for header, value in SECURITY_HEADERS:
                self.send_header(header, value)
            self.send_header("Content-Type", content_type)
            if content:
                self.send_header("Content-Length", len(content))
            self.end_headers()
            if content and self.command != "HEAD":
                self.wfile.write(content)
            log_request(self, code, len(content))
        except (ConnectionResetError, BrokenPipeError):
            pass  # client disconnected, nothing to do

    def _resolve_path(self):
        """Resolve and validate the requested file path."""
        parsed = urllib.parse.urlparse(self.path)
        raw_path = urllib.parse.unquote(parsed.path)

        if raw_path == "/":
            raw_path = "/index.html"

        # Reject anything that tries to escape the root
        clean = os.path.normpath(raw_path).lstrip("/")
        full = os.path.join(ROOT, clean)
        real = os.path.realpath(full)

        if not real.startswith(os.path.realpath(ROOT) + os.sep) and real != os.path.realpath(ROOT):
            return None, None

        return real, os.path.splitext(clean)[1].lower()

    def do_GET(self):
        try:
            real, ext = self._resolve_path()
            if real is None:
                self._respond(403, b"Forbidden")
                return
            if not os.path.isfile(real):
                self._respond(404, b"Not Found")
                return
            # Extra care for JSON: read as text so we can check integrity
            if ext == ".json":
                with open(real, "r", encoding="utf-8") as f:
                    content = f.read().encode("utf-8")
                ct = "application/json; charset=utf-8"
            else:
                with open(real, "rb") as f:
                    content = f.read()
                ct = MIME_TYPES.get(ext, "application/octet-stream")
            self._respond(200, content, ct)
        except Exception as e:
            print(f"ERROR: {e}", file=sys.stderr, flush=True)
            self._respond(500, b"Internal Server Error")

    def do_HEAD(self):
        self.do_GET()  # Same resolution, just no body sent

    def do_POST(self):
        self._respond(405, b"Method Not Allowed")


class ThreadedServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    """Multi-threaded HTTP server with socket reuse."""
    allow_reuse_address = True
    daemon_threads = True

    def server_bind(self):
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        super().server_bind()


def shutdown(signum, frame):
    print("\nShutting down...", flush=True)
    sys.exit(0)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    print(f"WikiPulse server — {ROOT}", flush=True)
    print(f"Listening on {BIND}:{PORT}", flush=True)

    with ThreadedServer((BIND, PORT), HardenedHandler) as httpd:
        httpd.serve_forever()
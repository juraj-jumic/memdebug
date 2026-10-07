"""A local, read-only web server for the ledger.

Threat model: the page shows UNTRUSTED text (an attacker can plant anything in a memory), and it runs
on a machine where other programs, other users and web pages in your browser can all reach
127.0.0.1. Defences, in the order a request meets them:

  1. The server listens on 127.0.0.1 only. On Windows it takes the port EXCLUSIVELY, because Windows
     would otherwise let another program share the same port.
  2. At most 64 connections and 16 requests at once, and a 10 second limit per connection
     (slow-connection attacks).
  3. Only GET and HEAD. Anything else is refused. Connections are never kept alive.
  4. The request line is checked: no absolute URLs, control characters, non-ASCII or oversized paths.
  5. The Host header must be exactly 127.0.0.1:PORT or localhost:PORT. This is what stops "DNS
     rebinding", where a web page in your browser tricks the browser into talking to this server
     under an attacker's hostname.
  6. Requests that the browser marks as coming from another site (Sec-Fetch-Site) or from another
     origin (Origin) are refused.
  7. A random 192-bit secret, shown only in the link printed in your terminal, is required. It is
     moved into an HttpOnly, SameSite=Strict cookie on first use and removed from the address.
  8. Query values are validated against strict patterns; anything unexpected is a 400.
  9. The ledger is opened read-only for each request: it can be neither changed nor upgraded here.
 10. Pages are built so untrusted text is always escaped and contain no scripts; the response
     headers forbid scripts, inline styles, framing, caching and referrers anyway.
 11. Errors never show paths, tracebacks or request input. The log never contains the secret.
"""
from __future__ import annotations

import hmac
import http.server
import logging
import os
import re
import secrets
import socket
import socketserver
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

from ..agents import scan_agents
from ..errors import LedgerError, SnapshotError
from ..ledger import Ledger, VerifyResult
from ..models import Op, Trust
from ..textsafe import safe_text
from . import pages
from .style import CSS

logger = logging.getLogger("memdebug.viewer")

LOOPBACK = "127.0.0.1"
MAX_PATH = 2048
MAX_QUERY_FIELDS = 12
MAX_CONCURRENT = 16
MAX_BODY_BYTES = 8 * 1024 * 1024
VERIFY_CACHE_SECONDS = 10.0
REQUEST_TIMEOUT_SECONDS = 10
MAX_CONNECTIONS = 64
LEDGER_BUSY_TIMEOUT_SECONDS = 5.0

CSP = ("default-src 'none'; style-src 'self'; base-uri 'none'; form-action 'self'; "
       "frame-ancestors 'none'; img-src 'none'; script-src 'none'")
SECURITY_HEADERS = (
    ("Content-Security-Policy", CSP),
    ("X-Content-Type-Options", "nosniff"),
    ("X-Frame-Options", "DENY"),
    ("Referrer-Policy", "no-referrer"),
    ("Cache-Control", "no-store"),
    ("Cross-Origin-Resource-Policy", "same-origin"),
    ("Cross-Origin-Opener-Policy", "same-origin"),
    ("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=(), usb=()"),
)

_ID_RE = {
    "event": re.compile(r"^e[1-9][0-9]{0,11}\Z"),
    "snapshot": re.compile(r"^s[1-9][0-9]{0,8}\Z"),
    "number": re.compile(r"^[1-9][0-9]{0,11}\Z"),
    "page": re.compile(r"^[1-9][0-9]{0,5}\Z"),
    "flag": re.compile(r"^1\Z"),
}
_ROUTES = [
    ("overview", re.compile(r"^/\Z")),
    ("timeline", re.compile(r"^/timeline\Z")),
    ("event", re.compile(r"^/event/(e[1-9][0-9]{0,11})\Z")),
    ("snapshots", re.compile(r"^/snapshots\Z")),
    ("snapshot", re.compile(r"^/snapshot/(s[1-9][0-9]{0,8})\Z")),
    ("diff", re.compile(r"^/diff\Z")),
    ("agents", re.compile(r"^/agents\Z")),
    ("integrity", re.compile(r"^/integrity\Z")),
    ("style", re.compile(r"^/style\.css\Z")),
    ("theme", re.compile(r"^/theme\Z")),
]
_NEXT_KEYS = frozenset({"op", "trust", "before", "event", "page", "from", "to", "full"})  # the only inputs a page reads
_NEXT_VALUE = re.compile(r"^[A-Za-z0-9_\-]{1,32}\Z")
_NEXT_TEXT = re.compile(r"^/(?!/)[A-Za-z0-9_\-./?=&%:~+,]{0,300}\Z")
_THEME_MODES = ("auto", "light", "dark")
THEME_MAX_AGE = 365 * 24 * 3600
_TOKEN_IN_TEXT = re.compile(r"token=[^&\s\"']*")


class BadRequest(Exception):
    """Input that failed validation. The message is generic; request input is never echoed."""


@dataclass
class _VerifyCache:
    result: VerifyResult
    counts: dict
    checked_at: datetime
    taken: float


class ViewerState:
    def __init__(self, ledger_path: Path, port: int, token: str):
        self.ledger_path = ledger_path
        self.token = token
        self.token_bytes = token.encode("ascii")
        self.allowed_hosts = {f"{LOOPBACK}:{port}", f"localhost:{port}"}
        self.allowed_origins = {f"http://{host}" for host in self.allowed_hosts}
        self.cookie_name = f"mdv_{port}"
        self.theme_cookie_name = f"mdt_{port}"
        self.slots = threading.BoundedSemaphore(MAX_CONCURRENT)
        self.verify_lock = threading.Lock()
        self.verify_cache: _VerifyCache | None = None
        self.context = pages.Context(ledger_name=ledger_path.name)


class ViewerServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False  # never share the port
    request_queue_size = 16

    def __init__(self, ledger_path: str | Path, port: int = 0, token: str | None = None):
        path = Path(ledger_path)
        Ledger.open_readonly(path).close()  # fail early, before binding anything
        if not (isinstance(port, int) and 0 <= port <= 65535):
            raise ValueError("port must be between 0 and 65535")
        self.token = token or secrets.token_urlsafe(24)
        self._connection_slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
        super().__init__((LOOPBACK, port), _Handler)
        self.state = ViewerState(path, self.server_address[1], self.token)

    def process_request(self, request, client_address) -> None:
        # One thread per connection would let idle connections pile up; beyond the cap, hang up at once.
        if not self._connection_slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        super().process_request(request, client_address)

    def process_request_thread(self, request, client_address) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._connection_slots.release()

    def server_bind(self) -> None:
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            # Without this, Windows lets another program bind the same port and receive requests.
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        socketserver.TCPServer.server_bind(self)  # not HTTPServer's: it does a slow name lookup
        self.server_name, self.server_port = LOOPBACK, self.socket.getsockname()[1]

    @property
    def port(self) -> int:
        return self.server_address[1]

    @property
    def url(self) -> str:
        return f"http://{LOOPBACK}:{self.port}/?token={self.token}"


class _Handler(http.server.BaseHTTPRequestHandler):
    server_version = "memdebug-viewer"
    sys_version = ""
    protocol_version = "HTTP/1.0"  # one request per connection
    timeout = REQUEST_TIMEOUT_SECONDS

    # -- logging that never records the secret or raw input ------------------------------------------------

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        text = _TOKEN_IN_TEXT.sub("token=...", format % args if args else format)
        logger.info("%s", safe_text(text, 200))

    def log_request(self, code="-", size="-") -> None:
        path = (getattr(self, "path", "") or "").split("?", 1)[0]
        logger.info("%s %s -> %s", safe_text(getattr(self, "command", "-"), 10), safe_text(path, 100), code)

    # -- responses -----------------------------------------------------------------------------------------------

    def _send(self, status: int, body: bytes, content_type: str, extra: tuple = ()) -> None:
        self.send_response(status)
        for name, value in SECURITY_HEADERS:
            self.send_header(name, value)
        for name, value in extra:
            self.send_header(name, value)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        if getattr(self, "command", None) != "HEAD":
            self.wfile.write(body)

    def _text(self, status: int, message: str, extra: tuple = ()) -> None:
        self._send(status, (message + "\n").encode("utf-8"), "text/plain; charset=utf-8", extra)

    def send_error(self, code, message=None, explain=None) -> None:  # replaces the default HTML page
        self._text(code, f"Error {code}")

    def _page(self, page: pages.Page) -> None:
        body = page.html.encode("utf-8")
        if len(body) > MAX_BODY_BYTES:
            return self._text(500, "That page is too large to show.")
        self._send(page.status, body, "text/html; charset=utf-8")

    # -- methods -----------------------------------------------------------------------------------------------------

    def do_GET(self) -> None:
        self._dispatch()

    do_HEAD = do_GET

    def _refuse_method(self) -> None:
        self._text(405, "Method not allowed. This viewer is read-only.", (("Allow", "GET, HEAD"),))

    do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _refuse_method

    def _dispatch(self) -> None:
        state: ViewerState = self.server.state  # type: ignore[attr-defined]
        if not state.slots.acquire(blocking=False):
            return self._text(503, "Busy. Try again in a moment.", (("Retry-After", "2"),))
        try:
            self._handle(state)
        except (ConnectionError, TimeoutError):  # includes Windows' ConnectionAbortedError
            pass  # the browser went away
        except BadRequest:
            self._text(400, "Bad request.")
        except Exception as exc:  # never show details; the class name is enough for the log
            logger.error("unexpected %s while handling a request", type(exc).__name__)
            try:
                self._text(500, "Something went wrong.")
            except OSError:
                pass
        finally:
            state.slots.release()

    # -- request checks ---------------------------------------------------------------------------------------------------

    def _cookie(self, name: str) -> str | None:
        for header in (self.headers.get_all("Cookie") or [])[:3]:
            for part in header.split(";"):
                key, _, value = part.strip().partition("=")
                if key == name:
                    return value
        return None

    def _token_ok(self, supplied: str | None, state: ViewerState) -> bool:
        if supplied is None:
            return False
        return hmac.compare_digest(supplied.encode("utf-8", "replace"), state.token_bytes)

    def _handle(self, state: ViewerState) -> None:
        raw = self.path
        # Python may rewrite a leading "//" in self.path (and older versions do not), so the target is
        # also read from the original request line: every version then answers the same way.
        parts = self.requestline.split(" ")
        original = parts[1] if len(parts) >= 3 else raw
        if (len(raw) > MAX_PATH or not raw.startswith("/") or raw.startswith("//") or original.startswith("//")
                or any(ord(c) < 33 or ord(c) > 126 for c in raw)):
            raise BadRequest
        hosts = self.headers.get_all("Host") or []
        if len(hosts) != 1 or hosts[0].strip().lower() not in state.allowed_hosts:
            return self._text(421, "Misdirected request.")
        fetch_site = self.headers.get("Sec-Fetch-Site")
        if fetch_site is not None and fetch_site.strip().lower() not in ("same-origin", "none"):
            return self._text(403, "Cross-site requests are not allowed.")
        origin = self.headers.get("Origin")
        if origin is not None and origin.strip().lower() not in state.allowed_origins:
            return self._text(403, "Cross-origin requests are not allowed.")
        try:
            split = urlsplit(raw)
            query = parse_qs(split.query, keep_blank_values=False, max_num_fields=MAX_QUERY_FIELDS)
        except ValueError:
            raise BadRequest from None
        path = split.path

        token_in_url = query.pop("token", [None])[0]
        if token_in_url is not None:
            if not self._token_ok(token_in_url, state):
                return self._text(403, "Access denied. Open the full link printed by 'memdebug serve'.")
            return self._redirect_without_token(path, query, state)
        if not self._token_ok(self._cookie(state.cookie_name), state):
            return self._text(403, "Access denied. Open the full link printed by 'memdebug serve'.")

        self.ctx = self._request_context(state, path, query)
        route = next(((name, m) for name, rx in _ROUTES if (m := rx.match(path))), None)
        if route is None:
            return self._page(pages.error_page(self.ctx, 404, "Not found", "There is nothing at that address."))
        self._route(route[0], route[1], query, state)

    @staticmethod
    def _local_target(path: str, query: dict) -> str:
        """A page address rebuilt from parts that are known to be safe: a real page, and only the inputs that page
        reads, each matching a strict pattern. Anything else is dropped, so this can never point elsewhere."""
        if not any(rx.match(path) for name, rx in _ROUTES if name not in ("theme", "style")):
            return "/"
        keep = {k: v[0] for k, v in query.items() if k in _NEXT_KEYS and v and _NEXT_VALUE.match(v[0])}
        return pages.url(path, **keep)

    def _request_context(self, state: ViewerState, path: str, query: dict) -> pages.Context:
        chosen = self._cookie(state.theme_cookie_name)
        return pages.Context(ledger_name=state.context.ledger_name, theme=chosen if chosen in ("light", "dark") else "auto",
                             here=self._local_target(path, query))

    def _set_theme(self, query: dict, state: ViewerState) -> None:
        modes = query.get("mode") or []
        if len(modes) != 1 or modes[0] not in _THEME_MODES:
            raise BadRequest
        target = "/"
        supplied = (query.get("next") or [None])[0]
        if supplied is not None and _NEXT_TEXT.match(supplied):
            try:
                where = urlsplit(supplied)
                target = self._local_target(where.path, parse_qs(where.query, max_num_fields=MAX_QUERY_FIELDS))
            except ValueError:
                target = "/"
        name = state.theme_cookie_name
        cookie = (f"{name}=; Max-Age=0; HttpOnly; SameSite=Strict; Path=/" if modes[0] == "auto"
                  else f"{name}={modes[0]}; Max-Age={THEME_MAX_AGE}; HttpOnly; SameSite=Strict; Path=/")
        self._send(303, b"", "text/plain; charset=utf-8", (("Location", target), ("Set-Cookie", cookie)))

    def _redirect_without_token(self, path: str, query: dict, state: ViewerState) -> None:
        known = any(rx.match(path) for _, rx in _ROUTES)
        target = path if known else "/"
        remaining = [(k, v) for k, vals in query.items() for v in vals[:1]] if known else []
        location = target + (f"?{urlencode(remaining)}" if remaining else "")
        cookie = f"{state.cookie_name}={state.token}; HttpOnly; SameSite=Strict; Path=/"
        self._send(303, b"", "text/plain; charset=utf-8", (("Location", location), ("Set-Cookie", cookie)))

    # -- routing and input validation -----------------------------------------------------------------------------------------

    @staticmethod
    def _param(query: dict, name: str, kind: str) -> str | None:
        values = query.get(name)
        if not values:
            return None
        if len(values) != 1 or not _ID_RE[kind].match(values[0]):
            raise BadRequest
        return values[0]

    def _route(self, name: str, match: re.Match, query: dict, state: ViewerState) -> None:
        if name == "style":
            return self._send(200, CSS.encode("utf-8"), "text/css; charset=utf-8")
        if name == "theme":
            return self._set_theme(query, state)
        if name == "event":
            target = pages.url("/timeline", event=match.group(1))
            return self._send(303, b"", "text/plain; charset=utf-8", (("Location", target),))
        if name == "agents":  # looks at the home folder, not the ledger, so it works while the ledger is busy
            return self._page(pages.agents_page(self.ctx, scan_agents()))
        op = trust = None
        if name == "timeline":
            op = (query.get("op") or [None])[0]
            trust = (query.get("trust") or [None])[0]
            if (len(query.get("op", [])) > 1 or len(query.get("trust", [])) > 1
                    or (op is not None and op not in {o.value for o in Op})
                    or (trust is not None and trust not in {t.value for t in Trust})):
                raise BadRequest
        before = self._param(query, "before", "number") if name == "timeline" else None
        event = self._param(query, "event", "event") if name == "timeline" else None
        page = self._param(query, "page", "page") if name == "snapshot" else None
        old = self._param(query, "from", "snapshot") if name == "diff" else None
        new = self._param(query, "to", "snapshot") if name == "diff" else None
        full = self._param(query, "full", "flag") == "1" if name == "diff" else False

        try:
            ledger = Ledger.open_readonly(state.ledger_path, busy_timeout=LEDGER_BUSY_TIMEOUT_SECONDS)
        except LedgerError as exc:
            return self._page(pages.error_page(
                self.ctx, 503, "The ledger cannot be read right now",
                f"{exc} If a sync is running, wait a moment and reload."))
        try:
            result = self._render(name, ledger, state, op=op, trust=trust, before=before and int(before),
                                  event=event, page=int(page or 1), old=old, new=new, full=full,
                                  snapshot_id=match.group(1) if name == "snapshot" else None)
        except SnapshotError as exc:
            missing = "does not exist" in str(exc)
            result = pages.error_page(
                self.ctx, 404 if missing else 500, "Snapshot not found" if missing else "Integrity problem", str(exc))
        except LedgerError as exc:
            result = pages.error_page(self.ctx, 500, "The ledger could not be read", str(exc))
        except sqlite3.Error:
            result = pages.error_page(self.ctx, 503, "The ledger is busy", "Reload in a moment.")
        finally:
            ledger.close()
        self._page(result)

    def _render(self, name, ledger: Ledger, state: ViewerState, **p) -> pages.Page:
        ctx = self.ctx
        if name == "overview":
            return pages.overview(ledger, ctx)
        if name == "timeline":
            return pages.timeline(ledger, ctx, op=p["op"], trust=p["trust"], before=p["before"], event_id=p["event"])
        if name == "snapshots":
            return pages.snapshots(ledger, ctx)
        if name == "snapshot":
            return pages.snapshot_detail(ledger, ctx, p["snapshot_id"], p["page"])
        if name == "diff":
            return pages.diff_page(ledger, ctx, p["old"], p["new"], p["full"])
        return self._integrity(ledger, state)

    def _integrity(self, ledger: Ledger, state: ViewerState) -> pages.Page:
        with state.verify_lock:  # one full check at a time; a recent result is reused
            cached = state.verify_cache
            if cached is None or time.monotonic() - cached.taken > VERIFY_CACHE_SECONDS:
                cached = _VerifyCache(ledger.verify(), ledger.counts(), datetime.now(timezone.utc), time.monotonic())
                state.verify_cache = cached
        return pages.integrity(self.ctx, cached.counts, cached.result, cached.checked_at)


def serve(ledger_path: str | Path, port: int, announce, open_browser: bool = False) -> None:
    """Run until interrupted. `announce(url)` is called once the server is listening."""
    server = ViewerServer(ledger_path, port)
    try:
        announce(server.url)
        if open_browser:
            import webbrowser

            webbrowser.open(server.url)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

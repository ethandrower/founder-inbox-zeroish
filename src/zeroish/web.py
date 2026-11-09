"""ABOUTME: `zeroish ui` — an optional local web page to edit the config, try rules, check health and sign in.
ABOUTME: Binds 127.0.0.1 only and every API call needs the per-launch token. The JSON file stays the source of truth."""
from __future__ import annotations

import copy
import glob
import json
import os
import secrets
import shutil
import threading
import time
import webbrowser
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources

from . import classify, config, engine, graph, rules

MAX_BODY = 2_000_000
KEEP_BACKUPS = 10
NON_BLOCKING = ("categories with no rule",)  # lint notes that are fine to save with


# ── the logic the page calls (plain functions, tested without a server) ──
def check(raw: dict) -> dict:
    """Validate a config without saving it. ok=False means it must not be saved."""
    try:
        cfg = config.normalise(copy.deepcopy(raw))
    except (config.ConfigError, KeyError, TypeError, ValueError, AttributeError) as e:
        return {"ok": False, "error": str(e), "problems": [], "notes": [], "prompt": None}
    found = rules.lint(cfg["rules"], set(cfg["categories"]))
    problems = [p for p in found if not p.startswith(NON_BLOCKING)]
    return {"ok": not problems, "error": None, "problems": problems,
            "notes": [p for p in found if p.startswith(NON_BLOCKING)], "prompt": classify.build_prompt(cfg)}


def simulate(raw: dict, f: dict, pass_name: str = "all") -> dict:
    """Which rule would fire for an email with these facts, under this (possibly unsaved) config."""
    cfg = config.normalise(copy.deepcopy(raw))
    facts = rules.Facts(sender_kind=f.get("sender", "unknown"), age_h=float(f.get("age_h", 0)),
                        read=bool(f.get("read")), replied=f.get("replied", "never"))
    content = {"category": f.get("category", "other"), "confidence": float(f.get("confidence", 0.95)),
               "reason": "simulated"}
    asked = []

    def classify_fn():
        asked.append(True)
        return content

    d = rules.evaluate(cfg["rules"], facts, pass_name, classify_fn, cfg.get("stale"))
    return {"action": d.action, "rule": d.rule, "to": d.to, "why": d.why if d.why != "simulated" else "",
            "classified": bool(asked), "trace": [{"rule": r, "result": why} for r, why in d.trace]}


def save(path: str, raw: dict, expected_mtime: float | None) -> dict:
    """Write the config if it checks out and nobody changed the file meanwhile. Keeps the last backups."""
    result = check(raw)
    if not result["ok"]:
        return {"saved": False, **result}
    if os.path.exists(path):
        if expected_mtime is not None and abs(os.path.getmtime(path) - expected_mtime) > 1e-6:
            return {"saved": False, "conflict": True,
                    "error": "the config file changed on disk since this page loaded it; reload first"}
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        shutil.copy2(path, f"{path}.bak-{stamp}")
        for old in sorted(glob.glob(f"{glob.escape(path)}.bak-*"))[:-KEEP_BACKUPS]:
            os.remove(old)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        fh.write(json.dumps(raw, indent=2, ensure_ascii=False) + "\n")
    os.replace(tmp, path)
    return {"saved": True, "mtime": os.path.getmtime(path), **result}


def starter() -> dict:
    """The example config, for a first run with no config file yet."""
    return json.loads(resources.files("zeroish").joinpath("config.example.json").read_text())


def read_raw(path: str) -> tuple[dict | None, str | None, float | None]:
    """(config, error, mtime). A broken file is reported, not fatal: fixing it is what the page is for."""
    if not os.path.exists(path):
        return None, None, None
    mtime = os.path.getmtime(path)
    try:
        with open(path) as fh:
            return json.load(fh), None, mtime
    except ValueError as e:
        return None, f"{path} is not valid JSON: {e}", mtime


def auth_info(raw: dict | None) -> dict:
    a = (raw or {}).get("auth") or {}
    mailbox = ((raw or {}).get("owner") or {}).get("mailbox", "").lower()
    if not a.get("tenant") or not a.get("client_id"):
        return {"configured": False, "user": None, "matches": False}
    user = graph.Auth(a["tenant"], a["client_id"], a.get("token_path", "~/.config/zeroish/token.json")).stored_user()
    return {"configured": True, "user": user, "matches": bool(user) and user == mailbox}


# ── HTTP ──────────────────────────────────────────────────────────────
class App:
    def __init__(self, path: str, port: int):
        self.path, self.port, self.token = path, port, secrets.token_urlsafe(24)
        self.device: dict | None = None   # an in-progress Microsoft sign-in
        self.lock = threading.Lock()

    def hosts(self) -> set[str]:
        return {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}

    def state(self) -> dict:
        raw, err, mtime = read_raw(self.path)
        return {"path": self.path, "exists": os.path.exists(self.path), "config": raw, "load_error": err,
                "mtime": mtime, "check": check(raw) if raw else None, "auth": auth_info(raw),
                "schema": {"sender_kinds": list(rules.SENDER_KINDS), "replied": list(rules.REPLIED),
                           "passes": list(rules.PASSES), "when_keys": sorted(rules.WHEN_KEYS)}}

    def route(self, method: str, route: str, body: dict) -> tuple[int, dict]:
        if method == "GET" and route == "/api/state":
            return 200, self.state()
        if method == "GET" and route == "/api/starter":
            return 200, {"config": starter()}
        if method == "POST" and route == "/api/check":
            return 200, check(body["config"])
        if method == "POST" and route == "/api/simulate":
            try:
                return 200, simulate(body["config"], body.get("facts") or {}, body.get("pass", "all"))
            except (config.ConfigError, ValueError, TypeError, KeyError) as e:
                return 400, {"error": f"fix the config first: {e}"}
        if method == "POST" and route == "/api/save":
            with self.lock:
                out = save(self.path, body["config"], body.get("mtime"))
            return (200 if out["saved"] else 409 if out.get("conflict") else 400), out
        if method == "GET" and route == "/api/status":
            try:
                return 200, engine.status(config.load(self.path))
            except config.ConfigError as e:
                return 400, {"error": str(e)}
        if method == "POST" and route == "/api/auth/start":
            raw, err, _ = read_raw(self.path)
            info = auth_info(raw)
            if err or not info["configured"]:
                return 400, {"error": "save a config with auth.tenant and auth.client_id first"}
            a = raw["auth"]
            au = graph.Auth(a["tenant"], a["client_id"], a.get("token_path", "~/.config/zeroish/token.json"))
            dc = au.device_start()
            self.device = {"dc": dc, "auth": au, "allowed": {raw["owner"]["mailbox"].lower()},
                           "deadline": time.time() + int(dc.get("expires_in", 900))}
            return 200, {"user_code": dc["user_code"], "verification_uri": dc["verification_uri"],
                         "interval": int(dc.get("interval", 5)), "expires_in": int(dc.get("expires_in", 900))}
        if method == "POST" and route == "/api/auth/poll":
            dev = self.device
            if not dev:
                return 400, {"error": "no sign-in in progress"}
            if time.time() > dev["deadline"]:
                self.device = None
                return 200, {"state": "expired"}
            try:
                user = dev["auth"].device_poll(dev["dc"], dev["allowed"], graph.whoami)
            except graph.GraphError as e:
                self.device = None
                return 200, {"state": "failed", "error": str(e)}
            if not user:
                return 200, {"state": "pending"}
            self.device = None
            return 200, {"state": "signed_in", "user": user}
        return 404, {"error": "not found"}


def make_handler(app: App):
    page = resources.files("zeroish").joinpath("ui.html").read_text()

    class Handler(BaseHTTPRequestHandler):
        server_version = "zeroish"

        def log_message(self, fmt, *args):  # quiet: the terminal shows only the URL and errors
            pass

        def _send(self, code: int, payload, ctype="application/json"):
            data = payload.encode() if isinstance(payload, str) else json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", f"{ctype}; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                             "connect-src 'self'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(data)

        def _handle(self, method: str):
            if self.headers.get("Host") not in app.hosts():  # blocks DNS-rebinding pages
                return self._send(HTTPStatus.FORBIDDEN, {"error": "bad host"})
            route = self.path.split("?", 1)[0]
            if method == "GET" and route in ("/", "/index.html"):
                return self._send(200, page, "text/html")
            if not route.startswith("/api/"):
                return self._send(404, {"error": "not found"})
            if not secrets.compare_digest(self.headers.get("X-Zeroish-Token", ""), app.token):
                return self._send(HTTPStatus.FORBIDDEN, {"error": "missing or wrong token; open the URL zeroish printed"})
            body = {}
            if method == "POST":
                n = int(self.headers.get("Content-Length") or 0)
                if n > MAX_BODY:
                    return self._send(413, {"error": "too large"})
                try:
                    body = json.loads(self.rfile.read(n) or b"{}")
                except ValueError:
                    return self._send(400, {"error": "body is not JSON"})
            try:
                code, out = app.route(method, route, body)
            except (graph.GraphError, OSError, KeyError) as e:
                code, out = 500, {"error": str(e)}
            self._send(code, out)

        def do_GET(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

    return Handler


def serve(path: str, port: int = 8765, open_browser: bool = True):
    app = App(path, port)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app))
    url = f"http://127.0.0.1:{port}/#t={app.token}"
    print(f"zeroish ui: {url}\n  config: {path}\n  Ctrl-C to stop. Editing the JSON file directly still works.",
          flush=True)
    if open_browser:
        threading.Timer(0.3, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()

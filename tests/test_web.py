"""ABOUTME: Tests for the optional `zeroish ui` web page: check, simulate, save (backups, conflicts) and the HTTP guards.
ABOUTME: Run: uv run python -m unittest discover -s tests"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from zeroish import web

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
with open(os.path.join(ROOT, "examples", "config.example.json")) as fh:
    RAW = json.load(fh)


def raw(**over):
    r = json.loads(json.dumps(RAW))
    r.update(over)
    return r


class Logic(unittest.TestCase):
    def test_packaged_example_matches_examples_folder(self):
        self.assertEqual(web.starter(), RAW)  # src/zeroish/config.example.json must be a copy of examples/

    def test_check_accepts_the_example_and_builds_the_prompt(self):
        out = web.check(raw())
        self.assertTrue(out["ok"], out)
        self.assertIn("vendor_pitch", out["prompt"])

    def test_check_blocks_an_unreachable_rule_and_a_broken_config(self):
        r = raw()
        r["rules"].insert(0, {"id": "everything", "when": {}, "then": "Archive"})
        out = web.check(r)
        self.assertFalse(out["ok"])
        self.assertTrue(any("can never match" in p for p in out["problems"]))
        broken = raw()
        broken["rules"][0]["when"] = {"colour": "red"}
        self.assertFalse(web.check(broken)["ok"])

    def test_simulate_shows_the_deciding_rule(self):
        out = web.simulate(raw(), {"sender": "unknown", "category": "vendor_pitch", "age_h": 1})
        self.assertEqual((out["action"], out["rule"], out["to"]), ("move", "cold-outreach", "Cold Outreach"))
        out = web.simulate(raw(), {"sender": "priority", "category": "vendor_pitch"})
        self.assertEqual((out["action"], out["rule"], out["classified"]), ("keep", "priority", False))

    def test_simulate_applies_the_backstop(self):
        out = web.simulate(raw(), {"sender": "unknown", "category": "prospect", "age_h": 31 * 24}, "cleanup")
        self.assertEqual((out["rule"], out["to"]), ("stale", "Archive"))

    def test_save_writes_backs_up_and_refuses_conflicts_and_bad_configs(self):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "config.json")
        first = web.save(path, raw(), None)
        self.assertTrue(first["saved"])
        r = raw()
        r["stale"]["after_days"] = 21
        second = web.save(path, r, first["mtime"])
        self.assertTrue(second["saved"])
        with open(path) as fh:
            self.assertEqual(json.load(fh)["stale"]["after_days"], 21)
        self.assertEqual(len([f for f in os.listdir(d) if ".bak-" in f]), 1)
        stale = web.save(path, raw(), first["mtime"] - 100)
        self.assertTrue(stale.get("conflict"))
        bad = raw()
        bad["categories"].pop("other")
        self.assertFalse(web.save(path, bad, second["mtime"])["saved"])
        with open(path) as fh:
            self.assertEqual(json.load(fh)["stale"]["after_days"], 21)  # untouched


class Http(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp()
        cls.path = os.path.join(cls.dir, "config.json")
        web.save(cls.path, raw(), None)
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), web.BaseHTTPRequestHandler)
        port = cls.httpd.server_address[1]
        cls.app = web.App(cls.path, port)
        cls.httpd.RequestHandlerClass = web.make_handler(cls.app)
        cls.base = f"http://127.0.0.1:{port}"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def call(self, route, body=None, token=True, host=None):
        req = urllib.request.Request(self.base + route, method="GET" if body is None else "POST",
                                     data=None if body is None else json.dumps(body).encode())
        if token:
            req.add_header("X-Zeroish-Token", self.app.token)
        if host:
            req.add_header("Host", host)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def test_page_is_served_and_api_needs_the_token(self):
        code, page = self.call("/", token=False)
        self.assertEqual(code, 200)
        self.assertIn("Inbox Zeroish", page)
        self.assertEqual(self.call("/api/state", token=False)[0], 403)
        code, body = self.call("/api/state")
        self.assertEqual(code, 200)
        state = json.loads(body)
        self.assertEqual(state["config"]["owner"]["mailbox"], RAW["owner"]["mailbox"])
        self.assertTrue(state["check"]["ok"])

    def test_foreign_host_header_is_refused(self):
        self.assertEqual(self.call("/api/state", host="evil.example:80")[0], 403)

    def test_simulate_and_save_round_trip(self):
        code, body = self.call("/api/simulate", {"config": raw(), "facts": {"category": "newsletter"}})
        self.assertEqual((code, json.loads(body)["to"]), (200, "Newsletters"))
        mtime = json.loads(self.call("/api/state")[1])["mtime"]
        code, body = self.call("/api/save", {"config": raw(), "mtime": mtime})
        self.assertEqual(code, 200, body)
        code, _ = self.call("/api/save", {"config": raw(), "mtime": 1.0})
        self.assertEqual(code, 409)


if __name__ == "__main__":
    unittest.main()

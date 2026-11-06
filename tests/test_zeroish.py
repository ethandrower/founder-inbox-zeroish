"""ABOUTME: Tests for rules, facts, config, classifier plumbing and the plan/apply engine (fake mailbox, fake LLM).
ABOUTME: Run: uv run python -m unittest discover -s tests"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from zeroish import classify, config, engine, facts, rules

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
with open(os.path.join(ROOT, "examples", "config.example.json")) as fh:
    RAW = json.load(fh)
NOW = datetime(2026, 11, 6, 18, 0, tzinfo=timezone.utc)


def cfg_with(**over):
    raw = json.loads(json.dumps(RAW))
    raw.update(over)
    tmp = tempfile.mkdtemp()
    raw["audit_log"] = os.path.join(tmp, "audit.jsonl")
    raw.setdefault("classifier", {})["cache_path"] = os.path.join(tmp, "cache.json")
    return config.normalise(raw)


CFG = cfg_with()


def fake(category, confidence=0.95):
    return lambda: {"category": category, "confidence": confidence, "reason": "fake"}


def boom():
    raise AssertionError("classifier called")


def F(sender="unknown", age=1.0, read=False, replied="never"):
    return rules.Facts(sender_kind=sender, age_h=age, read=read, replied=replied)


class Config(unittest.TestCase):
    def test_example_config_loads_and_lints_clean(self):
        problems = rules.lint(CFG["rules"], set(CFG["categories"]))
        self.assertEqual([p for p in problems if not p.startswith("categories with no rule")], [])

    def test_unknown_category_in_rule_is_rejected(self):
        raw = json.loads(json.dumps(RAW))
        raw["rules"].append({"id": "x", "when": {"category": "spam"}, "then": "Junk"})
        with self.assertRaisesRegex(config.ConfigError, "rule 'x'.*spam"):
            config.normalise(raw)

    def test_unknown_condition_and_bad_pass_are_rejected(self):
        for bad in ({"id": "a", "when": {"colour": "red"}, "then": "X"},
                    {"id": "b", "when": {}, "then": "X", "pass": "hourly"},
                    {"id": "c", "when": {"min_age_h": 5, "max_age_h": 1}, "then": "X"}):
            raw = json.loads(json.dumps(RAW))
            raw["rules"] = [bad]
            with self.assertRaises(config.ConfigError):
                config.normalise(raw)

    def test_other_category_is_required(self):
        raw = json.loads(json.dumps(RAW))
        raw["categories"].pop("other")
        with self.assertRaises(config.ConfigError):
            config.normalise(raw)


class Rules(unittest.TestCase):
    R = CFG["rules"]

    def test_first_match_wins_and_priority_is_never_moved(self):
        for pass_name in ("fast", "cleanup", "all"):
            d = rules.evaluate(self.R, F("priority", age=99, read=True, replied="after_latest"), pass_name, boom)
            self.assertEqual((d.action, d.rule), ("keep", "priority"))

    def test_handled_needs_no_llm_and_only_in_cleanup(self):
        d = rules.evaluate(self.R, F(replied="after_latest"), "cleanup", boom)
        self.assertEqual((d.action, d.to), ("move", "Archive"))
        d = rules.evaluate(self.R, F(replied="after_latest"), "fast", fake("other"))
        self.assertEqual(d.action, "keep")

    def test_action_needed_beats_everything_below_it(self):
        d = rules.evaluate(self.R, F(age=100, read=True), "all", fake("action_needed"))
        self.assertEqual((d.action, d.rule), ("keep", "action-needed"))

    def test_a_reply_settles_even_an_action_request(self):
        d = rules.evaluate(self.R, F(age=100, replied="after_latest"), "cleanup", fake("action_needed"))
        self.assertEqual((d.action, d.rule), ("move", "handled"))

    def test_cold_outreach_fast_only_and_never_from_trusted(self):
        self.assertEqual(rules.evaluate(self.R, F(), "fast", fake("capital_pitch")).to, "Cold Outreach")
        self.assertEqual(rules.evaluate(self.R, F(), "cleanup", fake("capital_pitch")).action, "keep")
        for kind in ("internal", "correspondent"):
            self.assertEqual(rules.evaluate(self.R, F(kind), "fast", fake("vendor_pitch")).action, "keep")

    def test_low_confidence_keeps(self):
        self.assertEqual(rules.evaluate(self.R, F(), "fast", fake("vendor_pitch", 0.6)).action, "keep")

    def test_time_and_read_dimensions(self):
        cases = [(F(age=31), "notification", "keep"), (F(age=33), "notification", "move"),
                 (F(age=23), "digest", "keep"), (F(age=25), "digest", "move"),
                 (F("correspondent", age=30, read=False), "client", "keep"),
                 (F("correspondent", age=30, read=True), "client", "move"),
                 (F(age=30, read=True), "pr_marketing", "move")]
        for facts_, cat, expect in cases:
            self.assertEqual(rules.evaluate(self.R, facts_, "cleanup", fake(cat)).action, expect, (facts_, cat))

    def test_classifier_failure_keeps_and_stops(self):
        def down():
            raise classify.Unavailable("no backend")
        d = rules.evaluate(self.R, F(age=99), "all", down)
        self.assertEqual((d.action, d.rule), ("keep", None))
        self.assertIn("not classified", d.why)

    def test_content_is_classified_once_and_only_when_needed(self):
        calls = []

        def counting():
            calls.append(1)
            return {"category": "other", "confidence": 0.9, "reason": ""}
        rules.evaluate(self.R, F(age=99, read=True), "all", counting)
        self.assertEqual(len(calls), 1)

    def test_trace_names_every_rule_considered(self):
        d = rules.evaluate(self.R, F(age=40), "cleanup", fake("notification"))
        self.assertEqual(d.rule, "notifications")
        self.assertEqual([t[0] for t in d.trace][-1], "notifications")


class Stale(unittest.TestCase):
    R, S = CFG["rules"], CFG["stale"]
    OLD = 31 * 24

    def test_unread_client_mail_is_filed_once_stale(self):
        d = rules.evaluate(self.R, F("correspondent", age=self.OLD, read=False), "cleanup", fake("client"), self.S)
        self.assertEqual((d.action, d.to, d.rule), ("move", "Customer Projects", "customers"))
        fresh = rules.evaluate(self.R, F("correspondent", age=48, read=False), "cleanup", fake("client"), self.S)
        self.assertEqual(fresh.action, "keep")

    def test_no_matching_rule_means_archive(self):
        d = rules.evaluate(self.R, F(age=self.OLD), "cleanup", fake("prospect"), self.S)
        self.assertEqual((d.action, d.to, d.rule), ("move", "Archive", "stale"))

    def test_keep_rules_stop_protecting_after_30_days(self):
        for facts_, cat in ((F("priority", age=self.OLD), "other"), (F(age=self.OLD), "action_needed")):
            d = rules.evaluate(self.R, facts_, "cleanup", fake(cat), self.S)
            self.assertEqual((d.action, d.to), ("move", "Archive"), facts_)

    def test_fast_pass_ignores_staleness(self):
        d = rules.evaluate(self.R, F("priority", age=self.OLD), "fast", fake("other"), self.S)
        self.assertEqual(d.action, "keep")

    def test_classifier_down_keeps_stale_mail_for_next_run(self):
        def down():
            raise classify.Unavailable("x")
        d = rules.evaluate(self.R, F(age=self.OLD), "cleanup", down, self.S)
        self.assertEqual(d.action, "keep")

    def test_bad_stale_config(self):
        for bad in ({"then": "keep"}, {"then": "Archive", "relax": ["category"]}):
            with self.assertRaises(rules.RuleError):
                rules.parse_stale(bad)


class Lint(unittest.TestCase):
    def parse(self, raw_rules):
        return rules.parse(raw_rules, set(RAW["categories"]), 0.85)

    def test_shadowed_rule_is_reported(self):
        rs = self.parse([{"id": "broad", "when": {"category": ["digest", "notification"]}, "then": "Notifications"},
                         {"id": "narrow", "when": {"category": "digest", "min_age_h": 24}, "then": "Notifications"}])
        self.assertTrue(any("'narrow' can never match" in p for p in rules.lint(rs, set(RAW["categories"]))))

    def test_different_passes_do_not_shadow(self):
        rs = self.parse([{"id": "a", "when": {"category": "digest"}, "then": "X", "pass": "fast"},
                         {"id": "b", "when": {"category": "digest"}, "then": "Y", "pass": "cleanup"}])
        self.assertFalse(any("never match" in p for p in rules.lint(rs, set(RAW["categories"]))))

    def test_priority_keep_after_a_move_rule_is_reported(self):
        rs = self.parse([{"id": "promo", "when": {"category": "newsletter"}, "then": "Newsletters"},
                         {"id": "vip", "when": {"sender": "priority"}, "then": "keep"}])
        self.assertTrue(any("'vip' protects priority senders" in p for p in rules.lint(rs, set(RAW["categories"]))))

    def test_missing_folders_reported_online(self):
        problems = rules.lint(CFG["rules"], set(CFG["categories"]), folders={"Archive", "Cold Outreach"})
        self.assertTrue(any("Newsletters" in p for p in problems))


def msg(mid, sender, subject="", received=None, read=False, conv=None, body="", folder="inbox"):
    return {"id": mid, "subject": subject, "bodyPreview": body, "body": {"content": body}, "isRead": read,
            "receivedDateTime": received or (NOW - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "conversationId": conv or f"c-{mid}", "parentFolderId": folder,
            "from": {"emailAddress": {"address": sender, "name": sender.split("@")[0]}}, "toRecipients": []}


class Facts(unittest.TestCase):
    def test_sender_kinds_and_replied(self):
        sent = facts.SentIndex([
            {"toRecipients": [{"emailAddress": {"address": "buyer@customer.example"}}], "conversationId": "t1",
             "sentDateTime": "2026-11-06T12:00:00Z"},
            {"toRecipients": [{"emailAddress": {"address": "friend@gmail.com"}}], "conversationId": "t2",
             "sentDateTime": "2026-11-01T12:00:00Z"}])
        cases = [("mentor@example.org", "priority"), ("ops@yourcompany.com", "internal"),
                 ("ceo@customer.example", "correspondent"), ("friend@gmail.com", "correspondent"),
                 ("stranger@gmail.com", "unknown"), ("rep@vendor.example", "unknown")]
        for sender, kind in cases:
            self.assertEqual(facts.compute(msg("m", sender), CFG, sent, NOW).sender_kind, kind, sender)
        replied = facts.compute(msg("m", "x@y.example", conv="t1", received="2026-11-06T10:00:00Z"), CFG, sent, NOW)
        again = facts.compute(msg("m", "x@y.example", conv="t1", received="2026-11-06T14:00:00Z"), CFG, sent, NOW)
        self.assertEqual((replied.replied, again.replied), ("after_latest", "before_latest"))


class Eval(unittest.TestCase):
    def test_outcome_scoring_separates_harmless_from_harmful(self):
        path = os.path.join(tempfile.mkdtemp(), "labels.jsonl")
        with open(path, "w") as fh:
            for ex in ({"from": "a@x.example", "subject": "promo", "expect": "newsletter", "sender": "unknown"},
                       {"from": "b@x.example", "subject": "deal", "expect": "prospect", "sender": "unknown"},
                       {"from": "c@x.example", "subject": "cpu", "expect": "notification", "sender": "unknown"}):
                fh.write(json.dumps(ex) + "\n")
        answers = iter(["vendor_pitch", "vendor_pitch", "digest"])  # harmless, HARMFUL, harmless

        def clf_fake(mid, text):
            return {"category": next(answers), "confidence": 0.95, "reason": ""}
        out = engine.evaluate_labels(CFG, path, clf_fake)
        self.assertEqual(out["label_accuracy"], 0.0)
        self.assertEqual(out["harmful"], 1)
        bad = [m for m in out["misses"] if m["harmful"]]
        self.assertEqual(bad[0]["subject"], "deal")
        self.assertEqual(bad[0]["would_go"], "Cold Outreach")


class NewCustomers(unittest.TestCase):
    """A customer you've never emailed must still never land in Cold Outreach."""

    def test_colleague_on_thread_makes_it_trusted(self):
        m = msg("m", "newbuyer@brandnew.example", "Question about the proposal")
        m["ccRecipients"] = [{"emailAddress": {"address": "pm@yourcompany.com"}}]
        f = facts.compute(m, CFG, facts.SentIndex([]), NOW)
        self.assertEqual(f.sender_kind, "team_thread")
        d = rules.evaluate(CFG["rules"], f, "fast", fake("vendor_pitch"))
        self.assertEqual(d.action, "keep")

    def test_owner_aliases_are_not_colleagues(self):
        raw = json.loads(json.dumps(RAW))
        raw["owner"]["aliases"] = ["sam@yourcompany.com", "sam.rivera@yourcompany.com"]
        cfg = config.normalise(raw)
        m = msg("m", "rep@leadgen.example", "Quick question")
        m["toRecipients"] = [{"emailAddress": {"address": "Sam.Rivera@yourcompany.com"}}]
        self.assertEqual(facts.compute(m, cfg, facts.SentIndex([]), NOW).sender_kind, "unknown")
        self.assertEqual(rules.evaluate(cfg["rules"], facts.compute(m, cfg, facts.SentIndex([]), NOW), "fast",
                                        fake("vendor_pitch")).to, "Cold Outreach")

    def test_owner_alone_on_to_line_is_not_a_team_thread(self):
        m = msg("m", "rep@leadgen.example")
        m["toRecipients"] = [{"emailAddress": {"address": "you@yourcompany.com"}}]
        self.assertEqual(facts.compute(m, CFG, facts.SentIndex([]), NOW).sender_kind, "unknown")

    def test_known_file_from_crm_sync(self):
        path = os.path.join(tempfile.mkdtemp(), "known.txt")
        with open(path, "w") as fh:
            fh.write("# synced from CRM\nbrandnew.example\nsolo@gmail.com  # a consultant\n")
        raw = json.loads(json.dumps(RAW))
        raw["senders"]["known_file"] = path
        cfg = config.normalise(raw)
        for sender in ("ceo@brandnew.example", "solo@gmail.com"):
            f = facts.compute(msg("m", sender), cfg, facts.SentIndex([]), NOW)
            self.assertEqual(f.sender_kind, "known", sender)
            self.assertEqual(rules.evaluate(cfg["rules"], f, "fast", fake("capital_pitch")).action, "keep")
        raw["senders"]["known_file"] = "/nonexistent/known.txt"
        self.assertEqual(config.normalise(raw)["senders"]["known"], set())  # failed sync never blocks triage


class Classifier(unittest.TestCase):
    def test_prompt_is_built_from_taxonomy_only(self):
        p = classify.build_prompt(CFG)
        for name in CFG["categories"]:
            self.assertIn(f"- {name}:", p)
        self.assertIn("capital_pitch, not investor", p)

    def test_sender_context_line(self):
        m = msg("m", "buyer@customer.example", "Re: quote")
        self.assertIn("known customer or open deal", classify.email_text(m, sender_kind="known"))
        self.assertIn("first contact", classify.email_text(m, sender_kind="unknown"))
        self.assertNotIn("Context", classify.email_text(m))

    def test_parse_reply(self):
        cats = set(CFG["categories"])
        self.assertEqual(classify.parse_reply('ok {"category":"digest","confidence":0.9}', cats)["category"], "digest")
        self.assertEqual(classify.parse_reply('{"category":"spam","confidence":7}', cats),
                         {"category": "other", "confidence": 1.0, "reason": ""})

    def test_unhealthy_backend_is_skipped_without_a_call(self):
        c = classify.Classifier({**CFG, "classifier": {**CFG["classifier"], "backends": [
            {"name": "local", "type": "openai", "url": "http://x", "model": "m", "key_env": "K",
             "health_url": "http://x/status"}]}})
        c.backends[0]._health = "overloaded"
        with mock.patch.object(c.backends[0], "ask", side_effect=AssertionError("called")):
            with self.assertRaisesRegex(classify.Unavailable, "overloaded, skipped"):
                c("m1", "text")

    def test_health_judgement(self):
        j = classify.Backend.judge
        self.assertEqual(j({"models": {"a": {"state": "ok"}}}, "a"), "ok")
        self.assertEqual(j({"models": {"a": {"state": "ok", "validation": {"passed": 0, "total": 8}}}}, "a"),
                         "failing validation (0/8)")
        self.assertEqual(j({"models": {"a": {"state": "busy", "validation": {"passed": 8, "total": 8}}}}, "a"), "busy")
        self.assertEqual(j({"state": "overloaded", "models": {}}, "b"), "overloaded")

    def test_falls_through_to_the_next_backend(self):
        c = classify.Classifier({**CFG, "classifier": {**CFG["classifier"], "backends": [
            {"name": "first", "type": "openai", "url": "http://x", "model": "a", "key_env": "K"},
            {"name": "second", "type": "openai", "url": "http://x", "model": "b", "key_env": "K"}]}})
        c.backends[0]._health = "overloaded"
        c.backends[0].spec["health_url"] = c.backends[1].spec["health_url"] = "http://x/status"
        c.backends[1]._health = "ok"
        with mock.patch.object(c.backends[1], "ask", return_value='{"category":"digest","confidence":0.9}'):
            out = c("m1", "text")
        self.assertEqual((out["category"], out["backend"]), ("digest", "second"))

    def test_changing_the_prompt_invalidates_cached_answers(self):
        path = os.path.join(tempfile.mkdtemp(), "c.json")
        v1 = classify.prompt_version(CFG)
        c = classify.Cache(path, version=v1)
        c.put("m1", {"category": "vendor_pitch", "confidence": 0.9, "reason": ""})
        c.save()
        raw = json.loads(json.dumps(RAW))
        raw["categories"]["notification"]["not"] = "Upwork message alerts are notification."
        v2 = classify.prompt_version(config.normalise(raw))
        self.assertNotEqual(v1, v2)
        self.assertIsNotNone(classify.Cache(path, version=v1).get("m1"))
        self.assertIsNone(classify.Cache(path, version=v2).get("m1"))

    def test_cache(self):
        path = os.path.join(tempfile.mkdtemp(), "c.json")
        cache = classify.Cache(path)
        cache.put("m1", {"category": "digest", "confidence": 0.9, "reason": "r"})
        cache.data["old"] = {"c": {"category": "other", "confidence": 0, "reason": ""}, "at": "2020-01-01T00:00:00+00:00"}
        cache.save()
        again = classify.Cache(path)
        self.assertEqual(again.get("m1")["backend"], "cache")
        self.assertIsNone(again.get("old"))


class FakeMailbox:
    def __init__(self, inbox, sent=()):
        self._inbox = {m["id"]: m for m in inbox}
        self._sent = list(sent)
        self.moves = []
        self._folders = {"inbox": {"name": "Inbox", "parent": None, "path": "Inbox"}}
        for i, name in enumerate(["Archive", "Cold Outreach", "Newsletters", "Notifications", "Customer Projects",
                                  "PR & Marketing", "Deleted Items", "HR"]):
            self._folders[f"f{i}"] = {"name": name, "parent": None, "path": name}
        self._inbox_id = "inbox"

    def inbox(self, since, limit):
        return list(self._inbox.values())[:limit]

    def inbox_page(self, bookmark, limit, newest_first):
        msgs = sorted((m for m in self._inbox.values() if m["parentFolderId"] == "inbox"),
                      key=lambda m: m["receivedDateTime"], reverse=newest_first)
        if bookmark:
            msgs = [m for m in msgs if (m["receivedDateTime"] < bookmark if newest_first
                                        else m["receivedDateTime"] > bookmark)]
        return msgs[:limit]

    def sent_history(self, n):
        return self._sent

    def message(self, mid):
        return self._inbox[mid]

    def folders(self):
        return self._folders

    def protected_ids(self):
        return {fid for fid, f in self._folders.items() if f["name"] == "Deleted Items"}

    def is_protected(self, fid, protected):
        return fid in protected

    def resolve(self, name):
        return next(fid for fid, f in self._folders.items() if f["path"] == name)

    def count_before(self, cutoff):
        return len(self.ids_before(cutoff, 10**6))

    def ids_before(self, cutoff, n):
        return [m["id"] for m in sorted(self._inbox.values(), key=lambda m: m["receivedDateTime"])
                if m["parentFolderId"] == "inbox" and m["receivedDateTime"] < cutoff][:n]

    def batch_move(self, ids, fid):
        for mid in ids:
            self.move(mid, fid)
        return list(ids), {}

    def move(self, mid, fid):
        self.moves.append((mid, self._folders[fid]["path"]))
        self._inbox[mid]["parentFolderId"] = fid
        return mid + "-new"


class Engine(unittest.TestCase):
    def setUp(self):
        old = (NOW - timedelta(hours=40)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.box = FakeMailbox([
            msg("pitch", "rep@leadgen.example", "Quick question"),
            msg("lender", "funds@fastcash.example", "Up to 500k in 48 hours"),
            msg("promo", "news@saasco.example", "Our new feature"),
            msg("alert", "alerts@cloud.example", "CPU high", received=old),
            msg("vip", "partner@bigfund.vc", "Quarterly check-in"),
            msg("hr", "noreply@gusto.com", "Your payroll is ready"),
            msg("done", "buyer@customer.example", "Re: renewal", conv="t-done", received=old, read=True),
        ], sent=[{"toRecipients": [{"emailAddress": {"address": "buyer@customer.example"}}],
                  "conversationId": "t-done", "sentDateTime": (NOW - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")}])
        labels = {"leadgen": "vendor_pitch", "fastcash": "capital_pitch", "saasco": "newsletter",
                  "cloud.example": "notification", "customer.example": "client"}

        def classifier(mid, text):
            return {"category": next(v for k, v in labels.items() if k in text), "confidence": 0.95, "reason": "fake"}
        self.classifier = classifier

    def test_plan_moves_nothing_and_hides_sensitive(self):
        out = engine.plan(CFG, self.box, "all", "x", 50, self.classifier, NOW)
        self.assertEqual(self.box.moves, [])
        self.assertEqual(out["withheld_sensitive"], 1)
        self.assertNotIn("payroll", json.dumps(out).lower())
        got = {r["id"]: (r["to"], r["rule"]) for r in out["to_move"]}
        self.assertEqual(got, {"pitch": ("Cold Outreach", "cold-outreach"), "lender": ("Cold Outreach", "cold-outreach"),
                               "promo": ("Newsletters", "promotions"), "alert": ("Notifications", "notifications"),
                               "done": ("Archive", "handled")})
        self.assertEqual([r["id"] for r in out["priority"]], ["vip"])

    def test_fast_pass_only_moves_outreach_and_promotions(self):
        out = engine.plan(CFG, self.box, "fast", "x", 50, self.classifier, NOW)
        self.assertEqual(sorted(r["id"] for r in out["to_move"]), ["lender", "pitch", "promo"])

    def test_apply_moves_and_audits_rule_ids_without_content(self):
        planned = engine.plan(CFG, self.box, "all", "x", 50, self.classifier, NOW)
        res = engine.apply(CFG, self.box, planned)
        self.assertEqual(res["moved"], 5)
        self.assertEqual(res["by_rule"]["cold-outreach"], 2)
        with open(os.path.expanduser(CFG["audit_log"])) as fh:
            log = fh.read()
        self.assertIn('"rule": "handled"', log)
        self.assertNotIn("Quick question", log)

    def test_apply_refuses_protected_and_sensitive_destinations(self):
        planned = {"to_move": [{"id": "pitch", "to": "Deleted Items", "rule": "x"},
                               {"id": "promo", "to": "HR", "rule": "y"}]}
        res = engine.apply(CFG, self.box, planned)
        self.assertEqual((res["moved"], len(res["refused"])), (0, 2))
        self.assertEqual(self.box.moves, [])

    def test_apply_skips_mail_no_longer_in_inbox(self):
        planned = engine.plan(CFG, self.box, "fast", "x", 50, self.classifier, NOW)
        self.box._inbox["pitch"]["parentFolderId"] = "f0"  # owner already filed it
        res = engine.apply(CFG, self.box, planned)
        self.assertIn("no longer in the inbox", json.dumps(res["refused"]))

    def test_cap_per_run(self):
        cfg = {**CFG, "limits": {**CFG["limits"], "max_moves_per_run": 1}}
        planned = engine.plan(cfg, self.box, "fast", "x", 50, self.classifier, NOW)
        res = engine.apply(cfg, self.box, planned)
        self.assertEqual((res["moved"], res["skipped_over_cap"]), (1, 2))


class Operations(unittest.TestCase):
    """Backlog sweep bookmark, run lock and health status: what makes it safe to put on cron."""

    def setUp(self):
        self.cfg = cfg_with()
        self.box = FakeMailbox([msg(f"m{i}", f"x{i}@somewhere.example", f"s{i}",
                                    received=(NOW - timedelta(days=40 - i)).strftime("%Y-%m-%dT%H:%M:%SZ"))
                                for i in range(5)])
        self.clf = lambda mid, text: {"category": "prospect", "confidence": 0.9, "reason": "r"}

    def test_sweep_walks_back_from_today_and_wraps(self):
        seen = []
        for _ in range(3):
            p = engine.plan(self.cfg, self.box, "cleanup", None, 2, self.clf, NOW, sweep=True)
            seen.append([r["id"] for r in p["to_move"] + p["kept"]])
            engine.commit_sweep(self.cfg, p)
        self.assertEqual(seen, [["m4", "m3"], ["m2", "m1"], ["m0"]])  # newest first, then older
        self.assertIsNone(engine.read_state(self.cfg).get("sweep_bookmark"))  # short page: back to today

    def test_sweep_can_go_oldest_first(self):
        cfg = {**self.cfg, "limits": {**self.cfg["limits"], "sweep_order": "oldest_first"}}
        p = engine.plan(cfg, self.box, "cleanup", None, 2, self.clf, NOW, sweep=True)
        self.assertEqual([r["id"] for r in p["to_move"] + p["kept"]], ["m0", "m1"])

    def test_stale_backlog_is_archived_by_the_sweep(self):
        p = engine.plan(self.cfg, self.box, "cleanup", None, 5, self.clf, NOW, sweep=True)
        self.assertEqual({r["to"] for r in p["to_move"]}, {"Archive"})  # 36–40 days old prospects: stale

    def test_archive_older_is_dry_by_default_then_chunks(self):
        cutoff = (NOW - timedelta(days=37)).strftime("%Y-%m-%dT%H:%M:%SZ")  # m0..m2 are older
        dry = engine.archive_older(self.cfg, self.box, cutoff, "Archive", 2, apply=False)
        self.assertEqual((dry["would_move"], self.box.moves), (3, []))
        first = engine.archive_older(self.cfg, self.box, cutoff, "Archive", 2, apply=True)
        second = engine.archive_older(self.cfg, self.box, cutoff, "Archive", 2, apply=True)
        self.assertEqual((first["moved"], second["moved"], second["remaining_after"]), (2, 1, 0))
        self.assertEqual({to for _, to in self.box.moves}, {"Archive"})

    def test_archive_older_refuses_protected_folder(self):
        with self.assertRaises(PermissionError):
            engine.archive_older(self.cfg, self.box, "2099-01-01T00:00:00Z", "Deleted Items", 10, apply=True)

    def test_lock_blocks_a_second_run(self):
        with engine.RunLock(self.cfg) as first:
            with engine.RunLock(self.cfg) as second:
                self.assertTrue(first)
                self.assertFalse(second)

    def test_status_reports_health(self):
        self.assertFalse(engine.status(self.cfg, now=NOW)["healthy"])  # nothing ran yet
        with mock.patch.object(engine, "datetime") as dt:
            dt.now.return_value = NOW - timedelta(minutes=5)
            dt.fromisoformat = datetime.fromisoformat
            dt.fromtimestamp = datetime.fromtimestamp
            engine.audit(self.cfg, {"cmd": "run", "lane": "new", "ok": True, "scanned": 10, "moved": 3,
                                    "unclassified": 0})
            engine.audit(self.cfg, {"cmd": "move", "to": "Cold Outreach", "rule": "cold-outreach"})
        s = engine.status(self.cfg, now=NOW)
        self.assertTrue(s["healthy"], s["problems"])
        self.assertEqual(s["by_rule"], {"cold-outreach": 1})
        later = engine.status(self.cfg, now=NOW + timedelta(hours=1))
        self.assertFalse(later["healthy"])
        self.assertIn("no new-mail run", later["problems"][0])

    def test_status_flags_failures_and_a_dead_classifier(self):
        engine.audit(self.cfg, {"cmd": "run", "lane": "new", "ok": False, "error": "Graph 401"})
        s = engine.status(self.cfg)
        self.assertIn("last new run failed: Graph 401", s["problems"])
        engine.audit(self.cfg, {"cmd": "run", "lane": "new", "ok": True, "scanned": 10, "unclassified": 9})
        self.assertTrue(any("unclassified" in p for p in engine.status(self.cfg)["problems"]))


if __name__ == "__main__":
    unittest.main()

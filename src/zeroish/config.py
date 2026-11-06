"""ABOUTME: Load and validate the zeroish config (JSON): owner, auth, senders, categories, classifier, rules, sensitive.
ABOUTME: Also the sensitive-mail guard: mail it matches is never classified, moved or listed."""
from __future__ import annotations

import json
import os
import re

from . import rules as rules_mod

DEFAULT_PATH = "~/.config/zeroish/config.json"


class ConfigError(ValueError):
    pass


def config_path(explicit: str | None = None) -> str:
    return os.path.expanduser(explicit or os.environ.get("ZEROISH_CONFIG") or DEFAULT_PATH)


def load(path: str | None = None) -> dict:
    p = config_path(path)
    try:
        with open(p) as fh:
            raw = json.load(fh)
    except FileNotFoundError:
        raise ConfigError(f"no config at {p}; start from examples/config.example.json") from None
    except ValueError as e:
        raise ConfigError(f"{p} is not valid JSON: {e}") from None
    cfg = normalise(raw)
    cfg["_path"] = p
    return cfg


def normalise(raw: dict) -> dict:
    for key in ("owner", "auth", "categories", "rules"):
        if key not in raw:
            raise ConfigError(f"missing section {key!r}")
    owner = raw["owner"]
    if not owner.get("mailbox") or not owner.get("description"):
        raise ConfigError("owner needs 'mailbox' and 'description'")
    cats = raw["categories"]
    if "other" not in cats:
        raise ConfigError("categories must include 'other' (the classifier's fallback)")
    for name, c in cats.items():
        if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
            raise ConfigError(f"category name {name!r}: use lower_snake_case")
        if not isinstance(c, dict) or not c.get("means"):
            raise ConfigError(f"category {name!r} needs a 'means' line")
    clf = raw.get("classifier") or {}
    cfg = {
        "owner": {"mailbox": owner["mailbox"].lower(), "description": owner["description"]},
        "auth": {"tenant": raw["auth"]["tenant"], "client_id": raw["auth"]["client_id"],
                 "token_path": raw["auth"].get("token_path", "~/.config/zeroish/token.json")},
        "senders": {**{k: {s.strip().lower() for s in (raw.get("senders") or {}).get(k, []) if s.strip()}
                       for k in ("priority", "internal", "known")},
                    "known_file": (raw.get("senders") or {}).get("known_file")},
        "categories": cats,
        "classifier": {"backends": clf.get("backends", []), "notes": clf.get("notes", []),
                       "min_confidence": float(clf.get("min_confidence", 0.85)),
                       "timeout_s": float(clf.get("timeout_s", 45)),
                       "cache_path": clf.get("cache_path", "~/.cache/zeroish/classifications.json")},
        "limits": {"max_moves_per_run": int((raw.get("limits") or {}).get("max_moves_per_run", 50)),
                   "sent_lookback": int((raw.get("limits") or {}).get("sent_lookback", 1000)),
                   "scan_limit": int((raw.get("limits") or {}).get("scan_limit", 50))},
        "audit_log": raw.get("audit_log", "~/.local/state/zeroish/audit.jsonl"),
        "sensitive": Sensitive(raw.get("sensitive") or {}),
    }
    cfg["senders"]["known"] |= read_known_file(cfg["senders"]["known_file"])
    try:
        cfg["rules"] = rules_mod.parse(raw["rules"], set(cats), cfg["classifier"]["min_confidence"])
        cfg["stale"] = rules_mod.parse_stale(raw.get("stale"))
    except rules_mod.RuleError as e:
        raise ConfigError(str(e)) from None
    return cfg


def read_known_file(path: str | None) -> set[str]:
    """Known senders written by your own sync job (CRM customers, open deals): one domain or address per line,
    '#' comments allowed. Missing file = empty list, so a failed sync never blocks triage."""
    if not path:
        return set()
    try:
        with open(os.path.expanduser(path)) as fh:
            return {ln.split("#", 1)[0].strip().lower() for ln in fh if ln.split("#", 1)[0].strip()}
    except OSError:
        return set()


class Sensitive:
    """Topics, senders and folders the assistant must never touch (HR, pay, legal, medical, ...).
    Matching mail is withheld: not sent to any LLM, not moved, not listed — only counted."""

    def __init__(self, d: dict):
        terms = sorted({t.strip().lower() for t in d.get("terms", []) if t.strip()}, key=len, reverse=True)
        self.term_re = re.compile(r"(?<![\w])(" + "|".join(re.escape(t) for t in terms) + r")(?![\w])", re.I) \
            if terms else None
        self.domains = {x.strip().lower() for x in d.get("sender_domains", [])}
        self.addresses = {x.strip().lower() for x in d.get("sender_addresses", [])}
        self.folders = {x.strip().lower() for x in d.get("folders", [])}

    def _addr_blocked(self, a: str | None) -> bool:
        a = (a or "").strip().lower()
        if not a:
            return False
        dom = a.rsplit("@", 1)[-1]
        return a in self.addresses or any(dom == d or dom.endswith("." + d) for d in self.domains)

    def folder_blocked(self, name: str | None) -> bool:
        return (name or "").strip().lower() in self.folders

    def reason(self, m: dict, folder_name: str | None = None) -> str | None:
        sender = ((m.get("from") or {}).get("emailAddress") or {}).get("address")
        if self._addr_blocked(sender):
            return "sender"
        recips = [((r or {}).get("emailAddress") or {}).get("address")
                  for r in (m.get("toRecipients") or []) + (m.get("ccRecipients") or [])]
        if any(self._addr_blocked(r) for r in recips):
            return "recipient"
        if self.folder_blocked(folder_name):
            return "folder"
        if self.term_re:
            for text in (m.get("subject"), m.get("bodyPreview"), (m.get("body") or {}).get("content")):
                if text and self.term_re.search(text):
                    return "topic"
        return None

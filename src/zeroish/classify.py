"""ABOUTME: The one content judgement — classify an email into exactly one category from the config's taxonomy.
ABOUTME: The prompt is generated from the taxonomy; rules never carry prompt text. Backends + a per-email cache."""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone


class Unavailable(Exception):
    """No backend could classify. Callers must not move the email."""


def build_prompt(cfg: dict) -> str:
    """System prompt from config: who the mailbox belongs to, the categories (one line each), tie-breakers."""
    cats = cfg["categories"]
    lines = [f"You sort email for {cfg['owner']['description']}. Pick exactly one category:"]
    lines += [f"- {name}: {c['means']}" for name, c in cats.items()]
    notes = [c["not"] for c in cats.values() if c.get("not")] + list(cfg.get("classifier", {}).get("notes", []))
    if notes:
        lines.append("Tie-breakers:")
        lines += [f"- {n}" for n in notes]
    lines.append('If unsure, use "other" with low confidence. Reply with JSON only: '
                 '{"category": "...", "confidence": 0.0-1.0, "reason": "under 15 words"}')
    return "\n".join(lines)


def email_text(m: dict, body_chars: int = 1500) -> str:
    sender = (m.get("from") or {}).get("emailAddress") or {}
    to = [((r or {}).get("emailAddress") or {}).get("address", "") for r in m.get("toRecipients") or []]
    cc = [((r or {}).get("emailAddress") or {}).get("address", "") for r in m.get("ccRecipients") or []]
    body = (m.get("body") or {}).get("content") or m.get("bodyPreview") or ""
    body = re.sub(r"\s+", " ", body)[:body_chars]
    return (f"From: {sender.get('name', '')} <{sender.get('address', '')}>\nTo: {', '.join(to)}\n"
            + (f"Cc: {', '.join(cc)}\n" if cc else "") + f"Subject: {m.get('subject', '')}\n\n{body}")


def parse_reply(content: str, categories) -> dict:
    m = re.search(r"\{.*\}", content or "", re.S)
    if not m:
        raise ValueError("no JSON in reply")
    d = json.loads(m.group(0))
    cat = d.get("category") if d.get("category") in categories else "other"
    try:
        conf = max(0.0, min(float(d.get("confidence", 0)), 1.0))
    except (TypeError, ValueError):
        conf = 0.0
    return {"category": cat, "confidence": conf, "reason": str(d.get("reason", ""))[:120]}


def _post(url, body, headers, timeout):
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


class Backend:
    """One way of asking an LLM. Configured in config.classifier.backends, tried in order."""

    def __init__(self, spec: dict):
        self.spec = spec
        self.kind = spec["type"]          # "openai" (any OpenAI-compatible server) or "anthropic"
        self.name = spec.get("name", self.kind)
        self._health = None

    def key(self):
        return os.environ.get(self.spec.get("key_env", ""), "")

    def healthy(self) -> str | None:
        """Optional health_url returning {"state": ...} or {"models": {model: {"state": ..., "validation":
        {"passed": n, "total": m}}}}. Only "ok" and "busy" get work, and a model whose last validation run had
        failures is skipped even if it says "ok" (healthy is not the same as correct).
        None = usable, or no health check configured."""
        url = self.spec.get("health_url")
        if not url:
            return None
        if self._health is None:
            try:
                req = urllib.request.Request(url, headers={"Authorization": f"Bearer {self.key()}"})
                with urllib.request.urlopen(req, timeout=5) as r:
                    s = json.load(r)
                self._health = self.judge(s, self.spec.get("model"))
            except (urllib.error.URLError, OSError, ValueError):
                self._health = "unreachable"
        return None if self._health in ("ok", "busy") else self._health

    @staticmethod
    def judge(status: dict, model: str | None) -> str:
        m = (status.get("models") or {}).get(model) or {}
        state = m.get("state") or status.get("state", "unknown")
        v = m.get("validation") or {}
        if v.get("total") and (v.get("passed") or 0) < v["total"]:
            return f"failing validation ({v.get('passed') or 0}/{v['total']})"
        return state

    def ask(self, system: str, text: str, timeout: float) -> str:
        if not self.key():
            raise Unavailable(f"{self.name}: {self.spec.get('key_env')} not set")
        if self.kind == "anthropic":
            res = _post("https://api.anthropic.com/v1/messages",
                        {"model": self.spec["model"], "max_tokens": 200, "system": system,
                         "messages": [{"role": "user", "content": text}]},
                        {"x-api-key": self.key(), "anthropic-version": "2023-06-01"}, timeout)
            return "".join(b.get("text", "") for b in res.get("content", []))
        body = {"model": self.spec["model"], "temperature": 0, "max_tokens": 200,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": text}]}
        if self.spec.get("disable_thinking"):
            # Qwen/Gemma-style servers reason by default and would spend the whole budget thinking.
            body["chat_template_kwargs"] = {"enable_thinking": False}
        res = _post(self.spec["url"].rstrip("/") + "/chat/completions", body,
                    {"Authorization": f"Bearer {self.key()}"}, timeout)
        return res["choices"][0]["message"]["content"]


class Classifier:
    def __init__(self, cfg: dict, cache: "Cache | None" = None):
        self.categories = set(cfg["categories"])
        self.system = build_prompt(cfg)
        self.backends = [Backend(b) for b in cfg.get("classifier", {}).get("backends", [])]
        self.timeout = float(cfg.get("classifier", {}).get("timeout_s", 45))
        self.cache = cache

    def __call__(self, msg_id: str, text: str) -> dict:
        if self.cache and (hit := self.cache.get(msg_id)):
            return hit
        errors = []
        for b in self.backends:
            if (state := b.healthy()):
                errors.append(f"{b.name}: {state}, skipped")
                continue
            try:
                out = parse_reply(b.ask(self.system, text, self.timeout), self.categories)
                out["backend"] = b.name
                if self.cache:
                    self.cache.put(msg_id, out)
                return out
            except (Unavailable, urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError) as e:
                errors.append(f"{b.name}: {e}"[:160])
        raise Unavailable("; ".join(errors) or "no classifier backends configured")


class Cache:
    """message id -> classification, so each email is sent to an LLM once. 30-day prune on save."""

    def __init__(self, path: str, keep_days: int = 30):
        self.path, self.keep_days = os.path.expanduser(path), keep_days
        try:
            with open(self.path) as fh:
                self.data = json.load(fh)
        except (OSError, ValueError):
            self.data = {}

    def get(self, key: str) -> dict | None:
        hit = self.data.get(key)
        return dict(hit["c"], backend="cache") if hit else None

    def put(self, key: str, c: dict):
        self.data[key] = {"c": {k: c[k] for k in ("category", "confidence", "reason")},
                          "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}

    def save(self):
        cutoff = datetime.now(timezone.utc).timestamp() - self.keep_days * 86400
        self.data = {k: v for k, v in self.data.items() if datetime.fromisoformat(v["at"]).timestamp() >= cutoff}
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(self.data, fh)
        os.replace(tmp, self.path)

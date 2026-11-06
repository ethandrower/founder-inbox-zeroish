"""ABOUTME: Per-email facts for the rules — sender kind, age, read, replied — computed without any LLM.
ABOUTME: Content (the category) is added lazily by the engine only when a rule needs it."""
from __future__ import annotations

import re
from datetime import datetime, timezone

from .rules import Facts

FREE_MAIL = {"gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com", "yahoo.com", "icloud.com",
             "me.com", "aol.com", "proton.me", "protonmail.com", "msn.com"}


def sender_key(address: str) -> str:
    """Domain for company mail; the full address for free-mail, so one gmail contact doesn't vouch for all of gmail."""
    a = (address or "").strip().lower()
    domain = a.rsplit("@", 1)[-1]
    return a if domain in FREE_MAIL else domain


def _matches(address: str, entries: set[str]) -> bool:
    a = address.lower()
    domain = a.rsplit("@", 1)[-1]
    return a in entries or domain in entries or any(domain.endswith("." + e) for e in entries if "@" not in e)


class SentIndex:
    """Who the owner writes to, and when they last wrote in each conversation."""

    def __init__(self, sent: list[dict]):
        self.correspondents: set[str] = set()
        self.last_sent: dict[str, str] = {}
        for m in sent:
            for r in (m.get("toRecipients") or []) + (m.get("ccRecipients") or []):
                a = ((r or {}).get("emailAddress") or {}).get("address")
                if a:
                    self.correspondents.add(sender_key(a))
            conv, at = m.get("conversationId"), m.get("sentDateTime") or ""
            if conv and at > self.last_sent.get(conv, ""):
                self.last_sent[conv] = at


def age_hours(received: str | None, now: datetime) -> float:
    if not received:
        return 0.0
    t = datetime.fromisoformat(received.replace("Z", "+00:00"))
    return max(0.0, (now - t).total_seconds() / 3600)


def colleague_on_thread(m: dict, cfg: dict) -> bool:
    """Someone from an internal domain (other than the owner) is on To/Cc: a team conversation, never cold."""
    owner = cfg["owner"]["mailbox"]
    for r in (m.get("toRecipients") or []) + (m.get("ccRecipients") or []):
        a = (((r or {}).get("emailAddress") or {}).get("address") or "").lower()
        if a and a != owner and _matches(a, cfg["senders"]["internal"]):
            return True
    return False


def compute(m: dict, cfg: dict, sent: SentIndex, now: datetime | None = None) -> Facts:
    now = now or datetime.now(timezone.utc)
    sender = (((m.get("from") or {}).get("emailAddress") or {}).get("address") or "").lower()
    senders = cfg["senders"]
    if _matches(sender, senders["priority"]):
        kind = "priority"
    elif _matches(sender, senders["internal"]):
        kind = "internal"
    elif _matches(sender, senders["known"]):
        kind = "known"                    # customers/deals from your CRM, via senders.known_file
    elif sender_key(sender) in sent.correspondents:
        kind = "correspondent"
    elif colleague_on_thread(m, cfg):
        kind = "team_thread"
    else:
        kind = "unknown"
    last = sent.last_sent.get(m.get("conversationId"))
    received = m.get("receivedDateTime") or ""
    replied = "never" if not last else ("after_latest" if last > received else "before_latest")
    return Facts(sender_kind=kind, age_h=age_hours(received, now), read=bool(m.get("isRead")), replied=replied)


def parse_since(s: str, now: datetime | None = None) -> str:
    """'2d', '12h' or 'YYYY-MM-DD' -> ISO timestamp for a Graph $filter."""
    from datetime import timedelta
    now = now or datetime.now(timezone.utc)
    m = re.fullmatch(r"(\d+)([dh])", s.strip())
    if m:
        delta = timedelta(days=int(m.group(1))) if m.group(2) == "d" else timedelta(hours=int(m.group(1)))
        return (now - delta).strftime("%Y-%m-%dT%H:%M:%SZ")
    return datetime.fromisoformat(s.strip()).strftime("%Y-%m-%dT00:00:00Z")

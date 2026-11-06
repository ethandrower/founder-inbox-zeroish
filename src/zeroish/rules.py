"""ABOUTME: The rules engine — ordered, declarative rules over per-email facts; first match wins.
ABOUTME: Rules hold conditions only, never prompt text. Content judgement lives in one classifier (classify.py)."""
from __future__ import annotations

from dataclasses import dataclass, field

PASSES = ("fast", "cleanup")
SENDER_KINDS = ("priority", "internal", "known", "correspondent", "team_thread", "unknown")
REPLIED = ("after_latest", "before_latest", "never")
WHEN_KEYS = {"category", "min_confidence", "sender", "trusted", "min_age_h", "max_age_h", "read", "replied"}


class RuleError(ValueError):
    pass


@dataclass(frozen=True)
class Rule:
    id: str
    then: str                      # "keep" or a folder path
    passes: tuple[str, ...]        # which passes may apply it
    category: frozenset[str] | None = None
    min_confidence: float | None = None
    sender: frozenset[str] | None = None
    trusted: bool | None = None
    min_age_h: float | None = None
    max_age_h: float | None = None
    read: bool | None = None
    replied: frozenset[str] | None = None

    @property
    def needs_content(self) -> bool:
        return self.category is not None

    @property
    def keeps(self) -> bool:
        return self.then == "keep"


def _set(v, allowed=None, what="value") -> frozenset[str]:
    items = [v] if isinstance(v, str) else list(v)
    if not items or not all(isinstance(x, str) for x in items):
        raise RuleError(f"{what} must be a string or a non-empty list of strings")
    if allowed is not None:
        bad = [x for x in items if x not in allowed]
        if bad:
            raise RuleError(f"{what} {bad} not in {list(allowed)}")
    return frozenset(items)


def parse(raw: list[dict], categories: set[str], default_confidence: float) -> list[Rule]:
    """Parse and validate the rules list. Raises RuleError with the rule id on any mistake."""
    rules, seen = [], set()
    for i, r in enumerate(raw):
        rid = r.get("id") or f"rule#{i + 1}"
        try:
            if rid in seen:
                raise RuleError("duplicate id")
            seen.add(rid)
            when = r.get("when") or {}
            unknown = set(when) - WHEN_KEYS
            if unknown:
                raise RuleError(f"unknown condition(s) {sorted(unknown)}; allowed: {sorted(WHEN_KEYS)}")
            then = r.get("then")
            if not isinstance(then, str) or not then.strip():
                raise RuleError("'then' must be 'keep' or a folder path")
            passes = r.get("pass", "both")
            passes = PASSES if passes == "both" else tuple(_set(passes, PASSES, "pass"))
            category = _set(when["category"], categories, "category") if "category" in when else None
            rule = Rule(
                id=rid, then=then.strip(), passes=passes, category=category,
                min_confidence=float(when.get("min_confidence", default_confidence)) if category else None,
                sender=_set(when["sender"], SENDER_KINDS, "sender") if "sender" in when else None,
                trusted=bool(when["trusted"]) if "trusted" in when else None,
                min_age_h=float(when["min_age_h"]) if "min_age_h" in when else None,
                max_age_h=float(when["max_age_h"]) if "max_age_h" in when else None,
                read=bool(when["read"]) if "read" in when else None,
                replied=_set(when["replied"], REPLIED, "replied") if "replied" in when else None,
            )
            if rule.min_age_h is not None and rule.max_age_h is not None and rule.min_age_h > rule.max_age_h:
                raise RuleError("min_age_h is greater than max_age_h; the rule can never match")
            rules.append(rule)
        except RuleError as e:
            raise RuleError(f"rule {rid!r}: {e}") from None
    return rules


# ── matching ───────────────────────────────────────────────────────────
@dataclass
class Facts:
    """Everything a rule can look at for one email. `content` is filled lazily (one LLM call, cached)."""
    sender_kind: str                       # priority | internal | correspondent | unknown
    age_h: float
    read: bool
    replied: str                           # after_latest | before_latest | never
    content: dict | None = None            # {category, confidence, reason}
    content_error: str | None = None

    @property
    def trusted(self) -> bool:
        return self.sender_kind != "unknown"


@dataclass
class Decision:
    action: str                            # keep | move
    rule: str | None                       # id of the rule that matched; None = no rule (default keep)
    to: str | None = None
    why: str = ""
    trace: list = field(default_factory=list)


def _cheap_match(r: Rule, f: Facts) -> tuple[bool, str]:
    if r.sender is not None and f.sender_kind not in r.sender:
        return False, f"sender is {f.sender_kind}"
    if r.trusted is not None and f.trusted != r.trusted:
        return False, f"trusted is {f.trusted}"
    if r.min_age_h is not None and f.age_h < r.min_age_h:
        return False, f"only {f.age_h:.0f}h old (needs {r.min_age_h:g}h)"
    if r.max_age_h is not None and f.age_h > r.max_age_h:
        return False, f"{f.age_h:.0f}h old (max {r.max_age_h:g}h)"
    if r.read is not None and f.read != r.read:
        return False, "unread" if r.read else "already read"
    if r.replied is not None and f.replied not in r.replied:
        return False, f"replied is {f.replied}"
    return True, ""


@dataclass(frozen=True)
class Stale:
    """Inbox-zero backstop: past `after_h`, nothing stays. Move rules apply with `relax`ed conditions ignored;
    keep rules no longer protect; whatever matches no move rule goes to `then`. Cleanup pass only."""
    after_h: float
    then: str
    relax: frozenset[str] = frozenset({"read"})


def parse_stale(raw: dict | None) -> Stale | None:
    if not raw:
        return None
    relax = frozenset(raw.get("relax", ["read"]))
    if not relax <= {"read", "replied"}:
        raise RuleError("stale.relax may only contain 'read' and 'replied'")
    if not raw.get("then") or raw["then"] == "keep":
        raise RuleError("stale.then must be a folder path")
    return Stale(after_h=float(raw.get("after_days", 30)) * 24, then=raw["then"], relax=relax)


def evaluate(rules: list[Rule], f: Facts, pass_name: str, classify, stale: Stale | None = None) -> Decision:
    """First matching rule wins. Content is classified only when a rule that needs it is reached.
    If classification fails at that point, evaluation stops and the email is kept (fail closed).
    Stale mail (cleanup pass, older than stale.after_h) skips keep rules and falls back to stale.then."""
    is_stale = stale is not None and pass_name in ("cleanup", "all") and f.age_h >= stale.after_h
    trace = []
    for r in rules:
        if pass_name not in r.passes and pass_name != "all":
            continue
        if is_stale:
            if r.keeps:
                trace.append((r.id, f"stale (> {stale.after_h / 24:g} days): keep rules no longer apply"))
                continue
            r = _relaxed(r, stale.relax)
        ok, why = _cheap_match(r, f)
        if ok and r.needs_content:
            if f.content is None and f.content_error is None:
                try:
                    f.content = classify()
                except Exception as e:  # noqa: BLE001 - any backend failure means "don't move"
                    f.content_error = str(e)[:200]
            if f.content_error:
                trace.append((r.id, "classifier unavailable"))
                return Decision("keep", None, why=f"not classified ({f.content_error}); kept", trace=trace)
            cat, conf = f.content["category"], f.content["confidence"]
            if cat not in r.category:
                ok, why = False, f"category is {cat}"
            elif conf < r.min_confidence:
                ok, why = False, f"{cat} at {conf:.2f} < {r.min_confidence:.2f}"
        if not ok:
            trace.append((r.id, why))
            continue
        reason = (f.content or {}).get("reason", "") if r.needs_content else ""
        trace.append((r.id, "matched"))
        if r.keeps:
            return Decision("keep", r.id, why=reason or f"rule {r.id}", trace=trace)
        return Decision("move", r.id, to=r.then, why=reason or f"rule {r.id}", trace=trace)
    if is_stale:
        trace.append(("stale", "matched"))
        return Decision("move", "stale", to=stale.then,
                        why=f"older than {stale.after_h / 24:g} days and no rule placed it", trace=trace)
    return Decision("keep", None, why="no rule matched", trace=trace)


def _relaxed(r: Rule, relax: frozenset[str]) -> Rule:
    from dataclasses import replace
    return replace(r, **{k: None for k in relax})


# ── lint ───────────────────────────────────────────────────────────────
def _covers(a: Rule, b: Rule) -> bool:
    """True if every email that matches b also matches a (so a, earlier, makes b unreachable)."""
    if not set(b.passes) <= set(a.passes):
        return False
    for attr in ("sender", "replied", "category"):
        av, bv = getattr(a, attr), getattr(b, attr)
        if av is not None and (bv is None or not bv <= av):
            return False
    for attr in ("trusted", "read"):
        av, bv = getattr(a, attr), getattr(b, attr)
        if av is not None and av != bv:
            return False
    if a.min_age_h is not None and (b.min_age_h is None or b.min_age_h < a.min_age_h):
        return False
    if a.max_age_h is not None and (b.max_age_h is None or b.max_age_h > a.max_age_h):
        return False
    if a.min_confidence is not None and (b.min_confidence is None or b.min_confidence < a.min_confidence):
        return False
    return True


def lint(rules: list[Rule], categories: set[str], folders: set[str] | None = None) -> list[str]:
    """Problems that would make the rules misbehave. Empty list = clean."""
    problems = []
    for i, b in enumerate(rules):
        for a in rules[:i]:
            if _covers(a, b):
                problems.append(f"rule {b.id!r} can never match: earlier rule {a.id!r} catches everything it would")
                break
    keep_seen_after_move = [r.id for i, r in enumerate(rules) if r.keeps and any(not x.keeps for x in rules[:i])
                            and r.sender is not None and "priority" in r.sender]
    for rid in keep_seen_after_move:
        problems.append(f"rule {rid!r} protects priority senders but comes after a move rule; put it first")
    unused = categories - {c for r in rules if r.category for c in r.category}
    if unused:
        problems.append(f"categories with no rule (they always stay): {sorted(unused)} — fine if intended")
    if folders is not None:
        missing = sorted({r.then for r in rules if not r.keeps} - folders)
        if missing:
            problems.append(f"folders that don't exist in the mailbox: {missing}")
    return problems

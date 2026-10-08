"""P15 review P2-1: highlight matching cost is bounded and equals the legacy per-term matcher."""

from __future__ import annotations

import random
import re
import time
from uuid import uuid4

from modules.dashboard.highlights import (
    MAX_TERMS_PER_DEFINITION,
    MAX_TERMS_PER_RULE,
    compile_rules,
    match_compiled,
)
from modules.dashboard.schemas import HighlightRule


def _rule(keywords: list[str]) -> HighlightRule:
    return HighlightRule(id=uuid4(), keywords=keywords, severity="info", notify=False)


def test_matching_cost_is_bounded_and_caps_apply() -> None:
    topic = uuid4()
    rules = [
        HighlightRule(id=uuid4(), topic_ids=[topic], severity="info", notify=False) for _ in range(32)
    ]
    terms = {topic: [f"term{i}x" for i in range(600)]}
    compiled = compile_rules(rules, terms)
    assert all(len(c.terms) <= MAX_TERMS_PER_RULE for c in compiled)
    assert sum(len(c.terms) for c in compiled) <= MAX_TERMS_PER_DEFINITION
    rng = random.Random(1)
    words = [f"w{rng.randrange(5000)}" for _ in range(300)]
    texts = [" ".join(words[i : i + 300]) for i in range(200)]
    started = time.perf_counter()
    for text in texts:
        match_compiled(text, compiled)
    assert time.perf_counter() - started < 1.0


def test_results_identical_to_legacy_per_term_matcher() -> None:
    keywords = ["new york", "york", "Rates", "C++", "fed", "ß", "inflation", "AI"]
    rule = _rule(keywords)
    legacy = [re.compile(rf"\b{re.escape(k)}\b", re.IGNORECASE) for k in keywords]
    samples = [
        "The Fed held rates in New York.", "inflation, AI and C++ everywhere", "yorkshire is not york",
        "STRASSE ß", "nothing here", "RATES rates Rates", "",
    ]
    (compiled,) = compile_rules([rule])
    for text in samples:
        expected = tuple(k for k, p in zip(keywords, legacy, strict=True) if p.search(text))
        got = match_compiled(text, [compiled])
        assert (got[0].matched_keywords if got else ()) == expected

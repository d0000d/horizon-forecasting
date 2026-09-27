from hashlib import sha256
from urllib.parse import urlsplit, urlunsplit
from .models import Evidence, Question, QuestionAnalysis


def analyze(question: Question) -> QuestionAnalysis:
    # User-supplied domains override this deliberately modest lexical fallback.
    domain = question.domain
    if domain == "other":
        text = question.text.lower()
        for candidate, words in {
            "economics": ("inflation", "gdp", "cpi", "unemployment"),
            "elections/politics": ("election", "vote", "president"),
            "AI/technology": ("artificial intelligence", "language model"),
            "climate/weather": ("temperature", "rainfall", "hurricane"),
        }.items():
            if any(word in text for word in words):
                domain = candidate
                break
    return QuestionAnalysis(domain, (question.deadline-question.as_of).total_seconds()/86400,
                            question.criteria_hash, question.units,
                            ("Verify YES/NO against criteria", "Check cutoff, units and threshold",
                             "Check fine print and plausible tail scenarios"))


def canonical_url(url: str) -> str:
    parts = urlsplit(url)
    # Preserve query parameters: they can identify different articles.
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip('/'), parts.query, ''))


def deduplicate(items: list[Evidence], question: Question) -> list[Evidence]:
    """Conservative exact/origin clustering, not semantic independence detection."""
    groups: list[tuple[set[str], Evidence]] = []
    for item in items:
        if max(item.published_at or item.available_at, item.available_at) > question.as_of:
            raise ValueError("Future evidence rejected")
        keys = {"url:"+canonical_url(item.source),
                "text:"+sha256(' '.join(item.text.lower().split()).encode()).hexdigest()}
        if item.origin_id:
            keys.add("origin:"+item.origin_id)
        merged = [item]
        remaining = list(groups)
        # Repeat because a later match can bridge an earlier unmatched cluster.
        changed = True
        while changed:
            changed = False
            unmatched = []
            for old_keys, old_item in remaining:
                if keys & old_keys:
                    keys |= old_keys
                    merged.append(old_item)
                    changed = True
                else:
                    unmatched.append((old_keys, old_item))
            remaining = unmatched
        best = max(merged, key=lambda e: (e.primary, e.reliability, e.relevance))
        groups = remaining + [(keys, best)]
    return [item for _, item in groups]

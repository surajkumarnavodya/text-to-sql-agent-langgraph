"""Risk scoring and review-queue ordering (Prompt 35).

The review queue is ordered by risk, not by volume. A single conflict on a
published metric must outrank any number of low-risk synonym candidates. The
score is a transparent sum of named parts, and each part is returned as a
reason, so an SME can see why an item sits where it does.

    base (kind)           conflict 50, ambiguity 40, rule candidate 20,
                          metric relationship 10, synonym 5, term cluster 5
    + published subjects  15 per published concept, at most two
    + dependents          5 per dependent, at most five
    + inference gap       (1 - confidence) * 20, rounded

Tiers: high at 70 and above, medium at 40 and above, otherwise low.
"""

from __future__ import annotations

from dataclasses import replace

from semantic.intelligence.detect import Finding

_BASE: dict[str, int] = {
    "conflict": 50,
    "ambiguity": 40,
    "rule_candidate": 20,
    "metric_relationship": 10,
    "synonym": 5,
    "term_cluster": 5,
}
HIGH_THRESHOLD = 70
MEDIUM_THRESHOLD = 40


def score(finding: Finding, dependents: int = 0) -> Finding:
    """Returns `finding` with `risk_score`, `risk_tier` and `reasons` set. The
    input is not mutated."""
    published = min(finding.published_count, 2)
    dependent_points = min(dependents, 5) * 5
    gap = round((1.0 - finding.confidence) * 20)
    total = min(100, _BASE.get(finding.kind, 0) + 15 * published + dependent_points + gap)
    tier = "high" if total >= HIGH_THRESHOLD else "medium" if total >= MEDIUM_THRESHOLD else "low"
    reasons = [f"{finding.kind} base {_BASE.get(finding.kind, 0)}"]
    if published:
        reasons.append(f"{published} published concept(s): +{15 * published}")
    if dependents:
        reasons.append(f"{dependents} dependent(s): +{dependent_points}")
    if gap:
        reasons.append(f"confidence {finding.confidence:.2f}: +{gap}")
    return replace(
        finding,
        risk_score=total,
        risk_tier=tier,
        reasons=tuple(reasons),
        dependents=dependents,
    )


def prioritize(findings: list[Finding]) -> list[Finding]:
    """Scores and sorts the queue, riskiest first. Ties break on kind and key,
    so the order is deterministic across runs."""
    scored = [score(f, f.dependents) for f in findings]
    return sorted(scored, key=lambda f: (-f.risk_score, f.kind, f.key))

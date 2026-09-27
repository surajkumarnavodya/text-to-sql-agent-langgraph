"""Shared, heuristic content-policy check for every real, metered
generative-media call this app makes -- media generation
(`agent.orchestrator.nodes.execute_generation`) and AI-guided image editing
(`attachments.ai_edit`) both call this, so there is exactly one keyword
list to keep in sync rather than two independently-drifting copies of a
sensitive safety list. Extracted from `agent/orchestrator/nodes.py` (2026-09-27,
alongside adding AI-guided image editing) -- previously private to that
module, with no second caller.
"""

from __future__ import annotations

# A keyword heuristic, deliberately broadened from an original two-word
# list -- still NOT a real moderation system, and still trivially bypassed
# by a synonym, a non-English phrasing, or an indirect description (this
# is a documented, known limitation, not a claim of completeness -- see
# docs/RESPONSIBLE_AI.md's media-generation section). Categories:
# explicit/sexual content, depictions of minors in a sexualized context,
# graphic violence/gore, and content designed to impersonate a real,
# identifiable person without consent (deepfake-style requests) -- the
# categories a real moderation API call should eventually replace this
# with, not an exhaustive list. Replace with a genuine moderation-API call
# (many image/video providers, including IMA, expose one) before this
# feature is exposed to untrusted users at scale.
DISALLOWED_PROMPT_SUBSTRINGS = (
    "nsfw",
    "explicit",
    "porn",
    "pornographic",
    "hentai",
    "nude",
    "naked",
    "sexual",
    "erotic",
    "fetish",
    "child sexual",
    "csam",
    "underage",
    "loli",
    "gore",
    "graphic violence",
    "beheading",
    "self-harm",
    "suicide method",
    "deepfake",
)


def basic_prompt_safety_check(text: str) -> str | None:
    """Returns a rejection reason if `text` fails the (still heuristic, see
    the constant above) content policy check, else `None`."""
    lowered = text.lower()
    for bad in DISALLOWED_PROMPT_SUBSTRINGS:
        if bad in lowered:
            return "This request was rejected by the content policy check."
    return None


__all__ = ["DISALLOWED_PROMPT_SUBSTRINGS", "basic_prompt_safety_check"]

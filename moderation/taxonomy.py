"""The restricted-category taxonomy the moderation gate checks every chunk
against, and how each category maps to an actual detection mechanism.

Deliberately explicit and auditable (a named table, not a vague "is this bad"
catch-all) per this feature's own requirement: what's blocked should be
defensible, not a black box.

**Provider reality check, stated here rather than left implicit:** Azure AI
Content Safety's real harm categories are exactly `Hate`, `SelfHarm`,
`Sexual`, `Violence` (`moderation/provider.py`) -- there is no first-party
"weapons," "drugs," or "synthetic/deepfake" category. This module is where
that gap is closed, honestly:

- `WEAPONS` and `DRUGS` have no dedicated Azure category. `WEAPONS` is
  covered two ways: a coarse proxy via the `VIOLENCE` category for imagery
  (Azure's Violence category is broader than "weapons" specifically, so this
  under- and over-detects relative to a purpose-built weapons classifier),
  plus a custom text blocklist (`config/moderation_blocklist.yaml`, loaded by
  `config/moderation_blocklist.py`) checked against OCR/caption/transcript/
  extracted text. `DRUGS` has **no visual signal at all** in this
  implementation -- text-mentions via the same blocklist only.
- `SYNTHETIC_MEDIA` ("hallucinated"/AI-generated/deepfake-style content) is
  a **disclosed placeholder**, not a real detector: no mainstream Azure API
  reliably classifies this today, so every chunk is recorded as
  `"not_checked"` for this category rather than silently omitted or falsely
  presented as covered. This is intentionally never a hard-reject even if a
  real detector is plugged in later -- deepfake/synthetic-media classifiers
  have real, well-documented accuracy limits, and auto-rejecting real user
  content on a false positive is a worse failure mode than under-flagging.
"""

from __future__ import annotations

from typing import Literal

Category = Literal[
    "sexual",
    "violence",
    "hate",
    "self_harm",
    "weapons",
    "drugs",
    "synthetic_media",
]

Decision = Literal["hard_reject", "soft_flag"]

# The one category that never blocks ingestion, regardless of confidence --
# see this module's docstring for why. Every other category is hard-reject:
# per this feature's own decision rule, a hard-reject on any chunk rejects
# the entire asset, no partial ingestion.
SOFT_FLAG_CATEGORIES: frozenset[Category] = frozenset({"synthetic_media"})

ALL_CATEGORIES: tuple[Category, ...] = (
    "sexual",
    "violence",
    "hate",
    "self_harm",
    "weapons",
    "drugs",
    "synthetic_media",
)


def decision_for(category: Category) -> Decision:
    """Whether a triggered category hard-rejects the asset or only soft-flags it."""
    return "soft_flag" if category in SOFT_FLAG_CATEGORIES else "hard_reject"


# Azure Content Safety category name -> our taxonomy. `VIOLENCE` doubles as
# the (coarse) weapons-imagery proxy -- a triggered Violence category is
# recorded under `"violence"`, never auto-relabeled as `"weapons"`, since
# Azure itself can't tell the two apart; `"weapons"` is only ever assigned
# by the text-blocklist check in `moderation/gate.py`, never by the Azure
# category mapping below.
AZURE_CATEGORY_MAP: dict[str, Category] = {
    "Hate": "hate",
    "SelfHarm": "self_harm",
    "Sexual": "sexual",
    "Violence": "violence",
}

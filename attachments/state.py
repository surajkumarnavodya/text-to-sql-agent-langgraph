"""Shared state for the attachment-QA LangGraph subgraph
(`attachments/graph.py`) -- the same "one TypedDict, nodes return partial
updates" shape `agent.state.AgentState`/`rag.graph.RagState` already use.
"""

from __future__ import annotations

from typing import Literal, TypedDict

from attachments.models import ProcessedAttachment

# What actually happened the one time call_model_node tried to produce an
# answer -- lets validate_response_node give a precise, differentiated
# failure message (never configured vs. unreachable vs. answered empty)
# instead of one generic "couldn't extract usable information" string
# regardless of cause. See a real, reported bug this closes: a user asking
# "extract image text" with no vision model configured AND no Tesseract
# binary installed got that same generic message, indistinguishable from
# "the model tried and failed" or "the file itself was unreadable."
ModelCallOutcome = Literal[
    "no_content",  # nothing to send at all -- no image, no extracted text
    "vision_ok",
    "vision_empty",  # a configured vision model was actually called, but
    # returned nothing usable (see attachments.vision.describe_images) --
    # distinct from vision never being configured at all.
    "text_llm_ok",
    "text_llm_empty",
    "text_llm_unavailable",  # Ollama itself was unreachable for the
    # text-only (document) path.
]


class AttachmentQAState(TypedDict, total=False):
    # Input
    question: str
    attachment_ids: list[str]
    # The authenticated caller's subject, for the same ownership check
    # `attachments.store.AttachmentStore` enforces everywhere else -- None
    # for an unauthenticated/"none"/"static_token"-auth-mode caller.
    owner_subject: str | None

    # Set by validate_attachments_node -- attachment_ids that actually
    # resolved to a stored, owned attachment. Never the full Attachment
    # objects themselves (state holds plain data, not model instances, the
    # same convention agent.state.AgentState follows).
    resolved_attachment_ids: list[str]
    # Structured, user-friendly errors -- one per missing/inaccessible id,
    # plus any raised later (a degraded vision fallback, an empty final
    # answer). Each is a plain dict (AttachmentError.model_dump()), not the
    # pydantic model instance.
    errors: list[dict]

    # Set by process_attachments_node
    processed_attachments: list[ProcessedAttachment]

    # Set by build_attachment_context_node
    attachment_context: str
    context_truncated: bool

    # Set by build_multimodal_message_node
    image_data_urls: list[str]
    # True when at least one image attachment exists but
    # Settings.media_vision_model isn't configured -- the image's content
    # was NOT silently discarded (see build_multimodal_message_node's own
    # docstring for what happens instead: OCR fallback text folded into
    # attachment_context).
    vision_unavailable: bool

    # Set by call_model_node -- see ModelCallOutcome's own docstring for why
    # this exists alongside `answer` rather than validate_response_node
    # trying to reverse-engineer the failure reason from `vision_unavailable`/
    # `attachment_context` alone.
    answer: str | None
    model_call_outcome: ModelCallOutcome | None

    # Set by validate_response_node
    used_attachment_ids: list[str]
    status: str  # "succeeded" | "failed" | "no_attachments"

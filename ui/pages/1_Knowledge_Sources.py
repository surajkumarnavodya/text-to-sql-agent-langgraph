"""Streamlit "Knowledge Sources" page -- upload PDFs into the "documents" or
"policies" RAG collections, and manage what's already been ingested.

Separate from `ui/app.py`'s chat interface (Streamlit's native multipage
convention: any script under `ui/pages/` becomes its own page, no change to
`ui/app.py` needed) -- this page's only job is ingestion + management; the
actual question-answering happens back in the chat page once the
multi-source router is on (`ENABLE_MULTI_SOURCE_ROUTER=true`).
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from rag.ingestion import ingest_pdf
from rag.store import (
    RagStoreNotConfiguredError,
    delete_document,
    ensure_schema,
    get_rag_engine,
    list_documents,
)

from config.settings import configure_logging, get_settings
from security.redaction import redact_secrets

configure_logging()
logger = logging.getLogger(__name__)

st.set_page_config(page_title="Knowledge Sources", page_icon="📚", layout="wide")
st.title("📚 Knowledge Sources")
st.caption(
    'Upload PDFs into the "documents" or "policies" collection for the agentic RAG '
    "subgraphs to search. Ingestion here is independent of the chat page -- a document "
    'uploaded here is queryable as soon as it shows "ready" below, without restarting the app.'
)

settings = get_settings()

_SENSITIVITY_OPTIONS = {
    "None": None,
    "Compensation & pay": "compensation",
    "Disciplinary / HR case content": "disciplinary",
    "Legal / litigation": "legal",
}


def _render_collection_tab(collection: str, enabled_flag: bool, label: str) -> None:
    if not enabled_flag:
        st.info(
            f"**{label}** is disabled. Set `ENABLE_{'DOCUMENT' if collection == 'documents' else 'POLICY'}_RAG=true` "
            "in `.env` to turn it on."
        )
        return
    if not settings.rag_store_connection_string:
        st.warning(
            "`RAG_STORE_CONNECTION_STRING` is not set in `.env` -- this collection is enabled "
            "but has nowhere to store chunks. See `.env.example`'s RAG section."
        )
        return

    try:
        engine = get_rag_engine(settings)
        ensure_schema(engine)
    except RagStoreNotConfiguredError as exc:
        st.error(redact_secrets(str(exc)))
        return
    except Exception as exc:  # noqa: BLE001 - show a real connection failure, don't crash the page
        st.error(f"Could not connect to the RAG store: {redact_secrets(str(exc))}")
        return

    st.subheader("Upload")
    sensitivity_label = "None"
    if collection == "policies":
        sensitivity_label = st.selectbox(
            "Sensitivity category (optional)",
            options=list(_SENSITIVITY_OPTIONS),
            key=f"{collection}_sensitivity",
            help=(
                "A category here means this document's content is never summarized into an "
                "answer -- this app has no per-user authorization system to check who's allowed "
                "to see it, so the RAG subgraph fails closed instead. Leave as None for policy "
                "content that isn't sensitive."
            ),
        )
    sensitivity_category = _SENSITIVITY_OPTIONS[sensitivity_label]

    uploaded_files = st.file_uploader(
        "PDF file(s)",
        type=["pdf"],
        accept_multiple_files=True,
        key=f"{collection}_uploader",
    )
    if uploaded_files and st.button("Ingest", key=f"{collection}_ingest_button", type="primary"):
        progress = st.progress(0.0, text="Starting...")
        results = []
        for i, uploaded_file in enumerate(uploaded_files):
            progress.progress(i / len(uploaded_files), text=f"Ingesting {uploaded_file.name}...")
            result = ingest_pdf(
                uploaded_file.getvalue(),
                uploaded_file.name,
                collection,  # type: ignore[arg-type]
                sensitivity_category=sensitivity_category,  # type: ignore[arg-type]
                settings=settings,
            )
            results.append(result)
        progress.progress(1.0, text="Done.")

        for result in results:
            if result.status == "ready":
                st.success(f"**{result.filename}**: {result.chunk_count} chunk(s) indexed.")
            else:
                st.error(f"**{result.filename}**: failed -- {result.error_message}")
            for warning in result.warnings:
                st.caption(f"⚠️ {warning}")

    st.subheader("Ingested documents")
    documents = list_documents(engine, collection=collection)  # type: ignore[arg-type]
    if not documents:
        st.caption("Nothing ingested into this collection yet.")
        return

    for doc in documents:
        cols = st.columns([4, 2, 2, 2, 1])
        status_icon = {"ready": "✅", "processing": "⏳", "failed": "❌"}.get(doc.status, "❓")
        cols[0].markdown(f"{status_icon} **{doc.filename}**")
        cols[1].caption(doc.upload_date)
        cols[2].caption(f"{doc.chunk_count} chunk(s)")
        cols[3].caption(doc.sensitivity_category or "—")
        if cols[4].button("🗑️", key=f"delete_{doc.id}", help="Delete this document and its chunks"):
            delete_document(engine, doc.id)
            st.rerun()
        if doc.status == "failed" and doc.error_message:
            st.caption(f"⚠️ {doc.error_message}")


documents_tab, policies_tab = st.tabs(["📄 Documents", "🔒 Policies"])
with documents_tab:
    _render_collection_tab("documents", settings.enable_document_rag, "Documents")
with policies_tab:
    _render_collection_tab("policies", settings.enable_policy_rag, "Policies")

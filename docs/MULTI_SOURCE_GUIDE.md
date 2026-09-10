# Multi-Source Guide

How to turn on and use each optional source beyond the SQL database(s):
document RAG, policy RAG (with sensitivity gating), and live web search.
Everything in this document is **off by default** — a fresh clone with an
unmodified `.env` behaves identically to the SQL-only app. See
[`docs/ARCHITECTURE.md`](ARCHITECTURE.md#4-multi-source-orchestration) for
how this works internally, and [`CLAUDE.md`](../CLAUDE.md) for the design
decisions behind it.

## 1. Turn on the router

```env
ENABLE_MULTI_SOURCE_ROUTER=true
```

With this off, `ui/app.py`/`api/main.py` call `agent.graph.run_agent`
directly — nothing below matters. With it on and nothing else configured,
the router still only ever sees `sql` as available and short-circuits to
it with zero added latency or LLM calls — turning this on by itself
changes nothing observable until you configure at least one more source
below.

**`Settings` is a cached, process-wide singleton — restart the app after
any `.env` change.** This is the single most common cause of "I set the
key but it's still not using it": the running process is still holding the
`.env` snapshot from when it started. If a question that should clearly
route to a source you just configured instead gets rejected or answered
from the wrong source, check the terminal log for the `[router] available=...`
line before assuming something's broken — if your new source isn't in
`available`, restart the app.

## 2. Document RAG (general PDF uploads)

```env
ENABLE_DOCUMENT_RAG=true
RAG_STORE_CONNECTION_STRING=<see step 4>
```

Once both are set and the app is restarted, go to the **Knowledge Sources**
page (appears automatically in the Streamlit sidebar — it's
`ui/pages/1_Knowledge_Sources.py`, Streamlit's native multipage
convention), the **📄 Documents** tab, and upload a PDF. You'll see a
progress bar, then a success/failure summary per file (chunk count, or the
error and any "may need OCR" warnings for near-empty pages). The document
appears in the management table below, status `ready` once indexed —
that's when it becomes queryable, no restart needed for a new upload.

Ask a question in the main chat page. If `documents` is the only
non-`sql` source configured, the router only needs to distinguish "is this
about the database or about the documents," which it does reliably; once
you also turn on `policy`/`web` (below), a question that's ambiguous
between sources may need to be phrased a bit more specifically.

## 3. Policy RAG (sensitive HR/company policy PDFs)

```env
ENABLE_POLICY_RAG=true
RAG_STORE_CONNECTION_STRING=<see step 4>
```

Same upload flow, on the **🔒 Policies** tab — but with one more field: a
**sensitivity category** selector (`None`, `Compensation & pay`,
`Disciplinary / HR case content`, `Legal / litigation`). Pick a category
if the document's content is genuinely restricted; leave it `None` for
ordinary policy content (leave policy, dress code, general HR process
docs, ...) that doesn't need gating.

**What the category actually does:** this application has no per-user
login or authorization system (see [`SECURITY.md`](../SECURITY.md)) — it
cannot check "is the person asking allowed to see this." So rather than
guess, a chunk from a categorized document is **never summarized into an
answer, ever** — the agent detects the category before it even calls the
LLM to generate a response, and instead returns a fixed message pointing
the user to HR/the policy owner directly. This is a real, tested gate, not
a prompt instruction the model could ignore. If you need real per-user
access control over policy content, that's a bigger feature (real
authentication + authorization) this project doesn't have — treat the
category selector as "block everyone equally," not "block the wrong
people."

Only categorize what's genuinely sensitive. Over-categorizing (e.g.
marking your whole leave policy "confidential" out of caution) just makes
the agent unable to answer normal questions about it.

## 4. The RAG store database

Both document and policy RAG share one SQL Server database (two
collections, one connection) — **SQL Server 2025+ or Azure SQL**, because
storage uses the native `VECTOR` column type
([`rag/store.py`](../rag/store.py)). Check your version before assuming
this works:

```sql
SELECT @@VERSION;
```

If you're not on 2025+/Azure SQL, this feature isn't usable as built —
that's a real constraint of the current implementation, not a config
option to work around.

**Use a dedicated database**, not one of your configured `DB_CONNECTIONS`
business databases — chunk/embedding storage isn't business data and
shouldn't share a schema with `AdventureWorksDW2025`/your HR database/etc.
The schema (`rag.documents`, `rag.chunks`) is created automatically on
first use (`rag.store.ensure_schema`) — you don't need to run any DDL by
hand, just point the connection string at an existing (possibly empty)
database:

```sql
-- One-time, if the database doesn't exist yet:
CREATE DATABASE RagKnowledgeStore;
```

Same LocalDB-style connection-string pattern as a `DB_<NAME>_CONNECTION_STRING`
override, if you're on a named instance (no plain `host:port`):

```env
RAG_STORE_CONNECTION_STRING=mssql+pyodbc:///?odbc_connect=DRIVER%3D%7BODBC+Driver+17+for+SQL+Server%7D%3BSERVER%3D%28localdb%29%5CMSSQLLocalDB%3BDATABASE%3DRagKnowledgeStore%3BUID%3D<user>%3BPWD%3D<password>%3BTrustServerCertificate%3Dyes%3B
```

For a standard host:port SQL Server, a plain SQLAlchemy connection string
works too:

```env
RAG_STORE_CONNECTION_STRING=mssql+pyodbc://<user>:<password>@<host>:1433/RagKnowledgeStore?driver=ODBC+Driver+17+for+SQL+Server
```

Tuning knobs (all optional, sensible defaults):

| Variable | Default | Purpose |
|---|---|---|
| `RAG_TOP_K` | `4` | Chunks retrieved per question, per collection. |
| `RAG_MAX_RETRIES` | `2` | Query-rewrite retries before "insufficient information." |
| `RAG_CHUNK_SIZE` | `1200` | Target chunk length in characters. |
| `RAG_CHUNK_OVERLAP` | `150` | Overlap between consecutive chunks. |
| `RAG_EMBEDDING_MODEL_NAME` | *(blank = reuse `EMBEDDING_MODEL_NAME`)* | Only set if documents genuinely need a different embedding model than schema retrieval. |

## 5. Live web search (Tavily)

```env
ENABLE_WEB_SEARCH=true
WEB_SEARCH_PROVIDER=tavily
WEB_SEARCH_API_KEY=tvly-...
```

**Where to get a key:** [tavily.com](https://tavily.com) — sign up, a free
tier is available, copy the API key from the dashboard into
`WEB_SEARCH_API_KEY` above. Both `ENABLE_WEB_SEARCH=true` **and** a real
key are required before the router will ever offer `web` as a destination
— leaving the key blank with the flag on is safe, web search just stays
unavailable (same pattern as document/policy RAG with no store connection
configured).

Ask something clearly outside your database/documents — current events,
general knowledge, anything external. The router should pick `web` on its
own; the answer always opens with "According to a live web search:" and
lists the source URLs it drew from, so it's never confused with your own
data. Swapping providers later (Bing, SerpAPI, ...) is a
`search/web_search.py::SUPPORTED_SEARCH_PROVIDERS` addition, not something
`.env` alone can do yet — only `tavily` is implemented today.

## 6. Multi-source questions

With 2+ sources configured, one extra LLM call classifies which source(s)
a question needs — visible in the terminal as
`[router] available=[...] sources=[...] reasoning=...`. A question that
genuinely needs two sources ("compare our leave policy with what's in the
database") fans out to both in parallel and the answer shows each source's
contribution under its own labeled heading, never blended together.

**Known limitation:** each routed source gets the *same, full, un-split*
question text. A single-topic multi-source question retrieves fine on
every side; a question that's really two separate, unrelated asks mashed
into one sentence can retrieve poorly on both sides even though each half
would work fine asked separately. If a multi-source answer looks wrong or
incomplete, try asking the two parts as separate questions first before
assuming something's broken.

## 7. Troubleshooting

- **"I set `WEB_SEARCH_API_KEY`/`RAG_STORE_CONNECTION_STRING` and it's
  still not offered as a source."** Restart the app — see step 1's note on
  `Settings` caching. Then check the `[router] available=[...]` log line
  to confirm the source is actually in the list before digging further.
- **A question about a topic you *know* a source covers gets answered by
  the wrong source, or rejected.** Check the same `[router]` log line —
  `reasoning=` shows what the classifier saw and picked. If the response
  was unparseable, the router falls back to *every* available source
  (logged as `classification unparseable (...)`), not to dropping the
  question.
- **Upload succeeds but the document never seems to get used.** Confirm
  its status is `ready` (not `processing`/`failed`) on the Knowledge
  Sources page, and that you asked a question the document actually
  answers — check the "Sources: ..." citation line under a document/
  policy/web answer to see which file(s) it actually drew from.
- **A policy question always gets refused.** Check whether the retrieved
  chunk(s) are tagged with a sensitivity category on the Knowledge Sources
  page — that's a hard gate (see step 3), not a bug. If the content
  shouldn't be restricted, re-upload it with `None` selected (delete the
  old version first via the 🗑️ button).
- **Native `VECTOR` errors, or `RagStoreNotConfiguredError`.** See step 4
  — confirm your SQL Server version supports `VECTOR`/`VECTOR_DISTANCE`
  (`SELECT @@VERSION`), and that `RAG_STORE_CONNECTION_STRING` points at a
  reachable database (`scripts/test_db_connection.py` doesn't check this
  connection — it's not one of `DB_CONNECTIONS` — so verify it manually or
  just try an upload and read the error shown).

# User Guide

A plain-language walkthrough of using the Text-to-SQL Dashboard — what you
see, what each button does, and what the messages mean. No prior knowledge
of the LangGraph/SQL-validation internals is assumed; if you want that
level of detail, see [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)
instead. This guide describes the Streamlit app (`streamlit run
ui/app.py`); if your organization exposes the [REST API](docs/API.md)
instead, the underlying behavior is the same, but there's no chat window —
see that document for the programmatic equivalent.

**Quick start:** type a question in the chat box → review the SQL the AI
proposes (nothing has run yet) → click **"▶ Confirm and Run"** → read the
results. Everything else in this guide explains what happens at each of
those steps and what to do when something doesn't go as expected.

### Contents

1. [Starting the application](#1-starting-the-application)
2. [Connecting and configuring a database](#2-connecting-and-configuring-a-database)
3. [Loading and indexing the schema](#3-loading-and-indexing-the-schema)
4. [Asking a natural-language question](#4-asking-a-natural-language-question)
5. [How schema retrieval works](#5-how-schema-retrieval-works-in-plain-terms)
6. [Reviewing the generated SQL](#6-reviewing-the-generated-sql)
7. [SQL safety and validation](#7-sql-safety-and-validation)
8. [Reviewing query cost](#8-reviewing-query-cost)
9. [Confirming execution](#9-confirming-execution)
10. [Viewing results](#10-viewing-results)
11. [Charts and AI insights](#11-charts-and-ai-insights)
12. [Editing SQL yourself](#12-editing-sql-yourself)
13. [Retry and self-correction behavior](#13-retry-and-self-correction-behavior)
14. [Follow-up questions](#14-follow-up-questions)
15. [Session history](#15-session-history)
16. [Understanding error and status messages](#16-understanding-error-and-status-messages)
17. [Security — what this app does and doesn't protect against](#17-security--what-this-app-does-and-doesnt-protect-against)
18. [Troubleshooting](#18-troubleshooting)

## 1. Starting the application

Once installed and configured (see [`README.md`](README.md)'s Setup
section), start the app with:

```bash
streamlit run ui/app.py
```

Or, if it's running via Docker (see
[`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md)), just open
`http://localhost:8501` in a browser — everything described below is
identical either way; only how the app is *started* differs.

It opens in your browser automatically. The page title is **"Text-to-SQL
Dashboard"**, with a tagline: *"Ask a question in plain English — get
validated, read-only SQL, a live result table, and an auto-picked chart."*
Three badges under the header show the active model, the connected
database, and how many tables were discovered.

**If the database can't be reached**, the app stops at a single screen —
*"⚠️ Database Connection Required"* — showing the connection error and a
suggestion to check `.env` or run `python scripts/test_db_connection.py`
for a detailed diagnostic. Nothing else loads until this is fixed; there's
no way to "use the app anyway" with a broken connection.

## 2. Connecting and configuring a database

There's no in-app login or connection form — the database is configured
once, in `.env`, before starting the app (see
[`docs/CONFIGURATION.md`](docs/CONFIGURATION.md)). Once running, the
sidebar's **"🔌 Database Connection"** panel shows what's currently
connected: database type, database name, a partially-masked username
(e.g. `j***n`), and schema (if restricted to one). A **"🔌 Test
Connection"** button re-checks the connection at any time and reports
success (with the database version) or the specific error.

If the connected account happens to have write access (INSERT/UPDATE/
DELETE), a warning banner appears here too. This app never issues writes
itself, but that warning is worth acting on — see
[`SECURITY.md`](SECURITY.md) for why a genuinely read-only account matters.

## 3. Loading and indexing the schema

Before the app can answer questions, it needs to know your database's
shape. This happens automatically once per app run, and is also available
on demand via the sidebar's **"🔄 Refresh Schema"** button — use it after
you've added, renamed, or removed tables/columns in the database. It
re-reads the schema and rebuilds the internal search index; a success
message shows how many tables were found. This is safe to click any time —
if nothing actually changed, it's a fast no-op.

The sidebar's **"📋 Discovered tables"** panel (collapsed by default) lists
every table the app currently knows about, with each column's name and
type — useful for confirming the app can see what you expect it to.

## 4. Asking a natural-language question

Type your question into the chat box at the bottom — *"Ask a question
about your data..."* — and press Enter. Your question appears in the chat
immediately, followed by the assistant's response once processing
finishes (a spinner reads *"Retrieving schema, generating SQL,
self-correcting if needed..."* while this happens).

If you ask the exact same question again in the same session (with the
same prior conversation context and insight setting), you'll see *"(served
from this session's cache — no LLM call made)"* — an instant answer with
no repeat processing.

## 5. How schema retrieval works (in plain terms)

Your database might have dozens or hundreds of tables. Rather than
showing the AI every single one (which would be slow and error-prone),
the app finds and shows it only the tables most likely relevant to your
question — plus any additional tables structurally required to connect
them (e.g. a linking table between two dimensions). You can see exactly
which tables were used for your question in the **"🔍 Retrieved schema
context"** expander, including each table's relevance score and its
structure.

## 6. Reviewing the generated SQL

After processing, the SQL query the AI generated appears under **"🛠️
Generated SQL"** in an editable text box, with the note: *"Edit if needed
— it will be re-validated and re-run when you click Confirm and Run."*
**Nothing has been run against your database yet at this point** — this
is a proposal for you to review, not a completed action.

You're free to edit the SQL directly in the box before running it — fix a
typo, add a filter, change a column — your edit is what actually runs, and
it's checked for safety fresh, exactly like AI-generated SQL (see the next
section).

## 7. SQL safety and validation

Every query — whether generated by the AI or edited by hand — is checked
before it's allowed to run. Only a single, read-only `SELECT`-style
query is ever permitted; anything else (an attempt to modify data, run
more than one statement, or call a small number of known-risky database
functions) is rejected outright, with no retry. This check happens
**every time**, including on your own hand-edits — there's no way to
bypass it from inside the app. See [`SECURITY.md`](SECURITY.md) for the
full technical detail, and its "What is explicitly not guaranteed"
section for this protection's honest limits.

## 8. Reviewing query cost

Before a query actually runs, the app estimates roughly how much work it
will involve (without running it). If a query looks like it will scan a
lot of data, you'll see a caption: *"⏳ This query scans a large amount of
data and may take a moment to run."* — informational, the query still
runs. If a query looks extremely expensive, it isn't run at all; the AI
is asked to try a narrower approach instead (this shows up as a step in
the retry timeline — see below — rather than a message to you directly).

## 9. Confirming execution

Click **"▶ Confirm and Run"** to actually execute the SQL currently shown
in the box (including any edits you made). This is the one moment
anything runs against your real database for something you'll actually
see. If the SQL fails the safety check at this point (e.g. you edited it
into something unsafe), you'll see *"Rejected: ..."* and nothing runs. If
it runs but the database itself returns an error, you'll see *"Execution
failed: ..."*.

## 10. Viewing results

A successful run shows a results table headed **"📊 Results (N rows)"**.
By default, purely technical columns (internal ID/key columns) are hidden
and column names are expanded into readable labels (e.g. `CustName`
becomes "Customer Name") — check **"Show technical columns"** to see the
raw, unmodified column names and every column instead.

If nothing but ID-type columns came back, you'll see a note: *"Only
identifier columns were returned."*

## 11. Charts and AI insights

If the result shape supports it (at least one numeric and one descriptive
column), a **"Show chart"** checkbox becomes available — a line chart for
results that look like a trend over time, otherwise a bar chart (capped
to the top 30 values for readability). If the result shape doesn't fit
either, the checkbox is disabled with the note *"No suitable chart for
this result shape"* — the app never forces a misleading chart.

If enabled (sidebar toggle, on by default: **"💡 Generate AI insight"**),
a short plain-English sentence about the result appears above the table,
labeled **"AI insight."** This sentence is checked against the actual
result numbers before being shown — if it doesn't hold up, it's silently
skipped rather than shown as an unverified guess. Turn the sidebar toggle
off if you'd rather not see this at all.

## 12. Editing SQL yourself

As covered in §6/§9: the SQL box is always editable, and clicking
**"Confirm and Run"** always uses whatever text is currently in the box —
not necessarily what the AI originally proposed. Every edit goes through
the same safety check as AI-generated SQL, with no exceptions.

## 13. Retry and self-correction behavior

When the AI's first attempt at SQL doesn't work — a syntax mistake, a
reference to a column that doesn't exist, an overly expensive query — the
app doesn't just give up. It automatically tries again (up to a
configured limit), using what went wrong as guidance for the next
attempt. You can see this entire process in the **"🔁 Retry timeline"**
expander: one line per attempt, showing what happened (succeeded,
timed out, too costly, etc.), the SQL that was tried, and the error if
any. This is a summary of outcomes only — it does not show the AI's
internal reasoning, only what it tried and what happened.

A summary line also tells you at a glance: *"Generated SQL after 2
retries."* (or *"Generated SQL."* if it worked on the first try).

Some problems are never retried — a safety rejection or a genuine
timeout ends the attempt immediately rather than trying again, since
retrying wouldn't help.

## 14. Follow-up questions

You can ask a question that refers back to your previous one — e.g. ask
*"What were total sales in 2012?"* and then *"What about 2013 instead?"*
The app recognizes this as a follow-up (using your last few successful
questions as context) and shows a small caption: *"↪ Following up on:
'What were total sales in 2012?'"* If your question is too ambiguous to
tell whether it's a follow-up or a new question, the app will ask you to
clarify instead of guessing.

## 15. Session history

The sidebar's **"📜 History"** panel lists every question you've asked
this session (most recent first), each with a status badge (succeeded,
failed, needs clarification, rejected, rate limited, or retried) and a
timestamp. Two actions per entry:

- **"👁 View"** — instantly restores exactly what you saw for that
  question (including its actual confirmed result, if you ran one) — no
  new processing.
- **"🔄 Re-run"** — asks the question again from scratch, as a fresh
  request (counts against the same rate limit as a brand-new question).

**"🗑️ Clear history"** removes everything. History is **session-only** —
it disappears when you refresh the page or restart the app; nothing here
is saved permanently.

## 16. Understanding error and status messages

| What you see | What it means |
|---|---|
| *"I couldn't process that question. Try rephrasing it..."* | Your question was declined before any processing — usually because it wasn't recognized as a database question, or looked like an attempt to manipulate the AI's instructions. The message is intentionally general. |
| *"Needs clarification: ..."* | The app couldn't tell what you were really asking (often because a follow-up reference was ambiguous). Rephrase with more detail. |
| *"Agent could not produce a working query: ..."* | The AI tried and retried but never produced a query that both passed safety checks and ran successfully. The last attempted SQL is shown below the message. |
| *"You're asking questions faster than I can process them — please wait a moment."* | You've hit the per-session question rate limit. Wait briefly and try again. |
| *"The system is handling a lot of requests right now — please wait a moment and try again."* | A stricter, shared limit (across all activity, not just yours) was hit mid-processing. |
| *"Rejected: ..."* (after clicking Confirm and Run) | The SQL currently in the box — likely one you edited — failed the safety check. |
| *"Execution failed: ..."* (after clicking Confirm and Run) | The query passed safety checks but the database itself returned an error (e.g. a genuine timeout). |

## 17. Security — what this app does and doesn't protect against

- Every query that runs is read-only by construction — the app cannot
  issue `INSERT`/`UPDATE`/`DELETE`/`DROP`, etc., no matter what you type
  or how the AI responds.
- This is **not** a substitute for using a properly restricted database
  account — see [`SECURITY.md`](SECURITY.md). If the account configured
  in `.env` has broader access than read-only, that's a real risk this
  app's own checks cannot fully cover.
- There is **no login or per-user access control** in this app as
  shipped — anyone who can open the app's URL can use it with whatever
  database access is configured. Don't expose it beyond a trusted network
  without adding your own authentication in front of it (see
  [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md)).
- The AI's accuracy is not perfect — see
  [`docs/EVALUATION.md`](docs/EVALUATION.md) for real, measured numbers.
  Always read the generated SQL and the results before trusting them for
  anything important.

## 18. Troubleshooting

See [`docs/TROUBLESHOOTING.md`](docs/TROUBLESHOOTING.md) for the full
technical reference. The most common day-to-day issues:

- **App won't start / shows a database error screen** — check your
  `.env` database settings; run `python scripts/test_db_connection.py`
  for specifics.
- **"Chroma index is empty" or questions never find the right tables** —
  click **"🔄 Refresh Schema"** in the sidebar, or run `python
  scripts/build_embeddings.py` from a terminal.
- **Every question is very slow** — expected with a local AI model on
  modest hardware; this trades speed for running entirely offline. See
  `docs/EVALUATION.md`'s latency numbers for what's typical.
- **A legitimate question keeps getting rejected** — try rephrasing it
  more plainly and directly; if it still seems wrong, see
  [`SECURITY.md`](SECURITY.md)'s "Reporting a vulnerability" section.

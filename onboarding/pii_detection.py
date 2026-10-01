"""Infers likely-PII columns -- Prompt 08 (`08_ONBOARDING_ENGINE_CONTRACT.md`).

`config/sensitive_columns.py` already *enforces* a hand-authored
classification (`db/value_sampling.py` never samples a "restricted"
column; `agent.nodes.validate_sql_node` rejects a SELECT of one) -- but
nothing in this codebase ever *detected* PII before this module. Every
finding here is `agent.provenance.DataTruthLevel.AI_INFERENCE`, never
auto-applied to `config/sensitive_columns.yaml` -- it becomes an
`OnboardingReviewItem` an SME must confirm or reject first (see
`onboarding/jobs.py`).

**"Minimize sensitive sampling" (this prompt's own requirement), taken
literally**: name-based detection (`detect_pii_columns`) touches no data
at all -- just the column name string already in hand from
`db.schema_introspection`. The optional, bounded data-verification pass
(`verify_pii_with_data`) is opt-in, samples a small, capped set of values
per flagged column (reusing `db.value_sampling`'s own identifier-quoting/
bounded-`fetchmany` pattern, not a second implementation of it), and
**never persists, logs, or returns a raw sampled value** -- only an
aggregate match-rate float ever leaves this module. A finding's
`evidence` field is always safe to store and display to an SME reviewer
verbatim.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, replace

from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError

from agent.provenance import DataTruthLevel
from db.schema_introspection import TableSchemaInfo

logger = logging.getLogger(__name__)

# Compound, specific column-name patterns only -- deliberately never a
# bare substring like "name"/"id" alone, which would false-positive on
# `ProductName`/`CompanyName`/`OrderId` constantly. Not exhaustive (the
# same honestly-disclosed-incomplete posture as
# `agent.sql_validator._DANGEROUS_FUNCTION_NAMES`).
_PII_NAME_PATTERNS: dict[str, re.Pattern[str]] = {
    "email": re.compile(r"e[-_]?mail", re.IGNORECASE),
    "phone": re.compile(r"phone|mobile|\bcell\b|\bfax\b|telephone", re.IGNORECASE),
    "ssn": re.compile(r"\bssn\b|social[-_ ]?security", re.IGNORECASE),
    "date_of_birth": re.compile(r"\bdob\b|date[-_ ]?of[-_ ]?birth|birth[-_ ]?date", re.IGNORECASE),
    # Checked before "address" below: "IpAddress" matches *both* patterns
    # (the generic "address" pattern has no word boundary, precisely so it
    # also catches "StreetAddress"/"AddressLine1"), and an IP address is
    # the more specific, more useful classification of the two.
    "ip_address": re.compile(r"\bip[-_ ]?address\b|\bip[-_ ]?addr\b", re.IGNORECASE),
    # No \b around "address"/"street" -- a PascalCase/camelCase column
    # name (e.g. "StreetAddress", "AddressLine1", "MailingAddress") has
    # no word-boundary transition between the two joined words, so a
    # bounded pattern would never match this extremely common real-world
    # naming convention.
    "address": re.compile(
        r"address|street|\baddr\d?\b|postal[-_ ]?code|zip[-_ ]?code", re.IGNORECASE
    ),
    "person_name": re.compile(
        r"first[-_ ]?name|last[-_ ]?name|middle[-_ ]?name|full[-_ ]?name|\bsurname\b",
        re.IGNORECASE,
    ),
    "credit_card": re.compile(r"credit[-_ ]?card|card[-_ ]?number|\bcc[-_ ]?num", re.IGNORECASE),
    # No \b around "passport" for the same PascalCase reason (e.g.
    # "PassportNumber").
    "national_id": re.compile(
        r"national[-_ ]?id|passport|tax[-_ ]?id|driver.?s?[-_ ]?licen[cs]e", re.IGNORECASE
    ),
    "gender": re.compile(r"\bgender\b|\bsex\b", re.IGNORECASE),
}

# A bare "Name" column is only worth flagging (at low, always-ambiguous
# confidence) when its own table looks like it represents a person --
# otherwise this fires on every `ProductName`/`CategoryName` in the
# schema. Kept separate from `_PII_NAME_PATTERNS` because it needs the
# table name too, not just the column name.
_BARE_NAME_RE = re.compile(r"\bname\b", re.IGNORECASE)
_PERSON_ENTITY_TABLE_RE = re.compile(
    r"customer|employee|contact|person|\buser\b|member|patient|student|applicant|staff",
    re.IGNORECASE,
)

# Regexes for the optional data-verification pass -- matched against one
# sampled value at a time, in-process, and immediately discarded (only the
# aggregate match count is ever kept). Deliberately omitted for categories
# with no reliable structural shape (person_name/address/date_of_birth/
# gender) -- a plausible-looking regex for a free-text name or address
# would be more likely to mislead than confirm.
_PII_VALUE_PATTERNS: dict[str, re.Pattern[str]] = {
    "email": re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$"),
    "phone": re.compile(r"^\+?[\d\s().-]{7,20}$"),
    "ssn": re.compile(r"^\d{3}-?\d{2}-?\d{4}$"),
    "ip_address": re.compile(r"^(\d{1,3}\.){3}\d{1,3}$"),
    "credit_card": re.compile(r"^\d{13,19}$"),
}

_MAX_VERIFICATION_SAMPLE_SIZE = 200


@dataclass(frozen=True)
class PiiEvidence:
    """One signal contributing to a `PiiFinding`'s confidence. `detail`
    is always safe to persist/display -- see this module's own docstring
    for the hard rule that a raw sampled value never reaches this field."""

    signal: str
    score: float
    detail: str


@dataclass(frozen=True)
class PiiFinding:
    """One column this module believes likely holds PII -- never a
    confirmed fact. `truth_level` is always `AI_INFERENCE`."""

    table_name: str
    column_name: str
    pii_category: str
    confidence: float
    evidence: tuple[PiiEvidence, ...]
    truth_level: DataTruthLevel = DataTruthLevel.AI_INFERENCE


def detect_pii_columns(tables: list[TableSchemaInfo]) -> list[PiiFinding]:
    """Name-based PII detection -- touches no data at all, only the
    column/table name strings `db.schema_introspection` already produced.

    Returns:
        One `PiiFinding` per column matching a known PII naming
        convention, sorted by descending confidence then table/column
        name. A column can only ever produce one finding (the first,
        most specific pattern that matches) -- never several competing
        categories for the same column.
    """
    findings: list[PiiFinding] = []
    for table in tables:
        if table.is_view:
            continue
        table_looks_like_a_person = bool(_PERSON_ENTITY_TABLE_RE.search(table.table_name))
        for column in table.columns:
            match = next(
                (
                    (category, pattern)
                    for category, pattern in _PII_NAME_PATTERNS.items()
                    if pattern.search(column.name)
                ),
                None,
            )
            if match is not None:
                category, pattern = match
                findings.append(
                    PiiFinding(
                        table_name=table.table_name,
                        column_name=column.name,
                        pii_category=category,
                        confidence=0.85,
                        evidence=(
                            PiiEvidence(
                                signal="column_name_pattern",
                                score=0.85,
                                detail=(
                                    f"column name '{column.name}' matches the "
                                    f"'{category}' naming convention"
                                ),
                            ),
                        ),
                    )
                )
            elif table_looks_like_a_person and _BARE_NAME_RE.search(column.name):
                findings.append(
                    PiiFinding(
                        table_name=table.table_name,
                        column_name=column.name,
                        pii_category="person_name",
                        confidence=0.5,
                        evidence=(
                            PiiEvidence(
                                signal="column_name_pattern",
                                score=0.4,
                                detail=(
                                    f"column name '{column.name}' contains 'name', and table "
                                    f"'{table.table_name}' looks like it represents a person"
                                ),
                            ),
                        ),
                    )
                )

    return sorted(findings, key=lambda f: (-f.confidence, f.table_name, f.column_name))


def _quote(engine: Engine, identifier: str) -> str:
    return engine.dialect.identifier_preparer.quote(identifier)


def verify_pii_with_data(
    findings: list[PiiFinding], engine: Engine, schema: str | None = None, sample_size: int = 100
) -> list[PiiFinding]:
    """Refines each finding's confidence with a small, bounded sample of
    real values -- explicitly opt-in (`Settings
    .enable_pii_data_verification`, default off), per this prompt's own
    "minimize sensitive sampling" requirement. Never a full scan (bounded
    via `fetchmany()`, the same established pattern `db.value_sampling
    ._sample_column` already uses), and the sampled values themselves are
    never returned, logged, or stored -- only the aggregate match rate.

    A category with no value pattern (`person_name`/`address`/
    `date_of_birth`/`gender`) is passed through unchanged -- there is no
    reliable structural shape to verify against for free text.

    Fails open per-finding on any query error (the name-based result is
    kept, unrefined, not dropped).
    """
    bounded_sample_size = min(sample_size, _MAX_VERIFICATION_SAMPLE_SIZE)
    refined: list[PiiFinding] = []
    for finding in findings:
        value_pattern = _PII_VALUE_PATTERNS.get(finding.pii_category)
        if value_pattern is None:
            refined.append(finding)
            continue
        try:
            match_rate = _sample_match_rate(
                engine,
                finding.table_name,
                finding.column_name,
                schema,
                value_pattern,
                bounded_sample_size,
            )
        except SQLAlchemyError as exc:
            logger.debug(
                "[pii_detection] verification failed for %s.%s: %s",
                finding.table_name,
                finding.column_name,
                exc,
            )
            refined.append(finding)
            continue

        if match_rate is None:
            refined.append(finding)
            continue

        evidence = (
            *finding.evidence,
            PiiEvidence(
                signal="value_pattern_match_rate",
                score=match_rate,
                detail=f"{match_rate:.0%} of sampled non-NULL values match the expected shape",
            ),
        )
        # Weighted toward the measured rate -- real data is stronger
        # evidence than a name alone, in both directions (confirming or
        # contradicting the name-based guess).
        confidence = round(min(finding.confidence * 0.3 + match_rate * 0.7, 1.0), 4)
        refined.append(replace(finding, confidence=confidence, evidence=evidence))

    return refined


def _sample_match_rate(
    engine: Engine,
    table_name: str,
    column_name: str,
    schema: str | None,
    pattern: re.Pattern[str],
    sample_size: int,
) -> float | None:
    quoted_column = _quote(engine, column_name)
    qualified_table = (
        f"{_quote(engine, schema)}.{_quote(engine, table_name)}"
        if schema
        else _quote(engine, table_name)
    )
    with engine.connect() as connection:
        cursor_result = connection.execute(
            text(
                f"SELECT {quoted_column} FROM {qualified_table} "  # nosec B608
                f"WHERE {quoted_column} IS NOT NULL"
            )
        )
        rows = cursor_result.fetchmany(sample_size)
    if not rows:
        return None
    matches = sum(1 for row in rows if isinstance(row[0], str) and pattern.match(row[0].strip()))
    return matches / len(rows)

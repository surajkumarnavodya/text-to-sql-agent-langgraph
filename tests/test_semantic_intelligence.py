"""Unit tests for the pure semantic-intelligence engine (Prompt 35,
`semantic/intelligence/`). No database, no LLM: every detector runs for real
over in-memory catalog snapshots."""

from __future__ import annotations

import uuid

import pytest
from semantic.catalog import CatalogConceptType, CatalogEntrySnapshot, CatalogStatus
from semantic.intelligence import detect, impact, risk
from semantic.intelligence.engine import run_analysis
from semantic.intelligence.normalize import canonical, content_tokens, same_concept

TENANT = "tenant-a"


def _entry(
    concept_key: str,
    *,
    name: str | None = None,
    concept_type: str = "metric",
    status: str = "published",
    version: int = 1,
    expression: str | None = None,
    tables: tuple[str, ...] = (),
    filters: tuple[str, ...] = (),
    aggregation: str | None = None,
    synonyms: tuple[str, ...] = (),
    confidence: float = 1.0,
) -> CatalogEntrySnapshot:
    return CatalogEntrySnapshot(
        id=str(uuid.uuid4()),
        tenant_id=TENANT,
        database_id="db1",
        concept_type=CatalogConceptType(concept_type),
        concept_key=concept_key,
        business_name=name or concept_key.replace("_", " "),
        status=CatalogStatus(status),
        version=version,
        approved_expression=expression,
        source_tables=tables,
        filters=filters,
        aggregation=aggregation,
        synonyms=synonyms,
        confidence=confidence,
    )


class TestOwnership:
    def test_a_finding_names_the_business_owner_of_each_concept_it_concerns(self):
        a = _entry("revenue_gross", name="Revenue", expression="SUM(a)", tables=("sales",))
        b = _entry("revenue_net", name="Revenue", expression="SUM(b)", tables=("sales",))
        a = a.model_copy(update={"owner": "finance-team"})
        b = b.model_copy(update={"owner": None})

        conflicts = [f for f in detect.detect_conflicts([a, b]) if f.kind == "conflict"]

        owners = {s["concept_key"]: s["owner"] for s in conflicts[0].subjects}
        assert owners == {"revenue_gross": "finance-team", "revenue_net": None}


def _finding(**overrides) -> detect.Finding:
    base = dict(
        kind="synonym", key="k", title="", detail="", subjects=(), evidence=(), confidence=1.0
    )
    base.update(overrides)
    return detect.Finding(**base)


# --- Normalization and false-match guards ----------------------------------


class TestNormalization:
    def test_camel_case_underscores_and_plurals_reduce_to_one_canonical_form(self):
        assert canonical("SalesAmount") == canonical("sales_amount") == canonical("Sales Amounts")

    def test_word_order_does_not_change_the_canonical_form(self):
        assert canonical("revenue by region") == canonical("region revenue")

    def test_abbreviations_expand(self):
        assert canonical("rev") == canonical("revenue")
        assert canonical("cust count") == canonical("customer count")

    def test_stopwords_and_punctuation_are_dropped(self):
        assert content_tokens("the number of, orders!") == content_tokens("number orders")

    def test_empty_or_symbol_only_text_has_no_canonical_form(self):
        assert canonical("!!! ---") == ""
        assert same_concept("", "revenue") == (False, "", 0.0)


class TestFalseMatchGuards:
    @pytest.mark.parametrize(
        "a, b",
        [
            ("date", "update"),  # small edit distance, different concepts
            ("rate", "date"),
            ("order date", "order update"),  # one shared word is not enough
            ("sales region", "sales territory"),
            ("net sales", "net sales amount"),  # 2/3 overlap is below the 0.75 bar
        ],
    )
    def test_terms_that_only_look_alike_are_not_matched(self, a, b):
        assert same_concept(a, b)[0] is False

    def test_a_single_token_term_matches_only_on_exact_canonical_equality(self):
        assert same_concept("Customer", "customers") == (True, "canonical_match", 0.9)
        assert same_concept("customer", "cust")[0] is True

    def test_multi_word_terms_with_high_overlap_match_as_token_overlap(self):
        matched, method, _ = same_concept("total sales amount", "sales total amount")
        assert matched and method == "canonical_match"  # same tokens, different order
        matched, method, confidence = same_concept(
            "gross sales revenue total", "gross sales revenue"
        )
        assert matched is True and method == "token_overlap" and 0 < confidence < 1


# --- Synonyms, clusters and ambiguity ---------------------------------------


class TestSynonyms:
    def test_a_synonym_already_declared_on_a_concept_is_not_rediscovered(self):
        a = _entry("revenue", name="Revenue")
        b = _entry("sales_total", name="Sales Total", synonyms=("revenue",))
        assert detect.detect_synonyms([a, b]) == []

    def test_two_concepts_with_the_same_name_are_ambiguity_not_synonyms(self):
        a = _entry("revenue_gross", name="Revenue")
        b = _entry("revenue_net", name="Revenue")
        assert detect.detect_synonyms([a, b]) == []

    def test_an_undeclared_match_between_two_concepts_is_proposed_for_review(self):
        c = _entry("gross_sales_revenue_total", name="Gross Sales Revenue Total")
        d = _entry("gross_sales_revenue", name="Gross Sales Revenue")
        findings = detect.detect_synonyms([c, d])

        assert len(findings) == 1 and findings[0].kind == "synonym"
        assert findings[0].truth_level == "ai_inference"

    def test_different_concept_types_are_never_proposed_as_synonyms(self):
        metric = _entry("customer_count", name="Customer", concept_type="metric")
        dimension = _entry("customer_dim", name="Customer Name", concept_type="dimension")
        assert detect.detect_synonyms([metric, dimension]) == []

    def test_near_miss_names_do_not_produce_a_synonym(self):
        a = _entry("order_date", name="Order Date", concept_type="dimension")
        b = _entry("order_update", name="Order Update", concept_type="dimension")
        assert detect.detect_synonyms([a, b]) == []


class TestClustersAndAmbiguity:
    def test_three_names_for_one_concept_form_a_cluster_finding(self):
        entries = [
            _entry("gross_a", name="Gross Sales Revenue", status="published"),
            _entry("gross_b", name="Sales Gross Revenue", status="draft"),
            _entry("gross_c", name="Gross Revenue Sales", status="draft"),
        ]
        findings, clusters = detect.analyze(entries)

        assert any(f.kind == "term_cluster" for f in findings)
        assert any(len(c.terms) >= 2 for c in clusters)

    def test_one_term_mapping_to_two_concepts_is_ambiguous(self):
        a = _entry("revenue_gross", name="Revenue", expression="SUM(gross)", tables=("sales",))
        b = _entry("revenue_net", name="Revenue", expression="SUM(net)", tables=("sales",))
        findings, _ = detect.analyze([a, b])

        ambiguity = [f for f in findings if f.kind == "ambiguity"]
        assert len(ambiguity) == 1
        assert "maps to several concepts" in ambiguity[0].detail

    def test_one_term_spanning_two_concept_types_is_ambiguous(self):
        metric = _entry("customer_count", name="Customer", concept_type="metric")
        entity = _entry("customer", name="Customer", concept_type="entity")
        findings, _ = detect.analyze([metric, entity])

        ambiguity = [f for f in findings if f.kind == "ambiguity"]
        assert ambiguity and "spans several concept types" in ambiguity[0].detail

    def test_an_unambiguous_term_is_not_flagged(self):
        findings, _ = detect.analyze([_entry("revenue", name="Revenue", expression="SUM(x)")])
        assert not [f for f in findings if f.kind == "ambiguity"]


# --- Conflicts ---------------------------------------------------------------


class TestConflicts:
    def test_a_draft_that_changes_a_published_definition_is_a_conflict(self):
        published = _entry(
            "revenue", expression="SUM(amount)", tables=("sales",), status="published"
        )
        draft = _entry(
            "revenue",
            expression="SUM(amount) - SUM(refunds)",
            tables=("sales",),
            status="draft",
            version=2,
        )
        findings = detect.detect_conflicts([published, draft])

        assert len(findings) == 1
        assert findings[0].payload["conflict"] == "proposed_definition_differs"
        assert "expression" in findings[0].payload["differing_fields"]

    def test_cosmetic_differences_are_not_conflicts(self):
        published = _entry(
            "revenue", expression="SUM(amount)", tables=("sales", "returns"), status="published"
        )
        draft = _entry(
            "revenue",
            expression="sum( amount )",
            tables=("returns", "sales"),
            status="draft",
            version=2,
        )
        assert detect.detect_conflicts([published, draft]) == []

    def test_two_metrics_sharing_a_term_with_different_formulas_conflict(self):
        a = _entry("revenue_gross", name="Revenue", expression="SUM(gross)", tables=("sales",))
        b = _entry("revenue_net", name="Revenue", expression="SUM(net)", tables=("sales",))
        findings = detect.detect_conflicts([a, b])

        assert len(findings) == 1
        assert findings[0].payload["conflict"] == "conflicting_metric_definitions"
        assert findings[0].published_count == 2

    def test_a_superseded_version_is_never_compared(self):
        old = _entry("revenue", expression="SUM(old)", status="superseded", version=1)
        current = _entry("revenue", expression="SUM(amount)", status="published", version=2)
        findings, _ = detect.analyze([old, current])

        assert not [f for f in findings if f.kind == "conflict"]

    def test_a_conflict_is_never_auto_resolved_to_a_winner(self):
        published = _entry("revenue", expression="SUM(a)", status="published")
        draft = _entry("revenue", expression="SUM(b)", status="draft", version=2)
        findings = detect.detect_conflicts([published, draft])

        # Both definitions are preserved in the evidence; neither is dropped.
        assert {e["version"] for e in findings[0].evidence} == {1, 2}


class TestRuleCandidates:
    def test_a_metric_filter_becomes_an_unconfirmed_candidate_rule(self):
        entry = _entry("active_revenue", expression="SUM(x)", filters=("status = 'active'",))
        findings = detect.detect_rule_candidates([entry])

        assert len(findings) == 1
        assert findings[0].confidence == 0.5
        assert "Candidate (unconfirmed) rule" in findings[0].detail


# --- Relationships and impact -----------------------------------------------


class TestRelationshipsAndImpact:
    def test_metrics_reading_a_common_table_share_a_source(self):
        a = _entry("revenue", expression="SUM(amount)", tables=("sales",))
        b = _entry("orders", expression="COUNT(id)", tables=("sales",))
        edges = impact.metric_relationships([a, b])

        assert [e.relation for e in edges] == ["shares_source_tables"]

    def test_a_metric_naming_another_metric_in_its_expression_references_it(self):
        base = _entry("revenue", name="Revenue", expression="SUM(amount)", tables=("sales",))
        margin = _entry(
            "margin", name="Margin", expression="Revenue - SUM(cost)", tables=("sales",)
        )
        edges = impact.metric_relationships([base, margin])

        assert any(e.relation == "references_metric" and e.target_key == "revenue" for e in edges)

    def test_a_noise_word_in_a_metric_name_is_not_treated_as_a_reference(self):
        count_named = _entry("count_metric", name="Count", expression="SUM(x)")
        other = _entry("orders", expression="COUNT(id)")
        edges = impact.metric_relationships([count_named, other])

        assert not [e for e in edges if e.relation == "references_metric"]

    def test_impact_lists_everything_that_depends_on_a_published_metric(self):
        base = _entry(
            "revenue",
            name="Revenue",
            expression="SUM(amount)",
            tables=("sales",),
            status="published",
        )
        margin = _entry(
            "margin", name="Margin", expression="Revenue - SUM(cost)", tables=("sales",)
        )
        report = impact.impact_of_change([base, margin], "revenue")

        assert {d["concept_key"] for d in report.dependents} == {"margin"}
        assert report.risk_tier == "high"

    def test_removing_a_table_reports_the_metrics_that_read_it(self):
        base = _entry(
            "revenue", expression="SUM(amount)", tables=("sales", "returns"), status="published"
        )
        orders = _entry("orders", expression="COUNT(id)", tables=("returns",))
        report = impact.impact_of_change(
            [base, orders], "revenue", proposed_source_tables=["sales"]
        )

        assert report.removed_tables == ("returns",)
        assert any("reads a table this change removes" in d["reason"] for d in report.dependents)
        # The shared-table edge is a separate, true reason on the same dependent.
        assert any("shares source tables" in d["reason"] for d in report.dependents)
        assert not any("referenced" in d["reason"] for d in report.dependents)

    def test_an_unknown_concept_is_an_explicit_error_not_an_empty_report(self):
        with pytest.raises(KeyError):
            impact.impact_of_change([_entry("revenue")], "missing")


# --- Risk ordering and the engine -------------------------------------------


class TestRiskOrdering:
    def test_one_conflict_on_published_metrics_outranks_many_synonyms(self):
        a = _entry(
            "revenue_gross",
            name="Revenue",
            expression="SUM(gross)",
            tables=("sales",),
            status="published",
        )
        b = _entry(
            "revenue_net",
            name="Revenue",
            expression="SUM(net)",
            tables=("sales",),
            status="published",
        )
        synonym_entries = []
        for i in range(10):
            synonym_entries.append(
                _entry(
                    f"term_{i}_a",
                    name=f"Gross Item {i} Total Sales Amount",
                    concept_type="dimension",
                )
            )
            synonym_entries.append(
                _entry(f"term_{i}_b", name=f"Item {i} Gross Total Sales", concept_type="dimension")
            )
        result = run_analysis([a, b, *synonym_entries])

        top = result.findings[0]
        assert top.kind == "conflict" and top.risk_tier == "high"
        synonyms = [f for f in result.findings if f.kind == "synonym"]
        assert len(synonyms) == 10
        assert all(f.risk_score < top.risk_score for f in synonyms)

    def test_lower_confidence_raises_the_score(self):
        sure = risk.score(_finding(confidence=1.0))
        unsure = risk.score(_finding(confidence=0.5))

        assert unsure.risk_score > sure.risk_score

    def test_score_tiers_follow_the_documented_thresholds(self):
        high = risk.score(_finding(kind="conflict", confidence=1.0, published_count=2))
        low = risk.score(_finding(kind="synonym", confidence=0.9))

        assert high.risk_tier == "high" and high.risk_score >= 70
        assert low.risk_tier == "low" and low.risk_score < 40

    def test_every_score_names_its_reasons(self):
        scored = risk.score(_finding(kind="conflict", confidence=0.9, published_count=1))

        assert scored.reasons and any("published" in r for r in scored.reasons)

    def test_the_queue_order_is_deterministic_across_runs(self):
        entries = [
            _entry("revenue_gross", name="Revenue", expression="SUM(gross)", tables=("sales",)),
            _entry("revenue_net", name="Revenue", expression="SUM(net)", tables=("sales",)),
            _entry("gross_sales", name="Gross Sales"),
            _entry("sales_gross", name="Sales Gross"),
        ]
        first = [f.key for f in run_analysis(entries).findings]
        second = [f.key for f in run_analysis(list(reversed(entries))).findings]

        assert first == second

    def test_no_finding_is_marked_as_confirmed_truth(self):
        result = run_analysis(
            [
                _entry("revenue_gross", name="Revenue", expression="SUM(a)", status="published"),
                _entry("revenue_net", name="Revenue", expression="SUM(b)", status="published"),
            ]
        )
        assert result.findings and all(f.truth_level == "ai_inference" for f in result.findings)

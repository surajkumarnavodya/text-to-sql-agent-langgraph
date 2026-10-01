"""Unit tests for onboarding/semantic_contract.py (Prompt 08,
`08_ONBOARDING_ENGINE_CONTRACT.md`) -- the "semantic contract" stage.
Fully offline: plain `ConfirmedReviewItem` dataclasses in, a plain dict
out, no engine, no identity/ORM dependency (per that module's own
docstring).
"""

from __future__ import annotations

from onboarding.semantic_contract import ConfirmedReviewItem, build_semantic_contract


def _item(item_type, table_name=None, column_name=None, payload=None):
    return ConfirmedReviewItem(
        item_type=item_type, table_name=table_name, column_name=column_name, payload=payload or {}
    )


class TestPiiClassification:
    def test_confirmed_pii_item_becomes_a_restricted_sensitive_column(self):
        items = [_item("pii_classification", "Customers", "Email")]
        contract = build_semantic_contract(items)
        assert contract["sensitive_columns"]["tables"] == [
            {"table_name": "Customers", "columns": [{"column": "Email", "tier": "restricted"}]}
        ]

    def test_pii_only_table_is_absent_from_table_descriptions(self):
        """A table with no confirmed semantic_label/relationship item gets
        no table_descriptions entry at all -- sensitive_columns is a
        parallel, independently-keyed section, not a trigger for one."""
        items = [_item("pii_classification", "Customers", "Email")]
        contract = build_semantic_contract(items)
        assert contract["table_descriptions"]["tables"] == []

    def test_multiple_pii_columns_on_one_table_accumulate(self):
        items = [
            _item("pii_classification", "Customers", "Email"),
            _item("pii_classification", "Customers", "Phone"),
        ]
        contract = build_semantic_contract(items)
        columns = contract["sensitive_columns"]["tables"][0]["columns"]
        assert {c["column"] for c in columns} == {"Email", "Phone"}


class TestSemanticLabel:
    def test_confirmed_semantic_label_becomes_a_column_note(self):
        items = [_item("semantic_label", "Sales", "Amount", payload={"label": "measure"})]
        contract = build_semantic_contract(items)
        table = contract["table_descriptions"]["tables"][0]
        assert table["table_name"] == "Sales"
        assert "measure" in table["column_notes"]["Amount"]
        assert "confirmed by SME review" in table["column_notes"]["Amount"]


class TestRelationship:
    def test_confirmed_relationship_becomes_a_key_relationship_note(self):
        items = [
            _item(
                "relationship",
                "Orders",
                payload={
                    "source_table": "Orders",
                    "target_table": "Customers",
                    "source_columns": ["CustomerId"],
                    "target_columns": ["Id"],
                },
            )
        ]
        contract = build_semantic_contract(items)
        table = contract["table_descriptions"]["tables"][0]
        assert table["table_name"] == "Orders"
        assert "references Customers" in table["key_relationships"]
        assert "CustomerId" in table["key_relationships"]

    def test_multiple_relationships_on_one_source_table_are_joined(self):
        items = [
            _item(
                "relationship",
                "Orders",
                payload={
                    "source_table": "Orders",
                    "target_table": "Customers",
                    "source_columns": ["CustomerId"],
                    "target_columns": ["Id"],
                },
            ),
            _item(
                "relationship",
                "Orders",
                payload={
                    "source_table": "Orders",
                    "target_table": "Products",
                    "source_columns": ["ProductId"],
                    "target_columns": ["Id"],
                },
            ),
        ]
        contract = build_semantic_contract(items)
        table = contract["table_descriptions"]["tables"][0]
        assert "Customers" in table["key_relationships"]
        assert "Products" in table["key_relationships"]
        assert "; " in table["key_relationships"]


class TestNonConfirmedItemsAreExcludedByConstruction:
    def test_golden_question_item_type_contributes_nothing(self):
        """This module has no branch for `golden_question` -- an item of
        that type (or any unrecognized type) is silently inert."""
        items = [_item("golden_question", "Sales", payload={"question": "How many rows?"})]
        contract = build_semantic_contract(items)
        assert contract["table_descriptions"]["tables"] == []
        assert contract["sensitive_columns"]["tables"] == []

    def test_empty_input_produces_an_empty_but_well_shaped_contract(self):
        contract = build_semantic_contract([])
        assert contract == {
            "table_descriptions": {"tables": []},
            "sensitive_columns": {"tables": []},
        }


class TestSorting:
    def test_tables_are_sorted_alphabetically_in_both_sections(self):
        items = [
            _item("pii_classification", "Zebra", "Id"),
            _item("pii_classification", "Apple", "Id"),
            _item("semantic_label", "Zebra", "Id", payload={"label": "identifier"}),
            _item("semantic_label", "Apple", "Id", payload={"label": "identifier"}),
        ]
        contract = build_semantic_contract(items)
        assert [t["table_name"] for t in contract["table_descriptions"]["tables"]] == [
            "Apple",
            "Zebra",
        ]
        assert [t["table_name"] for t in contract["sensitive_columns"]["tables"]] == [
            "Apple",
            "Zebra",
        ]

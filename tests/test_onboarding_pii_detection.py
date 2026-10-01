"""Unit tests for onboarding/pii_detection.py (Prompt 08,
`08_ONBOARDING_ENGINE_CONTRACT.md`).

`TestDetectPiiColumns` is fully offline (name-based only, no engine at
all). `TestVerifyPiiWithData` mocks the `Engine.connect()` context
manager -- never a real database -- and asserts no raw sampled value
ever appears in any returned evidence string, the hard rule this
module's own docstring states.
"""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import MagicMock

from sqlalchemy.exc import SQLAlchemyError

from db.schema_introspection import ColumnInfo, TableSchemaInfo
from onboarding.pii_detection import PiiFinding, detect_pii_columns, verify_pii_with_data


def _table(name: str, columns: list[ColumnInfo], is_view: bool = False) -> TableSchemaInfo:
    return TableSchemaInfo(
        table_name=name, columns=tuple(columns), foreign_keys=(), ddl="", is_view=is_view
    )


def _col(name: str, type_: str = "VARCHAR(50)") -> ColumnInfo:
    return ColumnInfo(name=name, type=type_, nullable=True, is_primary_key=False)


class TestDetectPiiColumnsCompoundPatterns:
    def test_email_column_is_flagged(self):
        table = _table("Customers", [_col("Email")])
        findings = detect_pii_columns([table])
        assert len(findings) == 1
        assert findings[0].pii_category == "email"
        assert findings[0].confidence == 0.85

    def test_first_and_last_name_are_flagged_as_person_name(self):
        table = _table("Customers", [_col("FirstName"), _col("LastName")])
        findings = detect_pii_columns([table])
        categories = {(f.column_name, f.pii_category) for f in findings}
        assert categories == {("FirstName", "person_name"), ("LastName", "person_name")}

    def test_ssn_phone_dob_address_ip_credit_card_national_id_gender(self):
        table = _table(
            "People",
            [
                _col("SSN"),
                _col("PhoneNumber"),
                _col("DateOfBirth"),
                _col("StreetAddress"),
                _col("IpAddress"),
                _col("CreditCardNumber"),
                _col("PassportNumber"),
                _col("Gender"),
            ],
        )
        findings = detect_pii_columns([table])
        categories = {f.pii_category for f in findings}
        assert categories == {
            "ssn",
            "phone",
            "date_of_birth",
            "address",
            "ip_address",
            "credit_card",
            "national_id",
            "gender",
        }


class TestDetectPiiColumnsAvoidsFalsePositives:
    def test_product_name_and_company_name_are_not_flagged(self):
        table = _table(
            "DimProduct", [_col("ProductName"), _col("CompanyName"), _col("CategoryName")]
        )
        assert detect_pii_columns([table]) == []

    def test_generic_id_and_unrelated_columns_are_not_flagged(self):
        table = _table("Orders", [_col("OrderId", "INTEGER"), _col("Quantity", "INTEGER")])
        assert detect_pii_columns([table]) == []

    def test_views_are_skipped_entirely(self):
        table = _table("CustomerView", [_col("Email")], is_view=True)
        assert detect_pii_columns([table]) == []


class TestDetectPiiColumnsBareNameHeuristic:
    def test_bare_name_column_flagged_only_when_table_looks_like_a_person(self):
        person_table = _table("Customers", [_col("Name")])
        findings = detect_pii_columns([person_table])
        assert len(findings) == 1
        assert findings[0].pii_category == "person_name"
        assert findings[0].confidence == 0.5

    def test_bare_name_column_not_flagged_for_a_non_person_table(self):
        product_table = _table("DimProduct", [_col("Name")])
        assert detect_pii_columns([product_table]) == []


class TestDetectPiiColumnsSorting:
    def test_sorted_by_descending_confidence(self):
        table = _table("Customers", [_col("Name"), _col("Email")])
        findings = detect_pii_columns([table])
        assert [f.confidence for f in findings] == sorted(
            (f.confidence for f in findings), reverse=True
        )


def _mock_engine(rows):
    connection = MagicMock()

    @contextmanager
    def _connect(*args, **kwargs):
        yield connection

    connection.execute.return_value.fetchmany.return_value = rows
    engine = MagicMock()
    engine.connect.side_effect = _connect
    engine.dialect.identifier_preparer.quote.side_effect = lambda name: f'"{name}"'
    return engine


class TestVerifyPiiWithDataConfidenceRefinement:
    def test_full_match_rate_raises_confidence_toward_one(self):
        finding = PiiFinding("Customers", "Email", "email", 0.85, ())
        rows = [("a@x.com",), ("b@x.com",), ("c@x.com",), ("d@x.com",)]
        engine = _mock_engine(rows)

        refined = verify_pii_with_data([finding], engine)

        assert refined[0].confidence > finding.confidence
        assert any(e.signal == "value_pattern_match_rate" for e in refined[0].evidence)

    def test_moderate_match_rate_pulls_confidence_toward_the_measured_rate(self):
        finding = PiiFinding("Customers", "Email", "email", 0.85, ())
        rows = [("a@x.com",), ("b@x.com",), ("c@x.com",), ("not-an-email",)]
        engine = _mock_engine(rows)

        refined = verify_pii_with_data([finding], engine)

        # 75% match rate, blended with the 0.85 name-based prior, lands
        # between the two -- neither unchanged nor pulled to either extreme.
        assert 0.75 < refined[0].confidence < 0.85

    def test_low_match_rate_lowers_confidence(self):
        finding = PiiFinding("Customers", "Email", "email", 0.85, ())
        rows = [("nope",), ("also-nope",), ("still-nope",)]
        engine = _mock_engine(rows)

        refined = verify_pii_with_data([finding], engine)

        assert refined[0].confidence < finding.confidence

    def test_no_raw_value_ever_appears_in_evidence(self):
        """The hard rule this module's docstring states: a sampled value
        is never returned, logged, or stored -- only the aggregate rate."""
        finding = PiiFinding("Customers", "Email", "email", 0.85, ())
        secret_looking_value = "super-secret-value-should-never-appear@x.com"
        rows = [(secret_looking_value,)]
        engine = _mock_engine(rows)

        refined = verify_pii_with_data([finding], engine)

        for evidence in refined[0].evidence:
            assert secret_looking_value not in evidence.detail

    def test_category_with_no_value_pattern_is_unchanged(self):
        finding = PiiFinding("Customers", "FirstName", "person_name", 0.85, ())
        engine = MagicMock()

        refined = verify_pii_with_data([finding], engine)

        assert refined == [finding]
        engine.connect.assert_not_called()

    def test_query_error_fails_open_leaving_finding_unchanged(self):
        finding = PiiFinding("Customers", "Email", "email", 0.85, ())
        engine = MagicMock()
        engine.connect.side_effect = SQLAlchemyError("connection refused")

        refined = verify_pii_with_data([finding], engine)

        assert refined == [finding]

    def test_no_rows_at_all_leaves_finding_unchanged(self):
        finding = PiiFinding("Customers", "Email", "email", 0.85, ())
        engine = _mock_engine([])

        refined = verify_pii_with_data([finding], engine)

        assert refined == [finding]

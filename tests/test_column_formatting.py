"""Unit tests for ui/column_formatting.py: display-label formatting and
surrogate-key detection for the results table.

Pure functions, no Streamlit/DB involved -- mirrors the style of
tests/test_schema_introspection.py (constructs TableSchemaInfo/ColumnInfo/
ForeignKeyInfo directly rather than mocking an Inspector).
"""

from __future__ import annotations

from db.schema_introspection import ColumnInfo, ForeignKeyInfo, TableSchemaInfo
from ui.column_formatting import (
    escape_markdown,
    format_column_label,
    get_display_columns,
    get_key_column_names,
    is_probable_surrogate_key,
)


class TestFormatColumnLabel:
    def test_pascal_case(self):
        assert format_column_label("CustName") == "Customer Name"

    def test_snake_case(self):
        assert format_column_label("product_category") == "Product Category"

    def test_expands_qty_abbreviation(self):
        assert format_column_label("OrderQty") == "Order Quantity"

    def test_expands_amt_abbreviation(self):
        assert format_column_label("TotalAmt") == "Total Amount"

    def test_expands_desc_abbreviation(self):
        assert format_column_label("EnglishDescription") == "English Description"
        assert format_column_label("ProductDesc") == "Product Description"

    def test_expands_num_abbreviation(self):
        assert format_column_label("SalesOrderNum") == "Sales Order Number"

    def test_already_clean_name_is_title_cased(self):
        assert format_column_label("CalendarYear") == "Calendar Year"

    def test_single_word(self):
        assert format_column_label("Status") == "Status"

    def test_empty_string_falls_back(self):
        assert format_column_label("") == ""

    def test_underscore_only_falls_back_without_crashing(self):
        # No usable tokens -- the basic title-case fallback still applies
        # cleanly rather than raising or guessing.
        assert format_column_label("___").strip() == ""


class TestGetKeyColumnNames:
    def _tables(self) -> list[TableSchemaInfo]:
        return [
            TableSchemaInfo(
                table_name="DimCustomer",
                columns=(
                    ColumnInfo("CustomerKey", "INT", False, True),
                    ColumnInfo("GeographyKey", "INT", True, False),
                    ColumnInfo("EmailAddress", "NVARCHAR", True, False),
                ),
                foreign_keys=(
                    ForeignKeyInfo(("GeographyKey",), "DimGeography", ("GeographyKey",)),
                ),
                ddl="",
            ),
            TableSchemaInfo(
                table_name="FactInternetSales",
                columns=(ColumnInfo("ProductKey", "INT", False, False),),
                foreign_keys=(ForeignKeyInfo(("ProductKey",), "DimProduct", ("ProductKey",)),),
                ddl="",
            ),
        ]

    def test_collects_primary_keys(self):
        keys = get_key_column_names(self._tables())
        assert "customerkey" in keys

    def test_collects_foreign_key_constrained_columns(self):
        keys = get_key_column_names(self._tables())
        assert "geographykey" in keys
        assert "productkey" in keys

    def test_excludes_ordinary_columns(self):
        keys = get_key_column_names(self._tables())
        assert "emailaddress" not in keys


class TestIsProbableSurrogateKey:
    def test_schema_confirmed_key_is_flagged(self):
        assert is_probable_surrogate_key("CustomerKey", key_columns={"customerkey"})

    def test_pattern_fallback_flags_unknown_id_column(self):
        # Not in key_columns (e.g. a computed/aliased result column), but
        # still ends in a bare "ID" token -- pattern fallback should catch it.
        assert is_probable_surrogate_key("RandomID", key_columns=set())

    def test_pattern_fallback_flags_bare_key_suffix(self):
        assert is_probable_surrogate_key("SalesTerritoryKey", key_columns=set())

    def test_does_not_flag_valid_despite_containing_id_substring(self):
        assert not is_probable_surrogate_key("Valid", key_columns=set())

    def test_does_not_flag_postal_code(self):
        assert not is_probable_surrogate_key("PostalCode", key_columns=set())

    def test_does_not_flag_account_number(self):
        assert not is_probable_surrogate_key("AccountNumber", key_columns=set())

    def test_does_not_flag_ordinary_business_column(self):
        assert not is_probable_surrogate_key("EnglishProductName", key_columns=set())


class TestGetDisplayColumns:
    def test_filters_out_surrogate_keys(self):
        columns = ["CustomerKey", "EnglishProductName", "SalesAmount"]
        display, used_fallback = get_display_columns(columns, key_columns={"customerkey"})

        assert display == ["EnglishProductName", "SalesAmount"]
        assert used_fallback is False

    def test_falls_back_to_original_when_everything_is_a_key(self):
        columns = ["CustomerKey", "ProductKey"]
        display, used_fallback = get_display_columns(columns, key_columns=set())

        assert display == columns
        assert used_fallback is True

    def test_no_keys_present_returns_all_columns_unchanged(self):
        columns = ["EnglishProductName", "SalesAmount"]
        display, used_fallback = get_display_columns(columns, key_columns=set())

        assert display == columns
        assert used_fallback is False


class TestEscapeMarkdown:
    """Regression coverage for a confirmed audit finding: raw (database-
    sourced, therefore untrusted) table/column names were interpolated
    directly into `st.markdown` calls in ui/app.py without escaping."""

    def test_ordinary_table_name_is_unchanged(self):
        assert escape_markdown("DimCustomer") == "DimCustomer"

    def test_bold_markup_is_escaped(self):
        malicious = "Products** DELETED **"
        escaped = escape_markdown(malicious)
        assert "**" not in escaped
        assert escaped == "Products\\*\\* DELETED \\*\\*"

    def test_markdown_link_syntax_is_escaped(self):
        malicious = "Click [here](javascript:alert(1))"
        escaped = escape_markdown(malicious)
        assert "[" not in escaped.replace("\\[", "")
        assert "\\[here\\]" in escaped

    def test_heading_syntax_is_escaped(self):
        malicious = "# Fake Heading"
        assert escape_markdown(malicious) == "\\# Fake Heading"

    def test_backtick_code_fence_is_escaped(self):
        malicious = "`; DROP TABLE x; --`"
        escaped = escape_markdown(malicious)
        assert "`" not in escaped.replace("\\`", "")

    def test_underscores_used_for_italics_are_escaped(self):
        assert escape_markdown("_italic_") == "\\_italic\\_"

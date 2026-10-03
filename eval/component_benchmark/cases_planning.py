"""Planning cases: the analytical-plan validator
(`agent.plan_validator.validate_plan`).

Prompt 12's own, extensive pytest unit-test suite already covers this
function's fine-grained code paths. These cases are deliberately different
in character, not a re-run of the same scenarios: each is a small, named,
realistic end-to-end scenario (a plan an LLM might plausibly produce for a
recognizable business question), tracked here as a regression set the way
`eval/benchmark/*.yaml`'s own cases are -- "did this class of plan stay
correctly accepted/rejected," not "does every branch execute."

Deliberately avoids the validator's `restricted_column` check (which reads
the real, operator-maintained `config/sensitive_columns.yaml`) -- every
case here is self-contained and independent of that file's actual
contents, so this dataset never silently breaks because someone edited an
unrelated classification entry.
"""

from __future__ import annotations

from agent.analytical_plan import AnalyticalPlan, PlanAggregation, PlanDimension, PlanMetric
from agent.plan_validator import validate_plan
from agent.state import TableSchema
from eval.component_benchmark.schema import ComponentCase

_ORDERS_DDL = (
    "CREATE TABLE orders (\n"
    "    id INTEGER PRIMARY KEY,\n"
    "    customer_id INTEGER,\n"
    "    order_total DECIMAL(10,2),\n"
    "    order_date DATE,\n"
    "    FOREIGN KEY (customer_id) REFERENCES customers (id)\n"
    ");"
)
_CUSTOMERS_DDL = (
    "CREATE TABLE customers (\n" "    id INTEGER PRIMARY KEY,\n" "    region VARCHAR(50)\n" ");"
)
_PRODUCTS_DDL = (
    "CREATE TABLE products (\n"
    "    id INTEGER PRIMARY KEY,\n"
    "    category VARCHAR(50)\n"
    ");"  # deliberately no FK to orders/customers -- used for the no-path case
)

_SCHEMA_TABLES = [
    TableSchema(table_name="orders", ddl=_ORDERS_DDL, similarity_score=0.95),
    TableSchema(table_name="customers", ddl=_CUSTOMERS_DDL, similarity_score=0.9),
    TableSchema(table_name="products", ddl=_PRODUCTS_DDL, similarity_score=0.5),
]


def _case_valid_single_table_plan_passes() -> tuple[bool, str]:
    plan = AnalyticalPlan(
        metrics=(
            PlanMetric(
                name="total_revenue",
                table="orders",
                column="order_total",
                aggregation=PlanAggregation.SUM,
            ),
        )
    )
    result = validate_plan(plan, _SCHEMA_TABLES, None, (), "postgresql")
    passed = result.is_valid and not result.violations
    return passed, f"is_valid={result.is_valid} violations={[v.code for v in result.violations]}"


def _case_valid_joined_plan_with_declared_fk_passes() -> tuple[bool, str]:
    """orders -> customers has a declared FK -- a plan spanning both must
    be accepted, not rejected as if they were unrelated."""
    plan = AnalyticalPlan(
        metrics=(
            PlanMetric(
                name="total_revenue",
                table="orders",
                column="order_total",
                aggregation=PlanAggregation.SUM,
            ),
        ),
        dimensions=(PlanDimension(name="region", table="customers", column="region"),),
    )
    result = validate_plan(plan, _SCHEMA_TABLES, None, (), "postgresql")
    passed = result.is_valid and not result.violations
    return passed, f"is_valid={result.is_valid} violations={[v.code for v in result.violations]}"


def _case_empty_metrics_rejected() -> tuple[bool, str]:
    plan = AnalyticalPlan()
    result = validate_plan(plan, _SCHEMA_TABLES, None, (), "postgresql")
    passed = not result.is_valid and any(v.code == "no_metrics" for v in result.violations)
    return passed, f"violations={[v.code for v in result.violations]}"


def _case_table_outside_retrieved_schema_rejected() -> tuple[bool, str]:
    """A plan naming a table never retrieved for this question -- the
    same 'only the tables shown to you exist' boundary generation's own
    system prompt already enforces, re-verified deterministically here."""
    plan = AnalyticalPlan(
        metrics=(
            PlanMetric(
                name="headcount", table="employees", column="id", aggregation=PlanAggregation.COUNT
            ),
        )
    )
    result = validate_plan(plan, _SCHEMA_TABLES, None, (), "postgresql")
    passed = not result.is_valid and any(v.code == "unknown_table" for v in result.violations)
    return passed, f"violations={[v.code for v in result.violations]}"


def _case_column_not_on_table_rejected() -> tuple[bool, str]:
    plan = AnalyticalPlan(
        metrics=(
            PlanMetric(
                name="total_revenue",
                table="orders",
                column="profit_margin",  # does not exist on orders
                aggregation=PlanAggregation.SUM,
            ),
        )
    )
    result = validate_plan(plan, _SCHEMA_TABLES, None, (), "postgresql")
    passed = not result.is_valid and any(v.code == "unknown_column" for v in result.violations)
    return passed, f"violations={[v.code for v in result.violations]}"


def _case_no_declared_fk_path_rejected() -> tuple[bool, str]:
    """orders and products share no declared FK in either table's own
    DDL -- a plan joining them must be rejected, not silently accepted on
    the assumption a join is always possible."""
    plan = AnalyticalPlan(
        metrics=(
            PlanMetric(
                name="total_revenue",
                table="orders",
                column="order_total",
                aggregation=PlanAggregation.SUM,
            ),
        ),
        dimensions=(PlanDimension(name="category", table="products", column="category"),),
    )
    result = validate_plan(plan, _SCHEMA_TABLES, None, (), "postgresql")
    passed = not result.is_valid and any(
        v.code == "no_relationship_path" for v in result.violations
    )
    return passed, f"violations={[v.code for v in result.violations]}"


def _case_unsupported_capability_for_target_db_rejected() -> tuple[bool, str]:
    """PERCENTILE_CONT has no MySQL equivalent -- a plan requiring it
    against a MySQL-typed connection must be rejected, by capability, not
    left to fail at generation/execution time."""
    plan = AnalyticalPlan(
        metrics=(
            PlanMetric(
                name="median_order_total",
                table="orders",
                column="order_total",
                aggregation=PlanAggregation.NONE,
            ),
        ),
        required_operations=("percentile_cont",),
    )
    result = validate_plan(plan, _SCHEMA_TABLES, None, (), "mysql")
    passed = not result.is_valid and any(
        v.code == "unsupported_operation" for v in result.violations
    )
    return passed, f"violations={[v.code for v in result.violations]}"


def _case_same_capability_supported_on_postgres_passes() -> tuple[bool, str]:
    """The identical plan from the case above, against a postgres-typed
    connection, must pass -- confirms the rejection above is genuinely
    capability-specific, not a blanket rejection of that metric shape."""
    plan = AnalyticalPlan(
        metrics=(
            PlanMetric(
                name="median_order_total",
                table="orders",
                column="order_total",
                aggregation=PlanAggregation.NONE,
            ),
        ),
        required_operations=("percentile_cont",),
    )
    result = validate_plan(plan, _SCHEMA_TABLES, None, (), "postgresql")
    passed = result.is_valid and not result.violations
    return passed, f"is_valid={result.is_valid} violations={[v.code for v in result.violations]}"


def _case_unknown_governed_metric_key_rejected() -> tuple[bool, str]:
    """A metric claiming a governed_metric_key that isn't among this
    question's actual published governing metrics -- must never be
    silently trusted just because it names one."""
    plan = AnalyticalPlan(
        metrics=(PlanMetric(name="net_revenue", governed_metric_key="Net Revenue"),)
    )
    result = validate_plan(
        plan, _SCHEMA_TABLES, [{"business_name": "Monthly Churn Rate"}], (), "postgresql"
    )
    passed = not result.is_valid and any(
        v.code == "unknown_governed_metric" for v in result.violations
    )
    return passed, f"violations={[v.code for v in result.violations]}"


CASES: list[ComponentCase] = [
    ComponentCase(
        "valid_single_table_plan_passes",
        "A simple, well-formed single-table plan validates cleanly.",
        _case_valid_single_table_plan_passes,
    ),
    ComponentCase(
        "valid_joined_plan_with_declared_fk_passes",
        "A plan joining two tables with a real declared FK between them validates cleanly.",
        _case_valid_joined_plan_with_declared_fk_passes,
    ),
    ComponentCase(
        "empty_metrics_rejected",
        "A plan with no metrics at all is rejected as computing nothing.",
        _case_empty_metrics_rejected,
    ),
    ComponentCase(
        "table_outside_retrieved_schema_rejected",
        "A plan naming a table never retrieved for this question is rejected.",
        _case_table_outside_retrieved_schema_rejected,
    ),
    ComponentCase(
        "column_not_on_table_rejected",
        "A plan naming a column that doesn't exist on its table is rejected.",
        _case_column_not_on_table_rejected,
    ),
    ComponentCase(
        "no_declared_fk_path_rejected",
        "A plan joining two tables with no declared FK between them is rejected.",
        _case_no_declared_fk_path_rejected,
    ),
    ComponentCase(
        "unsupported_capability_for_target_db_rejected",
        "A plan requiring PERCENTILE_CONT against MySQL (no equivalent) is rejected.",
        _case_unsupported_capability_for_target_db_rejected,
    ),
    ComponentCase(
        "same_capability_supported_on_postgres_passes",
        "The identical PERCENTILE_CONT plan against Postgres (supported) validates cleanly.",
        _case_same_capability_supported_on_postgres_passes,
    ),
    ComponentCase(
        "unknown_governed_metric_key_rejected",
        "A metric claiming a governed_metric_key not among the question's published metrics is rejected.",
        _case_unknown_governed_metric_key_rejected,
    ),
]

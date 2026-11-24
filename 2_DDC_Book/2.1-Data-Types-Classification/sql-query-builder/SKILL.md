---
name: "sql-query-builder"
description: "Build SQL queries for construction databases. Generate optimized SQL queries for construction data retrieval"
homepage: "https://datadrivenconstruction.io"
metadata: {"openclaw": {"emoji": "🏷️", "os": ["darwin", "linux", "win32"], "homepage": "https://datadrivenconstruction.io", "requires": {"bins": ["python3"]}}}
---
# SQL Query Builder

## Overview

Based on DDC methodology (Chapter 2.1), this skill builds SQL queries for construction databases, supporting common construction data patterns like cost tracking, schedule management, and resource allocation.

**Book Reference:** "Типы данных в строительстве" / "Data Types in Construction"

## Quick Start

```python
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Dict, Optional, Any, Union, NamedTuple
from datetime import datetime, date


class ParameterizedQuery(NamedTuple):
    """SQL text with driver placeholders plus the values to bind, in order.

    Unpacks as ``sql, params = query`` and is meant for ``cursor.execute(sql, params)``.
    Values are never written into ``sql``.
    """
    sql: str
    params: List[Any]


_IDENTIFIER_PART = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_PARAMSTYLES = ("qmark", "format", "numeric_dollar")
_DEFAULT_PARAMSTYLE = {"postgresql": "format", "mysql": "format", "sqlite": "qmark"}


def validate_identifier(name: str, allow_star: bool = False) -> str:
    """Accept only plain or dotted SQL identifiers (e.g. ``status``, ``ci.wbs_code``)."""
    if not isinstance(name, str):
        raise TypeError(f"SQL identifier must be a string, got {type(name).__name__}")
    parts = name.split(".")
    if len(parts) > 2:
        raise ValueError(f"Invalid SQL identifier: {name!r}")
    for index, part in enumerate(parts):
        if allow_star and part == "*" and index == len(parts) - 1:
            continue
        if not _IDENTIFIER_PART.fullmatch(part):
            raise ValueError(f"Invalid SQL identifier: {name!r}")
    return name


def validate_non_negative_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an int, got {type(value).__name__}")
    if value < 0:
        raise ValueError(f"{name} must be >= 0")
    return value


class QueryParams:
    """Collects bound values and emits the matching driver placeholder for each."""

    def __init__(self, paramstyle: str = "format"):
        if paramstyle not in _PARAMSTYLES:
            raise ValueError(f"paramstyle must be one of {_PARAMSTYLES}")
        self.paramstyle = paramstyle
        self.values: List[Any] = []

    def add(self, value: Any) -> str:
        self.values.append(value)
        if self.paramstyle == "qmark":
            return "?"
        if self.paramstyle == "numeric_dollar":
            return f"${len(self.values)}"
        return "%s"

class QueryType(Enum):
    """Types of SQL queries"""
    SELECT = "select"
    INSERT = "insert"
    UPDATE = "update"
    DELETE = "delete"
    AGGREGATE = "aggregate"
    JOIN = "join"

class AggregateFunction(Enum):
    """SQL aggregate functions"""
    SUM = "SUM"
    AVG = "AVG"
    COUNT = "COUNT"
    MIN = "MIN"
    MAX = "MAX"
    GROUP_CONCAT = "GROUP_CONCAT"

class JoinType(Enum):
    """SQL join types"""
    INNER = "INNER JOIN"
    LEFT = "LEFT JOIN"
    RIGHT = "RIGHT JOIN"
    FULL = "FULL OUTER JOIN"

class ComparisonOperator(Enum):
    """Comparison operators"""
    EQ = "="
    NE = "<>"
    GT = ">"
    GE = ">="
    LT = "<"
    LE = "<="
    LIKE = "LIKE"
    IN = "IN"
    BETWEEN = "BETWEEN"
    IS_NULL = "IS NULL"
    IS_NOT_NULL = "IS NOT NULL"

@dataclass
class WhereCondition:
    """SQL WHERE condition"""
    column: str
    operator: ComparisonOperator
    value: Any
    table_alias: Optional[str] = None

    def to_sql(self, params: QueryParams) -> str:
        """Render the condition; values are appended to ``params`` and replaced by placeholders."""
        validate_identifier(self.column)
        if self.table_alias:
            validate_identifier(self.table_alias)
        col = f"{self.table_alias}.{self.column}" if self.table_alias else self.column
        operator = ComparisonOperator(self.operator)

        if operator == ComparisonOperator.IS_NULL:
            return f"{col} IS NULL"
        elif operator == ComparisonOperator.IS_NOT_NULL:
            return f"{col} IS NOT NULL"
        elif operator == ComparisonOperator.IN:
            values = list(self.value)
            if not values:
                raise ValueError("IN condition needs at least one value")
            placeholders = ", ".join(params.add(v) for v in values)
            return f"{col} IN ({placeholders})"
        elif operator == ComparisonOperator.BETWEEN:
            low, high = self.value
            return f"{col} BETWEEN {params.add(low)} AND {params.add(high)}"
        else:
            return f"{col} {operator.value} {params.add(self.value)}"

@dataclass
class JoinClause:
    """SQL JOIN clause"""
    table: str
    alias: str
    join_type: JoinType
    on_left: str
    on_right: str

    def to_sql(self) -> str:
        for name in (self.table, self.alias, self.on_left, self.on_right):
            validate_identifier(name)
        return f"{JoinType(self.join_type).value} {self.table} {self.alias} ON {self.on_left} = {self.on_right}"

@dataclass
class SelectColumn:
    """Column selection with optional aggregate and alias"""
    column: str
    table_alias: Optional[str] = None
    aggregate: Optional[AggregateFunction] = None
    alias: Optional[str] = None

    def to_sql(self) -> str:
        validate_identifier(self.column, allow_star=True)
        if self.table_alias:
            validate_identifier(self.table_alias)
        col = f"{self.table_alias}.{self.column}" if self.table_alias else self.column

        if self.aggregate:
            col = f"{AggregateFunction(self.aggregate).value}({col})"

        if self.alias:
            col = f"{col} AS {validate_identifier(self.alias)}"

        return col

@dataclass
class OrderBy:
    """ORDER BY clause"""
    column: str
    descending: bool = False
    table_alias: Optional[str] = None

    def to_sql(self) -> str:
        validate_identifier(self.column)
        if self.table_alias:
            validate_identifier(self.table_alias)
        col = f"{self.table_alias}.{self.column}" if self.table_alias else self.column
        direction = "DESC" if self.descending else "ASC"
        return f"{col} {direction}"


class ConstructionQueryBuilder:
    """
    Build SQL queries for construction databases.
    Based on DDC methodology Chapter 2.1.

    Every builder method returns a ParameterizedQuery (sql, params). Values are
    bound by the database driver, never inlined into the SQL text. paramstyle
    selects the placeholder: "format" (%s, psycopg/MySQL drivers), "qmark"
    (?, sqlite3/pyodbc) or "numeric_dollar" ($1, asyncpg).
    """

    def __init__(self, dialect: str = "postgresql", paramstyle: Optional[str] = None):
        self.dialect = dialect
        self.paramstyle = paramstyle or _DEFAULT_PARAMSTYLE.get(dialect, "qmark")
        if self.paramstyle not in _PARAMSTYLES:
            raise ValueError(f"paramstyle must be one of {_PARAMSTYLES}")
        self.schemas = self._define_construction_schemas()

    def _params(self) -> QueryParams:
        return QueryParams(self.paramstyle)

    def _define_construction_schemas(self) -> Dict[str, Dict]:
        """Define common construction database schemas"""
        return {
            "projects": {
                "columns": ["id", "name", "code", "status", "start_date", "end_date", "budget", "client_id"],
                "primary_key": "id"
            },
            "cost_items": {
                "columns": ["id", "project_id", "wbs_code", "description", "budgeted_cost", "actual_cost", "committed_cost"],
                "primary_key": "id",
                "foreign_keys": {"project_id": "projects.id"}
            },
            "activities": {
                "columns": ["id", "project_id", "name", "wbs_code", "start_date", "end_date", "duration", "status", "percent_complete"],
                "primary_key": "id",
                "foreign_keys": {"project_id": "projects.id"}
            },
            "resources": {
                "columns": ["id", "name", "type", "rate", "unit"],
                "primary_key": "id"
            },
            "resource_assignments": {
                "columns": ["id", "activity_id", "resource_id", "units", "cost"],
                "primary_key": "id",
                "foreign_keys": {"activity_id": "activities.id", "resource_id": "resources.id"}
            },
            "change_orders": {
                "columns": ["id", "project_id", "number", "description", "amount", "status", "submitted_date", "approved_date"],
                "primary_key": "id",
                "foreign_keys": {"project_id": "projects.id"}
            },
            "invoices": {
                "columns": ["id", "project_id", "number", "amount", "status", "invoice_date", "due_date", "paid_date"],
                "primary_key": "id",
                "foreign_keys": {"project_id": "projects.id"}
            },
            "materials": {
                "columns": ["id", "name", "category", "unit", "unit_cost"],
                "primary_key": "id"
            },
            "material_requisitions": {
                "columns": ["id", "project_id", "material_id", "quantity", "required_date", "status"],
                "primary_key": "id",
                "foreign_keys": {"project_id": "projects.id", "material_id": "materials.id"}
            },
            "daily_reports": {
                "columns": ["id", "project_id", "report_date", "weather", "temperature", "crew_count", "notes"],
                "primary_key": "id",
                "foreign_keys": {"project_id": "projects.id"}
            }
        }

    def select(
        self,
        table: str,
        columns: List[Union[str, SelectColumn]],
        conditions: Optional[List[WhereCondition]] = None,
        order_by: Optional[List[OrderBy]] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        group_by: Optional[List[str]] = None
    ) -> ParameterizedQuery:
        """Build a SELECT query"""
        params = self._params()

        # Format columns
        cols = []
        for col in columns:
            if isinstance(col, str):
                cols.append(validate_identifier(col, allow_star=True))
            else:
                cols.append(col.to_sql())

        query = f"SELECT {', '.join(cols)}\nFROM {validate_identifier(table)}"

        # WHERE clause
        if conditions:
            where_parts = [c.to_sql(params) for c in conditions]
            query += f"\nWHERE {' AND '.join(where_parts)}"

        # GROUP BY
        if group_by:
            query += f"\nGROUP BY {', '.join(validate_identifier(g) for g in group_by)}"

        # ORDER BY
        if order_by:
            order_parts = [o.to_sql() for o in order_by]
            query += f"\nORDER BY {', '.join(order_parts)}"

        # LIMIT/OFFSET
        if limit is not None:
            query += f"\nLIMIT {validate_non_negative_int(limit, 'limit')}"
        if offset is not None:
            query += f"\nOFFSET {validate_non_negative_int(offset, 'offset')}"

        return ParameterizedQuery(query + ";", params.values)

    def select_with_joins(
        self,
        main_table: str,
        main_alias: str,
        columns: List[SelectColumn],
        joins: List[JoinClause],
        conditions: Optional[List[WhereCondition]] = None,
        group_by: Optional[List[str]] = None,
        having: Optional[List[WhereCondition]] = None,
        order_by: Optional[List[OrderBy]] = None,
        limit: Optional[int] = None
    ) -> ParameterizedQuery:
        """Build a SELECT query with JOINs"""
        params = self._params()

        # Columns
        cols = [col.to_sql() for col in columns]
        query = (
            f"SELECT {', '.join(cols)}\n"
            f"FROM {validate_identifier(main_table)} {validate_identifier(main_alias)}"
        )

        # JOINs
        for join in joins:
            query += f"\n{join.to_sql()}"

        # WHERE
        if conditions:
            where_parts = [c.to_sql(params) for c in conditions]
            query += f"\nWHERE {' AND '.join(where_parts)}"

        # GROUP BY
        if group_by:
            query += f"\nGROUP BY {', '.join(validate_identifier(g) for g in group_by)}"

        # HAVING
        if having:
            having_parts = [h.to_sql(params) for h in having]
            query += f"\nHAVING {' AND '.join(having_parts)}"

        # ORDER BY
        if order_by:
            order_parts = [o.to_sql() for o in order_by]
            query += f"\nORDER BY {', '.join(order_parts)}"

        # LIMIT
        if limit is not None:
            query += f"\nLIMIT {validate_non_negative_int(limit, 'limit')}"

        return ParameterizedQuery(query + ";", params.values)

    def insert(
        self,
        table: str,
        data: Dict[str, Any]
    ) -> ParameterizedQuery:
        """Build an INSERT query"""
        params = self._params()
        columns = [validate_identifier(c) for c in data.keys()]
        placeholders = [params.add(v) for v in data.values()]

        sql = (
            f"INSERT INTO {validate_identifier(table)} ({', '.join(columns)})\n"
            f"VALUES ({', '.join(placeholders)});"
        )
        return ParameterizedQuery(sql, params.values)

    def insert_many(
        self,
        table: str,
        columns: List[str],
        values: List[List[Any]]
    ) -> ParameterizedQuery:
        """Build a bulk INSERT query"""
        params = self._params()
        columns = [validate_identifier(c) for c in columns]
        formatted_values = []

        for row in values:
            row = list(row)
            if len(row) != len(columns):
                raise ValueError("Each row must have one value per column")
            formatted_values.append(f"({', '.join(params.add(v) for v in row)})")

        sql = (
            f"INSERT INTO {validate_identifier(table)} ({', '.join(columns)})\n"
            f"VALUES\n{','.join(formatted_values)};"
        )
        return ParameterizedQuery(sql, params.values)

    def update(
        self,
        table: str,
        data: Dict[str, Any],
        conditions: List[WhereCondition]
    ) -> ParameterizedQuery:
        """Build an UPDATE query"""
        params = self._params()
        set_parts = [f"{validate_identifier(col)} = {params.add(val)}" for col, val in data.items()]
        where_parts = [c.to_sql(params) for c in conditions]

        sql = (
            f"UPDATE {validate_identifier(table)}\n"
            f"SET {', '.join(set_parts)}\n"
            f"WHERE {' AND '.join(where_parts)};"
        )
        return ParameterizedQuery(sql, params.values)

    def delete(
        self,
        table: str,
        conditions: List[WhereCondition]
    ) -> ParameterizedQuery:
        """Build a DELETE query"""
        params = self._params()
        where_parts = [c.to_sql(params) for c in conditions]
        return ParameterizedQuery(
            f"DELETE FROM {validate_identifier(table)}\nWHERE {' AND '.join(where_parts)};",
            params.values,
        )

    # Construction-specific query templates
    def project_cost_summary(self, project_id: int) -> str:
        """Generate project cost summary query"""
        return self.select_with_joins(
            main_table="cost_items",
            main_alias="ci",
            columns=[
                SelectColumn("wbs_code", "ci"),
                SelectColumn("description", "ci"),
                SelectColumn("budgeted_cost", "ci", AggregateFunction.SUM, "total_budget"),
                SelectColumn("actual_cost", "ci", AggregateFunction.SUM, "total_actual"),
                SelectColumn("committed_cost", "ci", AggregateFunction.SUM, "total_committed")
            ],
            joins=[
                JoinClause("projects", "p", JoinType.INNER, "p.id", "ci.project_id")
            ],
            conditions=[
                WhereCondition("project_id", ComparisonOperator.EQ, project_id, "ci")
            ],
            group_by=["ci.wbs_code", "ci.description"],
            order_by=[OrderBy("wbs_code", table_alias="ci")]
        )

    def schedule_progress(self, project_id: int) -> str:
        """Generate schedule progress query"""
        return self.select_with_joins(
            main_table="activities",
            main_alias="a",
            columns=[
                SelectColumn("wbs_code", "a"),
                SelectColumn("name", "a"),
                SelectColumn("start_date", "a"),
                SelectColumn("end_date", "a"),
                SelectColumn("percent_complete", "a"),
                SelectColumn("status", "a")
            ],
            joins=[
                JoinClause("projects", "p", JoinType.INNER, "p.id", "a.project_id")
            ],
            conditions=[
                WhereCondition("project_id", ComparisonOperator.EQ, project_id, "a"),
                WhereCondition("status", ComparisonOperator.NE, "Completed", "a")
            ],
            order_by=[
                OrderBy("start_date", table_alias="a"),
                OrderBy("wbs_code", table_alias="a")
            ]
        )

    def resource_utilization(self, project_id: int) -> str:
        """Generate resource utilization query"""
        return self.select_with_joins(
            main_table="resource_assignments",
            main_alias="ra",
            columns=[
                SelectColumn("name", "r", alias="resource_name"),
                SelectColumn("type", "r", alias="resource_type"),
                SelectColumn("units", "ra", AggregateFunction.SUM, "total_units"),
                SelectColumn("cost", "ra", AggregateFunction.SUM, "total_cost")
            ],
            joins=[
                JoinClause("resources", "r", JoinType.INNER, "r.id", "ra.resource_id"),
                JoinClause("activities", "a", JoinType.INNER, "a.id", "ra.activity_id")
            ],
            conditions=[
                WhereCondition("project_id", ComparisonOperator.EQ, project_id, "a")
            ],
            group_by=["r.name", "r.type"],
            order_by=[OrderBy("total_cost", descending=True)]
        )

    def change_order_summary(self, project_id: int) -> ParameterizedQuery:
        """Generate change order summary query"""
        return self.select(
            table="change_orders",
            columns=[
                SelectColumn("status"),
                SelectColumn("status", aggregate=AggregateFunction.COUNT, alias="count"),
                SelectColumn("amount", aggregate=AggregateFunction.SUM, alias="total_amount")
            ],
            conditions=[
                WhereCondition("project_id", ComparisonOperator.EQ, project_id)
            ],
            group_by=["status"]
        )

    def cash_flow_projection(self, project_id: int) -> ParameterizedQuery:
        """Generate cash flow projection query"""
        params = self._params()
        sql = f"""
SELECT
    DATE_TRUNC('month', invoice_date) AS month,
    SUM(CASE WHEN status = 'paid' THEN amount ELSE 0 END) AS received,
    SUM(CASE WHEN status = 'pending' THEN amount ELSE 0 END) AS pending,
    SUM(amount) AS total
FROM invoices
WHERE project_id = {params.add(project_id)}
GROUP BY DATE_TRUNC('month', invoice_date)
ORDER BY month;
"""
        return ParameterizedQuery(sql, params.values)

    def material_requirements(self, project_id: int, from_date: date, to_date: date) -> str:
        """Generate material requirements query"""
        return self.select_with_joins(
            main_table="material_requisitions",
            main_alias="mr",
            columns=[
                SelectColumn("name", "m", alias="material_name"),
                SelectColumn("category", "m"),
                SelectColumn("quantity", "mr", AggregateFunction.SUM, "total_quantity"),
                SelectColumn("unit", "m"),
                SelectColumn("required_date", "mr", AggregateFunction.MIN, "earliest_need")
            ],
            joins=[
                JoinClause("materials", "m", JoinType.INNER, "m.id", "mr.material_id")
            ],
            conditions=[
                WhereCondition("project_id", ComparisonOperator.EQ, project_id, "mr"),
                WhereCondition("required_date", ComparisonOperator.BETWEEN, [from_date, to_date], "mr"),
                WhereCondition("status", ComparisonOperator.NE, "Cancelled", "mr")
            ],
            group_by=["m.name", "m.category", "m.unit"],
            order_by=[OrderBy("earliest_need")]
        )

    def daily_report_summary(self, project_id: int, month: str) -> ParameterizedQuery:
        """Generate daily report summary for a month"""
        params = self._params()
        sql = f"""
SELECT
    COUNT(*) AS total_reports,
    AVG(crew_count) AS avg_crew,
    COUNT(CASE WHEN weather = 'Rain' THEN 1 END) AS rain_days,
    COUNT(CASE WHEN weather = 'Clear' THEN 1 END) AS clear_days
FROM daily_reports
WHERE project_id = {params.add(project_id)}
  AND TO_CHAR(report_date, 'YYYY-MM') = {params.add(month)};
"""
        return ParameterizedQuery(sql, params.values)

    def generate_parameterized(
        self,
        query: str,
        params: Dict[str, Any]
    ) -> ParameterizedQuery:
        """Convert a hand-written query with :name placeholders to driver placeholders.

        Each :name is replaced in order of appearance and its value is bound, so
        values must be passed here rather than formatted into ``query``.
        """
        bound = self._params()
        if bound.paramstyle == "format":
            query = query.replace("%", "%%")

        def substitute(match: "re.Match") -> str:
            name = match.group(1)
            if name not in params:
                raise KeyError(f"No value supplied for :{name}")
            return bound.add(params[name])

        param_query = re.sub(r"(?<![:\w]):([A-Za-z_][A-Za-z0-9_]*)", substitute, query)
        return ParameterizedQuery(param_query, bound.values)


class QueryOptimizer:
    """Optimize SQL queries for construction databases"""

    def suggest_indexes(self, queries: List[Union[str, ParameterizedQuery]]) -> List[str]:
        """Suggest indexes based on query patterns"""
        suggestions = []

        # Common construction query patterns
        patterns = {
            "project_id": "CREATE INDEX idx_{table}_project ON {table}(project_id);",
            "wbs_code": "CREATE INDEX idx_{table}_wbs ON {table}(wbs_code);",
            "status": "CREATE INDEX idx_{table}_status ON {table}(status);",
            "date": "CREATE INDEX idx_{table}_date ON {table}({date_col});"
        }

        for query in queries:
            if isinstance(query, tuple):
                query = query[0]
            query_lower = query.lower()

            # Detect table from FROM clause
            if "from " in query_lower:
                table = query_lower.split("from ")[1].split()[0].rstrip(";")
                if not _IDENTIFIER_PART.fullmatch(table):
                    continue

                if "project_id" in query_lower:
                    suggestions.append(patterns["project_id"].format(table=table))
                if "wbs_code" in query_lower:
                    suggestions.append(patterns["wbs_code"].format(table=table))
                if "status" in query_lower:
                    suggestions.append(patterns["status"].format(table=table))

        return list(set(suggestions))

    def analyze_query(self, query: Union[str, ParameterizedQuery]) -> Dict:
        """Analyze query for optimization opportunities"""
        if isinstance(query, tuple):
            query = query[0]
        analysis = {
            "has_select_star": "*" in query and "SELECT *" in query.upper(),
            "has_indexes_hint": False,
            "join_count": query.upper().count("JOIN"),
            "subquery_count": query.upper().count("SELECT") - 1,
            "recommendations": []
        }

        if analysis["has_select_star"]:
            analysis["recommendations"].append(
                "Avoid SELECT * - specify only needed columns"
            )

        if analysis["join_count"] > 3:
            analysis["recommendations"].append(
                "Consider breaking down query with CTEs for readability"
            )

        if analysis["subquery_count"] > 0:
            analysis["recommendations"].append(
                "Consider replacing subqueries with JOINs where possible"
            )

        return analysis
```

## Common Use Cases

Builder methods return `(sql, params)`. Always execute them with the driver's
parameter binding, e.g. `cursor.execute(sql, params)`. Never format `params`
back into the SQL text: values from spreadsheets, uploads or user requests must
stay bound parameters. Table, column and alias names must be plain identifiers.

### Build Cost Summary Query

```python
builder = ConstructionQueryBuilder()  # PostgreSQL, %s placeholders (psycopg)
# ConstructionQueryBuilder(dialect="sqlite")                  -> ? placeholders
# ConstructionQueryBuilder(paramstyle="numeric_dollar")       -> $1 placeholders (asyncpg)

# Get project cost summary
sql, params = builder.project_cost_summary(project_id=123)
cursor.execute(sql, params)
```

### Build Custom SELECT Query

```python
sql, params = builder.select(
    table="activities",
    columns=[
        SelectColumn("name"),
        SelectColumn("status"),
        SelectColumn("percent_complete")
    ],
    conditions=[
        WhereCondition("project_id", ComparisonOperator.EQ, 123),
        WhereCondition("status", ComparisonOperator.IN, ["In Progress", "Delayed"])
    ],
    order_by=[OrderBy("percent_complete", descending=True)],
    limit=10
)
rows = cursor.execute(sql, params).fetchall()
```

### Build JOIN Query

```python
sql, params = builder.select_with_joins(
    main_table="invoices",
    main_alias="i",
    columns=[
        SelectColumn("name", "p", alias="project_name"),
        SelectColumn("number", "i", alias="invoice_no"),
        SelectColumn("amount", "i")
    ],
    joins=[
        JoinClause("projects", "p", JoinType.INNER, "p.id", "i.project_id")
    ],
    conditions=[
        WhereCondition("status", ComparisonOperator.EQ, "pending", "i")
    ]
)
cursor.execute(sql, params)
```

### Insert and Update Data

```python
# Insert new cost item
sql, params = builder.insert(
    table="cost_items",
    data={
        "project_id": 123,
        "wbs_code": "03.01.01",
        "description": "Concrete Foundation",
        "budgeted_cost": 50000,
        "actual_cost": 0
    }
)
cursor.execute(sql, params)

# Update progress
sql, params = builder.update(
    table="activities",
    data={"percent_complete": 75, "status": "In Progress"},
    conditions=[
        WhereCondition("id", ComparisonOperator.EQ, 456)
    ]
)
cursor.execute(sql, params)
```

## Quick Reference

| Component | Purpose |
|-----------|---------|
| `ConstructionQueryBuilder` | Main query builder |
| `ParameterizedQuery` | `(sql, params)` result to pass to `cursor.execute` |
| `WhereCondition` | WHERE clause conditions |
| `JoinClause` | JOIN definitions |
| `SelectColumn` | Column with aggregate/alias |
| `OrderBy` | ORDER BY clause |
| `QueryOptimizer` | Query optimization suggestions |

## Resources

- **Book**: "Data-Driven Construction" by Artem Boiko, Chapter 2.1
- **Website**: https://datadrivenconstruction.io

## Next Steps

- Use [data-type-classifier](../data-type-classifier/SKILL.md) to identify data types
- Use [etl-pipeline](../../Chapter-4.2/etl-pipeline/SKILL.md) for data integration
- Use [kpi-dashboard](../../Chapter-4.1/kpi-dashboard/SKILL.md) for visualization

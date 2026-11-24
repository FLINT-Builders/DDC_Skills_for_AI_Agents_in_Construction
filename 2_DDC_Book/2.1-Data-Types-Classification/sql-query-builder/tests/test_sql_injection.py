"""Security spec for the SQL query builder shipped in SKILL.md.

The tests load the first Python block of SKILL.md and run it. They fail while
string values are spliced into SQL, and they pass once every user-supplied
value is either bound as a parameter or kept inside one SQL string literal
(embedded single quotes doubled). Integer-typed arguments may instead be
rejected with TypeError or ValueError.

Accepted builder results are a SQL string, or a (sql, params) tuple whose
placeholders are ?, %s, or $1..$n in parameter order.

These tests are expected to fail until that fix lands.

    python3 -m unittest discover -s 2_DDC_Book/2.1-Data-Types-Classification/sql-query-builder/tests -v

Not covered here, because they do not format user values with this quote bug:
IS NULL and IS NOT NULL (the value is not rendered), date and datetime values
(isoformat only), and identifier interpolation in table, column, alias, JOIN,
ORDER BY, GROUP BY, and QueryOptimizer.suggest_indexes. Template helpers
project_cost_summary, schedule_progress, resource_utilization,
change_order_summary, and material_requirements pass values through
WhereCondition, which is exercised below.
"""

import sqlite3
import unittest
from pathlib import Path


SKILL_PATH = Path(__file__).resolve().parents[1] / "SKILL.md"


def load_builder_namespace():
    """Execute the implementation fence from SKILL.md. Later fences are examples."""
    text = SKILL_PATH.read_text(encoding="utf-8")
    marker = "```python\n"
    start = text.index(marker) + len(marker)
    end = text.index("\n```", start)
    namespace = {}
    exec(compile(text[start:end], str(SKILL_PATH), "exec"), namespace)
    return namespace


NS = load_builder_namespace()
ConstructionQueryBuilder = NS["ConstructionQueryBuilder"]
WhereCondition = NS["WhereCondition"]
ComparisonOperator = NS["ComparisonOperator"]
SelectColumn = NS["SelectColumn"]
JoinClause = NS["JoinClause"]
JoinType = NS["JoinType"]


def bind_params(sql, params):
    params = tuple(params)
    if "%s" in sql:
        sql = sql.replace("%s", "?")
    elif "$" in sql:
        numbers = []
        cursor = 0
        while True:
            found = sql.find("$", cursor)
            if found < 0:
                break
            end = found + 1
            while end < len(sql) and sql[end].isdigit():
                end += 1
            if end > found + 1:
                numbers.append(int(sql[found + 1:end]))
            cursor = found + 1
        for number in sorted(set(numbers), reverse=True):
            sql = sql.replace(f"${number}", "?")
    return sql, params


def apply_dml(conn, result):
    """Run a write statement. A full script is used so a second statement is not dropped."""
    try:
        if isinstance(result, tuple):
            sql, params = bind_params(result[0], result[1])
            conn.execute(sql, params)
        else:
            conn.executescript(result)
    except sqlite3.Error:
        return


def query_rows(conn, result):
    if isinstance(result, tuple):
        sql, params = bind_params(result[0], result[1])
        return conn.execute(sql, params).fetchall()
    return conn.execute(result).fetchall()


def decoded_string_literals(sql):
    """SQL string literals with '' decoded back to a single quote. Line comments are skipped."""
    literals = []
    index = 0
    length = len(sql)
    while index < length:
        char = sql[index]
        if char == "-" and index + 1 < length and sql[index + 1] == "-":
            while index < length and sql[index] != "\n":
                index += 1
            continue
        if char != "'":
            index += 1
            continue
        index += 1
        chars = []
        while index < length:
            if sql[index] == "'" and index + 1 < length and sql[index + 1] == "'":
                chars.append("'")
                index += 2
                continue
            if sql[index] == "'":
                index += 1
                break
            chars.append(sql[index])
            index += 1
        literals.append("".join(chars))
    return literals


def assert_payload_is_data(result, payload):
    """The payload is a bound parameter or one complete string literal, never raw SQL."""
    if isinstance(result, tuple):
        sql, params = result
        assert payload in tuple(params), (sql, params)
        assert payload not in sql, sql
        return
    assert isinstance(result, str), type(result)
    literals = decoded_string_literals(result)
    assert payload in literals, (literals, result)


def new_db():
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE projects (
            id INTEGER PRIMARY KEY, name TEXT, status TEXT
        );
        CREATE TABLE cost_items (
            id INTEGER PRIMARY KEY,
            project_id INT,
            wbs_code TEXT,
            description TEXT,
            budgeted_cost REAL,
            actual_cost REAL,
            committed_cost REAL
        );
        CREATE TABLE activities (
            id INTEGER PRIMARY KEY,
            project_id INT,
            name TEXT,
            status TEXT,
            percent_complete INT
        );
        CREATE TABLE invoices (
            id INTEGER PRIMARY KEY,
            project_id INT,
            number TEXT,
            amount REAL,
            status TEXT
        );
        INSERT INTO projects (id, name, status) VALUES (1, 'Tower', 'active');
        INSERT INTO activities (id, project_id, name, status, percent_complete) VALUES
            (1, 1, 'Pour', 'In Progress', 10),
            (2, 1, 'Cure', 'Completed', 100);
        INSERT INTO invoices (id, project_id, number, amount, status) VALUES
            (1, 1, 'INV-SECRET', 9999, 'pending'),
            (2, 1, 'INV-2', 50, 'paid');
        """
    )
    return conn


class SqlInjectionTests(unittest.TestCase):
    def setUp(self):
        self.builder = ConstructionQueryBuilder()
        self.conn = new_db()

    def test_benign_documented_insert_round_trips(self):
        description = "Concrete Foundation"
        sql = self.builder.insert(
            table="cost_items",
            data={
                "project_id": 123,
                "wbs_code": "03.01.01",
                "description": description,
                "budgeted_cost": 50000,
                "actual_cost": 0,
            },
        )
        apply_dml(self.conn, sql)
        stored = self.conn.execute("SELECT description FROM cost_items").fetchall()
        self.assertEqual(stored, [(description,)])

    def test_apostrophe_in_description_is_stored_literally(self):
        payload = "O'Brien footings"
        sql = self.builder.insert(
            "cost_items",
            {"project_id": 1, "description": payload},
        )
        apply_dml(self.conn, sql)
        stored = self.conn.execute("SELECT description FROM cost_items").fetchall()
        self.assertEqual(stored, [(payload,)])

    def test_insert_description_cannot_delete_invoices(self):
        # Same shape as the documented cost_items insert. The value closes the
        # quote, finishes the VALUES list, and appends a second statement.
        payload = "Concrete', 0, 0); DELETE FROM invoices; --"
        sql = self.builder.insert(
            "cost_items",
            {
                "project_id": 123,
                "wbs_code": "03.01.01",
                "description": payload,
                "budgeted_cost": 50000,
                "actual_cost": 0,
            },
        )
        apply_dml(self.conn, sql)
        stored = self.conn.execute("SELECT description FROM cost_items").fetchall()
        invoices = self.conn.execute("SELECT number FROM invoices ORDER BY id").fetchall()
        self.assertEqual(stored, [(payload,)])
        self.assertEqual(invoices, [("INV-SECRET",), ("INV-2",)])

    def test_insert_exact_attack_string_is_data(self):
        payload = "Concrete'); DELETE FROM invoices; --"
        sql = self.builder.insert(
            "cost_items",
            {
                "project_id": 123,
                "wbs_code": "03.01.01",
                "description": payload,
                "budgeted_cost": 50000,
                "actual_cost": 0,
            },
        )
        apply_dml(self.conn, sql)
        stored = self.conn.execute("SELECT description FROM cost_items").fetchall()
        invoices = self.conn.execute("SELECT COUNT(*) FROM invoices").fetchone()[0]
        self.assertEqual(stored, [(payload,)])
        self.assertEqual(invoices, 2)

    def test_insert_subquery_cannot_copy_invoice_number(self):
        payload = "x' || (SELECT number FROM invoices) || '"
        sql = self.builder.insert(
            "cost_items",
            {"project_id": 1, "description": payload},
        )
        apply_dml(self.conn, sql)
        stored = self.conn.execute("SELECT description FROM cost_items").fetchall()
        self.assertEqual(stored, [(payload,)])
        self.assertNotIn("INV-SECRET", stored[0][0] if stored else "")

    def test_generate_parameterized_does_not_leave_inlined_description(self):
        payload = "x' || (SELECT number FROM invoices) || '"
        inlined = self.builder.insert(
            "cost_items",
            {"project_id": 1, "description": payload},
        )
        if isinstance(inlined, tuple):
            apply_dml(self.conn, inlined)
        else:
            rewritten, params = self.builder.generate_parameterized(inlined, {})
            apply_dml(self.conn, (rewritten, params) if params else rewritten)
        stored = self.conn.execute("SELECT description FROM cost_items").fetchall()
        self.assertEqual(stored, [(payload,)])

    def test_insert_many_last_value_cannot_delete_invoices(self):
        payload = "ok'); DELETE FROM invoices; --"
        sql = self.builder.insert_many(
            "cost_items",
            ["project_id", "description"],
            [[1, payload]],
        )
        apply_dml(self.conn, sql)
        stored = self.conn.execute("SELECT description FROM cost_items").fetchall()
        invoices = self.conn.execute("SELECT COUNT(*) FROM invoices").fetchone()[0]
        self.assertEqual(stored, [(payload,)])
        self.assertEqual(invoices, 2)

    def test_update_set_string_cannot_rewrite_statement(self):
        payload = "In Progress'; DELETE FROM invoices; --"
        sql = self.builder.update(
            "activities",
            {"status": payload},
            [WhereCondition("id", ComparisonOperator.EQ, 1)],
        )
        apply_dml(self.conn, sql)
        rows = self.conn.execute(
            "SELECT id, status FROM activities ORDER BY id"
        ).fetchall()
        invoices = self.conn.execute("SELECT COUNT(*) FROM invoices").fetchone()[0]
        self.assertEqual(rows, [(1, payload), (2, "Completed")])
        self.assertEqual(invoices, 2)

    def test_update_where_string_cannot_touch_other_rows(self):
        payload = "nobody' OR '1'='1"
        sql = self.builder.update(
            "activities",
            {"percent_complete": 5},
            [WhereCondition("status", ComparisonOperator.EQ, payload)],
        )
        apply_dml(self.conn, sql)
        changed = self.conn.execute(
            "SELECT id FROM activities WHERE percent_complete = 5"
        ).fetchall()
        self.assertEqual(changed, [])

    def test_delete_where_string_cannot_delete_other_rows(self):
        payload = "nobody' OR '1'='1"
        sql = self.builder.delete(
            "activities",
            [WhereCondition("status", ComparisonOperator.EQ, payload)],
        )
        apply_dml(self.conn, sql)
        remaining = self.conn.execute("SELECT id FROM activities ORDER BY id").fetchall()
        self.assertEqual(remaining, [(1,), (2,)])

    def test_select_eq_filter_cannot_return_other_rows_or_union(self):
        payload = "nope' UNION SELECT number, amount FROM invoices --"
        sql = self.builder.select(
            "activities",
            ["id", "status"],
            [WhereCondition("status", ComparisonOperator.EQ, payload)],
        )
        rows = query_rows(self.conn, sql)
        self.assertEqual(rows, [])
        invoices = self.conn.execute("SELECT COUNT(*) FROM invoices").fetchone()[0]
        self.assertEqual(invoices, 2)

    def test_select_eq_matches_only_the_literal_status(self):
        payload = "zzz' OR '1'='1"
        self.conn.execute(
            "INSERT INTO activities (id, project_id, name, status, percent_complete) VALUES (3, 1, 'Odd', ?, 0)",
            (payload,),
        )
        sql = self.builder.select(
            "activities",
            ["id"],
            [
                WhereCondition("status", ComparisonOperator.EQ, payload),
                WhereCondition("project_id", ComparisonOperator.EQ, 1),
            ],
        )
        rows = query_rows(self.conn, sql)
        self.assertEqual(rows, [(3,)])

    def test_select_ne_string_is_data(self):
        payload = "x' AND '1'='0"
        sql = self.builder.select(
            "activities",
            ["id"],
            [WhereCondition("status", ComparisonOperator.NE, payload)],
        )
        # Neither stored status equals that literal, so both rows stay.
        # Breaking the quote turns the predicate into AND '1'='0'.
        rows = query_rows(self.conn, sql)
        self.assertEqual(sorted(row[0] for row in rows), [1, 2])

    def test_like_value_cannot_match_every_name(self):
        payload = "%' OR '1'='1"
        sql = self.builder.select(
            "activities",
            ["id"],
            [WhereCondition("name", ComparisonOperator.LIKE, payload)],
        )
        rows = query_rows(self.conn, sql)
        self.assertEqual(rows, [])

    def test_in_list_cannot_or_true(self):
        payload = "nope') OR ('1'='1"
        sql = self.builder.select(
            "activities",
            ["id"],
            [WhereCondition("status", ComparisonOperator.IN, ["absent", payload])],
        )
        rows = query_rows(self.conn, sql)
        self.assertEqual(rows, [])

    def test_between_strings_cannot_or_true(self):
        payload_hi = "z' OR '1'='1"
        sql = self.builder.select(
            "activities",
            ["id"],
            [WhereCondition("status", ComparisonOperator.BETWEEN, ["a", payload_hi])],
        )
        rows = query_rows(self.conn, sql)
        self.assertEqual(rows, [])

    def test_join_where_string_cannot_return_every_invoice(self):
        payload = "zzz' OR '1'='1"
        sql = self.builder.select_with_joins(
            main_table="invoices",
            main_alias="i",
            columns=[SelectColumn("number", "i")],
            joins=[
                JoinClause("projects", "p", JoinType.INNER, "p.id", "i.project_id")
            ],
            conditions=[
                WhereCondition("status", ComparisonOperator.EQ, payload, "i")
            ],
        )
        rows = query_rows(self.conn, sql)
        self.assertEqual(rows, [])

    def test_having_string_cannot_keep_every_group(self):
        payload = "zzz' OR '1'='1"
        sql = self.builder.select_with_joins(
            main_table="activities",
            main_alias="a",
            columns=[SelectColumn("status", "a")],
            joins=[],
            group_by=["a.status"],
            having=[WhereCondition("status", ComparisonOperator.EQ, payload, "a")],
        )
        rows = query_rows(self.conn, sql)
        self.assertEqual(rows, [])

    def test_daily_report_month_is_one_literal(self):
        payload = "2024-01'; DELETE FROM invoices; --"
        try:
            sql = self.builder.daily_report_summary(1, payload)
        except (TypeError, ValueError):
            self.fail("month is a string input and must be preserved as data")
        assert_payload_is_data(sql, payload)

    def test_cash_flow_project_id_is_not_raw_sql(self):
        payload = "1; DELETE FROM invoices; --"
        try:
            sql = self.builder.cash_flow_projection(payload)
        except (TypeError, ValueError):
            return
        assert_payload_is_data(sql, payload)

    def test_limit_and_offset_are_not_raw_sql(self):
        payload = "1; DELETE FROM invoices; --"
        try:
            sql = self.builder.select(
                "invoices",
                ["number"],
                limit=payload,
                offset=payload,
            )
        except (TypeError, ValueError):
            return
        apply_dml(self.conn, sql)
        invoices = self.conn.execute("SELECT COUNT(*) FROM invoices").fetchone()[0]
        self.assertEqual(invoices, 2)

    def test_join_limit_is_not_raw_sql(self):
        payload = "1; DELETE FROM invoices; --"
        try:
            sql = self.builder.select_with_joins(
                main_table="invoices",
                main_alias="i",
                columns=[SelectColumn("number", "i")],
                joins=[],
                limit=payload,
            )
        except (TypeError, ValueError):
            return
        apply_dml(self.conn, sql)
        invoices = self.conn.execute("SELECT COUNT(*) FROM invoices").fetchone()[0]
        self.assertEqual(invoices, 2)


if __name__ == "__main__":
    unittest.main()

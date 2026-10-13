"""Checks and rewrites every SQL query before it reaches BigQuery.

The model writes queries against four tables by their bare names. Before running them we:

1. parse the SQL and accept only a single read-only SELECT,
2. reject any reference to a PII column,
3. replace each table with a subquery that only contains the rows the user may see
   (their products, and the order items / orders / customers linked to them) and,
   for ``users``, only the non-PII columns.

So scope and PII protection do not depend on the model writing the right WHERE clause.
"""

from __future__ import annotations

import sqlglot
from sqlglot import exp

from retail_analyst.users import User

DATASET = "bigquery-public-data.thelook_ecommerce"
ALLOWED_TABLES = {"orders", "order_items", "products", "users"}
PII_COLUMNS = {"first_name", "last_name", "email", "street_address", "postal_code",
               "latitude", "longitude", "user_geom"}
SAFE_USER_COLUMNS = "id, age, gender, city, state, country, traffic_source, created_at"

_WRITE_NODES = (exp.Insert, exp.Update, exp.Delete, exp.Merge, exp.Create, exp.Drop,
                exp.Alter, exp.Command)


class UnsafeQuery(ValueError):
    """The query is not allowed; the message is shown to the model so it can rewrite it."""


def make_safe(sql: str, user: User) -> str:
    """Return the query rewritten onto the user's data, or raise ``UnsafeQuery``."""
    try:
        statements = [s for s in sqlglot.parse(sql, read="bigquery") if s is not None]
    except sqlglot.errors.ParseError as error:
        raise UnsafeQuery(f"SQL syntax error: {error}") from error
    if len(statements) != 1 or not isinstance(statements[0], exp.Query):
        raise UnsafeQuery("Only one SELECT statement is allowed.")
    tree = statements[0]
    if any(isinstance(node, _WRITE_NODES) for node in tree.walk()):
        raise UnsafeQuery("Only read-only SELECT statements are allowed.")

    for column in tree.find_all(exp.Column):
        if column.name.lower() in PII_COLUMNS:
            raise UnsafeQuery(f"Column '{column.name}' contains personal data and is not available.")

    cte_names = {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}
    if cte_names & ALLOWED_TABLES:
        raise UnsafeQuery("CTE names cannot reuse the table names.")

    for table in list(tree.find_all(exp.Table)):
        name = table.name.lower()
        if name in cte_names and not table.db:
            continue
        if not _is_allowed(table):
            raise UnsafeQuery(f"Table '{table.sql(dialect='bigquery')}' is not available. "
                              f"Use only: {', '.join(sorted(ALLOWED_TABLES))}.")
        scoped = sqlglot.parse_one(_scoped_table(name, user), read="bigquery")
        table.replace(scoped.subquery(table.alias or name))

    return tree.sql(dialect="bigquery")


def _is_allowed(table: exp.Table) -> bool:
    if table.name.lower() not in ALLOWED_TABLES:
        return False
    catalog, dataset = DATASET.split(".")
    if table.db and table.db != dataset:
        return False
    return not table.catalog or table.catalog == catalog


def _scoped_table(name: str, user: User) -> str:
    def source(table: str) -> str:
        return f"`{DATASET}.{table}`"

    columns = SAFE_USER_COLUMNS if name == "users" else "*"
    if user.all_products:
        return f"SELECT {columns} FROM {source(name)}"

    product_ids = f"SELECT id FROM {source('products')} WHERE {_product_filter(user)}"
    items = f"SELECT {{col}} FROM {source('order_items')} WHERE product_id IN ({product_ids})"
    where = {
        "products": _product_filter(user),
        "order_items": f"product_id IN ({product_ids})",
        "orders": f"order_id IN ({items.format(col='order_id')})",
        "users": f"id IN ({items.format(col='user_id')})",
    }[name]
    return f"SELECT {columns} FROM {source(name)} WHERE {where}"


def _product_filter(user: User) -> str:
    clauses = []
    if user.departments:
        clauses.append(f"department IN ({_literals(user.departments)})")
    if user.brands:
        clauses.append(f"brand IN ({_literals(user.brands)})")
    return " AND ".join(clauses)


def _literals(values: list[str]) -> str:
    return ", ".join("'" + v.replace("\\", "\\\\").replace("'", "\\'") + "'" for v in values)

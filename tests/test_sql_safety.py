import pytest

from retail_analyst.sql_safety import UnsafeQuery, make_safe
from retail_analyst.users import User

CEO = User(id="ceo", name="C", role="CEO", all_products=True)
LEVIS = User(id="levis", name="L", role="Brand manager", brands=["Levi's"])
MENSWEAR = User(id="men", name="M", role="Head of Menswear", departments=["Men"])


@pytest.mark.parametrize("sql", [
    "DELETE FROM orders WHERE TRUE",
    "DROP TABLE products",
    "SELECT 1; SELECT 2",
    "INSERT INTO orders (order_id) VALUES (1)",
])
def test_only_single_select_is_allowed(sql):
    with pytest.raises(UnsafeQuery):
        make_safe(sql, CEO)


@pytest.mark.parametrize("sql", [
    "SELECT email FROM users",
    "SELECT u.first_name, COUNT(*) FROM users u GROUP BY 1",
    "SELECT id FROM users WHERE street_address LIKE '%Main%'",
])
def test_pii_columns_are_rejected(sql):
    with pytest.raises(UnsafeQuery, match="personal data"):
        make_safe(sql, CEO)


def test_other_tables_are_rejected():
    with pytest.raises(UnsafeQuery, match="not available"):
        make_safe("SELECT * FROM events", CEO)
    with pytest.raises(UnsafeQuery, match="not available"):
        make_safe("SELECT * FROM `other-project.other.products`", CEO)


def test_cte_cannot_shadow_a_table():
    with pytest.raises(UnsafeQuery):
        make_safe("WITH products AS (SELECT 1 AS id) SELECT * FROM products", LEVIS)


def test_select_star_on_users_only_returns_safe_columns():
    sql = make_safe("SELECT * FROM users", CEO)
    assert "email" not in sql
    assert "SELECT id, age, gender" in sql


def test_unrestricted_user_reads_full_tables():
    sql = make_safe("SELECT SUM(sale_price) FROM order_items", CEO)
    assert "`bigquery-public-data.thelook_ecommerce.order_items`" in sql
    assert "WHERE" not in sql


def test_scope_is_applied_to_every_table():
    sql = make_safe(
        "SELECT p.category, SUM(oi.sale_price) FROM order_items oi "
        "JOIN products p ON p.id = oi.product_id GROUP BY 1", LEVIS)
    assert sql.count("brand IN ('Levi\\'s')") == 2  # products and order_items
    assert ") AS oi" in sql and ") AS p" in sql


def test_scope_cannot_be_escaped_by_omitting_the_join():
    sql = make_safe("SELECT COUNT(*) FROM orders", MENSWEAR)
    assert "department IN ('Men')" in sql


def test_ctes_written_by_the_model_still_work():
    sql = make_safe("WITH t AS (SELECT user_id FROM order_items) SELECT COUNT(*) FROM t", MENSWEAR)
    assert "department IN ('Men')" in sql

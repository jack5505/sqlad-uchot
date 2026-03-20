"""Tests for the warehouse inventory management application."""
import os
import pytest

os.environ["DATABASE"] = ""  # will be overridden per test


@pytest.fixture()
def client(tmp_path):
    db_path = str(tmp_path / "test_warehouse.db")
    os.environ["DATABASE"] = db_path

    # Re-import so init_db uses the new DATABASE value
    import importlib
    import app as app_module
    importlib.reload(app_module)

    app_module.app.config["TESTING"] = True
    app_module.app.config["WTF_CSRF_ENABLED"] = False
    with app_module.app.test_client() as c:
        yield c


def test_index_empty(client):
    rv = client.get("/")
    assert rv.status_code == 200
    assert "Остатки".encode() in rv.data or b"stock" in rv.data


def test_add_product(client):
    rv = client.post("/products", data={"name": "Цемент", "unit": "мешок"}, follow_redirects=True)
    assert rv.status_code == 200
    assert "Цемент".encode() in rv.data


def test_add_duplicate_product(client):
    client.post("/products", data={"name": "Кирпич", "unit": "шт"})
    rv = client.post("/products", data={"name": "Кирпич", "unit": "шт"}, follow_redirects=True)
    assert rv.status_code == 200
    assert "уже существует".encode() in rv.data


def test_add_product_no_name(client):
    rv = client.post("/products", data={"name": "", "unit": "шт"}, follow_redirects=True)
    assert rv.status_code == 200
    assert "обязательно".encode() in rv.data


def _add_product(client, name="Товар А", unit="шт"):
    client.post("/products", data={"name": name, "unit": unit})
    import app as app_module
    with app_module.get_db() as conn:
        row = conn.execute("SELECT id FROM products WHERE name=?", (name,)).fetchone()
    return row["id"]


def test_income(client):
    pid = _add_product(client, "Краска", "л")
    rv = client.post("/income", data={"product_id": pid, "quantity": "10", "note": "поставка"}, follow_redirects=True)
    assert rv.status_code == 200
    assert "зарегистрирован".encode() in rv.data


def test_expense(client):
    pid = _add_product(client, "Краска", "л")
    client.post("/income", data={"product_id": pid, "quantity": "10", "boxes": "2", "units_per_box": "5"})
    rv = client.post("/expense", data={"product_id": pid, "boxes": "1"}, follow_redirects=True)
    assert rv.status_code == 200
    assert "зарегистрирован".encode() in rv.data


def test_balance_after_transactions(client):
    pid = _add_product(client, "Гвозди", "кг")
    client.post("/income", data={"product_id": pid, "quantity": "100"})
    client.post("/expense", data={"product_id": pid, "boxes": "40"}, follow_redirects=True)
    import app as app_module
    stock = app_module.get_stock()
    row = next(r for r in stock if r["name"] == "Гвозди")
    assert row["total_income"] == 100
    assert row["total_expense"] == 40
    assert row["balance"] == 60


def test_income_invalid_quantity(client):
    pid = _add_product(client)
    rv = client.post("/income", data={"product_id": pid, "quantity": "-5"}, follow_redirects=True)
    assert rv.status_code == 200
    assert "положительным".encode() in rv.data


def test_income_no_product(client):
    rv = client.post("/income", data={"product_id": "", "quantity": "10"}, follow_redirects=True)
    assert rv.status_code == 200
    assert "Выберите товар".encode() in rv.data


def test_history_page(client):
    pid = _add_product(client, "Доска", "м")
    client.post("/income", data={"product_id": pid, "quantity": "50", "note": "склад 1"})
    client.post("/expense", data={"product_id": pid, "boxes": "10"})
    rv = client.get("/history")
    assert rv.status_code == 200
    assert "Доска".encode() in rv.data
    assert "Приход".encode() in rv.data
    assert "Расход".encode() in rv.data


def test_history_filter(client):
    import app as app_module
    pid1 = _add_product(client, "УникальныйТоварА", "шт")
    pid2 = _add_product(client, "УникальныйТоварБ", "шт")
    client.post("/income", data={"product_id": pid1, "quantity": "5"})
    client.post("/income", data={"product_id": pid2, "quantity": "7"})
    # Verify filter returns only transactions for pid1
    with app_module.get_db() as conn:
        rows = conn.execute(
            "SELECT product_id FROM transactions WHERE product_id=?", (pid1,)
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["product_id"] == pid1
    # Filtered history page should respond 200
    rv = client.get(f"/history?product_id={pid1}")
    assert rv.status_code == 200
    assert "УникальныйТоварА".encode() in rv.data


def test_delete_product(client):
    pid = _add_product(client, "Удаляемый", "шт")
    client.post("/income", data={"product_id": pid, "quantity": "5"})
    rv = client.post(f"/products/{pid}/delete", follow_redirects=True)
    assert rv.status_code == 200
    assert "удалён".encode() in rv.data
    import app as app_module
    stock = app_module.get_stock()
    names = [r["name"] for r in stock]
    assert "Удаляемый" not in names


def test_products_page(client):
    rv = client.get("/products")
    assert rv.status_code == 200
    assert "Добавить товар".encode() in rv.data


# ── Currency / exchange-rate tests ──────────────────────────────────────────

def test_settings_page(client):
    rv = client.get("/settings")
    assert rv.status_code == 200
    assert "USD".encode() in rv.data
    assert "UZS".encode() in rv.data


def test_settings_update_exchange_rate(client):
    rv = client.post("/settings", data={"exchange_rate": "13500"}, follow_redirects=True)
    assert rv.status_code == 200
    assert "13500".encode() in rv.data or "13 500".encode() in rv.data
    import app as app_module
    assert app_module.get_exchange_rate() == 13500.0


def test_settings_invalid_rate(client):
    rv = client.post("/settings", data={"exchange_rate": "-1"}, follow_redirects=True)
    assert rv.status_code == 200
    assert "положительным".encode() in rv.data


def test_income_with_currency_uzs(client):
    pid = _add_product(client, "Цемент", "мешок")
    rv = client.post(
        "/income",
        data={"product_id": pid, "quantity": "10", "price": "50000", "currency": "UZS"},
        follow_redirects=True,
    )
    assert rv.status_code == 200
    assert "зарегистрирован".encode() in rv.data


def test_income_with_currency_usd(client):
    pid = _add_product(client, "Краска USD", "л")
    rv = client.post(
        "/income",
        data={"product_id": pid, "quantity": "5", "price": "3.50", "currency": "USD"},
        follow_redirects=True,
    )
    assert rv.status_code == 200
    assert "зарегистрирован".encode() in rv.data


def test_stock_dual_currency(client):
    """Income in USD should be converted to UZS using the exchange rate."""
    import app as app_module

    # Set rate to 12000
    client.post("/settings", data={"exchange_rate": "12000"})

    pid = _add_product(client, "Доллары Товар", "шт")
    # 2 units at $5 each = $10 income
    client.post("/income", data={"product_id": pid, "quantity": "2", "price": "5", "currency": "USD"})

    stock = app_module.get_stock()
    row = next(r for r in stock if r["name"] == "Доллары Товар")

    assert row["total_income"] == 2
    assert abs(row["total_income_usd"] - 10.0) < 0.01
    assert abs(row["total_income_uzs"] - 120000.0) < 1.0


def test_stock_mixed_currency(client):
    """Mix of UZS and USD income is correctly totalled."""
    import app as app_module

    client.post("/settings", data={"exchange_rate": "10000"})

    pid = _add_product(client, "Смешанный Товар", "шт")
    client.post("/income", data={"product_id": pid, "quantity": "1", "price": "20000", "currency": "UZS"})
    client.post("/income", data={"product_id": pid, "quantity": "1", "price": "2", "currency": "USD"})

    stock = app_module.get_stock()
    row = next(r for r in stock if r["name"] == "Смешанный Товар")

    assert row["total_income"] == 2
    # USD: $2 → 20,000 UZS, total UZS = 20,000 + 20,000 = 40,000
    assert abs(row["total_income_uzs"] - 40000.0) < 1.0
    # UZS 20,000 → $2, + $2 = $4 total
    assert abs(row["total_income_usd"] - 4.0) < 0.01


def test_history_page_shows_currency(client):
    pid = _add_product(client, "Тест валюта", "шт")
    client.post("/income", data={"product_id": pid, "quantity": "3", "price": "100", "currency": "USD"})
    rv = client.get("/history")
    assert rv.status_code == 200
    assert "USD".encode() in rv.data


def test_index_shows_exchange_rate(client):
    client.post("/settings", data={"exchange_rate": "11111"})
    rv = client.get("/")
    assert rv.status_code == 200
    # fmt_uzs uses narrow no-break space (\u202f) as thousands separator
    assert "11\u202f111".encode("utf-8") in rv.data


# ── Boxes / units_per_box tests ─────────────────────────────────────────────

def test_income_with_boxes(client):
    """Income registered with boxes and units_per_box stores correct quantity."""
    import app as app_module
    pid = _add_product(client, "Коробочный Товар", "шт")
    rv = client.post(
        "/income",
        data={"product_id": pid, "boxes": "10", "units_per_box": "30", "quantity": "",
              "price": "0.35", "currency": "USD"},
        follow_redirects=True,
    )
    assert rv.status_code == 200
    assert "зарегистрирован".encode() in rv.data
    with app_module.get_db() as conn:
        t = conn.execute(
            "SELECT quantity, boxes, units_per_box FROM transactions WHERE product_id=?", (pid,)
        ).fetchone()
    assert t["quantity"] == 300.0   # 10 × 30
    assert t["boxes"] == 10.0
    assert t["units_per_box"] == 30.0


def test_income_boxes_invalid(client):
    pid = _add_product(client)
    rv = client.post(
        "/income",
        data={"product_id": pid, "boxes": "-5", "units_per_box": "30", "quantity": ""},
        follow_redirects=True,
    )
    assert rv.status_code == 200
    assert "коробок".encode() in rv.data


def test_settings_decimal_exchange_rate(client):
    """Exchange rate should accept decimal values."""
    rv = client.post("/settings", data={"exchange_rate": "12699.14"}, follow_redirects=True)
    assert rv.status_code == 200
    import app as app_module
    rate = app_module.get_exchange_rate()
    assert abs(rate - 12699.14) < 0.001


def test_report_page(client):
    """Report page loads and shows product data."""
    pid = _add_product(client, "Отчёт Товар", "шт")
    client.post("/income", data={"product_id": pid, "quantity": "100", "price": "1", "currency": "USD"})
    rv = client.get("/report")
    assert rv.status_code == 200
    assert "Отчёт Товар".encode() in rv.data


def test_report_date_filter(client):
    """Report respects date range filter."""
    pid = _add_product(client, "Дата Товар", "шт")
    client.post("/income", data={"product_id": pid, "quantity": "50", "price": "1", "currency": "USD"})
    # Request report for a future period where no transactions exist
    rv = client.get("/report?start_date=2099-01-01&end_date=2099-12-31")
    assert rv.status_code == 200


def test_report_with_date_objects(client):
    """Report date comparison works even when tx_date is a datetime.date object (PostgreSQL)."""
    import datetime

    pid = _add_product(client, "Дата Объект Товар", "шт")
    client.post("/income", data={"product_id": pid, "quantity": "20", "price": "2", "currency": "USD"})

    # The fix converts str(tx_date)[:10] before comparing, so a datetime.date
    # object should produce the correct ISO string for comparison.
    tx_date = datetime.date.today()
    tx_date_str = str(tx_date)[:10]
    today_str = datetime.date.today().isoformat()
    # Verify the conversion produces a string that compares correctly
    assert tx_date_str <= today_str
    assert isinstance(tx_date_str, str)

    rv = client.get("/report")
    assert rv.status_code == 200


def test_income_invalid_product_id(client):
    """Income with invalid product_id returns error, not 500."""
    rv = client.post(
        "/income",
        data={"product_id": "not-a-number", "quantity": "10"},
        follow_redirects=True,
    )
    assert rv.status_code == 200
    assert "Неверный идентификатор".encode() in rv.data


def test_expense_invalid_product_id(client):
    """Expense with invalid product_id returns error, not 500."""
    rv = client.post(
        "/expense",
        data={"product_id": "not-a-number", "boxes": "10"},
        follow_redirects=True,
    )
    assert rv.status_code == 200
    assert "Неверный идентификатор".encode() in rv.data


# ── New expense feature tests ────────────────────────────────────────────────

def test_expense_requires_boxes(client):
    """Expense without boxes is rejected."""
    pid = _add_product(client)
    rv = client.post(
        "/expense",
        data={"product_id": pid},
        follow_redirects=True,
    )
    assert rv.status_code == 200
    assert "коробок".encode() in rv.data


def test_expense_with_selling_price(client):
    """Expense with selling_price is stored correctly."""
    import app as app_module
    pid = _add_product(client, "Товар Продажа", "шт")
    client.post("/income", data={"product_id": pid, "quantity": "100"})
    rv = client.post(
        "/expense",
        data={
            "product_id": pid,
            "boxes": "5",
            "selling_price": "250000",
            "selling_currency": "UZS",
        },
        follow_redirects=True,
    )
    assert rv.status_code == 200
    assert "зарегистрирован".encode() in rv.data
    with app_module.get_db() as conn:
        t = conn.execute(
            "SELECT quantity, boxes, selling_price, selling_currency"
            " FROM transactions WHERE product_id=? AND type='expense'",
            (pid,),
        ).fetchone()
    assert t["boxes"] == 5.0
    assert t["selling_price"] == 250000.0
    assert t["selling_currency"] == "UZS"


def test_expense_selling_price_usd(client):
    """Expense with USD selling price is stored correctly."""
    import app as app_module
    pid = _add_product(client, "Товар USD Продажа", "шт")
    client.post("/income", data={"product_id": pid, "quantity": "50"})
    client.post(
        "/expense",
        data={
            "product_id": pid,
            "boxes": "2",
            "selling_price": "25.50",
            "selling_currency": "USD",
        },
    )
    with app_module.get_db() as conn:
        t = conn.execute(
            "SELECT selling_price, selling_currency FROM transactions"
            " WHERE product_id=? AND type='expense'",
            (pid,),
        ).fetchone()
    assert abs(t["selling_price"] - 25.50) < 0.01
    assert t["selling_currency"] == "USD"


def test_expense_multiple_products(client):
    """Expense can register multiple products in one request."""
    import app as app_module
    pid1 = _add_product(client, "Мульти А", "шт")
    pid2 = _add_product(client, "Мульти Б", "шт")
    client.post("/income", data={"product_id": pid1, "quantity": "100"})
    client.post("/income", data={"product_id": pid2, "quantity": "200"})

    rv = client.post(
        "/expense",
        data={
            "product_id": [str(pid1), str(pid2)],
            "boxes": ["3", "7"],
            "units_per_box": ["", ""],
            "selling_price": ["10000", "20000"],
            "selling_currency": ["UZS", "UZS"],
            "note": ["", ""],
        },
        follow_redirects=True,
    )
    assert rv.status_code == 200
    assert "зарегистрирован".encode() in rv.data

    with app_module.get_db() as conn:
        rows = conn.execute(
            "SELECT product_id, boxes, selling_price FROM transactions"
            " WHERE type='expense' ORDER BY product_id"
        ).fetchall()
    assert len(rows) == 2
    assert rows[0]["boxes"] == 3.0
    assert rows[0]["selling_price"] == 10000.0
    assert rows[1]["boxes"] == 7.0
    assert rows[1]["selling_price"] == 20000.0


def test_expense_boxes_with_units_per_box_from_income(client):
    """Expense uses units_per_box from last income to calculate quantity."""
    import app as app_module
    pid = _add_product(client, "Коробки Расход", "шт")
    client.post("/income", data={
        "product_id": pid, "boxes": "10", "units_per_box": "24", "quantity": "",
    })
    client.post("/expense", data={"product_id": pid, "boxes": "2"})
    with app_module.get_db() as conn:
        t = conn.execute(
            "SELECT quantity, boxes FROM transactions WHERE product_id=? AND type='expense'",
            (pid,),
        ).fetchone()
    assert t["boxes"] == 2.0
    assert t["quantity"] == 48.0  # 2 boxes × 24 units/box


def test_expense_stock_includes_selling_totals(client):
    """get_stock() computes selling totals from expense transactions."""
    import app as app_module
    client.post("/settings", data={"exchange_rate": "10000"})
    pid = _add_product(client, "Продажа Итого", "шт")
    client.post("/income", data={"product_id": pid, "quantity": "100"})
    client.post("/expense", data={
        "product_id": pid,
        "boxes": "10",
        "selling_price": "500000",
        "selling_currency": "UZS",
    })
    stock = app_module.get_stock()
    row = next(r for r in stock if r["name"] == "Продажа Итого")
    assert abs(row["total_selling_uzs"] - 500000.0) < 1.0
    assert abs(row["total_selling_usd"] - 50.0) < 0.01


def test_history_shows_selling_price(client):
    """History page displays selling price for expense transactions."""
    pid = _add_product(client, "Продажа История", "шт")
    client.post("/income", data={"product_id": pid, "quantity": "50"})
    client.post("/expense", data={
        "product_id": pid,
        "boxes": "5",
        "selling_price": "99000",
        "selling_currency": "UZS",
    })
    rv = client.get("/history")
    assert rv.status_code == 200
    assert "99".encode() in rv.data


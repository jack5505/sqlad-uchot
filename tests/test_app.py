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
    client.post("/income", data={"product_id": pid, "quantity": "10"})
    rv = client.post("/expense", data={"product_id": pid, "quantity": "3"}, follow_redirects=True)
    assert rv.status_code == 200
    assert "зарегистрирован".encode() in rv.data


def test_balance_after_transactions(client):
    pid = _add_product(client, "Гвозди", "кг")
    client.post("/income", data={"product_id": pid, "quantity": "100"})
    client.post("/expense", data={"product_id": pid, "quantity": "40"})
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
    client.post("/expense", data={"product_id": pid, "quantity": "10"})
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

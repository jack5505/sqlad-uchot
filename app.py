from flask import Flask, render_template, request, redirect, url_for, flash
import sqlite3
import os

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY") or __import__("secrets").token_hex(32)

DATABASE = os.environ.get("DATABASE", "warehouse.db")


def get_db():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                unit TEXT NOT NULL DEFAULT 'шт',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id INTEGER NOT NULL,
                type TEXT NOT NULL CHECK(type IN ('income', 'expense')),
                quantity REAL NOT NULL CHECK(quantity > 0),
                note TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (product_id) REFERENCES products(id)
            );
        """)


init_db()


def get_stock():
    """Return current stock balance for all products."""
    with get_db() as conn:
        rows = conn.execute("""
            SELECT p.id, p.name, p.unit,
                   COALESCE(SUM(CASE WHEN t.type='income' THEN t.quantity ELSE 0 END), 0) AS total_income,
                   COALESCE(SUM(CASE WHEN t.type='expense' THEN t.quantity ELSE 0 END), 0) AS total_expense,
                   COALESCE(SUM(CASE WHEN t.type='income' THEN t.quantity
                                     WHEN t.type='expense' THEN -t.quantity ELSE 0 END), 0) AS balance
            FROM products p
            LEFT JOIN transactions t ON p.id = t.product_id
            GROUP BY p.id, p.name, p.unit
            ORDER BY p.name
        """).fetchall()
    return rows


@app.route("/")
def index():
    stock = get_stock()
    return render_template("index.html", stock=stock)


@app.route("/products", methods=["GET", "POST"])
def products():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        unit = request.form.get("unit", "шт").strip() or "шт"
        if not name:
            flash("Название товара обязательно.", "danger")
        else:
            try:
                with get_db() as conn:
                    conn.execute(
                        "INSERT INTO products (name, unit) VALUES (?, ?)",
                        (name, unit),
                    )
                flash(f"Товар «{name}» добавлен.", "success")
            except sqlite3.IntegrityError:
                flash(f"Товар «{name}» уже существует.", "warning")
        return redirect(url_for("products"))

    with get_db() as conn:
        all_products = conn.execute(
            "SELECT * FROM products ORDER BY name"
        ).fetchall()
    return render_template("products.html", products=all_products)


@app.route("/products/<int:product_id>/delete", methods=["POST"])
def delete_product(product_id):
    with get_db() as conn:
        product = conn.execute(
            "SELECT name FROM products WHERE id=?", (product_id,)
        ).fetchone()
        if product:
            conn.execute("DELETE FROM transactions WHERE product_id=?", (product_id,))
            conn.execute("DELETE FROM products WHERE id=?", (product_id,))
            flash(f"Товар «{product['name']}» удалён.", "success")
        else:
            flash("Товар не найден.", "danger")
    return redirect(url_for("products"))


@app.route("/income", methods=["GET", "POST"])
def income():
    if request.method == "POST":
        product_id = request.form.get("product_id")
        quantity = request.form.get("quantity", "").strip()
        note = request.form.get("note", "").strip()
        error = None
        if not product_id:
            error = "Выберите товар."
        else:
            try:
                qty = float(quantity)
                if qty <= 0:
                    raise ValueError
            except (ValueError, TypeError):
                error = "Количество должно быть положительным числом."
        if error:
            flash(error, "danger")
        else:
            with get_db() as conn:
                conn.execute(
                    "INSERT INTO transactions (product_id, type, quantity, note) VALUES (?, 'income', ?, ?)",
                    (product_id, qty, note or None),
                )
            flash("Приход зарегистрирован.", "success")
        return redirect(url_for("income"))

    with get_db() as conn:
        all_products = conn.execute(
            "SELECT id, name, unit FROM products ORDER BY name"
        ).fetchall()
    return render_template("transaction_form.html", products=all_products, tx_type="income")


@app.route("/expense", methods=["GET", "POST"])
def expense():
    if request.method == "POST":
        product_id = request.form.get("product_id")
        quantity = request.form.get("quantity", "").strip()
        note = request.form.get("note", "").strip()
        error = None
        if not product_id:
            error = "Выберите товар."
        else:
            try:
                qty = float(quantity)
                if qty <= 0:
                    raise ValueError
            except (ValueError, TypeError):
                error = "Количество должно быть положительным числом."
        if not error:
            with get_db() as conn:
                product = conn.execute(
                    "SELECT id FROM products WHERE id=?", (product_id,)
                ).fetchone()
                if not product:
                    error = "Товар не найден."
        if error:
            flash(error, "danger")
        else:
            with get_db() as conn:
                conn.execute(
                    "INSERT INTO transactions (product_id, type, quantity, note) VALUES (?, 'expense', ?, ?)",
                    (product_id, qty, note or None),
                )
            flash("Расход зарегистрирован.", "success")
        return redirect(url_for("expense"))

    with get_db() as conn:
        all_products = conn.execute(
            "SELECT id, name, unit FROM products ORDER BY name"
        ).fetchall()
    return render_template("transaction_form.html", products=all_products, tx_type="expense")


@app.route("/history")
def history():
    product_id = request.args.get("product_id", "")
    with get_db() as conn:
        all_products = conn.execute(
            "SELECT id, name FROM products ORDER BY name"
        ).fetchall()
        query = """
            SELECT t.id, p.name AS product_name, p.unit, t.type, t.quantity, t.note, t.created_at
            FROM transactions t
            JOIN products p ON p.id = t.product_id
        """
        params = []
        if product_id:
            query += " WHERE t.product_id = ?"
            params.append(product_id)
        query += " ORDER BY t.created_at DESC"
        transactions = conn.execute(query, params).fetchall()
    return render_template(
        "history.html",
        transactions=transactions,
        products=all_products,
        selected_product=product_id,
    )


if __name__ == "__main__":
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    app.run(debug=debug)

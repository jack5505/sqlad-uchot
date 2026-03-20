from flask import Flask, render_template, request, redirect, url_for, flash
import logging
import os

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY") or __import__("secrets").token_hex(32)

DATABASE_URL = os.environ.get("DATABASE_URL", "")
_use_postgres = DATABASE_URL.startswith("postgres")

if _use_postgres:
    import psycopg2
    import psycopg2.extras
    import psycopg2.errors
    # Use the broader IntegrityError base class so that UniqueViolation
    # (and any other integrity failures) are always caught.
    _DuplicateKeyError = psycopg2.IntegrityError
else:
    import sqlite3
    DATABASE = os.environ.get("DATABASE", "warehouse.db")
    _DuplicateKeyError = sqlite3.IntegrityError


class _PGWrapper:
    """Wraps a psycopg2 connection with a sqlite3-compatible interface."""

    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, params=()):
        cur = self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(sql.replace("?", "%s"), params)
        return cur

    def executescript(self, sql):
        cur = self._conn.cursor()
        for stmt in [s.strip() for s in sql.split(";") if s.strip()]:
            cur.execute(stmt)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, *_):
        if exc_type is None:
            self._conn.commit()
        else:
            self._conn.rollback()
        self._conn.close()
        return False


def get_db():
    if _use_postgres:
        conn = psycopg2.connect(DATABASE_URL)
        return _PGWrapper(conn)
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    if _use_postgres:
        id_type = "SERIAL PRIMARY KEY"
    else:
        id_type = "INTEGER PRIMARY KEY AUTOINCREMENT"

    with get_db() as conn:
        conn.executescript(f"""
            CREATE TABLE IF NOT EXISTS products (
                id {id_type},
                name TEXT NOT NULL UNIQUE,
                unit TEXT NOT NULL DEFAULT 'шт',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

    with get_db() as conn:
        conn.executescript(f"""
            CREATE TABLE IF NOT EXISTS transactions (
                id {id_type},
                product_id INTEGER NOT NULL,
                type TEXT NOT NULL CHECK(type IN ('income', 'expense')),
                quantity REAL NOT NULL CHECK(quantity > 0),
                price REAL NOT NULL DEFAULT 0,
                currency TEXT NOT NULL DEFAULT 'UZS',
                note TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (product_id) REFERENCES products(id)
            )
        """)

    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)

    # Insert default exchange rate if not already present
    try:
        with get_db() as conn:
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?)",
                ("exchange_rate", "12000"),
            )
    except _DuplicateKeyError:
        pass  # already seeded

    # Migrations for existing databases that pre-date these columns
    if not _use_postgres:
        with get_db() as conn:
            col_names = [row[1] for row in conn.execute("PRAGMA table_info(transactions)").fetchall()]
        if "price" not in col_names:
            with get_db() as conn:
                conn.execute(
                    "ALTER TABLE transactions ADD COLUMN price REAL NOT NULL DEFAULT 0"
                )
        if "currency" not in col_names:
            with get_db() as conn:
                conn.execute(
                    "ALTER TABLE transactions ADD COLUMN currency TEXT NOT NULL DEFAULT 'UZS'"
                )
    else:
        with get_db() as conn:
            conn.execute(
                "ALTER TABLE transactions ADD COLUMN IF NOT EXISTS price REAL NOT NULL DEFAULT 0"
            )
        with get_db() as conn:
            conn.execute(
                "ALTER TABLE transactions ADD COLUMN IF NOT EXISTS currency TEXT NOT NULL DEFAULT 'UZS'"
            )


init_db()


@app.template_filter("fmt_uzs")
def fmt_uzs(value):
    """Format a number as UZS with thousands separator."""
    try:
        return "{:,.0f}".format(float(value)).replace(",", "\u202f")
    except (TypeError, ValueError):
        return "0"


@app.template_filter("fmt_usd")
def fmt_usd(value):
    """Format a number as USD with 2 decimal places."""
    try:
        return "{:,.2f}".format(float(value))
    except (TypeError, ValueError):
        return "0.00"


def get_exchange_rate():
    """Return the current USD→UZS exchange rate from settings."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE key='exchange_rate'"
        ).fetchone()
    if row:
        try:
            return float(row["value"])
        except (ValueError, TypeError):
            pass
    return 12000.0


def get_stock():
    """Return current stock balance for all products with UZS and USD totals."""
    rate = get_exchange_rate()
    with get_db() as conn:
        rows = conn.execute("""
            SELECT p.id, p.name, p.unit,
                   COALESCE(SUM(CASE WHEN t.type='income' THEN t.quantity ELSE 0 END), 0) AS total_income,
                   COALESCE(SUM(CASE WHEN t.type='expense' THEN t.quantity ELSE 0 END), 0) AS total_expense,
                   COALESCE(SUM(CASE WHEN t.type='income' THEN t.quantity
                                     WHEN t.type='expense' THEN -t.quantity ELSE 0 END), 0) AS balance,
                   COALESCE(SUM(CASE WHEN t.type='income' AND COALESCE(t.currency,'UZS')='UZS'
                                     THEN t.quantity * t.price ELSE 0 END), 0) AS income_uzs_native,
                   COALESCE(SUM(CASE WHEN t.type='expense' AND COALESCE(t.currency,'UZS')='UZS'
                                     THEN t.quantity * t.price ELSE 0 END), 0) AS expense_uzs_native,
                   COALESCE(SUM(CASE WHEN t.type='income' AND COALESCE(t.currency,'UZS')='USD'
                                     THEN t.quantity * t.price ELSE 0 END), 0) AS income_usd_native,
                   COALESCE(SUM(CASE WHEN t.type='expense' AND COALESCE(t.currency,'UZS')='USD'
                                     THEN t.quantity * t.price ELSE 0 END), 0) AS expense_usd_native
            FROM products p
            LEFT JOIN transactions t ON p.id = t.product_id
            GROUP BY p.id, p.name, p.unit
            ORDER BY p.name
        """).fetchall()
    result = []
    for row in rows:
        d = dict(row)
        d["total_income_uzs"] = d["income_uzs_native"] + d["income_usd_native"] * rate
        d["total_expense_uzs"] = d["expense_uzs_native"] + d["expense_usd_native"] * rate
        d["balance_uzs"] = d["total_income_uzs"] - d["total_expense_uzs"]
        d["total_income_usd"] = d["income_usd_native"] + (
            d["income_uzs_native"] / rate if rate > 0 else 0
        )
        d["total_expense_usd"] = d["expense_usd_native"] + (
            d["expense_uzs_native"] / rate if rate > 0 else 0
        )
        d["balance_usd"] = d["total_income_usd"] - d["total_expense_usd"]
        result.append(d)
    return result


@app.route("/")
def index():
    rate = get_exchange_rate()
    stock = get_stock()
    return render_template("index.html", stock=stock, exchange_rate=rate)


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
            except _DuplicateKeyError:
                flash(f"Товар «{name}» уже существует.", "warning")
            except Exception:
                app.logger.exception("Ошибка при добавлении товара «%s»", name)
                flash("Ошибка при добавлении товара. Попробуйте ещё раз.", "danger")
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
        price = request.form.get("price", "0").strip()
        currency = request.form.get("currency", "UZS").strip()
        if currency not in ("UZS", "USD"):
            currency = "UZS"
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
        if error is None:
            try:
                prc = float(price) if price else 0.0
                if prc < 0:
                    raise ValueError
            except (ValueError, TypeError):
                error = "Цена должна быть неотрицательным числом."
        if error:
            flash(error, "danger")
        else:
            with get_db() as conn:
                conn.execute(
                    "INSERT INTO transactions (product_id, type, quantity, price, currency, note)"
                    " VALUES (?, 'income', ?, ?, ?, ?)",
                    (product_id, qty, prc, currency, note or None),
                )
            flash("Приход зарегистрирован.", "success")
        return redirect(url_for("income"))

    rate = get_exchange_rate()
    with get_db() as conn:
        all_products = conn.execute(
            "SELECT id, name, unit FROM products ORDER BY name"
        ).fetchall()
    return render_template(
        "transaction_form.html",
        products=all_products,
        tx_type="income",
        exchange_rate=rate,
    )


@app.route("/expense", methods=["GET", "POST"])
def expense():
    if request.method == "POST":
        product_id = request.form.get("product_id")
        quantity = request.form.get("quantity", "").strip()
        price = request.form.get("price", "0").strip()
        currency = request.form.get("currency", "UZS").strip()
        if currency not in ("UZS", "USD"):
            currency = "UZS"
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
        if error is None:
            try:
                prc = float(price) if price else 0.0
                if prc < 0:
                    raise ValueError
            except (ValueError, TypeError):
                error = "Цена должна быть неотрицательным числом."
        if error is None:
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
                    "INSERT INTO transactions (product_id, type, quantity, price, currency, note)"
                    " VALUES (?, 'expense', ?, ?, ?, ?)",
                    (product_id, qty, prc, currency, note or None),
                )
            flash("Расход зарегистрирован.", "success")
        return redirect(url_for("expense"))

    rate = get_exchange_rate()
    with get_db() as conn:
        all_products = conn.execute(
            "SELECT id, name, unit FROM products ORDER BY name"
        ).fetchall()
    return render_template(
        "transaction_form.html",
        products=all_products,
        tx_type="expense",
        exchange_rate=rate,
    )


@app.route("/history")
def history():
    product_id = request.args.get("product_id", "")
    rate = get_exchange_rate()
    with get_db() as conn:
        all_products = conn.execute(
            "SELECT id, name FROM products ORDER BY name"
        ).fetchall()
        query = """
            SELECT t.id, p.name AS product_name, p.unit, t.type, t.quantity, t.price,
                   COALESCE(t.currency, 'UZS') AS currency, t.note, t.created_at
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
        exchange_rate=rate,
    )


@app.route("/settings", methods=["GET", "POST"])
def settings():
    if request.method == "POST":
        rate_str = request.form.get("exchange_rate", "").strip()
        try:
            rate = float(rate_str)
            if rate <= 0:
                raise ValueError
        except (ValueError, TypeError):
            flash("Курс должен быть положительным числом.", "danger")
            return redirect(url_for("settings"))
        with get_db() as conn:
            conn.execute(
                "UPDATE settings SET value=? WHERE key='exchange_rate'",
                (str(rate),),
            )
        flash(f"Курс обновлён: 1 USD = {rate:,.0f} UZS", "success")
        return redirect(url_for("settings"))

    rate = get_exchange_rate()
    return render_template("settings.html", exchange_rate=rate)


if __name__ == "__main__":
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    app.run(debug=debug)

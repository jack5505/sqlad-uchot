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
    _DatabaseError = psycopg2.Error
else:
    import sqlite3
    DATABASE = os.environ.get("DATABASE", "warehouse.db")
    _DuplicateKeyError = sqlite3.IntegrityError
    _DatabaseError = sqlite3.Error


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
        if "boxes" not in col_names:
            with get_db() as conn:
                conn.execute(
                    "ALTER TABLE transactions ADD COLUMN boxes REAL DEFAULT NULL"
                )
        if "units_per_box" not in col_names:
            with get_db() as conn:
                conn.execute(
                    "ALTER TABLE transactions ADD COLUMN units_per_box REAL DEFAULT NULL"
                )
        if "selling_price" not in col_names:
            with get_db() as conn:
                conn.execute(
                    "ALTER TABLE transactions ADD COLUMN selling_price REAL DEFAULT NULL"
                )
        if "selling_currency" not in col_names:
            with get_db() as conn:
                conn.execute(
                    "ALTER TABLE transactions ADD COLUMN selling_currency TEXT DEFAULT NULL"
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
        with get_db() as conn:
            conn.execute(
                "ALTER TABLE transactions ADD COLUMN IF NOT EXISTS boxes REAL DEFAULT NULL"
            )
        with get_db() as conn:
            conn.execute(
                "ALTER TABLE transactions ADD COLUMN IF NOT EXISTS units_per_box REAL DEFAULT NULL"
            )
        with get_db() as conn:
            conn.execute(
                "ALTER TABLE transactions ADD COLUMN IF NOT EXISTS selling_price REAL DEFAULT NULL"
            )
        with get_db() as conn:
            conn.execute(
                "ALTER TABLE transactions ADD COLUMN IF NOT EXISTS selling_currency TEXT DEFAULT NULL"
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
                   COALESCE(SUM(CASE WHEN t.type='income' AND t.boxes IS NOT NULL THEN t.boxes ELSE 0 END), 0) AS total_income_boxes,
                   COALESCE(SUM(CASE WHEN t.type='expense' AND t.boxes IS NOT NULL THEN t.boxes ELSE 0 END), 0) AS total_expense_boxes,
                   COALESCE(MAX(CASE WHEN t.boxes IS NOT NULL THEN 1 ELSE 0 END), 0) AS has_boxes_data,
                   COALESCE(SUM(CASE WHEN t.type='income' AND COALESCE(t.currency,'UZS')='UZS'
                                     THEN t.quantity * t.price ELSE 0 END), 0) AS income_uzs_native,
                   COALESCE(SUM(CASE WHEN t.type='expense' AND COALESCE(t.currency,'UZS')='UZS'
                                     THEN t.quantity * t.price ELSE 0 END), 0) AS expense_uzs_native,
                   COALESCE(SUM(CASE WHEN t.type='income' AND COALESCE(t.currency,'UZS')='USD'
                                     THEN t.quantity * t.price ELSE 0 END), 0) AS income_usd_native,
                   COALESCE(SUM(CASE WHEN t.type='expense' AND COALESCE(t.currency,'UZS')='USD'
                                     THEN t.quantity * t.price ELSE 0 END), 0) AS expense_usd_native,
                   COALESCE(SUM(CASE WHEN t.type='expense' AND COALESCE(t.selling_currency,'UZS')='UZS'
                                     THEN COALESCE(t.selling_price,0) ELSE 0 END), 0) AS selling_uzs_native,
                   COALESCE(SUM(CASE WHEN t.type='expense' AND COALESCE(t.selling_currency,'UZS')='USD'
                                     THEN COALESCE(t.selling_price,0) ELSE 0 END), 0) AS selling_usd_native,
                   (SELECT units_per_box FROM transactions
                    WHERE product_id = p.id AND units_per_box IS NOT NULL
                    ORDER BY created_at DESC LIMIT 1) AS last_units_per_box
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
        d["total_selling_uzs"] = d["selling_uzs_native"] + d["selling_usd_native"] * rate
        d["total_selling_usd"] = d["selling_usd_native"] + (
            d["selling_uzs_native"] / rate if rate > 0 else 0
        )
        upb = d.get("last_units_per_box")
        income_boxes = d["total_income_boxes"]
        expense_boxes = d["total_expense_boxes"]
        if d["has_boxes_data"]:
            d["balance_boxes"] = income_boxes - expense_boxes
        elif upb and upb > 0:
            d["balance_boxes"] = d["balance"] / upb
        else:
            d["balance_boxes"] = None
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
        boxes_str = request.form.get("boxes", "").strip()
        units_per_box_str = request.form.get("units_per_box", "").strip()
        price = request.form.get("price", "0").strip()
        currency = request.form.get("currency", "UZS").strip()
        if currency not in ("UZS", "USD"):
            currency = "UZS"
        note = request.form.get("note", "").strip()
        error = None

        pid = None
        if not product_id:
            error = "Выберите товар."
        else:
            try:
                pid = int(product_id)
            except (ValueError, TypeError):
                error = "Неверный идентификатор товара."

        boxes = None
        units_per_box = None

        # Parse optional boxes / units_per_box
        if error is None and boxes_str:
            try:
                boxes = float(boxes_str)
                if boxes <= 0:
                    raise ValueError
            except (ValueError, TypeError):
                error = "Количество коробок должно быть положительным числом."
        if error is None and units_per_box_str:
            try:
                units_per_box = float(units_per_box_str)
                if units_per_box <= 0:
                    raise ValueError
            except (ValueError, TypeError):
                error = "Штук в коробке должно быть положительным числом."

        # If both boxes and units_per_box are given, derive quantity from them
        if error is None:
            if boxes is not None and units_per_box is not None:
                qty = boxes * units_per_box
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
            try:
                with get_db() as conn:
                    conn.execute(
                        "INSERT INTO transactions (product_id, type, quantity, boxes, units_per_box, price, currency, note)"
                        " VALUES (?, 'income', ?, ?, ?, ?, ?, ?)",
                        (pid, qty, boxes, units_per_box, prc, currency, note or None),
                    )
                flash("Приход зарегистрирован.", "success")
            except _DatabaseError:
                app.logger.exception("Ошибка при регистрации прихода")
                flash("Ошибка при регистрации прихода. Попробуйте ещё раз.", "danger")
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
        product_ids = request.form.getlist("product_id")
        boxes_list = request.form.getlist("boxes")
        units_per_box_list = request.form.getlist("units_per_box")
        selling_price_list = request.form.getlist("selling_price")
        selling_currency_list = request.form.getlist("selling_currency")
        note_list = request.form.getlist("note")

        # Ensure all lists have the same length
        n = len(product_ids)

        def _pad(lst, default, length):
            if len(lst) < length:
                lst = lst + [default] * (length - len(lst))
            return lst[:length]

        boxes_list = _pad(boxes_list, "", n)
        units_per_box_list = _pad(units_per_box_list, "", n)
        selling_price_list = _pad(selling_price_list, "", n)
        selling_currency_list = _pad(selling_currency_list, "UZS", n)
        note_list = _pad(note_list, "", n)

        errors = []
        rows = []

        for i in range(n):
            pid_str = product_ids[i]
            boxes_str = boxes_list[i].strip()
            upb_str = units_per_box_list[i].strip()
            sp_str = selling_price_list[i].strip()
            sc = selling_currency_list[i].strip()
            note = note_list[i].strip()

            if sc not in ("UZS", "USD"):
                sc = "UZS"

            if not pid_str:
                errors.append(f"Строка {i+1}: Выберите товар.")
                continue

            try:
                pid = int(pid_str)
            except (ValueError, TypeError):
                errors.append(f"Строка {i+1}: Неверный идентификатор товара.")
                continue

            boxes = None
            units_per_box = None

            if boxes_str:
                try:
                    boxes = float(boxes_str)
                    if boxes <= 0:
                        raise ValueError
                except (ValueError, TypeError):
                    errors.append(f"Строка {i+1}: Количество коробок должно быть положительным числом.")
                    continue

            if upb_str:
                try:
                    units_per_box = float(upb_str)
                    if units_per_box <= 0:
                        raise ValueError
                except (ValueError, TypeError):
                    errors.append(f"Строка {i+1}: Штук в коробке должно быть положительным числом.")
                    continue

            # For expense, quantity is required and derived from boxes
            if boxes is None:
                errors.append(f"Строка {i+1}: Укажите количество коробок.")
                continue

            # Derive units_per_box from the product's last income if not provided
            if units_per_box is None:
                with get_db() as conn:
                    upb_row = conn.execute(
                        "SELECT units_per_box FROM transactions"
                        " WHERE product_id=? AND type='income' AND units_per_box IS NOT NULL"
                        " ORDER BY created_at DESC LIMIT 1",
                        (pid,),
                    ).fetchone()
                if upb_row:
                    units_per_box = upb_row["units_per_box"]

            if units_per_box and units_per_box > 0:
                qty = boxes * units_per_box
            else:
                qty = boxes  # treat boxes as plain quantity when upb unknown

            try:
                sp = float(sp_str) if sp_str else 0.0
                if sp < 0:
                    raise ValueError
            except (ValueError, TypeError):
                errors.append(f"Строка {i+1}: Продажная сумма должна быть неотрицательным числом.")
                continue

            with get_db() as conn:
                product = conn.execute(
                    "SELECT id FROM products WHERE id=?", (pid,)
                ).fetchone()
            if not product:
                errors.append(f"Строка {i+1}: Товар не найден.")
                continue

            rows.append((pid, qty, boxes, units_per_box, sp, sc, note or None))

        if errors:
            for err in errors:
                flash(err, "danger")
        elif not rows:
            flash("Добавьте хотя бы один товар.", "warning")
        else:
            try:
                with get_db() as conn:
                    for (pid, qty, boxes, units_per_box, sp, sc, note) in rows:
                        conn.execute(
                            "INSERT INTO transactions"
                            " (product_id, type, quantity, boxes, units_per_box, price, currency,"
                            "  selling_price, selling_currency, note)"
                            " VALUES (?, 'expense', ?, ?, ?, 0, 'UZS', ?, ?, ?)",
                            (pid, qty, boxes, units_per_box, sp, sc, note),
                        )
                flash("Расход зарегистрирован.", "success")
            except _DatabaseError:
                app.logger.exception("Ошибка при регистрации расхода")
                flash("Ошибка при регистрации расхода. Попробуйте ещё раз.", "danger")
        return redirect(url_for("expense"))

    rate = get_exchange_rate()
    with get_db() as conn:
        all_products = conn.execute(
            "SELECT id, name, unit FROM products ORDER BY name"
        ).fetchall()
    # Enrich products with last known units_per_box from income transactions
    products_with_upb = []
    for p in all_products:
        d = dict(p)
        with get_db() as conn:
            upb_row = conn.execute(
                "SELECT units_per_box FROM transactions"
                " WHERE product_id=? AND type='income' AND units_per_box IS NOT NULL"
                " ORDER BY created_at DESC LIMIT 1",
                (p["id"],),
            ).fetchone()
        d["last_units_per_box"] = upb_row["units_per_box"] if upb_row else None
        products_with_upb.append(d)
    return render_template(
        "expense_form.html",
        products=products_with_upb,
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
            SELECT t.id, p.name AS product_name, p.unit, t.type, t.quantity,
                   t.boxes,
                   t.price,
                   COALESCE(t.currency, 'UZS') AS currency,
                   t.selling_price, t.selling_currency,
                   t.note, t.created_at
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
        flash(f"Курс обновлён: 1 USD = {rate:g} UZS", "success")
        return redirect(url_for("settings"))

    rate = get_exchange_rate()
    return render_template("settings.html", exchange_rate=rate)


@app.route("/report")
def report():
    from datetime import date, timedelta
    today = date.today()
    default_start = today.replace(day=1).isoformat()
    default_end = today.isoformat()
    start_date = request.args.get("start_date", default_start)
    end_date = request.args.get("end_date", default_end)

    rate = get_exchange_rate()

    with get_db() as conn:
        products = conn.execute(
            "SELECT id, name, unit FROM products ORDER BY name"
        ).fetchall()

    report_rows = []
    for p in products:
        pid = p["id"]
        with get_db() as conn:
            txns = conn.execute(
                """
                SELECT type, quantity, boxes, units_per_box, price,
                       COALESCE(currency, 'UZS') AS currency,
                       DATE(created_at) AS tx_date
                FROM transactions
                WHERE product_id = ?
                ORDER BY created_at
                """,
                (pid,),
            ).fetchall()

        opening_qty = 0.0
        opening_val = 0.0
        income_qty = 0.0
        income_boxes = 0.0
        income_val = 0.0
        expense_qty = 0.0
        expense_boxes = 0.0
        expense_val = 0.0
        last_units_per_box = None
        last_price_usd = None

        for t in txns:
            q = t["quantity"] or 0.0
            bx = t["boxes"] or 0.0
            upb = t["units_per_box"]
            prc = t["price"] or 0.0
            cur = t["currency"]

            if cur == "USD":
                val_uzs = q * prc * rate
            else:
                val_uzs = q * prc

            tx_date_str = str(t["tx_date"])[:10]

            if tx_date_str < start_date:
                # Before the period → opening balance
                if t["type"] == "income":
                    opening_qty += q
                    opening_val += val_uzs
                else:
                    opening_qty -= q
                    opening_val -= val_uzs
            elif tx_date_str <= end_date:
                # During the period
                if t["type"] == "income":
                    income_qty += q
                    income_boxes += bx
                    income_val += val_uzs
                    if upb:
                        last_units_per_box = upb
                    if prc > 0:
                        if cur == "USD":
                            last_price_usd = prc
                        else:
                            last_price_usd = prc / rate if rate > 0 else None
                else:
                    expense_qty += q
                    expense_boxes += bx
                    expense_val += val_uzs

        closing_qty = opening_qty + income_qty - expense_qty
        closing_val = opening_val + income_val - expense_val

        upb = last_units_per_box
        price_uzs = last_price_usd * rate if last_price_usd and rate > 0 else None
        box_val = upb * price_uzs if upb and price_uzs else None
        total_cost_uzs = income_qty * price_uzs if price_uzs else income_val

        # Convert quantities to boxes for display when units_per_box is known
        if upb and upb > 0:
            opening_boxes_disp = opening_qty / upb
            closing_boxes_disp = closing_qty / upb
        else:
            opening_boxes_disp = opening_qty
            closing_boxes_disp = closing_qty

        report_rows.append({
            "name": p["name"],
            "unit": p["unit"],
            "total_qty": income_qty,
            "income_boxes": income_boxes,
            "units_per_box": upb,
            "price_usd": last_price_usd,
            "total_cost_usd": income_qty * last_price_usd if last_price_usd else 0.0,
            "price_uzs": price_uzs,
            "box_val": box_val,
            "total_cost_uzs": total_cost_uzs,
            "opening_boxes": opening_boxes_disp,
            "opening_val": opening_val,
            "income_boxes_disp": income_boxes if income_boxes else income_qty,
            "income_val": income_val,
            "expense_boxes_disp": expense_boxes if expense_boxes else expense_qty,
            "expense_val": expense_val,
            "closing_boxes": closing_boxes_disp,
            "closing_val": closing_val,
        })

    return render_template(
        "report.html",
        rows=report_rows,
        start_date=start_date,
        end_date=end_date,
        exchange_rate=rate,
    )


if __name__ == "__main__":
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    app.run(debug=debug)

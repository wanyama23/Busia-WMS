import os
import io
import csv
import json
import logging
import smtplib
import threading
from datetime import datetime
from email.message import EmailMessage

from flask import (
    Flask, render_template, request, jsonify, redirect, url_for,
    flash, session, make_response, current_app, abort
)
from dotenv import load_dotenv
from flask_wtf import CSRFProtect
from flask_wtf.csrf import CSRFError, generate_csrf
from flask_session import Session

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

import sqlite3

# -------------------------------------------------------------------
# App setup
# -------------------------------------------------------------------
load_dotenv()

app = Flask(__name__, template_folder='templates', static_folder='static')
app.logger.setLevel(logging.DEBUG)

app.secret_key = os.getenv('FLASK_SECRET', 'devsecret')
app.config.update({
    'SESSION_TYPE': 'filesystem',
    'SESSION_COOKIE_HTTPONLY': True,
    'SESSION_COOKIE_SAMESITE': 'Lax',
    'SESSION_COOKIE_SECURE': False,
})

Session(app)
csrf = CSRFProtect(app)

app.jinja_env.globals['generate_csrf'] = generate_csrf


@app.context_processor
def expose_helpers():
    def endpoint_exists(name):
        return name in current_app.view_functions
    return {'endpoint_exists': endpoint_exists}


@app.errorhandler(CSRFError)
def handle_csrf(e):
    app.logger.warning(
        "CSRFError: %s; headers=%s; body=%s",
        e.description, dict(request.headers), request.get_data(as_text=True)
    )
    return jsonify({'status': 'error', 'message': 'CSRF token missing or invalid'}), 400


# -------------------------------------------------------------------
# Config
# -------------------------------------------------------------------
DB_PATH = os.getenv('SQLITE_PATH', os.path.join(os.getcwd(), 'busia_wms.sqlite3'))
LOW_STOCK_THRESHOLD = int(os.getenv('LOW_STOCK_THRESHOLD', 10))

DEFAULT_UNITS = ['pcs', 'bag', 'bottle', 'pack', 'jerrycan']
DEFAULT_CATEGORIES = ['Food', 'NFI', 'Health', 'Beverages', 'General']

MAIL_HOST = os.getenv('BUSIA_SMTP_HOST') or os.getenv('MAIL_HOST')
MAIL_PORT = int(os.getenv('BUSIA_SMTP_PORT', os.getenv('MAIL_PORT', 587)))
MAIL_USER = os.getenv('BUSIA_SMTP_USER') or os.getenv('MAIL_USER')
MAIL_PASS = os.getenv('BUSIA_SMTP_PASS') or os.getenv('MAIL_PASS')
MAIL_FROM = os.getenv('BUSIA_EMAIL_FROM') or os.getenv('MAIL_FROM') or MAIL_USER or 'no-reply@example.com'
MAIL_FROM_NAME = os.getenv('BUSIA_EMAIL_FROM_NAME', 'Busia WMS')
MAIL_USE_TLS = os.getenv('BUSIA_SMTP_TLS', '1') not in ('0', 'false', 'False')

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, 'generated_docs')
os.makedirs(UPLOAD_DIR, exist_ok=True)


# -------------------------------------------------------------------
# DB helpers
# -------------------------------------------------------------------
def get_conn():
    conn = sqlite3.connect(DB_PATH, detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def init_db():
    ddl = """
    PRAGMA foreign_keys = ON;

    CREATE TABLE IF NOT EXISTS sequences (
      name TEXT PRIMARY KEY,
      last INTEGER NOT NULL DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS items (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      sku TEXT NOT NULL UNIQUE,
      name TEXT NOT NULL,
      unit TEXT DEFAULT 'pcs',
      category TEXT,
      on_hand INTEGER DEFAULT 0,
      reorder_level INTEGER DEFAULT 0,
      price REAL DEFAULT NULL,
      carton_price REAL DEFAULT NULL,
      description TEXT,
      created_at DATETIME DEFAULT (datetime('now')),
      updated_at DATETIME DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS locations (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      code TEXT UNIQUE,
      name TEXT NOT NULL,
      type TEXT,
      capacity INTEGER DEFAULT NULL,
      description TEXT,
      notes TEXT,
      created_at DATETIME DEFAULT (datetime('now')),
      updated_at DATETIME DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS suppliers (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      code TEXT UNIQUE,
      name TEXT NOT NULL,
      contact_name TEXT,
      phone TEXT,
      email TEXT,
      address TEXT,
      notes TEXT,
      created_at DATETIME DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS users (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      username TEXT NOT NULL UNIQUE,
      full_name TEXT,
      email TEXT,
      password_hash TEXT,
      role TEXT DEFAULT 'user',
      is_active INTEGER DEFAULT 1,
      created_at DATETIME DEFAULT (datetime('now'))
    );

    -- Goods Receipt Note header. grn_no / order_ref are generated on confirm,
    -- not on page load, so re-opening the page doesn't burn sequence numbers.
    CREATE TABLE IF NOT EXISTS receipts (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      receipt_no TEXT NOT NULL UNIQUE,
      grn_no TEXT DEFAULT NULL,
      order_ref TEXT DEFAULT NULL,
      supplier TEXT DEFAULT NULL,
      supplier_email TEXT DEFAULT NULL,
      po_no TEXT DEFAULT NULL,
      condition_status TEXT DEFAULT NULL,
      condition_notes TEXT DEFAULT NULL,
      received_by TEXT DEFAULT NULL,
      confirmed INTEGER DEFAULT 0,
      confirmed_at DATETIME DEFAULT NULL,
      document_path TEXT DEFAULT NULL,
      email_sent INTEGER DEFAULT 0,
      email_error TEXT DEFAULT NULL,
      created_at DATETIME DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS receipt_lines (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      receipt_id INTEGER NOT NULL,
      sku TEXT NOT NULL,
      qty REAL NOT NULL,
      unit TEXT DEFAULT 'pcs',
      location TEXT DEFAULT NULL,
      batch_id TEXT DEFAULT NULL,
      expiry TEXT DEFAULT NULL,
      created_at DATETIME DEFAULT (datetime('now')),
      FOREIGN KEY (receipt_id) REFERENCES receipts(id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS drivers (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      name TEXT NOT NULL,
      phone TEXT,
      license_no TEXT,
      created_at DATETIME DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS vehicles (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      reg_no TEXT NOT NULL UNIQUE,
      make_model TEXT,
      capacity TEXT,
      created_at DATETIME DEFAULT (datetime('now'))
    );

    -- Delivery Note header. delivery_no / order_ref are generated on confirm,
    -- same pattern as the GRN receipts above.
    CREATE TABLE IF NOT EXISTS deliveries (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      delivery_no TEXT NOT NULL UNIQUE,
      order_ref TEXT DEFAULT NULL,
      recipient TEXT DEFAULT NULL,
      recipient_phone TEXT DEFAULT NULL,
      recipient_email TEXT DEFAULT NULL,
      address TEXT,
      vehicle_id INTEGER DEFAULT NULL,
      vehicle_reg TEXT,
      driver_id INTEGER DEFAULT NULL,
      driver_name TEXT,
      driver_phone TEXT,
      remarks TEXT,
      created_by TEXT DEFAULT NULL,
      confirmed INTEGER DEFAULT 0,
      confirmed_at DATETIME DEFAULT NULL,
      document_path TEXT DEFAULT NULL,
      email_sent INTEGER DEFAULT 0,
      email_error TEXT DEFAULT NULL,
      created_at DATETIME DEFAULT (datetime('now')),
      updated_at DATETIME DEFAULT (datetime('now')),
      FOREIGN KEY (vehicle_id) REFERENCES vehicles(id) ON DELETE SET NULL,
      FOREIGN KEY (driver_id) REFERENCES drivers(id) ON DELETE SET NULL
    );

    CREATE TABLE IF NOT EXISTS delivery_lines (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      delivery_id INTEGER NOT NULL,
      sku TEXT NOT NULL,
      qty REAL NOT NULL,
      unit TEXT DEFAULT 'pcs',
      location TEXT DEFAULT NULL,
      batch_id TEXT DEFAULT NULL,
      created_at DATETIME DEFAULT (datetime('now')),
      FOREIGN KEY (delivery_id) REFERENCES deliveries(id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS stock_ledger (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      sku TEXT DEFAULT NULL,
      item_sku TEXT DEFAULT NULL,
      qty REAL NOT NULL,
      txn_type TEXT NOT NULL,
      location_id INTEGER DEFAULT NULL,
      from_location_id INTEGER DEFAULT NULL,
      to_location_id INTEGER DEFAULT NULL,
      reference TEXT DEFAULT NULL,
      batch_no TEXT DEFAULT NULL,
      created_by TEXT DEFAULT NULL,
      when_ts DATETIME DEFAULT (datetime('now')),
      notes TEXT,
      FOREIGN KEY (from_location_id) REFERENCES locations(id) ON DELETE SET NULL,
      FOREIGN KEY (to_location_id) REFERENCES locations(id) ON DELETE SET NULL
    );
    """
    conn = get_conn()
    try:
        conn.executescript(ddl)
        conn.commit()
    finally:
        conn.close()


init_db()


def ensure_columns(table, columns):
    """Add any missing columns to an existing table (simple migration for upgrades)."""
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(f"PRAGMA table_info({table})")
        existing = {row['name'] for row in cur.fetchall()}
        for col, decl in columns.items():
            if col not in existing:
                cur.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
        conn.commit()
    finally:
        conn.close()


# Covers databases created by an earlier version of this app that predates
# the driver/vehicle/recipient-contact and PDF/email columns on deliveries.
ensure_columns('deliveries', {
    'recipient_phone': 'TEXT',
    'recipient_email': 'TEXT',
    'vehicle_id': 'INTEGER',
    'driver_id': 'INTEGER',
    'driver_phone': 'TEXT',
    'confirmed': 'INTEGER DEFAULT 0',
    'confirmed_at': 'DATETIME',
    'document_path': 'TEXT',
    'email_sent': 'INTEGER DEFAULT 0',
    'email_error': 'TEXT',
})


# -------------------------------------------------------------------
# Sequence-based GRN / Order Ref generators
# -------------------------------------------------------------------
def next_sequence(name):
    """Atomically bump and return the next integer in a named sequence."""
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        cur.execute("SELECT last FROM sequences WHERE name = ?", (name,))
        row = cur.fetchone()
        if row is None:
            val = 1
            cur.execute("INSERT INTO sequences (name, last) VALUES (?, ?)", (name, val))
        else:
            val = int(row['last']) + 1
            cur.execute("UPDATE sequences SET last = ? WHERE name = ?", (val, name))
        conn.commit()
        return val
    finally:
        conn.close()


def generate_grn_no():
    seq = next_sequence('grn')
    return f"GRN-{datetime.utcnow().strftime('%Y%m%d')}-{seq:04d}"


def generate_order_ref():
    seq = next_sequence('order_ref')
    return f"ORD-{datetime.utcnow().strftime('%Y%m%d')}-{seq:04d}"


def generate_delivery_no():
    seq = next_sequence('delivery_no')
    return f"DN-{datetime.utcnow().strftime('%Y%m%d')}-{seq:04d}"


# -------------------------------------------------------------------
# PDF generation
# -------------------------------------------------------------------
def generate_pdf_for_receipt(grn_no, order_ref, supplier, supplier_email, condition, condition_notes, lines):
    filename = f"{grn_no}.pdf"
    path = os.path.join(UPLOAD_DIR, filename)
    c = canvas.Canvas(path, pagesize=A4)

    c.setFont("Helvetica-Bold", 14)
    c.drawString(40, 800, f"Goods Received Note {grn_no}")

    c.setFont("Helvetica", 10)
    c.drawString(40, 782, f"Date: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
    c.drawString(40, 767, f"Order Ref: {order_ref or ''}")
    c.drawString(40, 752, f"Supplier: {supplier or ''}")
    c.drawString(40, 737, f"Supplier Email: {supplier_email or ''}")
    c.drawString(40, 722, f"Condition: {condition or 'good'}")
    if condition_notes:
        c.drawString(40, 707, f"Notes: {condition_notes[:90]}")

    y = 680
    c.setFont("Helvetica-Bold", 10)
    c.drawString(40, y, "SKU")
    c.drawString(220, y, "Qty")
    c.drawString(280, y, "Unit")
    c.drawString(340, y, "Location")
    c.drawString(440, y, "Batch")
    c.setFont("Helvetica", 10)
    y -= 18

    for ln in lines:
        c.drawString(40, y, str(ln.get('sku', '')))
        c.drawString(220, y, str(ln.get('qty', '')))
        c.drawString(280, y, str(ln.get('unit', '') or ''))
        c.drawString(340, y, str(ln.get('location', '') or ''))
        c.drawString(440, y, str(ln.get('batch_id', '') or ''))
        y -= 16
        if y < 60:
            c.showPage()
            y = 800

    c.showPage()
    c.save()
    return path


# -------------------------------------------------------------------
# Email
# -------------------------------------------------------------------
def send_receipt_email(to_email, grn_no, order_ref, pdf_path, receipt_id):
    """Send the receipt PDF to the supplier and record success/failure on the receipt row."""
    def _send():
        error = None
        try:
            if not MAIL_HOST:
                raise RuntimeError("Mail server is not configured (BUSIA_SMTP_HOST missing)")

            msg = EmailMessage()
            msg['Subject'] = f"Goods Received Confirmation {grn_no}"
            msg['From'] = f"{MAIL_FROM_NAME} <{MAIL_FROM}>"
            msg['To'] = to_email
            msg.set_content(
                f"Dear Supplier,\n\n"
                f"We confirm receipt of goods for GRN {grn_no} (Order {order_ref}).\n"
                f"Please find the goods received note attached for your records.\n\n"
                f"Regards,\n{MAIL_FROM_NAME}"
            )
            with open(pdf_path, 'rb') as f:
                msg.add_attachment(
                    f.read(), maintype='application', subtype='pdf',
                    filename=os.path.basename(pdf_path)
                )

            if MAIL_USE_TLS:
                s = smtplib.SMTP(MAIL_HOST, MAIL_PORT, timeout=20)
                s.starttls()
            else:
                s = smtplib.SMTP_SSL(MAIL_HOST, MAIL_PORT, timeout=20)
            if MAIL_USER:
                s.login(MAIL_USER, MAIL_PASS)
            s.send_message(msg)
            s.quit()
        except Exception as exc:
            error = str(exc)
            app.logger.exception("Failed to email receipt %s to %s", grn_no, to_email)

        conn = get_conn()
        try:
            conn.execute(
                "UPDATE receipts SET email_sent = ?, email_error = ? WHERE id = ?",
                (0 if error else 1, error, receipt_id)
            )
            conn.commit()
        finally:
            conn.close()

    threading.Thread(target=_send, daemon=True).start()


def generate_pdf_for_delivery(delivery_no, order_ref, recipient, recipient_phone, recipient_email,
                               address, driver_name, driver_phone, vehicle_reg, remarks, lines):
    filename = f"{delivery_no}.pdf"
    path = os.path.join(UPLOAD_DIR, filename)
    c = canvas.Canvas(path, pagesize=A4)

    c.setFont("Helvetica-Bold", 14)
    c.drawString(40, 800, f"Delivery Note {delivery_no}")

    c.setFont("Helvetica", 10)
    c.drawString(40, 782, f"Date: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
    c.drawString(40, 767, f"Order Ref: {order_ref or ''}")
    c.drawString(40, 752, f"Recipient: {recipient or ''}")
    c.drawString(40, 737, f"Recipient Phone: {recipient_phone or ''}")
    c.drawString(40, 722, f"Recipient Email: {recipient_email or ''}")
    c.drawString(40, 707, f"Address: {(address or '')[:80]}")
    c.drawString(40, 692, f"Driver: {driver_name or ''}  ({driver_phone or ''})")
    c.drawString(40, 677, f"Vehicle: {vehicle_reg or ''}")
    if remarks:
        c.drawString(40, 662, f"Remarks: {remarks[:90]}")

    y = 635
    c.setFont("Helvetica-Bold", 10)
    c.drawString(40, y, "SKU")
    c.drawString(220, y, "Qty")
    c.drawString(280, y, "Unit")
    c.drawString(340, y, "Location")
    c.drawString(440, y, "Batch")
    c.setFont("Helvetica", 10)
    y -= 18

    for ln in lines:
        c.drawString(40, y, str(ln.get('sku', '')))
        c.drawString(220, y, str(ln.get('qty', '')))
        c.drawString(280, y, str(ln.get('unit', '') or ''))
        c.drawString(340, y, str(ln.get('location', '') or ''))
        c.drawString(440, y, str(ln.get('batch_id', '') or ''))
        y -= 16
        if y < 100:
            c.showPage()
            y = 800

    y -= 40
    if y < 100:
        c.showPage()
        y = 800
    c.drawString(40, y, "Recipient signature: ____________________     Date: ____________")

    c.showPage()
    c.save()
    return path


def send_delivery_email(to_email, delivery_no, order_ref, pdf_path, delivery_id):
    """Send the delivery note PDF to the recipient and record success/failure."""
    def _send():
        error = None
        try:
            if not MAIL_HOST:
                raise RuntimeError("Mail server is not configured (BUSIA_SMTP_HOST missing)")

            msg = EmailMessage()
            msg['Subject'] = f"Delivery Note {delivery_no}"
            msg['From'] = f"{MAIL_FROM_NAME} <{MAIL_FROM}>"
            msg['To'] = to_email
            msg.set_content(
                f"Dear Recipient,\n\n"
                f"Please find attached the delivery note {delivery_no} (Order {order_ref}) "
                f"for the goods dispatched to you.\n\n"
                f"Regards,\n{MAIL_FROM_NAME}"
            )
            with open(pdf_path, 'rb') as f:
                msg.add_attachment(
                    f.read(), maintype='application', subtype='pdf',
                    filename=os.path.basename(pdf_path)
                )

            if MAIL_USE_TLS:
                s = smtplib.SMTP(MAIL_HOST, MAIL_PORT, timeout=20)
                s.starttls()
            else:
                s = smtplib.SMTP_SSL(MAIL_HOST, MAIL_PORT, timeout=20)
            if MAIL_USER:
                s.login(MAIL_USER, MAIL_PASS)
            s.send_message(msg)
            s.quit()
        except Exception as exc:
            error = str(exc)
            app.logger.exception("Failed to email delivery note %s to %s", delivery_no, to_email)

        conn = get_conn()
        try:
            conn.execute(
                "UPDATE deliveries SET email_sent = ?, email_error = ? WHERE id = ?",
                (0 if error else 1, error, delivery_id)
            )
            conn.commit()
        finally:
            conn.close()

    threading.Thread(target=_send, daemon=True).start()


# -------------------------------------------------------------------
# Dashboard / index
# -------------------------------------------------------------------
@app.route('/')
def index():
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT sku, name, unit, category,
                   COALESCE(on_hand, 0) AS on_hand,
                   COALESCE(reorder_level, ?) AS reorder_level
            FROM items
            ORDER BY name ASC
        """, (LOW_STOCK_THRESHOLD,))
        items = [dict(r) for r in cur.fetchall()]

        cur.execute("""
            SELECT when_ts AS `when`, sku, qty, txn_type
            FROM stock_ledger
            ORDER BY when_ts DESC
            LIMIT 5
        """)
        recent_movements = [dict(r) for r in cur.fetchall()]
    except Exception:
        app.logger.exception("index() failed")
        items, recent_movements = [], []
    finally:
        conn.close()

    low_alerts = [it for it in items if it['on_hand'] <= it['reorder_level']]
    stats = {
        'active_skus': len(items),
        'units_on_hand': sum(it['on_hand'] for it in items),
        'recent_movements': len(recent_movements),
    }

    return render_template(
        'index.html', items=items, user=session.get('username'),
        low_alerts=low_alerts, recent_movements=recent_movements,
        stats=stats, now=datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
        # stats=stats, now=datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')
    )


@app.route('/api/dashboard', methods=['GET'])
def api_dashboard():
    conn = get_conn()
    payload = {'active_skus': 0, 'units_on_hand': 0, 'low_stock_alerts': 0,
               'reorder_alerts': [], 'recent_movements': []}
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(DISTINCT sku) AS n FROM items")
        payload['active_skus'] = cur.fetchone()['n']

        cur.execute("SELECT COALESCE(SUM(on_hand),0) AS n FROM items")
        payload['units_on_hand'] = cur.fetchone()['n']

        cur.execute("""
            SELECT sku, name, unit, category, on_hand,
                   COALESCE(reorder_level, ?) AS reorder_level
            FROM items
            WHERE on_hand <= COALESCE(reorder_level, ?)
            ORDER BY (COALESCE(reorder_level, ?) - on_hand) DESC
            LIMIT 10
        """, (LOW_STOCK_THRESHOLD, LOW_STOCK_THRESHOLD, LOW_STOCK_THRESHOLD))
        alerts = [dict(r) for r in cur.fetchall()]
        payload['reorder_alerts'] = alerts
        payload['low_stock_alerts'] = len(alerts)

        cur.execute("""
            SELECT when_ts AS ts, sku, qty, txn_type, created_by AS user
            FROM stock_ledger ORDER BY when_ts DESC LIMIT 10
        """)
        payload['recent_movements'] = [dict(r) for r in cur.fetchall()]
        return jsonify(payload), 200
    except Exception:
        app.logger.exception("api_dashboard failed")
        return jsonify({'status': 'error', 'message': 'Server error fetching dashboard',
                         'reorder_alerts': [], 'recent_movements': []}), 500
    finally:
        conn.close()


# -------------------------------------------------------------------
# Items
# -------------------------------------------------------------------
def parse_reorder_level(val):
    if val in (None, ''):
        return None
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


@app.route('/items')
def items():
    selected_category = (request.args.get('category') or '').strip() or None
    q = (request.args.get('q') or '').strip() or None

    conn = get_conn()
    try:
        cur = conn.cursor()
        sql = """
            SELECT sku, name, unit, category, COALESCE(on_hand,0) AS on_hand,
                   COALESCE(reorder_level, ?) AS reorder_level
            FROM items
        """
        params = [LOW_STOCK_THRESHOLD]
        clauses = []
        if selected_category:
            clauses.append("category = ?")
            params.append(selected_category)
        if q:
            clauses.append("(sku LIKE ? OR name LIKE ?)")
            params += [f"%{q}%", f"%{q}%"]
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY name ASC"

        cur.execute(sql, params)
        item_rows = [dict(r) for r in cur.fetchall()]

        low_alerts = [it for it in item_rows if it['on_hand'] <= it['reorder_level']]

        cur.execute("""
            SELECT when_ts AS `when`, sku, qty, txn_type
            FROM stock_ledger ORDER BY when_ts DESC LIMIT 10
        """)
        recent_movements = [dict(r) for r in cur.fetchall()]

        cur.execute("""
            SELECT category, COUNT(1) AS cnt FROM items
            WHERE category IS NOT NULL AND category != ''
            GROUP BY category ORDER BY category ASC
        """)
        cat_rows = cur.fetchall()
        categories = [{'name': r['category'], 'count': r['cnt']} for r in cat_rows] \
            or [{'name': c, 'count': ''} for c in DEFAULT_CATEGORIES]

        stats = {
            'active_skus': len(item_rows),
            'units_on_hand': sum(it['on_hand'] for it in item_rows),
            'recent_movements': len(recent_movements),
        }
    except Exception:
        app.logger.exception("Failed to load items dashboard")
        item_rows, low_alerts, recent_movements = [], [], []
        categories = [{'name': c, 'count': ''} for c in DEFAULT_CATEGORIES]
        stats = {'active_skus': 0, 'units_on_hand': 0, 'recent_movements': 0}
    finally:
        conn.close()

    return render_template(
        'items.html', items=item_rows, low_alerts=low_alerts, stats=stats,
        recent_movements=recent_movements, units=DEFAULT_UNITS, categories=categories,
        selected_category=selected_category, user=session.get('username')
    )


@app.route('/export_items')
def export_items():
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT sku, name, unit, category, COALESCE(on_hand,0) AS on_hand,
                   COALESCE(reorder_level, ?) AS reorder_level
            FROM items ORDER BY name ASC
        """, (LOW_STOCK_THRESHOLD,))
        rows = cur.fetchall()

        si = io.StringIO()
        writer = csv.writer(si)
        writer.writerow(['SKU', 'Name', 'Unit', 'Category', 'On hand', 'Reorder level'])
        for r in rows:
            writer.writerow([r['sku'], r['name'], r['unit'], r['category'], r['on_hand'], r['reorder_level']])

        output = make_response(si.getvalue())
        output.headers["Content-Disposition"] = "attachment; filename=items.csv"
        output.headers["Content-type"] = "text/csv"
        return output
    except Exception:
        app.logger.exception("Failed to export items")
        return redirect(url_for('items'))
    finally:
        conn.close()


@app.route('/api/items', methods=['POST'])
def api_create_item():
    data = request.get_json(silent=True) or request.form.to_dict()
    if not data:
        return jsonify({'status': 'error', 'message': 'Invalid or empty payload'}), 400

    sku = (data.get('sku') or '').strip()
    name = (data.get('name') or '').strip()
    unit = (data.get('unit') or '').strip() or 'pcs'
    category = (data.get('category') or '').strip()
    reorder_level = parse_reorder_level(data.get('reorder_level'))

    if not sku or not name:
        return jsonify({'status': 'error', 'message': 'SKU and name are required'}), 400

    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(1) AS cnt FROM items WHERE sku = ?", (sku,))
        if cur.fetchone()['cnt'] > 0:
            return jsonify({'status': 'error', 'message': 'SKU already exists'}), 400

        cur.execute("""
            INSERT INTO items (sku, name, unit, category, on_hand, reorder_level, created_at, updated_at)
            VALUES (?, ?, ?, ?, 0, ?, datetime('now'), datetime('now'))
        """, (sku, name, unit, category, reorder_level))
        conn.commit()

        return jsonify({'status': 'ok', 'item': {
            'sku': sku, 'name': name, 'unit': unit, 'category': category,
            'on_hand': 0, 'reorder_level': reorder_level if reorder_level is not None else LOW_STOCK_THRESHOLD
        }}), 200
    except Exception:
        conn.rollback()
        app.logger.exception("Failed to create item")
        return jsonify({'status': 'error', 'message': 'Server error creating item'}), 500
    finally:
        conn.close()


@app.route('/create_item', methods=['POST'])
def create_item():
    sku = (request.form.get('sku') or '').strip()
    name = (request.form.get('name') or '').strip()
    unit = (request.form.get('unit_final') or request.form.get('unit_select') or 'pcs').strip()
    category = (request.form.get('category_final') or request.form.get('category_select') or '').strip()
    reorder_level = parse_reorder_level(request.form.get('reorder_level'))

    if not sku or not name:
        flash('SKU and Name are required.', 'danger')
        return redirect(url_for('items'))

    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(1) AS cnt FROM items WHERE sku = ?", (sku,))
        if cur.fetchone()['cnt'] > 0:
            flash('SKU already exists.', 'danger')
            return redirect(url_for('items'))

        cur.execute("""
            INSERT INTO items (sku, name, unit, category, on_hand, reorder_level, created_at, updated_at)
            VALUES (?, ?, ?, ?, 0, ?, datetime('now'), datetime('now'))
        """, (sku, name, unit, category, reorder_level))
        conn.commit()
        flash(f'Item {sku} created.', 'success')
    except Exception:
        conn.rollback()
        app.logger.exception("Failed to create item (form)")
        flash('Server error while creating item.', 'danger')
    finally:
        conn.close()

    return redirect(url_for('items'))


@app.route('/item/<sku>')
def item_detail(sku):
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT sku, name, unit, category, COALESCE(on_hand,0) AS on_hand,
                   COALESCE(reorder_level, ?) AS reorder_level
            FROM items WHERE sku = ?
        """, (LOW_STOCK_THRESHOLD, sku))
        item = cur.fetchone()

        cur.execute("""
            SELECT when_ts AS `when`, qty, txn_type, location_id
            FROM stock_ledger WHERE sku = ? ORDER BY when_ts DESC LIMIT 10
        """, (sku,))
        movements = cur.fetchall()
    except Exception:
        app.logger.exception("Failed to load item detail")
        item, movements = None, []
    finally:
        conn.close()

    return render_template('item_detail.html', sku=sku, item=item, movements=movements, user=session.get('username'))


@app.route('/item/<sku>/adjust', methods=['GET', 'POST'])
def adjust_stock(sku):
    if request.method == 'GET':
        conn = get_conn()
        try:
            cur = conn.cursor()
            cur.execute("SELECT name FROM locations ORDER BY name ASC")
            locations_list = [r['name'] for r in cur.fetchall()]
        except Exception:
            app.logger.exception("Failed to load locations for adjust_stock")
            locations_list = []
        finally:
            conn.close()
        return render_template('adjust_stock.html', sku=sku, locations=locations_list, user=session.get('username'))

    data = request.get_json(silent=True) or {}
    try:
        qty = float(data.get('qty', 0) or 0)
    except (TypeError, ValueError):
        qty = 0
    txn_type = (data.get('txn_type') or 'adjust').strip().lower()
    location = data.get('location') or None
    note = data.get('note') or None

    if qty == 0:
        return jsonify({'status': 'error', 'message': 'Quantity required'}), 400

    # Normalize the sign by movement type: Receive always adds stock,
    # Issue always removes it. Only a plain Adjustment keeps the sign
    # the user actually typed (positive to add, negative to remove).
    if txn_type == 'receive':
        qty = abs(qty)
    elif txn_type == 'issue':
        qty = -abs(qty)

    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO stock_ledger (sku, item_sku, qty, txn_type, reference, notes, created_by, when_ts)
            VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
        """, (sku, sku, qty, txn_type.upper(), location, note, session.get('username', 'system')))
        cur.execute("UPDATE items SET on_hand = COALESCE(on_hand,0) + ?, updated_at = datetime('now') WHERE sku = ?", (qty, sku))
        conn.commit()
        return jsonify({'status': 'ok'})
    except Exception:
        conn.rollback()
        app.logger.exception("Failed to adjust stock")
        return jsonify({'status': 'error', 'message': 'Failed to adjust stock'}), 500
    finally:
        conn.close()


# -------------------------------------------------------------------
# Receive goods (GRN) — auto GRN/order ref + PDF + email on confirm
# -------------------------------------------------------------------
# import os
# import uuid
# import time
# import sqlite3
# from datetime import datetime, timezone
# from flask import request, jsonify, session, render_template, send_file, abort
# from reportlab.lib.pagesizes import A4
# from reportlab.pdfgen import canvas

# # --- DB and output paths ---
# DB_PATH = os.path.join(os.path.dirname(__file__), 'busia_wms.sqlite3')
# OUT_DIR = os.path.join(os.path.dirname(__file__), 'generated_docs')
# os.makedirs(OUT_DIR, exist_ok=True)

# def get_conn():
#     conn = sqlite3.connect(DB_PATH, timeout=30)
#     conn.row_factory = sqlite3.Row
#     return conn

# # --- Helper: Minimal PDF generator ---
# def generate_pdf_for_receipt(grn_no, order_ref, supplier_email, condition, condition_notes, lines):
#     filename = f"{grn_no}.pdf"
#     out_path = os.path.join(OUT_DIR, filename)

#     c = canvas.Canvas(out_path, pagesize=A4)
#     c.setTitle(f"GRN {grn_no}")
#     c.setFont("Helvetica-Bold", 14)
#     c.drawString(40, 800, f"Goods Received Note: {grn_no}")
#     c.setFont("Helvetica", 10)
#     c.drawString(40, 785, f"Order Ref: {order_ref or ''}")
#     c.drawString(300, 785, f"Date: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
#     c.drawString(40, 770, f"Supplier Email: {supplier_email or ''}")
#     c.drawString(40, 755, f"Condition: {condition} {('- ' + condition_notes) if condition_notes else ''}")

#     y = 730
#     c.setFont("Helvetica-Bold", 10)
#     c.drawString(40, y, "SKU")
#     c.drawString(180, y, "Qty")
#     c.drawString(230, y, "Unit")
#     c.drawString(290, y, "Batch")
#     c.drawString(380, y, "Location")
#     c.setFont("Helvetica", 10)
#     y -= 16

#     for ln in lines:
#         c.drawString(40, y, str(ln.get('sku', '')))
#         c.drawString(180, y, str(ln.get('qty', '')))
#         c.drawString(230, y, str(ln.get('unit', '')))
#         c.drawString(290, y, str(ln.get('batch_no') or ln.get('batch_id', '')))
#         c.drawString(380, y, str(ln.get('location', '')))
#         y -= 14
#         if y < 60:
#             c.showPage()
#             y = 800
#     c.showPage()
#     c.save()
#     return out_path

# # --- Helper: Collision-proof Receipt creation ---
# def create_receipt_row(cur, conn):
#     attempts = 0
#     max_attempts = 5
#     while attempts < max_attempts:
#         attempts += 1
#         receipt_no = f"RCPT-{uuid.uuid4().hex[:12]}"
#         try:
#             cur.execute(
#                 "INSERT INTO receipts (receipt_no, created_at) VALUES (?, datetime('now'))",
#                 (receipt_no,)
#             )
#             conn.commit()
#             receipt_id = cur.lastrowid
#             cur.execute("SELECT * FROM receipts WHERE id = ?", (receipt_id,))
#             return receipt_id, cur.fetchone()
#         except sqlite3.IntegrityError:
#             conn.rollback()
#             time.sleep(0.02)
#             continue
#     raise RuntimeError("Failed to create unique receipt after multiple attempts")


# # --- Routes ---

# @app.route('/receive', methods=['GET'])
# def receive():
#     conn = get_conn()
#     items_list, locations_list, receipt = [], [], None
#     try:
#         cur = conn.cursor()
#         cur.execute("SELECT sku, name, unit FROM items ORDER BY name ASC")
#         items_list = [dict(r) for r in cur.fetchall()]

#         cur.execute("SELECT name FROM locations ORDER BY name ASC")
#         locations_list = [r['name'] for r in cur.fetchall()]

#         receipt_id = request.args.get('id')
#         if receipt_id:
#             cur.execute("SELECT * FROM receipts WHERE id = ?", (receipt_id,))
#             row = cur.fetchone()
#             if row:
#                 receipt = dict(row)

#         if not receipt:
#             receipt_id, row = create_receipt_row(cur, conn)
#             receipt = dict(row)
#     except Exception:
#         app.logger.exception("Failed to load receive page data")
#         receipt = receipt or {'id': '', 'receipt_no': ''}
#     finally:
#         conn.close()

#     return render_template(
#         'receive.html', items=items_list, locations=locations_list,
#         user=session.get('username'), receipt=receipt
#     )


# @app.route('/api/receive', methods=['POST'])
# def api_receive():
#     data = request.get_json(silent=True) or {}

#     supplier = (data.get('supplier') or '').strip()
#     supplier_email = (data.get('supplier_email') or '').strip()
#     po_ref = (data.get('po_ref') or '').strip()
#     condition = (data.get('condition') or 'good').strip().lower()
#     condition_notes = (data.get('condition_notes') or '').strip()
#     confirmed_by = (data.get('confirmed_by') or session.get('username') or 'web').strip()
#     lines = data.get('lines') or []

#     if not supplier or not supplier_email:
#         return jsonify({'status': 'error', 'message': 'Supplier and email are required.'}), 400
#     if not lines or not any(l.get('sku') and float(l.get('qty') or 0) > 0 for l in lines):
#         return jsonify({'status': 'error', 'message': 'At least one valid line is required.'}), 400

#     conn = get_conn()
#     try:
#         cur = conn.cursor()
#         receipt_id = data.get('receipt_id')
        
#         if receipt_id:
#             cur.execute("SELECT * FROM receipts WHERE id = ?", (receipt_id,))
#             hdr = cur.fetchone()
#             if not hdr:
#                 receipt_id, hdr = create_receipt_row(cur, conn)
#         else:
#             receipt_id, hdr = create_receipt_row(cur, conn)

#         grn_no = hdr['grn_no'] or f"GRN-{datetime.now(timezone.utc).strftime('%Y%m%d')}-{int(datetime.now(timezone.utc).timestamp())}"
#         order_ref = hdr['order_ref'] or po_ref or f"ORD-{int(datetime.now(timezone.utc).timestamp())}"

#         clean_lines = []
#         for ln in lines:
#             sku = (ln.get('sku') or '').strip()
#             try:
#                 qty = float(ln.get('qty') or 0)
#             except (ValueError, TypeError):
#                 qty = 0
#             if not sku or qty <= 0:
#                 continue
                
#             unit = (ln.get('unit') or '').strip() or 'pcs'
#             location = (ln.get('location') or '').strip() or None
#             batch_id = (ln.get('batch') or ln.get('batch_id') or '').strip() or None
#             expiry = (ln.get('expiry') or '').strip() or None

#             cur.execute("""
#                 INSERT INTO receipt_lines (receipt_id, sku, qty, unit, location, batch_id, expiry)
#                 VALUES (?, ?, ?, ?, ?, ?, ?)
#             """, (receipt_id, sku, qty, unit, location, batch_id, expiry))

#             cur.execute("UPDATE items SET on_hand = COALESCE(on_hand,0) + ?, updated_at = datetime('now') WHERE sku = ?", (qty, sku))
#             if cur.rowcount == 0:
#                 cur.execute("""
#                     INSERT INTO items (sku, name, unit, category, on_hand, reorder_level, created_at, updated_at)
#                     VALUES (?, ?, ?, '', ?, NULL, datetime('now'), datetime('now'))
#                 """, (sku, sku, unit, qty))

#             cur.execute("""
#                 INSERT INTO stock_ledger (sku, item_sku, qty, txn_type, reference, batch_no, created_by, when_ts, notes)
#                 VALUES (?, ?, ?, 'RECEIVE', ?, ?, ?, datetime('now'), ?)
#             """, (sku, sku, qty, grn_no, batch_id, confirmed_by, f"GRN {grn_no}"))

#             clean_lines.append({'sku': sku, 'qty': qty, 'unit': unit, 'location': location, 'batch_id': batch_id})

#         cur.execute("""
#             UPDATE receipts
#             SET grn_no = ?, order_ref = ?, supplier = ?, supplier_email = ?, po_no = ?,
#                 condition_status = ?, condition_notes = ?, received_by = ?,
#                 confirmed = 1, confirmed_at = datetime('now')
#             WHERE id = ?
#         """, (grn_no, order_ref, supplier, supplier_email, po_ref, condition, condition_notes, confirmed_by, receipt_id))
#         conn.commit()

#         pdf_path = None
#         try:
#             pdf_path = generate_pdf_for_receipt(grn_no, order_ref, supplier_email, condition, condition_notes, clean_lines)
#             cur.execute("UPDATE receipts SET document_path = ? WHERE id = ?", (pdf_path, receipt_id))
#             conn.commit()
#         except Exception:
#             app.logger.exception("PDF generation failed for receipt %s", receipt_id)

#         return jsonify({
#             'status': 'ok',
#             'receipt_id': receipt_id,
#             'grn_no': grn_no,
#             'order_ref': order_ref,
#             'pdf_path': pdf_path
#         })

#     except Exception:
#         conn.rollback()
#         app.logger.exception("Failed to process GRN")
#         return jsonify({'status': 'error', 'message': 'Failed to process GRN'}), 500
#     finally:
#         conn.close()


# @app.route('/receive/<int:receipt_id>/pdf')
# def download_receipt_pdf(receipt_id):
#     conn = get_conn()
#     try:
#         cur = conn.cursor()
#         cur.execute("SELECT document_path, grn_no FROM receipts WHERE id = ?", (receipt_id,))
#         row = cur.fetchone()
#     finally:
#         conn.close()

#     if not row or not row['document_path'] or not os.path.exists(row['document_path']):
#         abort(404)

#     return send_file(row['document_path'], as_attachment=False,
#                       download_name=f"{row['grn_no'] or receipt_id}.pdf")


# @app.route('/receipt/<string:grn_no>')
# def print_receipt(grn_no):
#     conn = get_conn()
#     try:
#         cur = conn.cursor()
#         cur.execute("SELECT * FROM receipts WHERE grn_no = ?", (grn_no,))
#         tx = cur.fetchone()
#         if not tx:
#             abort(404)
#         cur.execute("""
#             SELECT rl.sku, i.name AS name, rl.qty, rl.unit, rl.location, rl.batch_id, rl.expiry
#             FROM receipt_lines rl
#             LEFT JOIN items i ON i.sku = rl.sku
#             WHERE rl.receipt_id = ?
#         """, (tx['id'],))
#         lines = [dict(r) for r in cur.fetchall()]
#     finally:
#         conn.close()

#     return render_template('grn_print.html', tx=dict(tx), lines=lines)


# @app.route('/receive', methods=['GET'])
# def receive():
#     conn = get_conn()
#     items_list, locations_list, receipt = [], [], None
#     try:
#         cur = conn.cursor()
#         cur.execute("SELECT sku, name, unit FROM items ORDER BY name ASC")
#         items_list = [dict(r) for r in cur.fetchall()]

#         cur.execute("SELECT name FROM locations ORDER BY name ASC")
#         locations_list = [r['name'] for r in cur.fetchall()]

#         receipt_id = request.args.get('id')
#         if receipt_id:
#             cur.execute("SELECT * FROM receipts WHERE id = ?", (receipt_id,))
#             row = cur.fetchone()
#             if row:
#                 receipt = dict(row)

#         if not receipt:
#             receipt_no = f"RCPT-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
#             # receipt_no = f"RCPT-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
#             cur.execute(
#                 "INSERT INTO receipts (receipt_no, created_at) VALUES (?, datetime('now'))",
#                 (receipt_no,)
#             )
#             conn.commit()
#             receipt = {'id': cur.lastrowid, 'receipt_no': receipt_no}
#     except Exception:
#         app.logger.exception("Failed to load receive page data")
#         receipt = receipt or {'id': ''}
#     finally:
#         conn.close()

#     return render_template(
#         'receive.html', items=items_list, locations=locations_list,
#         user=session.get('username'), receipt=receipt, csrf_token=generate_csrf()
#     )


# @app.route('/api/receive', methods=['POST'])
# def api_receive():
#     data = request.get_json(silent=True) or {}

#     supplier = (data.get('supplier') or '').strip()
#     supplier_email = (data.get('supplier_email') or '').strip()
#     po_ref = (data.get('po_ref') or '').strip()
#     condition = (data.get('condition') or 'good').strip().lower()
#     condition_notes = (data.get('condition_notes') or '').strip()
#     confirmed_by = (data.get('confirmed_by') or session.get('username') or 'web').strip()
#     lines = data.get('lines') or []

#     # Basic validation
#     if not supplier or not supplier_email:
#         return jsonify({'status': 'error', 'message': 'Supplier and email are required.'}), 400
#     if not lines or not any(l.get('sku') and float(l.get('qty') or 0) > 0 for l in lines):
#         return jsonify({'status': 'error', 'message': 'At least one valid line is required.'}), 400

#     conn = get_conn()
#     try:
#         cur = conn.cursor()

#         # Find or create receipt record
#         receipt_id = data.get('receipt_id')
#         if receipt_id:
#             cur.execute("SELECT * FROM receipts WHERE id = ?", (receipt_id,))
#             hdr = cur.fetchone()
#             if not hdr:
#                 receipt_id = None
#         if not receipt_id:
#             receipt_no = f"RCPT-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
#             cur.execute("INSERT INTO receipts (receipt_no, created_at) VALUES (?, datetime('now'))", (receipt_no,))
#             receipt_id = cur.lastrowid
#             cur.execute("SELECT * FROM receipts WHERE id = ?", (receipt_id,))
#             hdr = cur.fetchone()

#         # Generate GRN and Order ref
#         grn_no = hdr['grn_no'] or generate_grn_no()
#         order_ref = hdr['order_ref'] or po_ref or generate_order_ref()

#         # Insert lines, update stock, create ledger
#         clean_lines = []
#         for ln in lines:
#             sku = (ln.get('sku') or '').strip()
#             try:
#                 qty = float(ln.get('qty') or 0)
#             except:
#                 qty = 0
#             if not sku or qty <= 0:
#                 continue
#             unit = (ln.get('unit') or '').strip() or 'pcs'
#             location = (ln.get('location') or '').strip() or None
#             batch_id = (ln.get('batch') or ln.get('batch_id') or '').strip() or None
#             expiry = (ln.get('expiry') or '').strip() or None

#             # Insert receipt line
#             cur.execute("""
#                 INSERT INTO receipt_lines (receipt_id, sku, qty, unit, location, batch_id, expiry)
#                 VALUES (?, ?, ?, ?, ?, ?, ?)
#             """, (receipt_id, sku, qty, unit, location, batch_id, expiry))

#             # Update stock
#             cur.execute("UPDATE items SET on_hand = COALESCE(on_hand,0) + ?, updated_at = datetime('now') WHERE sku = ?", (qty, sku))
#             if cur.rowcount == 0:
#                 # Insert new item if not exists
#                 cur.execute("""
#                     INSERT INTO items (sku, name, unit, category, on_hand, reorder_level, created_at, updated_at)
#                     VALUES (?, ?, ?, '', ?, NULL, datetime('now'), datetime('now'))
#                 """, (sku, sku, unit, qty))
#             # Insert ledger entry
#             cur.execute("""
#                 INSERT INTO stock_ledger (sku, item_sku, qty, txn_type, reference, batch_no, created_by, when_ts, notes)
#                 VALUES (?, ?, ?, 'RECEIVE', ?, ?, ?, datetime('now'), ?)
#             """, (sku, sku, qty, grn_no, batch_id, confirmed_by, f"GRN {grn_no}"))

#             clean_lines.append({'sku': sku, 'qty': qty, 'unit': unit, 'location': location, 'batch_id': batch_id})

#         # Update receipt header
#         cur.execute("""
#             UPDATE receipts
#             SET grn_no = ?, order_ref = ?, supplier = ?, supplier_email = ?, po_no = ?,
#                 condition_status = ?, condition_notes = ?, received_by = ?,
#                 confirmed = 1, confirmed_at = datetime('now')
#             WHERE id = ?
#         """, (grn_no, order_ref, supplier, supplier_email, po_ref, condition, condition_notes, confirmed_by, receipt_id))
#         conn.commit()

#         # Generate PDF
#         pdf_path = None
#         try:
#             pdf_path = generate_pdf_for_receipt(
#                 grn_no, order_ref, supplier, supplier_email, condition, condition_notes, clean_lines
#             )
#             cur.execute("UPDATE receipts SET document_path = ? WHERE id = ?", (pdf_path, receipt_id))
#             conn.commit()
#         except Exception:
#             app.logger.exception("PDF generation failed for receipt %s", receipt_id)

#         # Send email if condition is 'good'
#         email_sent = False
#         if pdf_path and condition == 'good':
#             try:
#                 send_receipt_email(supplier_email, grn_no, order_ref, pdf_path, receipt_id)
#                 email_sent = True
#             except Exception:
#                 app.logger.exception("Failed to send email for receipt %s", receipt_id)

#         return jsonify({
#             'status': 'ok',
#             'receipt_id': receipt_id,
#             'grn_no': grn_no,
#             'order_ref': order_ref,
#             'pdf_path': pdf_path,
#             'email_sent': email_sent
#         })

#     except Exception:
#         conn.rollback()
#         app.logger.exception("Failed to process GRN")
#         return jsonify({'status': 'error', 'message': 'Failed to process GRN'}), 500
#     finally:
#         conn.close()

# # place near the top of app.py with your other imports
# # --- top-of-file imports (ensure these are present once) ---
# import os
# import sqlite3
# import inspect
# import random
# from datetime import datetime, timezone
# from flask import request, jsonify, session  # DO NOT import current_app as app
# from reportlab.lib.pagesizes import A4
# from reportlab.pdfgen import canvas

# # --- DB and output paths (one-time) ---
# DB_PATH = os.path.join(os.path.dirname(__file__), 'busia_wms.sqlite3')
# OUT_DIR = os.path.join(os.path.dirname(__file__), 'generated_docs')
# os.makedirs(OUT_DIR, exist_ok=True)

# def get_conn():
#     conn = sqlite3.connect(DB_PATH, timeout=30)
#     conn.row_factory = sqlite3.Row
#     return conn

# # --- Minimal PDF generator matching the signature your app expects ---
# def generate_pdf_for_receipt(grn_no, order_ref, supplier, supplier_email, condition, condition_notes, lines):
#     filename = f"{grn_no}.pdf"
#     out_path = os.path.join(OUT_DIR, filename)
#     c = canvas.Canvas(out_path, pagesize=A4)
#     c.setFont("Helvetica-Bold", 14)
#     c.drawString(40, 800, f"Goods Received Note {grn_no}")
#     c.setFont("Helvetica", 10)
#     c.drawString(40, 785, f"Order Ref: {order_ref or ''}")
#     c.drawString(300, 785, f"Date: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
#     c.drawString(40, 770, f"Supplier: {supplier or ''}")
#     c.drawString(40, 755, f"Supplier Email: {supplier_email or ''}")
#     c.drawString(40, 740, f"Condition: {condition} {('- ' + condition_notes) if condition_notes else ''}")

#     y = 715
#     c.setFont("Helvetica-Bold", 10)
#     c.drawString(40, y, "SKU"); c.drawString(180, y, "Qty")
#     c.drawString(230, y, "Unit"); c.drawString(290, y, "Batch"); c.drawString(380, y, "Location")
#     c.setFont("Helvetica", 10)
#     y -= 16
#     for ln in lines:
#         c.drawString(40, y, str(ln.get('sku', '')))
#         c.drawString(180, y, str(ln.get('qty', '')))
#         c.drawString(230, y, str(ln.get('unit', '')))
#         c.drawString(290, y, str(ln.get('batch_id', '')))
#         c.drawString(380, y, str(ln.get('location', '')))
#         y -= 14
#         if y < 60:
#             c.showPage(); y = 800
#     c.showPage()
#     c.save()
#     return out_path



# import uuid
# import sqlite3
# import time
# from datetime import datetime, timezone
# from flask import request, jsonify, session

# def create_receipt_row(cur, conn):
#     """
#     Insert a new receipts row with a collision-proof receipt_no.
#     Returns (receipt_id, row) on success or raises RuntimeError after retries.
#     """
#     attempts = 0
#     max_attempts = 5
#     while attempts < max_attempts:
#         attempts += 1
#         receipt_no = f"RCPT-{uuid.uuid4().hex}"
#         try:
#             cur.execute(
#                 "INSERT INTO receipts (receipt_no, created_at) VALUES (?, datetime('now'))",
#                 (receipt_no,)
#             )
#             conn.commit()
#             receipt_id = cur.lastrowid
#             cur.execute("SELECT * FROM receipts WHERE id = ?", (receipt_id,))
#             return receipt_id, cur.fetchone()
#         except sqlite3.IntegrityError as e:
#             conn.rollback()
#             app.logger.warning("Receipt insert collision (attempt %s): %s", attempts, e)
#             time.sleep(0.02)
#             continue
#     raise RuntimeError("Failed to create unique receipt after multiple attempts")



# @app.route('/receive/<int:receipt_id>/pdf')
# def download_receipt_pdf(receipt_id):
#     conn = get_conn()
#     try:
#         cur = conn.cursor()
#         cur.execute("SELECT document_path, grn_no FROM receipts WHERE id = ?", (receipt_id,))
#         row = cur.fetchone()
#     finally:
#         conn.close()

#     if not row or not row['document_path'] or not os.path.exists(row['document_path']):
#         abort(404)

#     from flask import send_file
#     return send_file(row['document_path'], as_attachment=False,
#                       download_name=f"{row['grn_no'] or receipt_id}.pdf")


# @app.route('/receipt/<string:grn_no>')
# def print_receipt(grn_no):
#     """Printable HTML view of a GRN (opens window.print() automatically)."""
#     conn = get_conn()
#     try:
#         cur = conn.cursor()
#         cur.execute("SELECT * FROM receipts WHERE grn_no = ?", (grn_no,))
#         tx = cur.fetchone()
#         if not tx:
#             abort(404)
#         cur.execute("""
#             SELECT rl.sku, i.name AS name, rl.qty, rl.unit, rl.location, rl.batch_id, rl.expiry
#             FROM receipt_lines rl
#             LEFT JOIN items i ON i.sku = rl.sku
#             WHERE rl.receipt_id = ?
#         """, (tx['id'],))
#         lines = [dict(r) for r in cur.fetchall()]
#     finally:
#         conn.close()

#     return render_template('grn_print.html', tx=dict(tx), lines=lines)

# =====================================================================
# RECEIVE / GRN MODULE
# Paste this block into app.py, replacing EVERY existing definition of:
#   - /receive              (GET)
#   - /api/receive          (POST)
#   - /api/receive/confirm  (DELETE if present — this endpoint must not exist)
#   - /receive/<id>/pdf     (GET)
#   - /receipt/<grn_no>     (GET)
#   - generate_pdf_for_receipt
#   - generate_grn_no / generate_order_ref / next_sequence
#
# Assumes these already exist earlier in app.py: app, get_conn(), session,
# generate_csrf, and the 'sequences' table in your schema. If 'sequences'
# doesn't exist yet, run this once:
#
#   CREATE TABLE IF NOT EXISTS sequences (
#     name TEXT PRIMARY KEY,
#     last INTEGER NOT NULL DEFAULT 0
#   );
# =====================================================================

# =====================================================================
# RECEIVE / GRN MODULE
# Paste this block into app.py, replacing EVERY existing definition of:
#   - /receive              (GET)
#   - /api/receive          (POST)
#   - /receive/<id>/pdf     (GET)
#   - /receipt/<grn_no>     (GET)
#   - generate_pdf_for_receipt
#   - generate_grn_no / generate_order_ref / next_sequence
#
# Assumes app, get_conn(), session, generate_csrf already exist earlier
# in app.py, and that a 'sequences' table exists:
#
#   CREATE TABLE IF NOT EXISTS sequences (
#     name TEXT PRIMARY KEY,
#     last INTEGER NOT NULL DEFAULT 0
#   );
#
# IMPORTANT ONE-LINE FIX to your existing get_conn():
# add a busy_timeout so concurrent requests from different users WAIT
# briefly for a lock instead of failing instantly:
#
#   def get_conn():
#       conn = sqlite3.connect(DB_PATH, ...)
#       conn.row_factory = sqlite3.Row
#       conn.execute("PRAGMA foreign_keys = ON;")
#       conn.execute("PRAGMA busy_timeout = 5000;")   # <-- add this line
#       return conn
# =====================================================================
import os
import time
import sqlite3
from datetime import datetime, timezone

from flask import render_template, request, jsonify, session, abort, send_file
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

OUT_DIR = os.path.join(os.getcwd(), 'generated_docs')
os.makedirs(OUT_DIR, exist_ok=True)


# ---------------------------------------------------------------------
# Sequence-based GRN / Order Ref generators.
#
# Each call opens its OWN short-lived connection, commits, and closes —
# it must never be called while another connection in the same request
# still has an uncommitted write open, or SQLite's single-writer lock
# will deadlock the two connections against each other and raise
# "database is locked". That's why api_receive() below calls these
# BEFORE opening its own write connection.
# ---------------------------------------------------------------------
def next_sequence(name, retries=3):
    last_err = None
    for attempt in range(retries):
        conn = get_conn()
        try:
            cur = conn.cursor()
            cur.execute("BEGIN IMMEDIATE")
            cur.execute("SELECT last FROM sequences WHERE name = ?", (name,))
            row = cur.fetchone()
            if row is None:
                val = 1
                cur.execute("INSERT INTO sequences (name, last) VALUES (?, ?)", (name, val))
            else:
                val = int(row['last']) + 1
                cur.execute("UPDATE sequences SET last = ? WHERE name = ?", (val, name))
            conn.commit()
            return val
        except sqlite3.OperationalError as e:
            last_err = e
            try:
                conn.rollback()
            except Exception:
                pass
            time.sleep(0.15 * (attempt + 1))  # brief backoff, then retry
        finally:
            conn.close()
    raise last_err


def generate_grn_no():
    seq = next_sequence('grn')
    return f"GRN-{datetime.now(timezone.utc).strftime('%Y%m%d')}-{seq:04d}"


def generate_order_ref():
    seq = next_sequence('order_ref')
    return f"ORD-{datetime.now(timezone.utc).strftime('%Y%m%d')}-{seq:04d}"


# ---------------------------------------------------------------------
# PDF generator — ONE definition only, 7 positional args
# ---------------------------------------------------------------------
def generate_pdf_for_receipt(grn_no, order_ref, supplier, supplier_email,
                              condition, condition_notes, lines):
    out_path = os.path.join(OUT_DIR, f"{grn_no}.pdf")
    c = canvas.Canvas(out_path, pagesize=A4)

    c.setFont("Helvetica-Bold", 14)
    c.drawString(40, 800, f"Goods Received Note {grn_no}")

    c.setFont("Helvetica", 10)
    c.drawString(40, 782, f"Order Ref: {order_ref or ''}")
    c.drawString(300, 782, f"Date: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    c.drawString(40, 767, f"Supplier: {supplier or ''}")
    c.drawString(40, 752, f"Supplier Email: {supplier_email or ''}")
    c.drawString(40, 737, f"Condition: {condition or 'good'}")
    if condition_notes:
        c.drawString(40, 722, f"Notes: {condition_notes[:90]}")

    y = 695
    c.setFont("Helvetica-Bold", 10)
    c.drawString(40, y, "SKU")
    c.drawString(200, y, "Qty")
    c.drawString(250, y, "Unit")
    c.drawString(310, y, "Batch")
    c.drawString(410, y, "Location")
    c.setFont("Helvetica", 10)
    y -= 16

    for ln in lines:
        c.drawString(40, y, str(ln.get('sku', '')))
        c.drawString(200, y, str(ln.get('qty', '')))
        c.drawString(250, y, str(ln.get('unit', '') or ''))
        c.drawString(310, y, str(ln.get('batch_id', '') or ''))
        c.drawString(410, y, str(ln.get('location', '') or ''))
        y -= 14
        if y < 60:
            c.showPage()
            y = 800

    c.showPage()
    c.save()
    return out_path


# ---------------------------------------------------------------------
# GET /receive — load the form
# ---------------------------------------------------------------------
@app.route('/receive', methods=['GET'])
def receive():
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT sku, name, unit FROM items ORDER BY name ASC")
        items_list = [dict(r) for r in cur.fetchall()]

        cur.execute("SELECT name FROM locations ORDER BY name ASC")
        locations_list = [r['name'] for r in cur.fetchall()]
    except Exception:
        app.logger.exception("Failed to load receive page data")
        items_list, locations_list = [], []
    finally:
        conn.close()

    return render_template(
        'receive.html', items=items_list, locations=locations_list,
        user=session.get('username'), receipt={'id': ''}, csrf_token=generate_csrf()
    )


# ---------------------------------------------------------------------
# POST /api/receive — confirm the GRN: save lines, update stock,
# generate the PDF, email the supplier if condition is good.
# ---------------------------------------------------------------------
@app.route('/api/receive', methods=['POST'])
def api_receive():
    data = request.get_json(silent=True) or {}

    supplier = (data.get('supplier') or '').strip()
    supplier_email = (data.get('supplier_email') or '').strip()
    po_ref = (data.get('po_ref') or '').strip()
    condition = (data.get('condition') or 'good').strip().lower()
    condition_notes = (data.get('condition_notes') or '').strip()
    confirmed_by = (data.get('confirmed_by') or session.get('username') or 'web').strip()
    lines = data.get('lines') or []

    if not supplier:
        return jsonify({'status': 'error', 'message': 'Supplier is required'}), 400
    if not supplier_email:
        return jsonify({'status': 'error', 'message': 'Supplier email is required'}), 400
    if not lines or not any(l.get('sku') and float(l.get('qty', 0) or 0) > 0 for l in lines):
        return jsonify({'status': 'error', 'message': 'At least one valid line is required'}), 400

    # ---- Generate identifiers FIRST, on their own short-lived connection,
    # before opening the main write connection below. This is the fix for
    # "database is locked" — it guarantees only one write transaction is
    # ever open at a time within this request. ----
    try:
        grn_no = generate_grn_no()
        order_ref = po_ref or generate_order_ref()
    except sqlite3.OperationalError:
        app.logger.exception("Could not allocate GRN/order sequence")
        return jsonify({'status': 'error', 'message': 'Database was busy, please try again.'}), 503

    conn = get_conn()
    try:
        cur = conn.cursor()

        receipt_no = f"RCPT-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
        cur.execute("INSERT INTO receipts (receipt_no, created_at) VALUES (?, datetime('now'))", (receipt_no,))
        receipt_id = cur.lastrowid

        clean_lines = []
        for ln in lines:
            sku = (ln.get('sku') or '').strip()
            try:
                qty = float(ln.get('qty') or 0)
            except (TypeError, ValueError):
                qty = 0
            if not sku or qty <= 0:
                continue

            unit = (ln.get('unit') or '').strip() or None
            location = (ln.get('location') or '').strip() or None
            batch_id = (ln.get('batch') or ln.get('batch_id') or '').strip() or None
            expiry = (ln.get('expiry') or '').strip() or None

            cur.execute("""
                INSERT INTO receipt_lines (receipt_id, sku, qty, unit, location, batch_id, expiry)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (receipt_id, sku, qty, unit, location, batch_id, expiry))

            cur.execute("UPDATE items SET on_hand = COALESCE(on_hand,0) + ?, updated_at = datetime('now') WHERE sku = ?", (qty, sku))
            if cur.rowcount == 0:
                cur.execute("""
                    INSERT INTO items (sku, name, unit, category, on_hand, reorder_level, created_at, updated_at)
                    VALUES (?, ?, ?, '', ?, NULL, datetime('now'), datetime('now'))
                """, (sku, sku, unit or 'pcs', qty))

            cur.execute("""
                INSERT INTO stock_ledger (sku, item_sku, qty, txn_type, reference, batch_no, created_by, when_ts, notes)
                VALUES (?, ?, ?, 'RECEIVE', ?, ?, ?, datetime('now'), ?)
            """, (sku, sku, qty, grn_no, batch_id, confirmed_by, f"GRN {grn_no}"))

            clean_lines.append({'sku': sku, 'qty': qty, 'unit': unit, 'location': location, 'batch_id': batch_id})

        cur.execute("""
            UPDATE receipts
            SET grn_no = ?, order_ref = ?, supplier = ?, supplier_email = ?, po_no = ?,
                condition_status = ?, condition_notes = ?, received_by = ?,
                confirmed = 1, confirmed_at = datetime('now')
            WHERE id = ?
        """, (grn_no, order_ref, supplier, supplier_email, po_ref,
              condition, condition_notes, confirmed_by, receipt_id))
        conn.commit()

    except Exception:
        conn.rollback()
        app.logger.exception("Failed to process GRN")
        return jsonify({'status': 'error', 'message': 'Failed to process GRN'}), 500
    finally:
        conn.close()

    # ---- PDF generation and email happen AFTER the write connection above
    # has been committed and closed, so they can't contend with it either. ----
    pdf_path = None
    try:
        pdf_path = generate_pdf_for_receipt(
            grn_no, order_ref, supplier, supplier_email, condition, condition_notes, clean_lines
        )
        conn2 = get_conn()
        try:
            conn2.execute("UPDATE receipts SET document_path = ? WHERE id = ?", (pdf_path, receipt_id))
            conn2.commit()
        finally:
            conn2.close()
    except Exception:
        app.logger.exception("PDF generation failed for receipt %s", receipt_id)

    emailed = False
    if pdf_path and condition == 'good':
        try:
            send_receipt_email(supplier_email, grn_no, order_ref, pdf_path, receipt_id)
            emailed = True
        except NameError:
            app.logger.warning("send_receipt_email() not defined — skipping email step")

    return jsonify({
        'status': 'ok',
        'receipt_id': receipt_id,
        'grn_no': grn_no,
        'order_ref': order_ref,
        'pdf_generated': bool(pdf_path),
        'email_queued': emailed,
    }), 200


# ---------------------------------------------------------------------
# GET /receive/<id>/pdf — download the stored PDF
# ---------------------------------------------------------------------
@app.route('/receive/<int:receipt_id>/pdf')
def download_receipt_pdf(receipt_id):
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT document_path, grn_no FROM receipts WHERE id = ?", (receipt_id,))
        row = cur.fetchone()
    finally:
        conn.close()

    if not row or not row['document_path'] or not os.path.exists(row['document_path']):
        abort(404)

    return send_file(row['document_path'], as_attachment=False,
                      download_name=f"{row['grn_no'] or receipt_id}.pdf")


# ---------------------------------------------------------------------
# GET /receipt/<grn_no> — printable HTML view
# ---------------------------------------------------------------------
@app.route('/receipt/<string:grn_no>')
def print_receipt(grn_no):
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM receipts WHERE grn_no = ?", (grn_no,))
        tx = cur.fetchone()
        if not tx:
            abort(404)
        cur.execute("""
            SELECT rl.sku, i.name AS name, rl.qty, rl.unit, rl.location, rl.batch_id, rl.expiry
            FROM receipt_lines rl
            LEFT JOIN items i ON i.sku = rl.sku
            WHERE rl.receipt_id = ?
        """, (tx['id'],))
        lines = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()

    return render_template('grn_print.html', tx=dict(tx), lines=lines)



# import os
# from datetime import datetime, timezone

# from flask import render_template, request, jsonify, session, abort, send_file
# from reportlab.lib.pagesizes import A4
# from reportlab.pdfgen import canvas

# OUT_DIR = os.path.join(os.getcwd(), 'generated_docs')
# os.makedirs(OUT_DIR, exist_ok=True)


# # ---------------------------------------------------------------------
# # Sequence-based GRN / Order Ref generators (no duplicates elsewhere!)
# # ---------------------------------------------------------------------
# def next_sequence(name):
#     conn = get_conn()
#     try:
#         cur = conn.cursor()
#         cur.execute("BEGIN IMMEDIATE")
#         cur.execute("SELECT last FROM sequences WHERE name = ?", (name,))
#         row = cur.fetchone()
#         if row is None:
#             val = 1
#             cur.execute("INSERT INTO sequences (name, last) VALUES (?, ?)", (name, val))
#         else:
#             val = int(row['last']) + 1
#             cur.execute("UPDATE sequences SET last = ? WHERE name = ?", (val, name))
#         conn.commit()
#         return val
#     finally:
#         conn.close()


# def generate_grn_no():
#     seq = next_sequence('grn')
#     return f"GRN-{datetime.now(timezone.utc).strftime('%Y%m%d')}-{seq:04d}"


# def generate_order_ref():
#     seq = next_sequence('order_ref')
#     return f"ORD-{datetime.now(timezone.utc).strftime('%Y%m%d')}-{seq:04d}"


# # ---------------------------------------------------------------------
# # PDF generator — ONE definition only, 7 positional args
# # ---------------------------------------------------------------------
# def generate_pdf_for_receipt(grn_no, order_ref, supplier, supplier_email,
#                               condition, condition_notes, lines):
#     out_path = os.path.join(OUT_DIR, f"{grn_no}.pdf")
#     c = canvas.Canvas(out_path, pagesize=A4)

#     c.setFont("Helvetica-Bold", 14)
#     c.drawString(40, 800, f"Goods Received Note {grn_no}")

#     c.setFont("Helvetica", 10)
#     c.drawString(40, 782, f"Order Ref: {order_ref or ''}")
#     c.drawString(300, 782, f"Date: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
#     c.drawString(40, 767, f"Supplier: {supplier or ''}")
#     c.drawString(40, 752, f"Supplier Email: {supplier_email or ''}")
#     c.drawString(40, 737, f"Condition: {condition or 'good'}")
#     if condition_notes:
#         c.drawString(40, 722, f"Notes: {condition_notes[:90]}")

#     y = 695
#     c.setFont("Helvetica-Bold", 10)
#     c.drawString(40, y, "SKU")
#     c.drawString(200, y, "Qty")
#     c.drawString(250, y, "Unit")
#     c.drawString(310, y, "Batch")
#     c.drawString(410, y, "Location")
#     c.setFont("Helvetica", 10)
#     y -= 16

#     for ln in lines:
#         c.drawString(40, y, str(ln.get('sku', '')))
#         c.drawString(200, y, str(ln.get('qty', '')))
#         c.drawString(250, y, str(ln.get('unit', '') or ''))
#         c.drawString(310, y, str(ln.get('batch_id', '') or ''))
#         c.drawString(410, y, str(ln.get('location', '') or ''))
#         y -= 14
#         if y < 60:
#             c.showPage()
#             y = 800

#     c.showPage()
#     c.save()
#     return out_path


# # ---------------------------------------------------------------------
# # GET /receive — load the form
# # ---------------------------------------------------------------------
# @app.route('/receive', methods=['GET'])
# def receive():
#     conn = get_conn()
#     try:
#         cur = conn.cursor()
#         cur.execute("SELECT sku, name, unit FROM items ORDER BY name ASC")
#         items_list = [dict(r) for r in cur.fetchall()]

#         cur.execute("SELECT name FROM locations ORDER BY name ASC")
#         locations_list = [r['name'] for r in cur.fetchall()]
#     except Exception:
#         app.logger.exception("Failed to load receive page data")
#         items_list, locations_list = [], []
#     finally:
#         conn.close()

#     return render_template(
#         'receive.html', items=items_list, locations=locations_list,
#         user=session.get('username'), receipt={'id': ''}, csrf_token=generate_csrf()
#     )


# # ---------------------------------------------------------------------
# # POST /api/receive — confirm the GRN: save lines, update stock,
# # generate the PDF, email the supplier if condition is good.
# # ---------------------------------------------------------------------
# @app.route('/api/receive', methods=['POST'])
# def api_receive():
#     data = request.get_json(silent=True) or {}

#     supplier = (data.get('supplier') or '').strip()
#     supplier_email = (data.get('supplier_email') or '').strip()
#     po_ref = (data.get('po_ref') or '').strip()
#     condition = (data.get('condition') or 'good').strip().lower()
#     condition_notes = (data.get('condition_notes') or '').strip()
#     confirmed_by = (data.get('confirmed_by') or session.get('username') or 'web').strip()
#     lines = data.get('lines') or []

#     if not supplier:
#         return jsonify({'status': 'error', 'message': 'Supplier is required'}), 400
#     if not supplier_email:
#         return jsonify({'status': 'error', 'message': 'Supplier email is required'}), 400
#     if not lines or not any(l.get('sku') and float(l.get('qty', 0) or 0) > 0 for l in lines):
#         return jsonify({'status': 'error', 'message': 'At least one valid line is required'}), 400

#     conn = get_conn()
#     try:
#         cur = conn.cursor()

#         receipt_no = f"RCPT-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
#         cur.execute("INSERT INTO receipts (receipt_no, created_at) VALUES (?, datetime('now'))", (receipt_no,))
#         receipt_id = cur.lastrowid

#         grn_no = generate_grn_no()
#         order_ref = po_ref or generate_order_ref()

#         clean_lines = []
#         for ln in lines:
#             sku = (ln.get('sku') or '').strip()
#             try:
#                 qty = float(ln.get('qty') or 0)
#             except (TypeError, ValueError):
#                 qty = 0
#             if not sku or qty <= 0:
#                 continue

#             unit = (ln.get('unit') or '').strip() or None
#             location = (ln.get('location') or '').strip() or None
#             batch_id = (ln.get('batch') or ln.get('batch_id') or '').strip() or None
#             expiry = (ln.get('expiry') or '').strip() or None

#             cur.execute("""
#                 INSERT INTO receipt_lines (receipt_id, sku, qty, unit, location, batch_id, expiry)
#                 VALUES (?, ?, ?, ?, ?, ?, ?)
#             """, (receipt_id, sku, qty, unit, location, batch_id, expiry))

#             cur.execute("UPDATE items SET on_hand = COALESCE(on_hand,0) + ?, updated_at = datetime('now') WHERE sku = ?", (qty, sku))
#             if cur.rowcount == 0:
#                 cur.execute("""
#                     INSERT INTO items (sku, name, unit, category, on_hand, reorder_level, created_at, updated_at)
#                     VALUES (?, ?, ?, '', ?, NULL, datetime('now'), datetime('now'))
#                 """, (sku, sku, unit or 'pcs', qty))

#             cur.execute("""
#                 INSERT INTO stock_ledger (sku, item_sku, qty, txn_type, reference, batch_no, created_by, when_ts, notes)
#                 VALUES (?, ?, ?, 'RECEIVE', ?, ?, ?, datetime('now'), ?)
#             """, (sku, sku, qty, grn_no, batch_id, confirmed_by, f"GRN {grn_no}"))

#             clean_lines.append({'sku': sku, 'qty': qty, 'unit': unit, 'location': location, 'batch_id': batch_id})

#         cur.execute("""
#             UPDATE receipts
#             SET grn_no = ?, order_ref = ?, supplier = ?, supplier_email = ?, po_no = ?,
#                 condition_status = ?, condition_notes = ?, received_by = ?,
#                 confirmed = 1, confirmed_at = datetime('now')
#             WHERE id = ?
#         """, (grn_no, order_ref, supplier, supplier_email, po_ref,
#               condition, condition_notes, confirmed_by, receipt_id))
#         conn.commit()

#         pdf_path = None
#         try:
#             pdf_path = generate_pdf_for_receipt(
#                 grn_no, order_ref, supplier, supplier_email, condition, condition_notes, clean_lines
#             )
#             cur.execute("UPDATE receipts SET document_path = ? WHERE id = ?", (pdf_path, receipt_id))
#             conn.commit()
#         except Exception:
#             app.logger.exception("PDF generation failed for receipt %s", receipt_id)

#         emailed = False
#         if pdf_path and condition == 'good':
#             try:
#                 send_receipt_email(supplier_email, grn_no, order_ref, pdf_path, receipt_id)
#                 emailed = True
#             except NameError:
#                 app.logger.warning("send_receipt_email() not defined — skipping email step")

#         return jsonify({
#             'status': 'ok',
#             'receipt_id': receipt_id,
#             'grn_no': grn_no,
#             'order_ref': order_ref,
#             'pdf_generated': bool(pdf_path),
#             'email_queued': emailed,
#         }), 200

#     except Exception:
#         conn.rollback()
#         app.logger.exception("Failed to process GRN")
#         return jsonify({'status': 'error', 'message': 'Failed to process GRN'}), 500
#     finally:
#         conn.close()


# # ---------------------------------------------------------------------
# # GET /receive/<id>/pdf — download the stored PDF
# # ---------------------------------------------------------------------
# @app.route('/receive/<int:receipt_id>/pdf')
# def download_receipt_pdf(receipt_id):
#     conn = get_conn()
#     try:
#         cur = conn.cursor()
#         cur.execute("SELECT document_path, grn_no FROM receipts WHERE id = ?", (receipt_id,))
#         row = cur.fetchone()
#     finally:
#         conn.close()

#     if not row or not row['document_path'] or not os.path.exists(row['document_path']):
#         abort(404)

#     return send_file(row['document_path'], as_attachment=False,
#                       download_name=f"{row['grn_no'] or receipt_id}.pdf")


# # ---------------------------------------------------------------------
# # GET /receipt/<grn_no> — printable HTML view
# # ---------------------------------------------------------------------
# @app.route('/receipt/<string:grn_no>')
# def print_receipt(grn_no):
#     conn = get_conn()
#     try:
#         cur = conn.cursor()
#         cur.execute("SELECT * FROM receipts WHERE grn_no = ?", (grn_no,))
#         tx = cur.fetchone()
#         if not tx:
#             abort(404)
#         cur.execute("""
#             SELECT rl.sku, i.name AS name, rl.qty, rl.unit, rl.location, rl.batch_id, rl.expiry
#             FROM receipt_lines rl
#             LEFT JOIN items i ON i.sku = rl.sku
#             WHERE rl.receipt_id = ?
#         """, (tx['id'],))
#         lines = [dict(r) for r in cur.fetchall()]
#     finally:
#         conn.close()

#     return render_template('grn_print.html', tx=dict(tx), lines=lines)
# -------------------------------------------------------------------
# Drivers & Vehicles (used to populate the issue/delivery dropdowns)
# -------------------------------------------------------------------
@app.route('/api/drivers', methods=['GET'])
def api_list_drivers():
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, name, phone, license_no FROM drivers ORDER BY name ASC")
        return jsonify({'status': 'ok', 'drivers': [dict(r) for r in cur.fetchall()]})
    finally:
        conn.close()


@app.route('/api/drivers', methods=['POST'])
def api_create_driver():
    data = request.get_json(silent=True) or {}
    name = (data.get('name') or '').strip()
    phone = (data.get('phone') or '').strip()
    license_no = (data.get('license_no') or '').strip()
    if not name:
        return jsonify({'status': 'error', 'message': 'Driver name is required'}), 400

    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("INSERT INTO drivers (name, phone, license_no, created_at) VALUES (?, ?, ?, datetime('now'))",
                    (name, phone, license_no))
        conn.commit()
        return jsonify({'status': 'ok', 'driver': {'id': cur.lastrowid, 'name': name, 'phone': phone, 'license_no': license_no}}), 201
    except Exception:
        conn.rollback()
        app.logger.exception("Failed to create driver")
        return jsonify({'status': 'error', 'message': 'Failed to create driver'}), 500
    finally:
        conn.close()


@app.route('/api/vehicles', methods=['GET'])
def api_list_vehicles():
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, reg_no, make_model, capacity FROM vehicles ORDER BY reg_no ASC")
        return jsonify({'status': 'ok', 'vehicles': [dict(r) for r in cur.fetchall()]})
    finally:
        conn.close()


@app.route('/api/vehicles', methods=['POST'])
def api_create_vehicle():
    data = request.get_json(silent=True) or {}
    reg_no = (data.get('reg_no') or '').strip()
    make_model = (data.get('make_model') or '').strip()
    capacity = (data.get('capacity') or '').strip()
    if not reg_no:
        return jsonify({'status': 'error', 'message': 'Vehicle registration number is required'}), 400

    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(1) AS cnt FROM vehicles WHERE reg_no = ?", (reg_no,))
        if cur.fetchone()['cnt'] > 0:
            return jsonify({'status': 'error', 'message': 'Vehicle already registered'}), 400

        cur.execute("INSERT INTO vehicles (reg_no, make_model, capacity, created_at) VALUES (?, ?, ?, datetime('now'))",
                    (reg_no, make_model, capacity))
        conn.commit()
        return jsonify({'status': 'ok', 'vehicle': {'id': cur.lastrowid, 'reg_no': reg_no, 'make_model': make_model, 'capacity': capacity}}), 201
    except Exception:
        conn.rollback()
        app.logger.exception("Failed to create vehicle")
        return jsonify({'status': 'error', 'message': 'Failed to create vehicle'}), 500
    finally:
        conn.close()


# -------------------------------------------------------------------
# Issue goods / Delivery Note — auto delivery/order no + PDF + optional email
# -------------------------------------------------------------------
@app.route('/issue')
def issue():
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT sku, name, unit FROM items ORDER BY name ASC")
        items_list = [dict(r) for r in cur.fetchall()]
        cur.execute("SELECT name FROM locations ORDER BY name ASC")
        locations_list = [r['name'] for r in cur.fetchall()]
        cur.execute("SELECT id, name, phone, license_no FROM drivers ORDER BY name ASC")
        drivers_list = [dict(r) for r in cur.fetchall()]
        cur.execute("SELECT id, reg_no, make_model, capacity FROM vehicles ORDER BY reg_no ASC")
        vehicles_list = [dict(r) for r in cur.fetchall()]
    except Exception:
        app.logger.exception("Failed to load issue page data")
        items_list, locations_list, drivers_list, vehicles_list = [], [], [], []
    finally:
        conn.close()

    return render_template(
        'issue.html', items=items_list, locations=locations_list,
        drivers=drivers_list, vehicles=vehicles_list,
        user=session.get('username'), csrf_token=generate_csrf()
    )


# @app.route('/api/issue', methods=['POST'])
# def api_issue():
#     """
#     Confirms a delivery in one shot:
#       1. auto-generates delivery_no / order_ref
#       2. resolves the chosen driver/vehicle (or accepts free text)
#       3. saves the picked lines, decrements item on_hand, writes stock_ledger entries
#       4. generates the delivery note PDF (stored for later printing/download)
#       5. emails the PDF to the recipient if an email address was given
#     """
#     data = request.get_json(silent=True) or {}

#     recipient = (data.get('recipient') or '').strip()
#     recipient_phone = (data.get('recipient_phone') or '').strip()
#     recipient_email = (data.get('recipient_email') or '').strip()
#     address = (data.get('address') or '').strip()
#     remarks = (data.get('remarks') or '').strip()
#     order_ref_in = (data.get('order_ref') or '').strip()
#     driver_id = data.get('driver_id') or None
#     vehicle_id = data.get('vehicle_id') or None
#     # free-text fallbacks, used if no driver_id/vehicle_id was selected
#     driver_name = (data.get('driver_name') or '').strip()
#     driver_phone = (data.get('driver_phone') or '').strip()
#     vehicle_reg = (data.get('vehicle_reg') or '').strip()
#     lines = data.get('lines') or []

#     if not recipient:
#         return jsonify({'status': 'error', 'message': 'Recipient name is required'}), 400
#     if not lines or not any(l.get('sku') and float(l.get('qty', 0) or 0) > 0 for l in lines):
#         return jsonify({'status': 'error', 'message': 'At least one valid line is required'}), 400

#     conn = get_conn()
#     try:
#         cur = conn.cursor()

#         # resolve driver
#         if driver_id:
#             cur.execute("SELECT name, phone FROM drivers WHERE id = ?", (driver_id,))
#             d = cur.fetchone()
#             if d:
#                 driver_name, driver_phone = d['name'], d['phone']

#         # resolve vehicle
#         if vehicle_id:
#             cur.execute("SELECT reg_no FROM vehicles WHERE id = ?", (vehicle_id,))
#             v = cur.fetchone()
#             if v:
#                 vehicle_reg = v['reg_no']

#         delivery_no = generate_delivery_no()
#         order_ref = order_ref_in or generate_order_ref()

#         cur.execute("""
#             INSERT INTO deliveries (
#                 delivery_no, order_ref, recipient, recipient_phone, recipient_email,
#                 address, vehicle_id, vehicle_reg, driver_id, driver_name, driver_phone,
#                 remarks, created_by, confirmed, confirmed_at, created_at, updated_at
#             ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, datetime('now'), datetime('now'), datetime('now'))
#         """, (delivery_no, order_ref, recipient, recipient_phone, recipient_email,
#               address, vehicle_id, vehicle_reg, driver_id, driver_name, driver_phone,
#               remarks, session.get('username', 'web')))
#         delivery_id = cur.lastrowid

#         clean_lines = []
#         for ln in lines:
#             sku = (ln.get('sku') or '').strip()
#             try:
#                 qty = float(ln.get('qty') or 0)
#             except (TypeError, ValueError):
#                 qty = 0
#             if not sku or qty <= 0:
#                 continue
#             location = (ln.get('location') or '').strip() or None
#             batch_id = (ln.get('batch_id') or '').strip() or None

#             cur.execute("""
#                 INSERT INTO delivery_lines (delivery_id, sku, qty, location, batch_id)
#                 VALUES (?, ?, ?, ?, ?)
#             """, (delivery_id, sku, qty, location, batch_id))

#             cur.execute("""
#                 INSERT INTO stock_ledger (sku, item_sku, qty, txn_type, reference, batch_no, created_by, when_ts, notes)
#                 VALUES (?, ?, ?, 'ISSUE', ?, ?, ?, datetime('now'), ?)
#             """, (sku, sku, -abs(qty), delivery_no, batch_id, session.get('username', 'web'), f"Delivery {delivery_no}"))
#             cur.execute("UPDATE items SET on_hand = COALESCE(on_hand,0) - ?, updated_at = datetime('now') WHERE sku = ?", (abs(qty), sku))

#             clean_lines.append({'sku': sku, 'qty': qty, 'location': location, 'batch_id': batch_id})

#         conn.commit()

#         # generate the delivery note PDF
#         pdf_path = None
#         try:
#             pdf_path = generate_pdf_for_delivery(
#                 delivery_no, order_ref, recipient, recipient_phone, recipient_email,
#                 address, driver_name, driver_phone, vehicle_reg, remarks, clean_lines
#             )
#             cur.execute("UPDATE deliveries SET document_path = ? WHERE id = ?", (pdf_path, delivery_id))
#             conn.commit()
#         except Exception:
#             app.logger.exception("PDF generation failed for delivery %s", delivery_id)

#         emailed = False
#         if pdf_path and recipient_email:
#             send_delivery_email(recipient_email, delivery_no, order_ref, pdf_path, delivery_id)
#             emailed = True

#         return jsonify({
#             'status': 'ok',
#             'delivery_id': delivery_id,
#             'delivery_no': delivery_no,
#             'order_ref': order_ref,
#             'pdf_generated': bool(pdf_path),
#             'email_queued': emailed,
#         })
#     except Exception:
#         conn.rollback()
#         app.logger.exception("Failed to create issue")
#         return jsonify({'status': 'error', 'message': 'Failed to issue goods'}), 500
#     finally:
#         conn.close()
@app.route('/api/issue', methods=['POST'])
def api_issue():
    """
    Create a delivery (issue) and persist lines using schema column names:
    delivery_lines.item_sku, location_id, batch_no, expiry, line_notes.
    """
    data = request.get_json(silent=True) or {}

    recipient = (data.get('recipient') or '').strip()
    recipient_phone = (data.get('recipient_phone') or '').strip()
    recipient_email = (data.get('recipient_email') or '').strip()
    address = (data.get('address') or '').strip()
    remarks = (data.get('remarks') or '').strip()
    order_ref_in = (data.get('order_ref') or '').strip()
    driver_id = data.get('driver_id') or None
    vehicle_id = data.get('vehicle_id') or None
    driver_name = (data.get('driver_name') or '').strip()
    driver_phone = (data.get('driver_phone') or '').strip()
    vehicle_reg = (data.get('vehicle_reg') or '').strip()
    lines = data.get('lines') or []

    if not recipient:
        return jsonify({'status': 'error', 'message': 'Recipient name is required'}), 400

    # Normalize and validate lines first (ensure item_sku present)
    normalized_lines = []
    for ln in lines:
        if not isinstance(ln, dict):
            continue
        item_sku = (ln.get('item_sku') or ln.get('sku') or '').strip()
        try:
            qty = float(ln.get('qty') or 0)
        except (TypeError, ValueError):
            qty = 0
        if not item_sku or qty <= 0:
            app.logger.debug("Skipping invalid issue line (missing sku or qty): %s", ln)
            continue
        unit = (ln.get('unit') or '').strip() or 'pcs'

        # prefer numeric location_id; accept location or location_id
        location_id = None
        if ln.get('location_id') is not None:
            location_id = ln.get('location_id')
        elif ln.get('location') is not None:
            try:
                location_id = int(ln.get('location'))
            except Exception:
                location_id = None

        # use batch_no column (DB uses batch_no)
        batch_no = (ln.get('batch') or ln.get('batch_id') or ln.get('batch_no') or '').strip() or None
        expiry = (ln.get('expiry') or '').strip() or None
        line_notes = (ln.get('line_notes') or ln.get('notes') or '').strip() or None

        normalized_lines.append({
            'item_sku': item_sku,
            'qty': qty,
            'unit': unit,
            'location_id': location_id,
            'batch_no': batch_no,
            'expiry': expiry,
            'line_notes': line_notes
        })

    if not normalized_lines:
        return jsonify({'status': 'error', 'message': 'At least one valid line is required.'}), 400

    conn = get_conn()
    try:
        cur = conn.cursor()

        # resolve driver
        if driver_id:
            cur.execute("SELECT name, phone FROM drivers WHERE id = ?", (driver_id,))
            d = cur.fetchone()
            if d:
                driver_name, driver_phone = d['name'], d['phone']

        # resolve vehicle
        if vehicle_id:
            cur.execute("SELECT reg_no FROM vehicles WHERE id = ?", (vehicle_id,))
            v = cur.fetchone()
            if v:
                vehicle_reg = v['reg_no']

        delivery_no = generate_delivery_no()
        order_ref = order_ref_in or generate_order_ref()

        cur.execute("""
            INSERT INTO deliveries (
                delivery_no, order_ref, recipient, recipient_phone, recipient_email,
                address, vehicle_id, vehicle_reg, driver_id, driver_name, driver_phone,
                remarks, created_by, confirmed, confirmed_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, datetime('now'), datetime('now'), datetime('now'))
        """, (delivery_no, order_ref, recipient, recipient_phone, recipient_email,
              address, vehicle_id, vehicle_reg, driver_id, driver_name, driver_phone,
              remarks, session.get('username', 'web')))
        delivery_id = cur.lastrowid

        # Insert normalized lines using schema column names
        for ln in normalized_lines:
            item_sku = ln['item_sku']
            qty = ln['qty']
            unit = ln['unit']
            location_id = ln['location_id']
            batch_no = ln['batch_no']
            expiry = ln['expiry']
            line_notes = ln['line_notes']

            cur.execute("""
                INSERT INTO delivery_lines
                    (delivery_id, item_sku, qty, unit, location_id, batch_no, expiry, line_notes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (delivery_id, item_sku, qty, unit, location_id, batch_no, expiry, line_notes))

            # ledger: record negative qty for issue
            cur.execute("""
                INSERT INTO stock_ledger (sku, item_sku, qty, txn_type, reference, batch_no, created_by, when_ts, notes)
                VALUES (?, ?, ?, 'ISSUE', ?, ?, ?, datetime('now'), ?)
            """, (item_sku, item_sku, -abs(qty), delivery_no, batch_no, session.get('username', 'web'), f"Delivery {delivery_no}"))

            # decrement on_hand; if item missing, create placeholder with negative on_hand
            cur.execute("UPDATE items SET on_hand = COALESCE(on_hand,0) - ?, updated_at = datetime('now') WHERE sku = ?", (abs(qty), item_sku))
            if cur.rowcount == 0:
                cur.execute("""
                    INSERT INTO items (sku, name, unit, category, on_hand, reorder_level, created_at, updated_at)
                    VALUES (?, ?, ?, '', ?, NULL, datetime('now'), datetime('now'))
                """, (item_sku, item_sku, unit, -qty))

        conn.commit()

        # generate the delivery note PDF
        pdf_path = None
        try:
            pdf_path = generate_pdf_for_delivery(
                delivery_no, order_ref, recipient, recipient_phone, recipient_email,
                address, driver_name, driver_phone, vehicle_reg, remarks, normalized_lines
            )
            if pdf_path:
                cur.execute("UPDATE deliveries SET document_path = ? WHERE id = ?", (pdf_path, delivery_id))
                conn.commit()
                app.logger.info("PDF generated at: %s", pdf_path)
        except Exception:
            app.logger.exception("PDF generation failed for delivery %s", delivery_id)

        emailed = False
        if pdf_path and recipient_email:
            try:
                send_delivery_email(recipient_email, delivery_no, order_ref, pdf_path, delivery_id)
                emailed = True
            except Exception:
                app.logger.exception("Failed to send delivery email for %s", delivery_id)

        return jsonify({
            'status': 'ok',
            'delivery_id': delivery_id,
            'delivery_no': delivery_no,
            'order_ref': order_ref,
            'pdf_generated': bool(pdf_path),
            'email_queued': emailed,
        })
    except Exception:
        conn.rollback()
        app.logger.exception("Failed to create issue")
        return jsonify({'status': 'error', 'message': 'Failed to issue goods'}), 500
    finally:
        conn.close()


@app.route('/issue/<int:delivery_id>/pdf')
def download_delivery_pdf(delivery_id):
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT document_path, delivery_no FROM deliveries WHERE id = ?", (delivery_id,))
        row = cur.fetchone()
    finally:
        conn.close()

    if not row or not row['document_path'] or not os.path.exists(row['document_path']):
        abort(404)

    from flask import send_file
    return send_file(row['document_path'], as_attachment=False,
                      download_name=f"{row['delivery_no'] or delivery_id}.pdf")

@app.route('/delivery/<string:delivery_no>')
def print_delivery_note(delivery_no):
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM deliveries WHERE delivery_no = ?", (delivery_no,))
        tx = cur.fetchone()
        if not tx:
            abort(404)
        cur.execute("""
            SELECT dl.item_sku AS sku, i.name AS name, dl.qty, dl.unit,
                   dl.batch_no AS batch_id,
                   COALESCE(l.name, l.code, '') AS location
            FROM delivery_lines dl
            LEFT JOIN items i ON i.sku = dl.item_sku
            LEFT JOIN locations l ON l.id = dl.location_id
            WHERE dl.delivery_id = ?
        """, (tx['id'],))
        lines = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()
    return render_template('delivery_note.html', tx=dict(tx), lines=lines)
# @app.route('/delivery/<string:delivery_no>')
# def print_delivery_note(delivery_no):
#     """Printable HTML view of a delivery note (opens window.print() automatically)."""
#     conn = get_conn()
#     try:
#         cur = conn.cursor()
#         cur.execute("SELECT * FROM deliveries WHERE delivery_no = ?", (delivery_no,))
#         tx = cur.fetchone()
#         if not tx:
#             abort(404)
#         cur.execute("""
#             SELECT dl.sku, i.name AS name, dl.qty, dl.location, dl.batch_id
#             FROM delivery_lines dl
#             LEFT JOIN items i ON i.sku = dl.sku
#             WHERE dl.delivery_id = ?
#         """, (tx['id'],))
#         lines = [dict(r) for r in cur.fetchall()]
#     finally:
#         conn.close()

#     return render_template('delivery_note.html', tx=dict(tx), lines=lines)


# -------------------------------------------------------------------
# Ledger
# -------------------------------------------------------------------
@app.route('/ledger')
def ledger():
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT when_ts AS created_at, txn_type AS movement_type, sku, qty, reference, created_by AS username
            FROM stock_ledger ORDER BY when_ts DESC LIMIT 200
        """)
        rows = [dict(r) for r in cur.fetchall()]
    except Exception:
        app.logger.exception("Failed to load ledger")
        rows = []
    finally:
        conn.close()
    return render_template('ledger.html', rows=rows, user=session.get('username'))


# -------------------------------------------------------------------
# Locations
# -------------------------------------------------------------------
@app.route('/locations')
def locations():
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, code, name, type, capacity, description FROM locations ORDER BY name ASC")
        rows = [dict(r) for r in cur.fetchall()]
    except Exception:
        app.logger.exception("Failed to load locations")
        rows = []
    finally:
        conn.close()
    return render_template('locations.html', locations=rows, user=session.get('username'))


@app.route('/api/locations', methods=['POST'])
def api_create_location():
    data = request.get_json(silent=True) or {}
    code = (data.get('code') or '').strip()
    name = (data.get('name') or '').strip()
    loc_type = (data.get('type') or '').strip() or 'Storage'
    try:
        capacity = int(data.get('capacity') or 0)
    except (TypeError, ValueError):
        capacity = 0
    description = (data.get('description') or '').strip()

    if not name:
        return jsonify({'status': 'error', 'message': 'Name is required'}), 400

    conn = get_conn()
    try:
        cur = conn.cursor()
        if code:
            cur.execute("SELECT COUNT(1) AS cnt FROM locations WHERE code = ?", (code,))
            if cur.fetchone()['cnt'] > 0:
                return jsonify({'status': 'error', 'message': 'Location code already exists'}), 400
        else:
            prefix = ''.join(c for c in loc_type.upper() if c.isalnum())[:4] or 'LOC'
            code = f"{prefix}-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"

        cur.execute("""
            INSERT INTO locations (code, name, type, capacity, description, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, datetime('now'), datetime('now'))
        """, (code, name, loc_type, capacity, description))
        conn.commit()

        cur.execute("SELECT id, code, name, type, capacity, description FROM locations WHERE code = ?", (code,))
        loc = dict(cur.fetchone())
        return jsonify({'status': 'ok', 'location': loc}), 201
    except Exception:
        conn.rollback()
        app.logger.exception("Failed to create location")
        return jsonify({'status': 'error', 'message': 'Failed to create location'}), 500
    finally:
        conn.close()


@app.route('/export_locations')
def export_locations():
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT code, name, type, capacity, description FROM locations ORDER BY name ASC")
        rows = cur.fetchall()

        si = io.StringIO()
        writer = csv.writer(si)
        writer.writerow(['Code', 'Name', 'Type', 'Capacity', 'Description'])
        for r in rows:
            writer.writerow([r['code'], r['name'], r['type'], r['capacity'], r['description']])

        output = make_response(si.getvalue())
        output.headers["Content-Disposition"] = "attachment; filename=locations.csv"
        output.headers["Content-type"] = "text/csv"
        return output
    except Exception:
        app.logger.exception("Failed to export locations")
        return redirect(url_for('locations'))
    finally:
        conn.close()


@app.route('/api/locations/<string:code>', methods=['PUT'])
def api_update_location(code):
    data = request.get_json(silent=True) or {}
    new_code = (data.get('code') or '').strip()
    name = (data.get('name') or '').strip()
    loc_type = (data.get('type') or '').strip() or 'Storage'
    try:
        capacity = int(data.get('capacity') or 0)
    except (TypeError, ValueError):
        capacity = 0
    description = (data.get('description') or '').strip()

    if not name:
        return jsonify({'status': 'error', 'message': 'Name is required'}), 400

    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id FROM locations WHERE code = ?", (code,))
        if not cur.fetchone():
            return jsonify({'status': 'error', 'message': 'Location not found'}), 404

        final_code = new_code or code
        if new_code and new_code != code:
            cur.execute("SELECT COUNT(1) AS cnt FROM locations WHERE code = ?", (new_code,))
            if cur.fetchone()['cnt'] > 0:
                return jsonify({'status': 'error', 'message': 'New code already exists'}), 400

        cur.execute("""
            UPDATE locations SET code = ?, name = ?, type = ?, capacity = ?, description = ?, updated_at = datetime('now')
            WHERE code = ?
        """, (final_code, name, loc_type, capacity, description, code))
        conn.commit()

        cur.execute("SELECT id, code, name, type, capacity, description FROM locations WHERE code = ?", (final_code,))
        loc = dict(cur.fetchone())
        return jsonify({'status': 'ok', 'location': loc}), 200
    except Exception:
        conn.rollback()
        app.logger.exception("Failed to update location")
        return jsonify({'status': 'error', 'message': 'Failed to update location'}), 500
    finally:
        conn.close()


@app.route('/api/locations/<string:code>', methods=['DELETE'])
def api_delete_location(code):
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id FROM locations WHERE code = ?", (code,))
        if not cur.fetchone():
            return jsonify({'status': 'error', 'message': 'Location not found'}), 404

        cur.execute("SELECT COUNT(1) AS cnt FROM stock_ledger WHERE location_id = (SELECT id FROM locations WHERE code = ?)", (code,))
        if cur.fetchone()['cnt'] > 0:
            return jsonify({'status': 'error', 'message': 'Cannot delete a location with stock movements.'}), 400

        cur.execute("DELETE FROM locations WHERE code = ?", (code,))
        conn.commit()
        return jsonify({'status': 'ok'}), 200
    except Exception:
        conn.rollback()
        app.logger.exception("Failed to delete location")
        return jsonify({'status': 'error', 'message': 'Failed to delete location'}), 500
    finally:
        conn.close()




# -------------------------------------------------------------------
# Debug / seed (debug mode only)
# -------------------------------------------------------------------
@app.route('/debug_db')
def debug_db():
    if not app.debug:
        abort(403)
    conn = get_conn()
    out = {'db_path': DB_PATH}
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) AS cnt FROM items")
        out['items_count'] = cur.fetchone()['cnt']
        cur.execute("SELECT sku, name, unit, category, on_hand, reorder_level FROM items ORDER BY sku LIMIT 20")
        out['sample_items'] = [dict(r) for r in cur.fetchall()]
    except Exception as e:
        out['error'] = str(e)
    finally:
        conn.close()
    return app.response_class(response=json.dumps(out, default=str, indent=2), mimetype='application/json')


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', 5000)), debug=True)






# @app.route('/api/receive', methods=['POST'])
# def api_receive():
#     """
#     Confirms a GRN in one shot:
#       1. auto-generates grn_no / order_ref if not already set on the receipt
#       2. saves the received lines
#       3. updates item on_hand + writes stock_ledger entries
#       4. generates the GRN PDF
#       5. emails the PDF to the supplier (async, only if condition == 'good')
#     """
#     data = request.get_json(silent=True) or {}

#     supplier = (data.get('supplier') or '').strip()
#     supplier_email = (data.get('supplier_email') or '').strip()
#     po_ref = (data.get('po_ref') or '').strip()
#     condition = (data.get('condition') or 'good').strip().lower()
#     condition_notes = (data.get('condition_notes') or '').strip()
#     confirmed_by = (data.get('confirmed_by') or session.get('username') or 'web').strip()
#     lines = data.get('lines') or []

#     if not supplier:
#         return jsonify({'status': 'error', 'message': 'Supplier is required'}), 400
#     if not supplier_email:
#         return jsonify({'status': 'error', 'message': 'Supplier email is required'}), 400
#     if not lines or not any(l.get('sku') and float(l.get('qty', 0) or 0) > 0 for l in lines):
#         return jsonify({'status': 'error', 'message': 'At least one valid line is required'}), 400

#     conn = get_conn()
#     try:
#         cur = conn.cursor()

#         # find or create the receipt header
#         receipt_id = data.get('receipt_id') or None
#         if receipt_id:
#             cur.execute("SELECT * FROM receipts WHERE id = ?", (receipt_id,))
#             hdr = cur.fetchone()
#             if not hdr:
#                 receipt_id = None
#         if not receipt_id:
#             receipt_no = f"RCPT-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
#             cur.execute("INSERT INTO receipts (receipt_no, created_at) VALUES (?, datetime('now'))", (receipt_no,))
#             receipt_id = cur.lastrowid
#             cur.execute("SELECT * FROM receipts WHERE id = ?", (receipt_id,))
#             hdr = cur.fetchone()

#         grn_no = hdr['grn_no'] or generate_grn_no()
#         order_ref = hdr['order_ref'] or po_ref or generate_order_ref()

#         # insert lines + update stock
#         clean_lines = []
#         for ln in lines:
#             sku = (ln.get('sku') or '').strip()
#             try:
#                 qty = float(ln.get('qty') or 0)
#             except (TypeError, ValueError):
#                 qty = 0
#             if not sku or qty <= 0:
#                 continue

#             unit = (ln.get('unit') or '').strip() or None
#             location = (ln.get('location') or '').strip() or None
#             batch_id = (ln.get('batch') or ln.get('batch_id') or '').strip() or None
#             expiry = (ln.get('expiry') or '').strip() or None

#             cur.execute("""
#                 INSERT INTO receipt_lines (receipt_id, sku, qty, unit, location, batch_id, expiry)
#                 VALUES (?, ?, ?, ?, ?, ?, ?)
#             """, (receipt_id, sku, qty, unit, location, batch_id, expiry))

#             cur.execute("UPDATE items SET on_hand = COALESCE(on_hand,0) + ?, updated_at = datetime('now') WHERE sku = ?", (qty, sku))
#             if cur.rowcount == 0:
#                 cur.execute("""
#                     INSERT INTO items (sku, name, unit, category, on_hand, reorder_level, created_at, updated_at)
#                     VALUES (?, ?, ?, '', ?, NULL, datetime('now'), datetime('now'))
#                 """, (sku, sku, unit or 'pcs', qty))

#             cur.execute("""
#                 INSERT INTO stock_ledger (sku, item_sku, qty, txn_type, reference, batch_no, created_by, when_ts, notes)
#                 VALUES (?, ?, ?, 'RECEIVE', ?, ?, ?, datetime('now'), ?)
#             """, (sku, sku, qty, grn_no, batch_id, confirmed_by, f"GRN {grn_no}"))

#             clean_lines.append({'sku': sku, 'qty': qty, 'unit': unit, 'location': location, 'batch_id': batch_id})

#         # finalize header
#         cur.execute("""
#             UPDATE receipts
#             SET grn_no = ?, order_ref = ?, supplier = ?, supplier_email = ?, po_no = ?,
#                 condition_status = ?, condition_notes = ?, received_by = ?,
#                 confirmed = 1, confirmed_at = datetime('now')
#             WHERE id = ?
#         """, (grn_no, order_ref, supplier, supplier_email, po_ref,
#               condition, condition_notes, confirmed_by, receipt_id))
#         conn.commit()

#         # generate the PDF
#         pdf_path = None
#         try:
#             pdf_path = generate_pdf_for_receipt(
#                 grn_no, order_ref, supplier, supplier_email, condition, condition_notes, clean_lines
#             )
#             cur.execute("UPDATE receipts SET document_path = ? WHERE id = ?", (pdf_path, receipt_id))
#             conn.commit()
#         except Exception:
#             app.logger.exception("PDF generation failed for receipt %s", receipt_id)

#         # email the supplier — only when goods were confirmed in good condition
#         emailed = False
#         if pdf_path and condition == 'good':
#             send_receipt_email(supplier_email, grn_no, order_ref, pdf_path, receipt_id)
#             emailed = True

#         return jsonify({
#             'status': 'ok',
#             'receipt_id': receipt_id,
#             'grn_no': grn_no,
#             'order_ref': order_ref,
#             'pdf_generated': bool(pdf_path),
#             'email_queued': emailed,
#         }), 200

#     except Exception:
#         conn.rollback()
#         app.logger.exception("Failed to process GRN")
#         return jsonify({'status': 'error', 'message': 'Failed to process GRN'}), 500
#     finally:
#         conn.close()



# @app.route('/api/receive/confirm', methods=['POST'])
# def api_receive_confirm():
#     app.logger.debug("Incoming /api/receive/confirm headers: %s", dict(request.headers))
#     app.logger.debug("Incoming /api/receive/confirm raw body: %s", request.get_data(as_text=True))

#     data = request.get_json(silent=True) or {}
#     receipt_id = data.get('receipt_id')
#     confirmed_by = data.get('confirmed_by') or session.get('username') or 'web'

#     if not receipt_id:
#         return jsonify({'status': 'error', 'message': 'receipt_id is required'}), 400

#     conn = None
#     cur = None
#     try:
#         conn = get_conn()
#         cur = conn.cursor()

#         cur.execute("SELECT id, receipt_no, grn_no, order_ref, supplier_email, confirmed FROM receipts WHERE id = ?", (receipt_id,))
#         hdr = cur.fetchone()
#         if not hdr:
#             return jsonify({'status': 'error', 'message': 'Receipt not found'}), 404

#         def row_val(row, key, default=None):
#             try:
#                 v = row[key]
#                 return v if v is not None else default
#             except Exception:
#                 return default

#         if row_val(hdr, 'confirmed', 0):
#             return jsonify({'status': 'error', 'message': 'Receipt already confirmed'}), 400

#         grn_no = row_val(hdr, 'grn_no')
#         if not grn_no:
#             grn_no = f"GRN-{datetime.utcnow().strftime('%Y%m%d')}-{int(datetime.utcnow().timestamp())}"
#             cur.execute("UPDATE receipts SET grn_no = ? WHERE id = ?", (grn_no, receipt_id))

#         order_ref = row_val(hdr, 'order_ref')
#         if not order_ref:
#             order_ref = f"ORD-{int(datetime.utcnow().timestamp())}"
#             cur.execute("UPDATE receipts SET order_ref = ? WHERE id = ?", (order_ref, receipt_id))

#         try:
#             cur.execute("SELECT sku, qty, unit, location, batch_id, expiry FROM receipt_lines WHERE receipt_id = ?", (receipt_id,))
#             rows = cur.fetchall() or []
#             if rows:
#                 pdf_lines = []
#                 for r in rows:
#                     pdf_lines.append({
#                         'sku': r['sku'],
#                         'qty': r['qty'],
#                         'unit': r['unit'] if 'unit' in r.keys() else None,
#                         'location': r['location'] if 'location' in r.keys() else None,
#                         'batch_id': r['batch_id'] if 'batch_id' in r.keys() else None,
#                         'expiry': r['expiry'] if 'expiry' in r.keys() else None
#                     })
#             else:
#                 pdf_lines = data.get('lines') or []
#         except Exception:
#             pdf_lines = data.get('lines') or []

#         cur.execute("UPDATE receipts SET confirmed = 1, confirmed_at = datetime('now'), received_by = ? WHERE id = ?",
#                     (confirmed_by, receipt_id))
#         conn.commit()

#         try:
#             supplier_email = row_val(hdr, 'supplier_email') or data.get('supplier_email') or None
#             pdf_path = generate_pdf_for_receipt(grn_no, order_ref, supplier_email, pdf_lines)
#             app.logger.info("PDF generated at: %s", pdf_path)
#             try:
#                 cur.execute("UPDATE receipts SET document_path = ? WHERE id = ?", (pdf_path, receipt_id))
#                 conn.commit()
#             except Exception:
#                 app.logger.exception("Failed to update receipts.document_path for %s", receipt_id)
#         except Exception:
#             app.logger.exception("PDF generation failed for receipt %s", receipt_id)
#             pdf_path = None

#         return jsonify({'status': 'ok', 'receipt_id': receipt_id, 'grn_no': grn_no, 'order_ref': order_ref, 'document_path': pdf_path}), 200

#     except Exception:
#         if conn:
#             try:
#                 conn.rollback()
#             except Exception:
#                 pass
#         app.logger.exception("Failed to confirm receipt %s", receipt_id)
#         return jsonify({'status': 'error', 'message': 'Failed to confirm receipt'}), 500
#     finally:
#         try:
#             if cur: cur.close()
#         except Exception:
#             pass
#         try:
#             if conn: conn.close()
#         except Exception:
#             pass






# def generate_pdf_for_receipt(grn_no, order_ref, supplier, supplier_email, condition, condition_notes, lines):
#     filename = f"{grn_no}.pdf"
#     out_path = os.path.join(OUT_DIR, filename)

#     c = canvas.Canvas(out_path, pagesize=A4)
#     c.setTitle(f"GRN {grn_no}")
#     c.setFont("Helvetica-Bold", 14)
#     c.drawString(40, 800, f"Goods Received Note {grn_no}")
#     c.setFont("Helvetica", 10)
#     c.drawString(40, 785, f"Order Ref: {order_ref or ''}")
#     c.drawString(300, 785, f"Date: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
#     c.drawString(40, 770, f"Supplier: {supplier_email or ''}")
#     c.drawString(40, 755, f"Condition: {condition} {('- ' + condition_notes) if condition_notes else ''}")

#     y = 730
#     c.setFont("Helvetica-Bold", 10)
#     c.drawString(40, y, "SKU")
#     c.drawString(180, y, "Qty")
#     c.drawString(230, y, "Unit")
#     c.drawString(290, y, "Batch")
#     c.drawString(380, y, "Location")
#     c.setFont("Helvetica", 10)
#     y -= 16

#     for ln in lines:
#         c.drawString(40, y, str(ln.get('sku', '')))
#         c.drawString(180, y, str(ln.get('qty', '')))
#         c.drawString(230, y, str(ln.get('unit', '')))
#         c.drawString(290, y, str(ln.get('batch_no', '')))
#         c.drawString(380, y, str(ln.get('location', '')))
#         y -= 14
#         if y < 60:
#             c.showPage()
#             y = 800
#     c.showPage()
#     c.save()
#     return out_path



# @app.route('/api/receive/confirm', methods=['POST'])
# def api_receive_confirm():
#     app.logger.debug("Incoming /api/receive/confirm headers: %s", dict(request.headers))
#     app.logger.debug("Incoming /api/receive/confirm raw body: %s", request.get_data(as_text=True))

#     data = request.get_json(silent=True) or {}
#     # normalize receipt_id and confirmed_by
#     receipt_id = (data.get('receipt_id') or '').strip()
#     confirmed_by = data.get('confirmed_by') or session.get('username') or 'web'

#     conn = None
#     cur = None
#     try:
#         conn = get_conn()
#         cur = conn.cursor()

#         # If no receipt_id provided, create one safely and get its header row
#         if not receipt_id:
#             try:
#                 receipt_id, hdr = create_receipt_row(cur, conn)
#             except Exception as e:
#                 app.logger.exception("Failed to create receipt id: %s", e)
#                 return jsonify({'status': 'error', 'message': 'Failed to create receipt id'}), 500
#         else:
#             # ensure receipt_id is an integer when passed as string
#             try:
#                 receipt_id = int(receipt_id)
#             except Exception:
#                 # if it's not an integer, try to look up by receipt_no
#                 cur.execute("SELECT id FROM receipts WHERE receipt_no = ? LIMIT 1", (receipt_id,))
#                 r = cur.fetchone()
#                 if r:
#                     receipt_id = r['id']
#                 else:
#                     return jsonify({'status': 'error', 'message': 'Invalid receipt_id'}), 400

#         # load header
#         cur.execute("SELECT id, receipt_no, grn_no, order_ref, supplier_email, confirmed FROM receipts WHERE id = ?", (receipt_id,))
#         hdr = cur.fetchone()
#         if not hdr:
#             return jsonify({'status': 'error', 'message': 'Receipt not found'}), 404

#         def row_val(row, key, default=None):
#             try:
#                 v = row[key]
#                 return v if v is not None else default
#             except Exception:
#                 try:
#                     return row.get(key, default)
#                 except Exception:
#                     return default

#         if row_val(hdr, 'confirmed', 0):
#             return jsonify({'status': 'error', 'message': 'Receipt already confirmed'}), 400

#         # timezone-aware grn/order generation
#         grn_no = row_val(hdr, 'grn_no')
#         if not grn_no:
#             grn_no = f"GRN-{datetime.now(timezone.utc).strftime('%Y%m%d')}-{int(datetime.now(timezone.utc).timestamp())}"
#             cur.execute("UPDATE receipts SET grn_no = ? WHERE id = ?", (grn_no, receipt_id))

#         order_ref = row_val(hdr, 'order_ref')
#         if not order_ref:
#             order_ref = f"ORD-{int(datetime.now(timezone.utc).timestamp())}"
#             cur.execute("UPDATE receipts SET order_ref = ? WHERE id = ?", (order_ref, receipt_id))

#         # load lines using actual schema; fallback to request lines
#         try:
#             cur.execute("SELECT * FROM receipt_lines WHERE receipt_id = ?", (receipt_id,))
#             rows = cur.fetchall() or []
#             if rows:
#                 pdf_lines = []
#                 for r in rows:
#                     rd = dict(r)
#                     pdf_lines.append({
#                         'sku': rd.get('item_sku') or rd.get('sku') or '',
#                         'qty': rd.get('qty') or 0,
#                         'unit': rd.get('unit') or '',
#                         'batch_no': rd.get('batch_no') or rd.get('batch') or '',
#                         'line_notes': rd.get('line_notes') or '',
#                         'location_id': rd.get('location_id') or rd.get('location') or None
#                     })
#             else:
#                 pdf_lines = data.get('lines') or []
#         except Exception:
#             pdf_lines = data.get('lines') or []

#         # resolve location names
#         def resolve_location_name(cur, loc_id):
#             if not loc_id:
#                 return ''
#             try:
#                 cur.execute("SELECT name, code FROM locations WHERE id = ? LIMIT 1", (loc_id,))
#                 r = cur.fetchone()
#                 if r:
#                     try:
#                         return r['name'] or r['code'] or str(loc_id)
#                     except Exception:
#                         return r[0] or r[1] or str(loc_id)
#             except Exception:
#                 pass
#             return str(loc_id)

#         for ln in pdf_lines:
#             loc_id = ln.pop('location_id', None)
#             ln['location'] = resolve_location_name(cur, loc_id)

#         # generate PDF (use defaults for condition fields)
#         supplier_email = row_val(hdr, 'supplier_email') or data.get('supplier_email') or None
#         condition = data.get('condition', 'Good')
#         condition_notes = data.get('condition_notes', '')

#         # call helper (ensure signature matches your implementation)
#         pdf_path = generate_pdf_for_receipt(grn_no, order_ref, supplier_email, condition, condition_notes, pdf_lines)
#         app.logger.info("PDF generated at: %s", pdf_path)

#         # mark confirmed and save document path
#         cur.execute(
#             "UPDATE receipts SET confirmed = 1, confirmed_at = datetime('now'), received_by = ?, document_path = ? WHERE id = ?",
#             (confirmed_by, pdf_path, receipt_id)
#         )
#         conn.commit()

#         return jsonify({
#             'status': 'ok',
#             'receipt_id': receipt_id,
#             'grn_no': grn_no,
#             'order_ref': order_ref,
#             'document_path': pdf_path
#         }), 200

#     except Exception:
#         if conn:
#             try:
#                 conn.rollback()
#             except Exception:
#                 pass
#         app.logger.exception("Failed to confirm receipt %s", receipt_id)
#         return jsonify({'status': 'error', 'message': 'Failed to confirm receipt'}), 500
#     finally:
#         try:
#             if cur: cur.close()
#         except Exception:
#             pass
#         try:
#             if conn: conn.close()
#         except Exception:
#             pass


# --- The confirmed endpoint (paste this where your other routes live) ---
# @app.route('/api/receive/confirm', methods=['POST'])
# def api_receive_confirm():
#     app.logger.debug("Incoming /api/receive/confirm headers: %s", dict(request.headers))
#     app.logger.debug("Incoming /api/receive/confirm raw body: %s", request.get_data(as_text=True))

#     data = request.get_json(silent=True) or {}
#     receipt_id = data.get('receipt_id')
#     confirmed_by = data.get('confirmed_by') or session.get('username') or 'web'

#     if not receipt_id:
#         return jsonify({'status': 'error', 'message': 'receipt_id is required'}), 400

#     conn = None
#     cur = None
#     try:
#         conn = get_conn()
#         cur = conn.cursor()

#         cur.execute("SELECT id, receipt_no, grn_no, order_ref, supplier_email, confirmed FROM receipts WHERE id = ?", (receipt_id,))
#         hdr = cur.fetchone()
#         if not hdr:
#             return jsonify({'status': 'error', 'message': 'Receipt not found'}), 404

#         def row_val(row, key, default=None):
#             try:
#                 v = row[key]
#                 return v if v is not None else default
#             except Exception:
#                 try:
#                     return row.get(key, default)
#                 except Exception:
#                     return default

#         if row_val(hdr, 'confirmed', 0):
#             return jsonify({'status': 'error', 'message': 'Receipt already confirmed'}), 400

#         # timezone-aware grn/order generation
#         grn_no = row_val(hdr, 'grn_no')
#         if not grn_no:
#             grn_no = f"GRN-{datetime.now(timezone.utc).strftime('%Y%m%d')}-{int(datetime.now(timezone.utc).timestamp())}"
#             cur.execute("UPDATE receipts SET grn_no = ? WHERE id = ?", (grn_no, receipt_id))

#         order_ref = row_val(hdr, 'order_ref')
#         if not order_ref:
#             order_ref = f"ORD-{int(datetime.now(timezone.utc).timestamp())}"
#             cur.execute("UPDATE receipts SET order_ref = ? WHERE id = ?", (order_ref, receipt_id))

#         # load lines using actual schema; fallback to request lines
#         try:
#             cur.execute("SELECT * FROM receipt_lines WHERE receipt_id = ?", (receipt_id,))
#             rows = cur.fetchall() or []
#             if rows:
#                 pdf_lines = []
#                 for r in rows:
#                     rd = dict(r)
#                     pdf_lines.append({
#                         'sku': rd.get('item_sku') or rd.get('sku') or '',
#                         'qty': rd.get('qty') or 0,
#                         'unit': rd.get('unit') or '',
#                         'batch_no': rd.get('batch_no') or rd.get('batch') or '',
#                         'line_notes': rd.get('line_notes') or '',
#                         'location_id': rd.get('location_id') or rd.get('location') or None
#                     })
#             else:
#                 pdf_lines = data.get('lines') or []
#         except Exception:
#             pdf_lines = data.get('lines') or []

#         # resolve location names
#         def resolve_location_name(cur, loc_id):
#             if not loc_id:
#                 return ''
#             try:
#                 cur.execute("SELECT name, code FROM locations WHERE id = ? LIMIT 1", (loc_id,))
#                 r = cur.fetchone()
#                 if r:
#                     try:
#                         return r['name'] or r['code'] or str(loc_id)
#                     except Exception:
#                         return r[0] or r[1] or str(loc_id)
#             except Exception:
#                 pass
#             return str(loc_id)

#         for ln in pdf_lines:
#             loc_id = ln.pop('location_id', None)
#             ln['location'] = resolve_location_name(cur, loc_id)

#         # generate PDF (use defaults for condition fields)
#         supplier_email = row_val(hdr, 'supplier_email') or data.get('supplier_email') or None
#         condition = data.get('condition', 'Good')
#         condition_notes = data.get('condition_notes', '')

#         # call helper (this implementation matches the signature above)
#         pdf_path = generate_pdf_for_receipt(grn_no, order_ref, supplier_email, condition, condition_notes, pdf_lines)
#         app.logger.info("PDF generated at: %s", pdf_path)

#         # mark confirmed and save document path
#         cur.execute(
#             "UPDATE receipts SET confirmed = 1, confirmed_at = datetime('now'), received_by = ?, document_path = ? WHERE id = ?",
#             (confirmed_by, pdf_path, receipt_id)
#         )
#         conn.commit()

#         return jsonify({
#             'status': 'ok',
#             'receipt_id': receipt_id,
#             'grn_no': grn_no,
#             'order_ref': order_ref,
#             'document_path': pdf_path
#         }), 200

#     except Exception:
#         if conn:
#             try:
#                 conn.rollback()
#             except Exception:
#                 pass
#         app.logger.exception("Failed to confirm receipt %s", receipt_id)
#         return jsonify({'status': 'error', 'message': 'Failed to confirm receipt'}), 500
#     finally:
#         try:
#             if cur: cur.close()
#         except Exception:
#             pass
#         try:
#             if conn: conn.close()
#         except Exception:
#             pass







#!/usr/bin/env python3
# regen_pdf_for_receipt.py
# import sqlite3
# import os
# import sys
# from datetime import datetime
# from reportlab.lib.pagesizes import A4
# from reportlab.pdfgen import canvas

# DB = 'busia_wms.sqlite3'
# OUT_DIR = os.path.join(os.path.dirname(__file__), 'generated_docs')
# os.makedirs(OUT_DIR, exist_ok=True)

# def pdf_from_rows(path, grn_no, order_ref, supplier_email, lines, receipt_meta):
#     c = canvas.Canvas(path, pagesize=A4)
#     c.setTitle(f"GRN {grn_no}")
#     c.setFont("Helvetica-Bold", 14)
#     c.drawString(40, 800, f"Goods Received Note {grn_no}")
#     c.setFont("Helvetica", 10)
#     c.drawString(40, 785, f"Order Ref: {order_ref or ''}")
#     c.drawString(300, 785, f"Date: {receipt_meta.get('created_at','')}")
#     c.drawString(40, 770, f"Supplier Email: {supplier_email or ''}")
#     y = 740
#     c.setFont("Helvetica-Bold", 10)
#     c.drawString(40, y, "SKU")
#     c.drawString(180, y, "Qty")
#     c.drawString(230, y, "Unit")
#     c.drawString(290, y, "Batch")
#     c.drawString(380, y, "Location")
#     c.setFont("Helvetica", 10)
#     y -= 16
#     for ln in lines:
#         c.drawString(40, y, str(ln.get('sku','')))
#         c.drawString(180, y, str(ln.get('qty','')))
#         c.drawString(230, y, str(ln.get('unit','')))
#         c.drawString(290, y, str(ln.get('batch_no','')))
#         c.drawString(380, y, str(ln.get('location','')))
#         y -= 14
#         if y < 60:
#             c.showPage()
#             y = 800
#     c.showPage()
#     c.save()

# def resolve_location(cur, location_id):
#     if not location_id:
#         return ''
#     try:
#         cur.execute("SELECT name, code FROM locations WHERE id = ? LIMIT 1", (location_id,))
#         r = cur.fetchone()
#         if r:
#             return r['name'] or r['code'] or str(location_id)
#     except Exception:
#         pass
#     return str(location_id)

# def main(receipt_id):
#     conn = sqlite3.connect(DB)
#     conn.row_factory = sqlite3.Row
#     cur = conn.cursor()

#     # load receipt header
#     cur.execute("SELECT id, receipt_no, grn_no, order_ref, supplier_email, created_at FROM receipts WHERE id = ?", (receipt_id,))
#     hdr = cur.fetchone()
#     if not hdr:
#         print("Receipt not found:", receipt_id)
#         conn.close()
#         sys.exit(1)

#     receipt_no = hdr['receipt_no'] or f"RCPT-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
#     grn_no = hdr['grn_no'] or f"GRN-{datetime.utcnow().strftime('%Y%m%d')}-{int(datetime.utcnow().timestamp())}"
#     order_ref = hdr['order_ref']
#     supplier_email = hdr['supplier_email']
#     receipt_meta = {'created_at': hdr['created_at']}

#     # fetch lines using actual schema
#     cur.execute("SELECT * FROM receipt_lines WHERE receipt_id = ?", (receipt_id,))
#     rows = cur.fetchall()
#     lines = []
#     for r in rows:
#         rowd = dict(r)
#         ln = {
#             'sku': rowd.get('item_sku') or rowd.get('sku') or '',
#             'qty': rowd.get('qty') or 0,
#             'unit': rowd.get('unit') or '',
#             'batch_no': rowd.get('batch_no') or rowd.get('batch') or '',
#             'line_notes': rowd.get('line_notes') or '',
#             'created_at': rowd.get('created_at') or ''
#         }
#         # resolve location_id -> name if present
#         loc_id = rowd.get('location_id') or rowd.get('location') or None
#         ln['location'] = resolve_location(cur, loc_id)
#         lines.append(ln)

#     # generate PDF
#     filename = f"{grn_no}.pdf"
#     out_path = os.path.join(OUT_DIR, filename)
#     pdf_from_rows(out_path, grn_no, order_ref, supplier_email, lines, receipt_meta)
#     print("PDF written to:", out_path)

#     # update receipts.document_path
#     try:
#         cur.execute("UPDATE receipts SET document_path = ? WHERE id = ?", (out_path, receipt_id))
#         conn.commit()
#         print("Updated receipts.document_path for id", receipt_id)
#     except Exception as e:
#         print("Failed to update receipts.document_path:", e)

#     cur.close()
#     conn.close()




# import uuid
# import sqlite3
# import time

# def create_receipt_row(cur, conn):
#     """
#     Insert a new receipts row with a collision-proof receipt_no.
#     Returns (receipt_id, row) on success or raises RuntimeError after retries.
#     """
#     attempts = 0
#     max_attempts = 5
#     while attempts < max_attempts:
#         attempts += 1
#         receipt_no = f"RCPT-{uuid.uuid4().hex}"
#         try:
#             cur.execute(
#                 "INSERT INTO receipts (receipt_no, created_at) VALUES (?, datetime('now'))",
#                 (receipt_no,)
#             )
#             conn.commit()
#             receipt_id = cur.lastrowid
#             cur.execute("SELECT * FROM receipts WHERE id = ?", (receipt_id,))
#             return receipt_id, cur.fetchone()
#         except sqlite3.IntegrityError as e:
#             conn.rollback()
#             app.logger.warning("Receipt insert collision (attempt %s): %s", attempts, e)
#             time.sleep(0.02)
#             continue
#     raise RuntimeError("Failed to create unique receipt after multiple attempts")



# import os
# import io
# import csv
# import json
# import logging
# import smtplib
# import threading
# from datetime import datetime
# from email.message import EmailMessage

# from flask import (
#     Flask, render_template, request, jsonify, redirect, url_for,
#     flash, session, make_response, current_app, abort
# )
# from dotenv import load_dotenv
# from flask_wtf import CSRFProtect
# from flask_wtf.csrf import CSRFError, generate_csrf
# from flask_session import Session

# from reportlab.lib.pagesizes import A4
# from reportlab.pdfgen import canvas

# import sqlite3

# # -------------------------------------------------------------------
# # App setup
# # -------------------------------------------------------------------
# load_dotenv()

# app = Flask(__name__, template_folder='templates', static_folder='static')
# app.logger.setLevel(logging.DEBUG)

# app.secret_key = os.getenv('FLASK_SECRET', 'devsecret')
# app.config.update({
#     'SESSION_TYPE': 'filesystem',
#     'SESSION_COOKIE_HTTPONLY': True,
#     'SESSION_COOKIE_SAMESITE': 'Lax',
#     'SESSION_COOKIE_SECURE': False,
# })

# Session(app)
# csrf = CSRFProtect(app)

# app.jinja_env.globals['generate_csrf'] = generate_csrf


# @app.context_processor
# def expose_helpers():
#     def endpoint_exists(name):
#         return name in current_app.view_functions
#     return {'endpoint_exists': endpoint_exists}


# @app.errorhandler(CSRFError)
# def handle_csrf(e):
#     app.logger.warning(
#         "CSRFError: %s; headers=%s; body=%s",
#         e.description, dict(request.headers), request.get_data(as_text=True)
#     )
#     return jsonify({'status': 'error', 'message': 'CSRF token missing or invalid'}), 400


# # -------------------------------------------------------------------
# # Config
# # -------------------------------------------------------------------
# DB_PATH = os.getenv('SQLITE_PATH', os.path.join(os.getcwd(), 'busia_wms.sqlite3'))
# LOW_STOCK_THRESHOLD = int(os.getenv('LOW_STOCK_THRESHOLD', 10))

# DEFAULT_UNITS = ['pcs', 'bag', 'bottle', 'pack', 'jerrycan']
# DEFAULT_CATEGORIES = ['Food', 'NFI', 'Health', 'Beverages', 'General']

# MAIL_HOST = os.getenv('BUSIA_SMTP_HOST') or os.getenv('MAIL_HOST')
# MAIL_PORT = int(os.getenv('BUSIA_SMTP_PORT', os.getenv('MAIL_PORT', 587)))
# MAIL_USER = os.getenv('BUSIA_SMTP_USER') or os.getenv('MAIL_USER')
# MAIL_PASS = os.getenv('BUSIA_SMTP_PASS') or os.getenv('MAIL_PASS')
# MAIL_FROM = os.getenv('BUSIA_EMAIL_FROM') or os.getenv('MAIL_FROM') or MAIL_USER or 'no-reply@example.com'
# MAIL_FROM_NAME = os.getenv('BUSIA_EMAIL_FROM_NAME', 'Busia WMS')
# MAIL_USE_TLS = os.getenv('BUSIA_SMTP_TLS', '1') not in ('0', 'false', 'False')

# BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# UPLOAD_DIR = os.path.join(BASE_DIR, 'generated_docs')
# os.makedirs(UPLOAD_DIR, exist_ok=True)


# # -------------------------------------------------------------------
# # DB helpers
# # -------------------------------------------------------------------
# def get_conn():
#     conn = sqlite3.connect(DB_PATH, detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES)
#     conn.row_factory = sqlite3.Row
#     conn.execute("PRAGMA foreign_keys = ON;")
#     return conn


# def init_db():
#     ddl = """
#     PRAGMA foreign_keys = ON;

#     CREATE TABLE IF NOT EXISTS sequences (
#       name TEXT PRIMARY KEY,
#       last INTEGER NOT NULL DEFAULT 0
#     );

#     CREATE TABLE IF NOT EXISTS items (
#       id INTEGER PRIMARY KEY AUTOINCREMENT,
#       sku TEXT NOT NULL UNIQUE,
#       name TEXT NOT NULL,
#       unit TEXT DEFAULT 'pcs',
#       category TEXT,
#       on_hand INTEGER DEFAULT 0,
#       reorder_level INTEGER DEFAULT 0,
#       price REAL DEFAULT NULL,
#       carton_price REAL DEFAULT NULL,
#       description TEXT,
#       created_at DATETIME DEFAULT (datetime('now')),
#       updated_at DATETIME DEFAULT (datetime('now'))
#     );

#     CREATE TABLE IF NOT EXISTS locations (
#       id INTEGER PRIMARY KEY AUTOINCREMENT,
#       code TEXT UNIQUE,
#       name TEXT NOT NULL,
#       type TEXT,
#       capacity INTEGER DEFAULT NULL,
#       description TEXT,
#       notes TEXT,
#       created_at DATETIME DEFAULT (datetime('now')),
#       updated_at DATETIME DEFAULT (datetime('now'))
#     );

#     CREATE TABLE IF NOT EXISTS suppliers (
#       id INTEGER PRIMARY KEY AUTOINCREMENT,
#       code TEXT UNIQUE,
#       name TEXT NOT NULL,
#       contact_name TEXT,
#       phone TEXT,
#       email TEXT,
#       address TEXT,
#       notes TEXT,
#       created_at DATETIME DEFAULT (datetime('now'))
#     );

#     CREATE TABLE IF NOT EXISTS users (
#       id INTEGER PRIMARY KEY AUTOINCREMENT,
#       username TEXT NOT NULL UNIQUE,
#       full_name TEXT,
#       email TEXT,
#       password_hash TEXT,
#       role TEXT DEFAULT 'user',
#       is_active INTEGER DEFAULT 1,
#       created_at DATETIME DEFAULT (datetime('now'))
#     );

#     -- Goods Receipt Note header. grn_no / order_ref are generated on confirm,
#     -- not on page load, so re-opening the page doesn't burn sequence numbers.
#     CREATE TABLE IF NOT EXISTS receipts (
#       id INTEGER PRIMARY KEY AUTOINCREMENT,
#       receipt_no TEXT NOT NULL UNIQUE,
#       grn_no TEXT DEFAULT NULL,
#       order_ref TEXT DEFAULT NULL,
#       supplier TEXT DEFAULT NULL,
#       supplier_email TEXT DEFAULT NULL,
#       po_no TEXT DEFAULT NULL,
#       condition_status TEXT DEFAULT NULL,
#       condition_notes TEXT DEFAULT NULL,
#       received_by TEXT DEFAULT NULL,
#       confirmed INTEGER DEFAULT 0,
#       confirmed_at DATETIME DEFAULT NULL,
#       document_path TEXT DEFAULT NULL,
#       email_sent INTEGER DEFAULT 0,
#       email_error TEXT DEFAULT NULL,
#       created_at DATETIME DEFAULT (datetime('now'))
#     );

#     CREATE TABLE IF NOT EXISTS receipt_lines (
#       id INTEGER PRIMARY KEY AUTOINCREMENT,
#       receipt_id INTEGER NOT NULL,
#       sku TEXT NOT NULL,
#       qty REAL NOT NULL,
#       unit TEXT DEFAULT 'pcs',
#       location TEXT DEFAULT NULL,
#       batch_id TEXT DEFAULT NULL,
#       expiry TEXT DEFAULT NULL,
#       created_at DATETIME DEFAULT (datetime('now')),
#       FOREIGN KEY (receipt_id) REFERENCES receipts(id) ON DELETE CASCADE
#     );

#     CREATE TABLE IF NOT EXISTS drivers (
#       id INTEGER PRIMARY KEY AUTOINCREMENT,
#       name TEXT NOT NULL,
#       phone TEXT,
#       license_no TEXT,
#       created_at DATETIME DEFAULT (datetime('now'))
#     );

#     CREATE TABLE IF NOT EXISTS vehicles (
#       id INTEGER PRIMARY KEY AUTOINCREMENT,
#       reg_no TEXT NOT NULL UNIQUE,
#       make_model TEXT,
#       capacity TEXT,
#       created_at DATETIME DEFAULT (datetime('now'))
#     );

#     -- Delivery Note header. delivery_no / order_ref are generated on confirm,
#     -- same pattern as the GRN receipts above.
#     CREATE TABLE IF NOT EXISTS deliveries (
#       id INTEGER PRIMARY KEY AUTOINCREMENT,
#       delivery_no TEXT NOT NULL UNIQUE,
#       order_ref TEXT DEFAULT NULL,
#       recipient TEXT DEFAULT NULL,
#       recipient_phone TEXT DEFAULT NULL,
#       recipient_email TEXT DEFAULT NULL,
#       address TEXT,
#       vehicle_id INTEGER DEFAULT NULL,
#       vehicle_reg TEXT,
#       driver_id INTEGER DEFAULT NULL,
#       driver_name TEXT,
#       driver_phone TEXT,
#       remarks TEXT,
#       created_by TEXT DEFAULT NULL,
#       confirmed INTEGER DEFAULT 0,
#       confirmed_at DATETIME DEFAULT NULL,
#       document_path TEXT DEFAULT NULL,
#       email_sent INTEGER DEFAULT 0,
#       email_error TEXT DEFAULT NULL,
#       created_at DATETIME DEFAULT (datetime('now')),
#       updated_at DATETIME DEFAULT (datetime('now')),
#       FOREIGN KEY (vehicle_id) REFERENCES vehicles(id) ON DELETE SET NULL,
#       FOREIGN KEY (driver_id) REFERENCES drivers(id) ON DELETE SET NULL
#     );

#     CREATE TABLE IF NOT EXISTS delivery_lines (
#       id INTEGER PRIMARY KEY AUTOINCREMENT,
#       delivery_id INTEGER NOT NULL,
#       sku TEXT NOT NULL,
#       qty REAL NOT NULL,
#       unit TEXT DEFAULT 'pcs',
#       location TEXT DEFAULT NULL,
#       batch_id TEXT DEFAULT NULL,
#       created_at DATETIME DEFAULT (datetime('now')),
#       FOREIGN KEY (delivery_id) REFERENCES deliveries(id) ON DELETE CASCADE
#     );

#     CREATE TABLE IF NOT EXISTS stock_ledger (
#       id INTEGER PRIMARY KEY AUTOINCREMENT,
#       sku TEXT DEFAULT NULL,
#       item_sku TEXT DEFAULT NULL,
#       qty REAL NOT NULL,
#       txn_type TEXT NOT NULL,
#       location_id INTEGER DEFAULT NULL,
#       from_location_id INTEGER DEFAULT NULL,
#       to_location_id INTEGER DEFAULT NULL,
#       reference TEXT DEFAULT NULL,
#       batch_no TEXT DEFAULT NULL,
#       created_by TEXT DEFAULT NULL,
#       when_ts DATETIME DEFAULT (datetime('now')),
#       notes TEXT,
#       FOREIGN KEY (from_location_id) REFERENCES locations(id) ON DELETE SET NULL,
#       FOREIGN KEY (to_location_id) REFERENCES locations(id) ON DELETE SET NULL
#     );
#     """
#     conn = get_conn()
#     try:
#         conn.executescript(ddl)
#         conn.commit()
#     finally:
#         conn.close()


# init_db()


# def ensure_columns(table, columns):
#     """Add any missing columns to an existing table (simple migration for upgrades)."""
#     conn = get_conn()
#     try:
#         cur = conn.cursor()
#         cur.execute(f"PRAGMA table_info({table})")
#         existing = {row['name'] for row in cur.fetchall()}
#         for col, decl in columns.items():
#             if col not in existing:
#                 cur.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
#         conn.commit()
#     finally:
#         conn.close()


# # Covers databases created by an earlier version of this app that predates
# # the driver/vehicle/recipient-contact and PDF/email columns on deliveries.
# ensure_columns('deliveries', {
#     'recipient_phone': 'TEXT',
#     'recipient_email': 'TEXT',
#     'vehicle_id': 'INTEGER',
#     'driver_id': 'INTEGER',
#     'driver_phone': 'TEXT',
#     'confirmed': 'INTEGER DEFAULT 0',
#     'confirmed_at': 'DATETIME',
#     'document_path': 'TEXT',
#     'email_sent': 'INTEGER DEFAULT 0',
#     'email_error': 'TEXT',
# })


# # -------------------------------------------------------------------
# # Sequence-based GRN / Order Ref generators
# # -------------------------------------------------------------------
# def next_sequence(name):
#     """Atomically bump and return the next integer in a named sequence."""
#     conn = get_conn()
#     try:
#         cur = conn.cursor()
#         cur.execute("BEGIN IMMEDIATE")
#         cur.execute("SELECT last FROM sequences WHERE name = ?", (name,))
#         row = cur.fetchone()
#         if row is None:
#             val = 1
#             cur.execute("INSERT INTO sequences (name, last) VALUES (?, ?)", (name, val))
#         else:
#             val = int(row['last']) + 1
#             cur.execute("UPDATE sequences SET last = ? WHERE name = ?", (val, name))
#         conn.commit()
#         return val
#     finally:
#         conn.close()


# def generate_grn_no():
#     seq = next_sequence('grn')
#     return f"GRN-{datetime.utcnow().strftime('%Y%m%d')}-{seq:04d}"


# def generate_order_ref():
#     seq = next_sequence('order_ref')
#     return f"ORD-{datetime.utcnow().strftime('%Y%m%d')}-{seq:04d}"


# def generate_delivery_no():
#     seq = next_sequence('delivery_no')
#     return f"DN-{datetime.utcnow().strftime('%Y%m%d')}-{seq:04d}"


# # -------------------------------------------------------------------
# # PDF generation
# # -------------------------------------------------------------------
# def generate_pdf_for_receipt(grn_no, order_ref, supplier, supplier_email, condition, condition_notes, lines):
#     filename = f"{grn_no}.pdf"
#     path = os.path.join(UPLOAD_DIR, filename)
#     c = canvas.Canvas(path, pagesize=A4)

#     c.setFont("Helvetica-Bold", 14)
#     c.drawString(40, 800, f"Goods Received Note {grn_no}")

#     c.setFont("Helvetica", 10)
#     c.drawString(40, 782, f"Date: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
#     c.drawString(40, 767, f"Order Ref: {order_ref or ''}")
#     c.drawString(40, 752, f"Supplier: {supplier or ''}")
#     c.drawString(40, 737, f"Supplier Email: {supplier_email or ''}")
#     c.drawString(40, 722, f"Condition: {condition or 'good'}")
#     if condition_notes:
#         c.drawString(40, 707, f"Notes: {condition_notes[:90]}")

#     y = 680
#     c.setFont("Helvetica-Bold", 10)
#     c.drawString(40, y, "SKU")
#     c.drawString(220, y, "Qty")
#     c.drawString(280, y, "Unit")
#     c.drawString(340, y, "Location")
#     c.drawString(440, y, "Batch")
#     c.setFont("Helvetica", 10)
#     y -= 18

#     for ln in lines:
#         c.drawString(40, y, str(ln.get('sku', '')))
#         c.drawString(220, y, str(ln.get('qty', '')))
#         c.drawString(280, y, str(ln.get('unit', '') or ''))
#         c.drawString(340, y, str(ln.get('location', '') or ''))
#         c.drawString(440, y, str(ln.get('batch_id', '') or ''))
#         y -= 16
#         if y < 60:
#             c.showPage()
#             y = 800

#     c.showPage()
#     c.save()
#     return path


# # -------------------------------------------------------------------
# # Email
# # -------------------------------------------------------------------
# def send_receipt_email(to_email, grn_no, order_ref, pdf_path, receipt_id):
#     """Send the receipt PDF to the supplier and record success/failure on the receipt row."""
#     def _send():
#         error = None
#         try:
#             if not MAIL_HOST:
#                 raise RuntimeError("Mail server is not configured (BUSIA_SMTP_HOST missing)")

#             msg = EmailMessage()
#             msg['Subject'] = f"Goods Received Confirmation {grn_no}"
#             msg['From'] = f"{MAIL_FROM_NAME} <{MAIL_FROM}>"
#             msg['To'] = to_email
#             msg.set_content(
#                 f"Dear Supplier,\n\n"
#                 f"We confirm receipt of goods for GRN {grn_no} (Order {order_ref}).\n"
#                 f"Please find the goods received note attached for your records.\n\n"
#                 f"Regards,\n{MAIL_FROM_NAME}"
#             )
#             with open(pdf_path, 'rb') as f:
#                 msg.add_attachment(
#                     f.read(), maintype='application', subtype='pdf',
#                     filename=os.path.basename(pdf_path)
#                 )

#             if MAIL_USE_TLS:
#                 s = smtplib.SMTP(MAIL_HOST, MAIL_PORT, timeout=20)
#                 s.starttls()
#             else:
#                 s = smtplib.SMTP_SSL(MAIL_HOST, MAIL_PORT, timeout=20)
#             if MAIL_USER:
#                 s.login(MAIL_USER, MAIL_PASS)
#             s.send_message(msg)
#             s.quit()
#         except Exception as exc:
#             error = str(exc)
#             app.logger.exception("Failed to email receipt %s to %s", grn_no, to_email)

#         conn = get_conn()
#         try:
#             conn.execute(
#                 "UPDATE receipts SET email_sent = ?, email_error = ? WHERE id = ?",
#                 (0 if error else 1, error, receipt_id)
#             )
#             conn.commit()
#         finally:
#             conn.close()

#     threading.Thread(target=_send, daemon=True).start()


# def generate_pdf_for_delivery(delivery_no, order_ref, recipient, recipient_phone, recipient_email,
#                                address, driver_name, driver_phone, vehicle_reg, remarks, lines):
#     filename = f"{delivery_no}.pdf"
#     path = os.path.join(UPLOAD_DIR, filename)
#     c = canvas.Canvas(path, pagesize=A4)

#     c.setFont("Helvetica-Bold", 14)
#     c.drawString(40, 800, f"Delivery Note {delivery_no}")

#     c.setFont("Helvetica", 10)
#     c.drawString(40, 782, f"Date: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
#     c.drawString(40, 767, f"Order Ref: {order_ref or ''}")
#     c.drawString(40, 752, f"Recipient: {recipient or ''}")
#     c.drawString(40, 737, f"Recipient Phone: {recipient_phone or ''}")
#     c.drawString(40, 722, f"Recipient Email: {recipient_email or ''}")
#     c.drawString(40, 707, f"Address: {(address or '')[:80]}")
#     c.drawString(40, 692, f"Driver: {driver_name or ''}  ({driver_phone or ''})")
#     c.drawString(40, 677, f"Vehicle: {vehicle_reg or ''}")
#     if remarks:
#         c.drawString(40, 662, f"Remarks: {remarks[:90]}")

#     y = 635
#     c.setFont("Helvetica-Bold", 10)
#     c.drawString(40, y, "SKU")
#     c.drawString(220, y, "Qty")
#     c.drawString(280, y, "Unit")
#     c.drawString(340, y, "Location")
#     c.drawString(440, y, "Batch")
#     c.setFont("Helvetica", 10)
#     y -= 18

#     for ln in lines:
#         c.drawString(40, y, str(ln.get('sku', '')))
#         c.drawString(220, y, str(ln.get('qty', '')))
#         c.drawString(280, y, str(ln.get('unit', '') or ''))
#         c.drawString(340, y, str(ln.get('location', '') or ''))
#         c.drawString(440, y, str(ln.get('batch_id', '') or ''))
#         y -= 16
#         if y < 100:
#             c.showPage()
#             y = 800

#     y -= 40
#     if y < 100:
#         c.showPage()
#         y = 800
#     c.drawString(40, y, "Recipient signature: ____________________     Date: ____________")

#     c.showPage()
#     c.save()
#     return path


# def send_delivery_email(to_email, delivery_no, order_ref, pdf_path, delivery_id):
#     """Send the delivery note PDF to the recipient and record success/failure."""
#     def _send():
#         error = None
#         try:
#             if not MAIL_HOST:
#                 raise RuntimeError("Mail server is not configured (BUSIA_SMTP_HOST missing)")

#             msg = EmailMessage()
#             msg['Subject'] = f"Delivery Note {delivery_no}"
#             msg['From'] = f"{MAIL_FROM_NAME} <{MAIL_FROM}>"
#             msg['To'] = to_email
#             msg.set_content(
#                 f"Dear Recipient,\n\n"
#                 f"Please find attached the delivery note {delivery_no} (Order {order_ref}) "
#                 f"for the goods dispatched to you.\n\n"
#                 f"Regards,\n{MAIL_FROM_NAME}"
#             )
#             with open(pdf_path, 'rb') as f:
#                 msg.add_attachment(
#                     f.read(), maintype='application', subtype='pdf',
#                     filename=os.path.basename(pdf_path)
#                 )

#             if MAIL_USE_TLS:
#                 s = smtplib.SMTP(MAIL_HOST, MAIL_PORT, timeout=20)
#                 s.starttls()
#             else:
#                 s = smtplib.SMTP_SSL(MAIL_HOST, MAIL_PORT, timeout=20)
#             if MAIL_USER:
#                 s.login(MAIL_USER, MAIL_PASS)
#             s.send_message(msg)
#             s.quit()
#         except Exception as exc:
#             error = str(exc)
#             app.logger.exception("Failed to email delivery note %s to %s", delivery_no, to_email)

#         conn = get_conn()
#         try:
#             conn.execute(
#                 "UPDATE deliveries SET email_sent = ?, email_error = ? WHERE id = ?",
#                 (0 if error else 1, error, delivery_id)
#             )
#             conn.commit()
#         finally:
#             conn.close()

#     threading.Thread(target=_send, daemon=True).start()


# # -------------------------------------------------------------------
# # Dashboard / index
# # -------------------------------------------------------------------
# @app.route('/')
# def index():
#     conn = get_conn()
#     try:
#         cur = conn.cursor()
#         cur.execute("""
#             SELECT sku, name, unit, category,
#                    COALESCE(on_hand, 0) AS on_hand,
#                    COALESCE(reorder_level, ?) AS reorder_level
#             FROM items
#             ORDER BY name ASC
#         """, (LOW_STOCK_THRESHOLD,))
#         items = [dict(r) for r in cur.fetchall()]

#         cur.execute("""
#             SELECT when_ts AS `when`, sku, qty, txn_type
#             FROM stock_ledger
#             ORDER BY when_ts DESC
#             LIMIT 5
#         """)
#         recent_movements = [dict(r) for r in cur.fetchall()]
#     except Exception:
#         app.logger.exception("index() failed")
#         items, recent_movements = [], []
#     finally:
#         conn.close()

#     low_alerts = [it for it in items if it['on_hand'] <= it['reorder_level']]
#     stats = {
#         'active_skus': len(items),
#         'units_on_hand': sum(it['on_hand'] for it in items),
#         'recent_movements': len(recent_movements),
#     }

#     return render_template(
#         'index.html', items=items, user=session.get('username'),
#         low_alerts=low_alerts, recent_movements=recent_movements,
#         stats=stats, now=datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')
#     )


# @app.route('/api/dashboard', methods=['GET'])
# def api_dashboard():
#     conn = get_conn()
#     payload = {'active_skus': 0, 'units_on_hand': 0, 'low_stock_alerts': 0,
#                'reorder_alerts': [], 'recent_movements': []}
#     try:
#         cur = conn.cursor()
#         cur.execute("SELECT COUNT(DISTINCT sku) AS n FROM items")
#         payload['active_skus'] = cur.fetchone()['n']

#         cur.execute("SELECT COALESCE(SUM(on_hand),0) AS n FROM items")
#         payload['units_on_hand'] = cur.fetchone()['n']

#         cur.execute("""
#             SELECT sku, name, unit, category, on_hand,
#                    COALESCE(reorder_level, ?) AS reorder_level
#             FROM items
#             WHERE on_hand <= COALESCE(reorder_level, ?)
#             ORDER BY (COALESCE(reorder_level, ?) - on_hand) DESC
#             LIMIT 10
#         """, (LOW_STOCK_THRESHOLD, LOW_STOCK_THRESHOLD, LOW_STOCK_THRESHOLD))
#         alerts = [dict(r) for r in cur.fetchall()]
#         payload['reorder_alerts'] = alerts
#         payload['low_stock_alerts'] = len(alerts)

#         cur.execute("""
#             SELECT when_ts AS ts, sku, qty, txn_type, created_by AS user
#             FROM stock_ledger ORDER BY when_ts DESC LIMIT 10
#         """)
#         payload['recent_movements'] = [dict(r) for r in cur.fetchall()]
#         return jsonify(payload), 200
#     except Exception:
#         app.logger.exception("api_dashboard failed")
#         return jsonify({'status': 'error', 'message': 'Server error fetching dashboard',
#                          'reorder_alerts': [], 'recent_movements': []}), 500
#     finally:
#         conn.close()


# # -------------------------------------------------------------------
# # Items
# # -------------------------------------------------------------------
# def parse_reorder_level(val):
#     if val in (None, ''):
#         return None
#     try:
#         return int(val)
#     except (TypeError, ValueError):
#         return None


# @app.route('/items')
# def items():
#     selected_category = (request.args.get('category') or '').strip() or None
#     q = (request.args.get('q') or '').strip() or None

#     conn = get_conn()
#     try:
#         cur = conn.cursor()
#         sql = """
#             SELECT sku, name, unit, category, COALESCE(on_hand,0) AS on_hand,
#                    COALESCE(reorder_level, ?) AS reorder_level
#             FROM items
#         """
#         params = [LOW_STOCK_THRESHOLD]
#         clauses = []
#         if selected_category:
#             clauses.append("category = ?")
#             params.append(selected_category)
#         if q:
#             clauses.append("(sku LIKE ? OR name LIKE ?)")
#             params += [f"%{q}%", f"%{q}%"]
#         if clauses:
#             sql += " WHERE " + " AND ".join(clauses)
#         sql += " ORDER BY name ASC"

#         cur.execute(sql, params)
#         item_rows = [dict(r) for r in cur.fetchall()]

#         low_alerts = [it for it in item_rows if it['on_hand'] <= it['reorder_level']]

#         cur.execute("""
#             SELECT when_ts AS `when`, sku, qty, txn_type
#             FROM stock_ledger ORDER BY when_ts DESC LIMIT 10
#         """)
#         recent_movements = [dict(r) for r in cur.fetchall()]

#         cur.execute("""
#             SELECT category, COUNT(1) AS cnt FROM items
#             WHERE category IS NOT NULL AND category != ''
#             GROUP BY category ORDER BY category ASC
#         """)
#         cat_rows = cur.fetchall()
#         categories = [{'name': r['category'], 'count': r['cnt']} for r in cat_rows] \
#             or [{'name': c, 'count': ''} for c in DEFAULT_CATEGORIES]

#         stats = {
#             'active_skus': len(item_rows),
#             'units_on_hand': sum(it['on_hand'] for it in item_rows),
#             'recent_movements': len(recent_movements),
#         }
#     except Exception:
#         app.logger.exception("Failed to load items dashboard")
#         item_rows, low_alerts, recent_movements = [], [], []
#         categories = [{'name': c, 'count': ''} for c in DEFAULT_CATEGORIES]
#         stats = {'active_skus': 0, 'units_on_hand': 0, 'recent_movements': 0}
#     finally:
#         conn.close()

#     return render_template(
#         'items.html', items=item_rows, low_alerts=low_alerts, stats=stats,
#         recent_movements=recent_movements, units=DEFAULT_UNITS, categories=categories,
#         selected_category=selected_category, user=session.get('username')
#     )


# @app.route('/export_items')
# def export_items():
#     conn = get_conn()
#     try:
#         cur = conn.cursor()
#         cur.execute("""
#             SELECT sku, name, unit, category, COALESCE(on_hand,0) AS on_hand,
#                    COALESCE(reorder_level, ?) AS reorder_level
#             FROM items ORDER BY name ASC
#         """, (LOW_STOCK_THRESHOLD,))
#         rows = cur.fetchall()

#         si = io.StringIO()
#         writer = csv.writer(si)
#         writer.writerow(['SKU', 'Name', 'Unit', 'Category', 'On hand', 'Reorder level'])
#         for r in rows:
#             writer.writerow([r['sku'], r['name'], r['unit'], r['category'], r['on_hand'], r['reorder_level']])

#         output = make_response(si.getvalue())
#         output.headers["Content-Disposition"] = "attachment; filename=items.csv"
#         output.headers["Content-type"] = "text/csv"
#         return output
#     except Exception:
#         app.logger.exception("Failed to export items")
#         return redirect(url_for('items'))
#     finally:
#         conn.close()


# @app.route('/api/items', methods=['POST'])
# def api_create_item():
#     data = request.get_json(silent=True) or request.form.to_dict()
#     if not data:
#         return jsonify({'status': 'error', 'message': 'Invalid or empty payload'}), 400

#     sku = (data.get('sku') or '').strip()
#     name = (data.get('name') or '').strip()
#     unit = (data.get('unit') or '').strip() or 'pcs'
#     category = (data.get('category') or '').strip()
#     reorder_level = parse_reorder_level(data.get('reorder_level'))

#     if not sku or not name:
#         return jsonify({'status': 'error', 'message': 'SKU and name are required'}), 400

#     conn = get_conn()
#     try:
#         cur = conn.cursor()
#         cur.execute("SELECT COUNT(1) AS cnt FROM items WHERE sku = ?", (sku,))
#         if cur.fetchone()['cnt'] > 0:
#             return jsonify({'status': 'error', 'message': 'SKU already exists'}), 400

#         cur.execute("""
#             INSERT INTO items (sku, name, unit, category, on_hand, reorder_level, created_at, updated_at)
#             VALUES (?, ?, ?, ?, 0, ?, datetime('now'), datetime('now'))
#         """, (sku, name, unit, category, reorder_level))
#         conn.commit()

#         return jsonify({'status': 'ok', 'item': {
#             'sku': sku, 'name': name, 'unit': unit, 'category': category,
#             'on_hand': 0, 'reorder_level': reorder_level if reorder_level is not None else LOW_STOCK_THRESHOLD
#         }}), 200
#     except Exception:
#         conn.rollback()
#         app.logger.exception("Failed to create item")
#         return jsonify({'status': 'error', 'message': 'Server error creating item'}), 500
#     finally:
#         conn.close()


# @app.route('/create_item', methods=['POST'])
# def create_item():
#     sku = (request.form.get('sku') or '').strip()
#     name = (request.form.get('name') or '').strip()
#     unit = (request.form.get('unit_final') or request.form.get('unit_select') or 'pcs').strip()
#     category = (request.form.get('category_final') or request.form.get('category_select') or '').strip()
#     reorder_level = parse_reorder_level(request.form.get('reorder_level'))

#     if not sku or not name:
#         flash('SKU and Name are required.', 'danger')
#         return redirect(url_for('items'))

#     conn = get_conn()
#     try:
#         cur = conn.cursor()
#         cur.execute("SELECT COUNT(1) AS cnt FROM items WHERE sku = ?", (sku,))
#         if cur.fetchone()['cnt'] > 0:
#             flash('SKU already exists.', 'danger')
#             return redirect(url_for('items'))

#         cur.execute("""
#             INSERT INTO items (sku, name, unit, category, on_hand, reorder_level, created_at, updated_at)
#             VALUES (?, ?, ?, ?, 0, ?, datetime('now'), datetime('now'))
#         """, (sku, name, unit, category, reorder_level))
#         conn.commit()
#         flash(f'Item {sku} created.', 'success')
#     except Exception:
#         conn.rollback()
#         app.logger.exception("Failed to create item (form)")
#         flash('Server error while creating item.', 'danger')
#     finally:
#         conn.close()

#     return redirect(url_for('items'))


# @app.route('/item/<sku>')
# def item_detail(sku):
#     conn = get_conn()
#     try:
#         cur = conn.cursor()
#         cur.execute("""
#             SELECT sku, name, unit, category, COALESCE(on_hand,0) AS on_hand,
#                    COALESCE(reorder_level, ?) AS reorder_level
#             FROM items WHERE sku = ?
#         """, (LOW_STOCK_THRESHOLD, sku))
#         item = cur.fetchone()

#         cur.execute("""
#             SELECT when_ts AS `when`, qty, txn_type, location_id
#             FROM stock_ledger WHERE sku = ? ORDER BY when_ts DESC LIMIT 10
#         """, (sku,))
#         movements = cur.fetchall()
#     except Exception:
#         app.logger.exception("Failed to load item detail")
#         item, movements = None, []
#     finally:
#         conn.close()

#     return render_template('item_detail.html', sku=sku, item=item, movements=movements, user=session.get('username'))


# @app.route('/item/<sku>/adjust', methods=['GET', 'POST'])
# def adjust_stock(sku):
#     if request.method == 'GET':
#         return render_template('adjust_stock.html', sku=sku, user=session.get('username'))

#     data = request.get_json(silent=True) or {}
#     qty = float(data.get('qty', 0) or 0)
#     txn_type = data.get('txn_type', 'adjust')
#     location = data.get('location') or None
#     note = data.get('note') or None

#     if qty == 0:
#         return jsonify({'status': 'error', 'message': 'Quantity required'}), 400

#     conn = get_conn()
#     try:
#         cur = conn.cursor()
#         cur.execute("""
#             INSERT INTO stock_ledger (sku, item_sku, qty, txn_type, reference, notes, created_by, when_ts)
#             VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
#         """, (sku, sku, qty, txn_type, location, note, session.get('username', 'system')))
#         cur.execute("UPDATE items SET on_hand = COALESCE(on_hand,0) + ?, updated_at = datetime('now') WHERE sku = ?", (qty, sku))
#         conn.commit()
#         return jsonify({'status': 'ok'})
#     except Exception:
#         conn.rollback()
#         app.logger.exception("Failed to adjust stock")
#         return jsonify({'status': 'error', 'message': 'Failed to adjust stock'}), 500
#     finally:
#         conn.close()


# # -------------------------------------------------------------------
# # Receive goods (GRN) — auto GRN/order ref + PDF + email on confirm
# # -------------------------------------------------------------------
# @app.route('/receive', methods=['GET'])
# def receive():
#     conn = get_conn()
#     items_list, locations_list, receipt = [], [], None
#     try:
#         cur = conn.cursor()
#         cur.execute("SELECT sku, name, unit FROM items ORDER BY name ASC")
#         items_list = [dict(r) for r in cur.fetchall()]

#         cur.execute("SELECT name FROM locations ORDER BY name ASC")
#         locations_list = [r['name'] for r in cur.fetchall()]

#         receipt_id = request.args.get('id')
#         if receipt_id:
#             cur.execute("SELECT * FROM receipts WHERE id = ?", (receipt_id,))
#             row = cur.fetchone()
#             if row:
#                 receipt = dict(row)

#         if not receipt:
#             receipt_no = f"RCPT-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
#             cur.execute(
#                 "INSERT INTO receipts (receipt_no, created_at) VALUES (?, datetime('now'))",
#                 (receipt_no,)
#             )
#             conn.commit()
#             receipt = {'id': cur.lastrowid, 'receipt_no': receipt_no}
#     except Exception:
#         app.logger.exception("Failed to load receive page data")
#         receipt = receipt or {'id': ''}
#     finally:
#         conn.close()

#     return render_template(
#         'receive.html', items=items_list, locations=locations_list,
#         user=session.get('username'), receipt=receipt, csrf_token=generate_csrf()
#     )


# @app.route('/api/receive', methods=['POST'])
# def api_receive():
#     """
#     Confirms a GRN in one shot:
#       1. auto-generates grn_no / order_ref if not already set on the receipt
#       2. saves the received lines
#       3. updates item on_hand + writes stock_ledger entries
#       4. generates the GRN PDF
#       5. emails the PDF to the supplier (async, only if condition == 'good')
#     """
#     data = request.get_json(silent=True) or {}

#     supplier = (data.get('supplier') or '').strip()
#     supplier_email = (data.get('supplier_email') or '').strip()
#     po_ref = (data.get('po_ref') or '').strip()
#     condition = (data.get('condition') or 'good').strip().lower()
#     condition_notes = (data.get('condition_notes') or '').strip()
#     confirmed_by = (data.get('confirmed_by') or session.get('username') or 'web').strip()
#     lines = data.get('lines') or []

#     if not supplier:
#         return jsonify({'status': 'error', 'message': 'Supplier is required'}), 400
#     if not supplier_email:
#         return jsonify({'status': 'error', 'message': 'Supplier email is required'}), 400
#     if not lines or not any(l.get('sku') and float(l.get('qty', 0) or 0) > 0 for l in lines):
#         return jsonify({'status': 'error', 'message': 'At least one valid line is required'}), 400

#     conn = get_conn()
#     try:
#         cur = conn.cursor()

#         # find or create the receipt header
#         receipt_id = data.get('receipt_id') or None
#         if receipt_id:
#             cur.execute("SELECT * FROM receipts WHERE id = ?", (receipt_id,))
#             hdr = cur.fetchone()
#             if not hdr:
#                 receipt_id = None
#         if not receipt_id:
#             receipt_no = f"RCPT-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
#             cur.execute("INSERT INTO receipts (receipt_no, created_at) VALUES (?, datetime('now'))", (receipt_no,))
#             receipt_id = cur.lastrowid
#             cur.execute("SELECT * FROM receipts WHERE id = ?", (receipt_id,))
#             hdr = cur.fetchone()

#         grn_no = hdr['grn_no'] or generate_grn_no()
#         order_ref = hdr['order_ref'] or po_ref or generate_order_ref()

#         # insert lines + update stock
#         clean_lines = []
#         for ln in lines:
#             sku = (ln.get('sku') or '').strip()
#             try:
#                 qty = float(ln.get('qty') or 0)
#             except (TypeError, ValueError):
#                 qty = 0
#             if not sku or qty <= 0:
#                 continue

#             unit = (ln.get('unit') or '').strip() or None
#             location = (ln.get('location') or '').strip() or None
#             batch_id = (ln.get('batch') or ln.get('batch_id') or '').strip() or None
#             expiry = (ln.get('expiry') or '').strip() or None

#             cur.execute("""
#                 INSERT INTO receipt_lines (receipt_id, sku, qty, unit, location, batch_id, expiry)
#                 VALUES (?, ?, ?, ?, ?, ?, ?)
#             """, (receipt_id, sku, qty, unit, location, batch_id, expiry))

#             cur.execute("UPDATE items SET on_hand = COALESCE(on_hand,0) + ?, updated_at = datetime('now') WHERE sku = ?", (qty, sku))
#             if cur.rowcount == 0:
#                 cur.execute("""
#                     INSERT INTO items (sku, name, unit, category, on_hand, reorder_level, created_at, updated_at)
#                     VALUES (?, ?, ?, '', ?, NULL, datetime('now'), datetime('now'))
#                 """, (sku, sku, unit or 'pcs', qty))

#             cur.execute("""
#                 INSERT INTO stock_ledger (sku, item_sku, qty, txn_type, reference, batch_no, created_by, when_ts, notes)
#                 VALUES (?, ?, ?, 'RECEIVE', ?, ?, ?, datetime('now'), ?)
#             """, (sku, sku, qty, grn_no, batch_id, confirmed_by, f"GRN {grn_no}"))

#             clean_lines.append({'sku': sku, 'qty': qty, 'unit': unit, 'location': location, 'batch_id': batch_id})

#         # finalize header
#         cur.execute("""
#             UPDATE receipts
#             SET grn_no = ?, order_ref = ?, supplier = ?, supplier_email = ?, po_no = ?,
#                 condition_status = ?, condition_notes = ?, received_by = ?,
#                 confirmed = 1, confirmed_at = datetime('now')
#             WHERE id = ?
#         """, (grn_no, order_ref, supplier, supplier_email, po_ref,
#               condition, condition_notes, confirmed_by, receipt_id))
#         conn.commit()

#         # generate the PDF
#         pdf_path = None
#         try:
#             pdf_path = generate_pdf_for_receipt(
#                 grn_no, order_ref, supplier, supplier_email, condition, condition_notes, clean_lines
#             )
#             cur.execute("UPDATE receipts SET document_path = ? WHERE id = ?", (pdf_path, receipt_id))
#             conn.commit()
#         except Exception:
#             app.logger.exception("PDF generation failed for receipt %s", receipt_id)

#         # email the supplier — only when goods were confirmed in good condition
#         emailed = False
#         if pdf_path and condition == 'good':
#             send_receipt_email(supplier_email, grn_no, order_ref, pdf_path, receipt_id)
#             emailed = True

#         return jsonify({
#             'status': 'ok',
#             'receipt_id': receipt_id,
#             'grn_no': grn_no,
#             'order_ref': order_ref,
#             'pdf_generated': bool(pdf_path),
#             'email_queued': emailed,
#         }), 200

#     except Exception:
#         conn.rollback()
#         app.logger.exception("Failed to process GRN")
#         return jsonify({'status': 'error', 'message': 'Failed to process GRN'}), 500
#     finally:
#         conn.close()


# @app.route('/receive/<int:receipt_id>/pdf')
# def download_receipt_pdf(receipt_id):
#     conn = get_conn()
#     try:
#         cur = conn.cursor()
#         cur.execute("SELECT document_path, grn_no FROM receipts WHERE id = ?", (receipt_id,))
#         row = cur.fetchone()
#     finally:
#         conn.close()

#     if not row or not row['document_path'] or not os.path.exists(row['document_path']):
#         abort(404)

#     from flask import send_file
#     return send_file(row['document_path'], as_attachment=False,
#                       download_name=f"{row['grn_no'] or receipt_id}.pdf")


# @app.route('/receipt/<string:grn_no>')
# def print_receipt(grn_no):
#     """Printable HTML view of a GRN (opens window.print() automatically)."""
#     conn = get_conn()
#     try:
#         cur = conn.cursor()
#         cur.execute("SELECT * FROM receipts WHERE grn_no = ?", (grn_no,))
#         tx = cur.fetchone()
#         if not tx:
#             abort(404)
#         cur.execute("""
#             SELECT rl.sku, i.name AS name, rl.qty, rl.unit, rl.location, rl.batch_id, rl.expiry
#             FROM receipt_lines rl
#             LEFT JOIN items i ON i.sku = rl.sku
#             WHERE rl.receipt_id = ?
#         """, (tx['id'],))
#         lines = [dict(r) for r in cur.fetchall()]
#     finally:
#         conn.close()

#     return render_template('grn_print.html', tx=dict(tx), lines=lines)


# # -------------------------------------------------------------------
# # Drivers & Vehicles (used to populate the issue/delivery dropdowns)
# # -------------------------------------------------------------------
# @app.route('/api/drivers', methods=['GET'])
# def api_list_drivers():
#     conn = get_conn()
#     try:
#         cur = conn.cursor()
#         cur.execute("SELECT id, name, phone, license_no FROM drivers ORDER BY name ASC")
#         return jsonify({'status': 'ok', 'drivers': [dict(r) for r in cur.fetchall()]})
#     finally:
#         conn.close()


# @app.route('/api/drivers', methods=['POST'])
# def api_create_driver():
#     data = request.get_json(silent=True) or {}
#     name = (data.get('name') or '').strip()
#     phone = (data.get('phone') or '').strip()
#     license_no = (data.get('license_no') or '').strip()
#     if not name:
#         return jsonify({'status': 'error', 'message': 'Driver name is required'}), 400

#     conn = get_conn()
#     try:
#         cur = conn.cursor()
#         cur.execute("INSERT INTO drivers (name, phone, license_no, created_at) VALUES (?, ?, ?, datetime('now'))",
#                     (name, phone, license_no))
#         conn.commit()
#         return jsonify({'status': 'ok', 'driver': {'id': cur.lastrowid, 'name': name, 'phone': phone, 'license_no': license_no}}), 201
#     except Exception:
#         conn.rollback()
#         app.logger.exception("Failed to create driver")
#         return jsonify({'status': 'error', 'message': 'Failed to create driver'}), 500
#     finally:
#         conn.close()


# @app.route('/api/vehicles', methods=['GET'])
# def api_list_vehicles():
#     conn = get_conn()
#     try:
#         cur = conn.cursor()
#         cur.execute("SELECT id, reg_no, make_model, capacity FROM vehicles ORDER BY reg_no ASC")
#         return jsonify({'status': 'ok', 'vehicles': [dict(r) for r in cur.fetchall()]})
#     finally:
#         conn.close()


# @app.route('/api/vehicles', methods=['POST'])
# def api_create_vehicle():
#     data = request.get_json(silent=True) or {}
#     reg_no = (data.get('reg_no') or '').strip()
#     make_model = (data.get('make_model') or '').strip()
#     capacity = (data.get('capacity') or '').strip()
#     if not reg_no:
#         return jsonify({'status': 'error', 'message': 'Vehicle registration number is required'}), 400

#     conn = get_conn()
#     try:
#         cur = conn.cursor()
#         cur.execute("SELECT COUNT(1) AS cnt FROM vehicles WHERE reg_no = ?", (reg_no,))
#         if cur.fetchone()['cnt'] > 0:
#             return jsonify({'status': 'error', 'message': 'Vehicle already registered'}), 400

#         cur.execute("INSERT INTO vehicles (reg_no, make_model, capacity, created_at) VALUES (?, ?, ?, datetime('now'))",
#                     (reg_no, make_model, capacity))
#         conn.commit()
#         return jsonify({'status': 'ok', 'vehicle': {'id': cur.lastrowid, 'reg_no': reg_no, 'make_model': make_model, 'capacity': capacity}}), 201
#     except Exception:
#         conn.rollback()
#         app.logger.exception("Failed to create vehicle")
#         return jsonify({'status': 'error', 'message': 'Failed to create vehicle'}), 500
#     finally:
#         conn.close()


# # -------------------------------------------------------------------
# # Issue goods / Delivery Note — auto delivery/order no + PDF + optional email
# # -------------------------------------------------------------------
# @app.route('/issue')
# def issue():
#     conn = get_conn()
#     try:
#         cur = conn.cursor()
#         cur.execute("SELECT sku, name, unit FROM items ORDER BY name ASC")
#         items_list = [dict(r) for r in cur.fetchall()]
#         cur.execute("SELECT name FROM locations ORDER BY name ASC")
#         locations_list = [r['name'] for r in cur.fetchall()]
#         cur.execute("SELECT id, name, phone, license_no FROM drivers ORDER BY name ASC")
#         drivers_list = [dict(r) for r in cur.fetchall()]
#         cur.execute("SELECT id, reg_no, make_model, capacity FROM vehicles ORDER BY reg_no ASC")
#         vehicles_list = [dict(r) for r in cur.fetchall()]
#     except Exception:
#         app.logger.exception("Failed to load issue page data")
#         items_list, locations_list, drivers_list, vehicles_list = [], [], [], []
#     finally:
#         conn.close()

#     return render_template(
#         'issue.html', items=items_list, locations=locations_list,
#         drivers=drivers_list, vehicles=vehicles_list,
#         user=session.get('username'), csrf_token=generate_csrf()
#     )


# @app.route('/api/issue', methods=['POST'])
# def api_issue():
#     """
#     Confirms a delivery in one shot:
#       1. auto-generates delivery_no / order_ref
#       2. resolves the chosen driver/vehicle (or accepts free text)
#       3. saves the picked lines, decrements item on_hand, writes stock_ledger entries
#       4. generates the delivery note PDF (stored for later printing/download)
#       5. emails the PDF to the recipient if an email address was given
#     """
#     data = request.get_json(silent=True) or {}

#     recipient = (data.get('recipient') or '').strip()
#     recipient_phone = (data.get('recipient_phone') or '').strip()
#     recipient_email = (data.get('recipient_email') or '').strip()
#     address = (data.get('address') or '').strip()
#     remarks = (data.get('remarks') or '').strip()
#     order_ref_in = (data.get('order_ref') or '').strip()
#     driver_id = data.get('driver_id') or None
#     vehicle_id = data.get('vehicle_id') or None
#     # free-text fallbacks, used if no driver_id/vehicle_id was selected
#     driver_name = (data.get('driver_name') or '').strip()
#     driver_phone = (data.get('driver_phone') or '').strip()
#     vehicle_reg = (data.get('vehicle_reg') or '').strip()
#     lines = data.get('lines') or []

#     if not recipient:
#         return jsonify({'status': 'error', 'message': 'Recipient name is required'}), 400
#     if not lines or not any(l.get('sku') and float(l.get('qty', 0) or 0) > 0 for l in lines):
#         return jsonify({'status': 'error', 'message': 'At least one valid line is required'}), 400

#     conn = get_conn()
#     try:
#         cur = conn.cursor()

#         # resolve driver
#         if driver_id:
#             cur.execute("SELECT name, phone FROM drivers WHERE id = ?", (driver_id,))
#             d = cur.fetchone()
#             if d:
#                 driver_name, driver_phone = d['name'], d['phone']

#         # resolve vehicle
#         if vehicle_id:
#             cur.execute("SELECT reg_no FROM vehicles WHERE id = ?", (vehicle_id,))
#             v = cur.fetchone()
#             if v:
#                 vehicle_reg = v['reg_no']

#         delivery_no = generate_delivery_no()
#         order_ref = order_ref_in or generate_order_ref()

#         cur.execute("""
#             INSERT INTO deliveries (
#                 delivery_no, order_ref, recipient, recipient_phone, recipient_email,
#                 address, vehicle_id, vehicle_reg, driver_id, driver_name, driver_phone,
#                 remarks, created_by, confirmed, confirmed_at, created_at, updated_at
#             ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, datetime('now'), datetime('now'), datetime('now'))
#         """, (delivery_no, order_ref, recipient, recipient_phone, recipient_email,
#               address, vehicle_id, vehicle_reg, driver_id, driver_name, driver_phone,
#               remarks, session.get('username', 'web')))
#         delivery_id = cur.lastrowid

#         clean_lines = []
#         for ln in lines:
#             sku = (ln.get('sku') or '').strip()
#             try:
#                 qty = float(ln.get('qty') or 0)
#             except (TypeError, ValueError):
#                 qty = 0
#             if not sku or qty <= 0:
#                 continue
#             location = (ln.get('location') or '').strip() or None
#             batch_id = (ln.get('batch_id') or '').strip() or None

#             cur.execute("""
#                 INSERT INTO delivery_lines (delivery_id, sku, qty, location, batch_id)
#                 VALUES (?, ?, ?, ?, ?)
#             """, (delivery_id, sku, qty, location, batch_id))

#             cur.execute("""
#                 INSERT INTO stock_ledger (sku, item_sku, qty, txn_type, reference, batch_no, created_by, when_ts, notes)
#                 VALUES (?, ?, ?, 'ISSUE', ?, ?, ?, datetime('now'), ?)
#             """, (sku, sku, -abs(qty), delivery_no, batch_id, session.get('username', 'web'), f"Delivery {delivery_no}"))
#             cur.execute("UPDATE items SET on_hand = COALESCE(on_hand,0) - ?, updated_at = datetime('now') WHERE sku = ?", (abs(qty), sku))

#             clean_lines.append({'sku': sku, 'qty': qty, 'location': location, 'batch_id': batch_id})

#         conn.commit()

#         # generate the delivery note PDF
#         pdf_path = None
#         try:
#             pdf_path = generate_pdf_for_delivery(
#                 delivery_no, order_ref, recipient, recipient_phone, recipient_email,
#                 address, driver_name, driver_phone, vehicle_reg, remarks, clean_lines
#             )
#             cur.execute("UPDATE deliveries SET document_path = ? WHERE id = ?", (pdf_path, delivery_id))
#             conn.commit()
#         except Exception:
#             app.logger.exception("PDF generation failed for delivery %s", delivery_id)

#         emailed = False
#         if pdf_path and recipient_email:
#             send_delivery_email(recipient_email, delivery_no, order_ref, pdf_path, delivery_id)
#             emailed = True

#         return jsonify({
#             'status': 'ok',
#             'delivery_id': delivery_id,
#             'delivery_no': delivery_no,
#             'order_ref': order_ref,
#             'pdf_generated': bool(pdf_path),
#             'email_queued': emailed,
#         })
#     except Exception:
#         conn.rollback()
#         app.logger.exception("Failed to create issue")
#         return jsonify({'status': 'error', 'message': 'Failed to issue goods'}), 500
#     finally:
#         conn.close()


# @app.route('/issue/<int:delivery_id>/pdf')
# def download_delivery_pdf(delivery_id):
#     conn = get_conn()
#     try:
#         cur = conn.cursor()
#         cur.execute("SELECT document_path, delivery_no FROM deliveries WHERE id = ?", (delivery_id,))
#         row = cur.fetchone()
#     finally:
#         conn.close()

#     if not row or not row['document_path'] or not os.path.exists(row['document_path']):
#         abort(404)

#     from flask import send_file
#     return send_file(row['document_path'], as_attachment=False,
#                       download_name=f"{row['delivery_no'] or delivery_id}.pdf")


# @app.route('/delivery/<string:delivery_no>')
# def print_delivery_note(delivery_no):
#     """Printable HTML view of a delivery note (opens window.print() automatically)."""
#     conn = get_conn()
#     try:
#         cur = conn.cursor()
#         cur.execute("SELECT * FROM deliveries WHERE delivery_no = ?", (delivery_no,))
#         tx = cur.fetchone()
#         if not tx:
#             abort(404)
#         cur.execute("""
#             SELECT dl.sku, i.name AS name, dl.qty, dl.location, dl.batch_id
#             FROM delivery_lines dl
#             LEFT JOIN items i ON i.sku = dl.sku
#             WHERE dl.delivery_id = ?
#         """, (tx['id'],))
#         lines = [dict(r) for r in cur.fetchall()]
#     finally:
#         conn.close()

#     return render_template('delivery_note.html', tx=dict(tx), lines=lines)


# # -------------------------------------------------------------------
# # Ledger
# # -------------------------------------------------------------------
# @app.route('/ledger')
# def ledger():
#     conn = get_conn()
#     try:
#         cur = conn.cursor()
#         cur.execute("""
#             SELECT when_ts AS created_at, txn_type AS movement_type, sku, qty, reference, created_by AS username
#             FROM stock_ledger ORDER BY when_ts DESC LIMIT 200
#         """)
#         rows = [dict(r) for r in cur.fetchall()]
#     except Exception:
#         app.logger.exception("Failed to load ledger")
#         rows = []
#     finally:
#         conn.close()
#     return render_template('ledger.html', rows=rows, user=session.get('username'))


# # -------------------------------------------------------------------
# # Locations
# # -------------------------------------------------------------------
# @app.route('/locations')
# def locations():
#     conn = get_conn()
#     try:
#         cur = conn.cursor()
#         cur.execute("SELECT id, code, name, type, capacity, description FROM locations ORDER BY name ASC")
#         rows = [dict(r) for r in cur.fetchall()]
#     except Exception:
#         app.logger.exception("Failed to load locations")
#         rows = []
#     finally:
#         conn.close()
#     return render_template('locations.html', locations=rows, user=session.get('username'))


# @app.route('/api/locations', methods=['POST'])
# def api_create_location():
#     data = request.get_json(silent=True) or {}
#     code = (data.get('code') or '').strip()
#     name = (data.get('name') or '').strip()
#     loc_type = (data.get('type') or '').strip() or 'Storage'
#     try:
#         capacity = int(data.get('capacity') or 0)
#     except (TypeError, ValueError):
#         capacity = 0
#     description = (data.get('description') or '').strip()

#     if not name:
#         return jsonify({'status': 'error', 'message': 'Name is required'}), 400

#     conn = get_conn()
#     try:
#         cur = conn.cursor()
#         if code:
#             cur.execute("SELECT COUNT(1) AS cnt FROM locations WHERE code = ?", (code,))
#             if cur.fetchone()['cnt'] > 0:
#                 return jsonify({'status': 'error', 'message': 'Location code already exists'}), 400
#         else:
#             prefix = ''.join(c for c in loc_type.upper() if c.isalnum())[:4] or 'LOC'
#             code = f"{prefix}-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"

#         cur.execute("""
#             INSERT INTO locations (code, name, type, capacity, description, created_at, updated_at)
#             VALUES (?, ?, ?, ?, ?, datetime('now'), datetime('now'))
#         """, (code, name, loc_type, capacity, description))
#         conn.commit()

#         cur.execute("SELECT id, code, name, type, capacity, description FROM locations WHERE code = ?", (code,))
#         loc = dict(cur.fetchone())
#         return jsonify({'status': 'ok', 'location': loc}), 201
#     except Exception:
#         conn.rollback()
#         app.logger.exception("Failed to create location")
#         return jsonify({'status': 'error', 'message': 'Failed to create location'}), 500
#     finally:
#         conn.close()


# @app.route('/export_locations')
# def export_locations():
#     conn = get_conn()
#     try:
#         cur = conn.cursor()
#         cur.execute("SELECT code, name, type, capacity, description FROM locations ORDER BY name ASC")
#         rows = cur.fetchall()

#         si = io.StringIO()
#         writer = csv.writer(si)
#         writer.writerow(['Code', 'Name', 'Type', 'Capacity', 'Description'])
#         for r in rows:
#             writer.writerow([r['code'], r['name'], r['type'], r['capacity'], r['description']])

#         output = make_response(si.getvalue())
#         output.headers["Content-Disposition"] = "attachment; filename=locations.csv"
#         output.headers["Content-type"] = "text/csv"
#         return output
#     except Exception:
#         app.logger.exception("Failed to export locations")
#         return redirect(url_for('locations'))
#     finally:
#         conn.close()


# @app.route('/api/locations/<string:code>', methods=['PUT'])
# def api_update_location(code):
#     data = request.get_json(silent=True) or {}
#     new_code = (data.get('code') or '').strip()
#     name = (data.get('name') or '').strip()
#     loc_type = (data.get('type') or '').strip() or 'Storage'
#     try:
#         capacity = int(data.get('capacity') or 0)
#     except (TypeError, ValueError):
#         capacity = 0
#     description = (data.get('description') or '').strip()

#     if not name:
#         return jsonify({'status': 'error', 'message': 'Name is required'}), 400

#     conn = get_conn()
#     try:
#         cur = conn.cursor()
#         cur.execute("SELECT id FROM locations WHERE code = ?", (code,))
#         if not cur.fetchone():
#             return jsonify({'status': 'error', 'message': 'Location not found'}), 404

#         final_code = new_code or code
#         if new_code and new_code != code:
#             cur.execute("SELECT COUNT(1) AS cnt FROM locations WHERE code = ?", (new_code,))
#             if cur.fetchone()['cnt'] > 0:
#                 return jsonify({'status': 'error', 'message': 'New code already exists'}), 400

#         cur.execute("""
#             UPDATE locations SET code = ?, name = ?, type = ?, capacity = ?, description = ?, updated_at = datetime('now')
#             WHERE code = ?
#         """, (final_code, name, loc_type, capacity, description, code))
#         conn.commit()

#         cur.execute("SELECT id, code, name, type, capacity, description FROM locations WHERE code = ?", (final_code,))
#         loc = dict(cur.fetchone())
#         return jsonify({'status': 'ok', 'location': loc}), 200
#     except Exception:
#         conn.rollback()
#         app.logger.exception("Failed to update location")
#         return jsonify({'status': 'error', 'message': 'Failed to update location'}), 500
#     finally:
#         conn.close()


# @app.route('/api/locations/<string:code>', methods=['DELETE'])
# def api_delete_location(code):
#     conn = get_conn()
#     try:
#         cur = conn.cursor()
#         cur.execute("SELECT id FROM locations WHERE code = ?", (code,))
#         if not cur.fetchone():
#             return jsonify({'status': 'error', 'message': 'Location not found'}), 404

#         cur.execute("SELECT COUNT(1) AS cnt FROM stock_ledger WHERE location_id = (SELECT id FROM locations WHERE code = ?)", (code,))
#         if cur.fetchone()['cnt'] > 0:
#             return jsonify({'status': 'error', 'message': 'Cannot delete a location with stock movements.'}), 400

#         cur.execute("DELETE FROM locations WHERE code = ?", (code,))
#         conn.commit()
#         return jsonify({'status': 'ok'}), 200
#     except Exception:
#         conn.rollback()
#         app.logger.exception("Failed to delete location")
#         return jsonify({'status': 'error', 'message': 'Failed to delete location'}), 500
#     finally:
#         conn.close()


# # -------------------------------------------------------------------
# # Debug / seed (debug mode only)
# # -------------------------------------------------------------------
# @app.route('/debug_db')
# def debug_db():
#     if not app.debug:
#         abort(403)
#     conn = get_conn()
#     out = {'db_path': DB_PATH}
#     try:
#         cur = conn.cursor()
#         cur.execute("SELECT COUNT(*) AS cnt FROM items")
#         out['items_count'] = cur.fetchone()['cnt']
#         cur.execute("SELECT sku, name, unit, category, on_hand, reorder_level FROM items ORDER BY sku LIMIT 20")
#         out['sample_items'] = [dict(r) for r in cur.fetchall()]
#     except Exception as e:
#         out['error'] = str(e)
#     finally:
#         conn.close()
#     return app.response_class(response=json.dumps(out, default=str, indent=2), mimetype='application/json')


# if __name__ == '__main__':
#     app.run(host='0.0.0.0', port=int(os.getenv('PORT', 5000)), debug=True)















# import os
# import io
# import csv
# import json
# import logging
# import smtplib
# import threading
# from datetime import datetime
# from email.message import EmailMessage

# from flask import (
#     Flask, render_template, request, jsonify, redirect, url_for,
#     flash, session, make_response, current_app, abort
# )
# from dotenv import load_dotenv
# from flask_wtf import CSRFProtect
# from flask_wtf.csrf import CSRFError, generate_csrf
# from flask_session import Session

# from reportlab.lib.pagesizes import A4
# from reportlab.pdfgen import canvas

# import sqlite3

# # -------------------------------------------------------------------
# # App setup
# # -------------------------------------------------------------------
# load_dotenv()

# app = Flask(__name__, template_folder='templates', static_folder='static')
# app.logger.setLevel(logging.DEBUG)

# app.secret_key = os.getenv('FLASK_SECRET', 'devsecret')
# app.config.update({
#     'SESSION_TYPE': 'filesystem',
#     'SESSION_COOKIE_HTTPONLY': True,
#     'SESSION_COOKIE_SAMESITE': 'Lax',
#     'SESSION_COOKIE_SECURE': False,
# })

# Session(app)
# csrf = CSRFProtect(app)

# app.jinja_env.globals['generate_csrf'] = generate_csrf


# @app.context_processor
# def expose_helpers():
#     def endpoint_exists(name):
#         return name in current_app.view_functions
#     return {'endpoint_exists': endpoint_exists}


# @app.errorhandler(CSRFError)
# def handle_csrf(e):
#     app.logger.warning(
#         "CSRFError: %s; headers=%s; body=%s",
#         e.description, dict(request.headers), request.get_data(as_text=True)
#     )
#     return jsonify({'status': 'error', 'message': 'CSRF token missing or invalid'}), 400


# # -------------------------------------------------------------------
# # Config
# # -------------------------------------------------------------------
# DB_PATH = os.getenv('SQLITE_PATH', os.path.join(os.getcwd(), 'busia_wms.sqlite3'))
# LOW_STOCK_THRESHOLD = int(os.getenv('LOW_STOCK_THRESHOLD', 10))

# DEFAULT_UNITS = ['pcs', 'bag', 'bottle', 'pack', 'jerrycan']
# DEFAULT_CATEGORIES = ['Food', 'NFI', 'Health', 'Beverages', 'General']

# MAIL_HOST = os.getenv('BUSIA_SMTP_HOST') or os.getenv('MAIL_HOST')
# MAIL_PORT = int(os.getenv('BUSIA_SMTP_PORT', os.getenv('MAIL_PORT', 587)))
# MAIL_USER = os.getenv('BUSIA_SMTP_USER') or os.getenv('MAIL_USER')
# MAIL_PASS = os.getenv('BUSIA_SMTP_PASS') or os.getenv('MAIL_PASS')
# MAIL_FROM = os.getenv('BUSIA_EMAIL_FROM') or os.getenv('MAIL_FROM') or MAIL_USER or 'no-reply@example.com'
# MAIL_FROM_NAME = os.getenv('BUSIA_EMAIL_FROM_NAME', 'Busia WMS')
# MAIL_USE_TLS = os.getenv('BUSIA_SMTP_TLS', '1') not in ('0', 'false', 'False')

# BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# UPLOAD_DIR = os.path.join(BASE_DIR, 'generated_docs')
# os.makedirs(UPLOAD_DIR, exist_ok=True)


# # -------------------------------------------------------------------
# # DB helpers
# # -------------------------------------------------------------------
# def get_conn():
#     conn = sqlite3.connect(DB_PATH, detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES)
#     conn.row_factory = sqlite3.Row
#     conn.execute("PRAGMA foreign_keys = ON;")
#     return conn


# def init_db():
#     ddl = """
#     PRAGMA foreign_keys = ON;

#     CREATE TABLE IF NOT EXISTS sequences (
#       name TEXT PRIMARY KEY,
#       last INTEGER NOT NULL DEFAULT 0
#     );

#     CREATE TABLE IF NOT EXISTS items (
#       id INTEGER PRIMARY KEY AUTOINCREMENT,
#       sku TEXT NOT NULL UNIQUE,
#       name TEXT NOT NULL,
#       unit TEXT DEFAULT 'pcs',
#       category TEXT,
#       on_hand INTEGER DEFAULT 0,
#       reorder_level INTEGER DEFAULT 0,
#       price REAL DEFAULT NULL,
#       carton_price REAL DEFAULT NULL,
#       description TEXT,
#       created_at DATETIME DEFAULT (datetime('now')),
#       updated_at DATETIME DEFAULT (datetime('now'))
#     );

#     CREATE TABLE IF NOT EXISTS locations (
#       id INTEGER PRIMARY KEY AUTOINCREMENT,
#       code TEXT UNIQUE,
#       name TEXT NOT NULL,
#       type TEXT,
#       capacity INTEGER DEFAULT NULL,
#       description TEXT,
#       notes TEXT,
#       created_at DATETIME DEFAULT (datetime('now')),
#       updated_at DATETIME DEFAULT (datetime('now'))
#     );

#     CREATE TABLE IF NOT EXISTS suppliers (
#       id INTEGER PRIMARY KEY AUTOINCREMENT,
#       code TEXT UNIQUE,
#       name TEXT NOT NULL,
#       contact_name TEXT,
#       phone TEXT,
#       email TEXT,
#       address TEXT,
#       notes TEXT,
#       created_at DATETIME DEFAULT (datetime('now'))
#     );

#     CREATE TABLE IF NOT EXISTS users (
#       id INTEGER PRIMARY KEY AUTOINCREMENT,
#       username TEXT NOT NULL UNIQUE,
#       full_name TEXT,
#       email TEXT,
#       password_hash TEXT,
#       role TEXT DEFAULT 'user',
#       is_active INTEGER DEFAULT 1,
#       created_at DATETIME DEFAULT (datetime('now'))
#     );

#     -- Goods Receipt Note header. grn_no / order_ref are generated on confirm,
#     -- not on page load, so re-opening the page doesn't burn sequence numbers.
#     CREATE TABLE IF NOT EXISTS receipts (
#       id INTEGER PRIMARY KEY AUTOINCREMENT,
#       receipt_no TEXT NOT NULL UNIQUE,
#       grn_no TEXT DEFAULT NULL,
#       order_ref TEXT DEFAULT NULL,
#       supplier TEXT DEFAULT NULL,
#       supplier_email TEXT DEFAULT NULL,
#       po_no TEXT DEFAULT NULL,
#       condition_status TEXT DEFAULT NULL,
#       condition_notes TEXT DEFAULT NULL,
#       received_by TEXT DEFAULT NULL,
#       confirmed INTEGER DEFAULT 0,
#       confirmed_at DATETIME DEFAULT NULL,
#       document_path TEXT DEFAULT NULL,
#       email_sent INTEGER DEFAULT 0,
#       email_error TEXT DEFAULT NULL,
#       created_at DATETIME DEFAULT (datetime('now'))
#     );

#     CREATE TABLE IF NOT EXISTS receipt_lines (
#       id INTEGER PRIMARY KEY AUTOINCREMENT,
#       receipt_id INTEGER NOT NULL,
#       sku TEXT NOT NULL,
#       qty REAL NOT NULL,
#       unit TEXT DEFAULT 'pcs',
#       location TEXT DEFAULT NULL,
#       batch_id TEXT DEFAULT NULL,
#       expiry TEXT DEFAULT NULL,
#       created_at DATETIME DEFAULT (datetime('now')),
#       FOREIGN KEY (receipt_id) REFERENCES receipts(id) ON DELETE CASCADE
#     );

#     CREATE TABLE IF NOT EXISTS deliveries (
#       id INTEGER PRIMARY KEY AUTOINCREMENT,
#       delivery_no TEXT NOT NULL UNIQUE,
#       order_ref TEXT DEFAULT NULL,
#       recipient TEXT DEFAULT NULL,
#       address TEXT,
#       vehicle_reg TEXT,
#       driver_name TEXT,
#       remarks TEXT,
#       created_by TEXT DEFAULT NULL,
#       created_at DATETIME DEFAULT (datetime('now')),
#       updated_at DATETIME DEFAULT (datetime('now'))
#     );

#     CREATE TABLE IF NOT EXISTS stock_ledger (
#       id INTEGER PRIMARY KEY AUTOINCREMENT,
#       sku TEXT DEFAULT NULL,
#       item_sku TEXT DEFAULT NULL,
#       qty REAL NOT NULL,
#       txn_type TEXT NOT NULL,
#       location_id INTEGER DEFAULT NULL,
#       from_location_id INTEGER DEFAULT NULL,
#       to_location_id INTEGER DEFAULT NULL,
#       reference TEXT DEFAULT NULL,
#       batch_no TEXT DEFAULT NULL,
#       created_by TEXT DEFAULT NULL,
#       when_ts DATETIME DEFAULT (datetime('now')),
#       notes TEXT,
#       FOREIGN KEY (from_location_id) REFERENCES locations(id) ON DELETE SET NULL,
#       FOREIGN KEY (to_location_id) REFERENCES locations(id) ON DELETE SET NULL
#     );
#     """
#     conn = get_conn()
#     try:
#         conn.executescript(ddl)
#         conn.commit()
#     finally:
#         conn.close()


# init_db()


# # -------------------------------------------------------------------
# # Sequence-based GRN / Order Ref generators
# # -------------------------------------------------------------------
# def next_sequence(name):
#     """Atomically bump and return the next integer in a named sequence."""
#     conn = get_conn()
#     try:
#         cur = conn.cursor()
#         cur.execute("BEGIN IMMEDIATE")
#         cur.execute("SELECT last FROM sequences WHERE name = ?", (name,))
#         row = cur.fetchone()
#         if row is None:
#             val = 1
#             cur.execute("INSERT INTO sequences (name, last) VALUES (?, ?)", (name, val))
#         else:
#             val = int(row['last']) + 1
#             cur.execute("UPDATE sequences SET last = ? WHERE name = ?", (val, name))
#         conn.commit()
#         return val
#     finally:
#         conn.close()


# def generate_grn_no():
#     seq = next_sequence('grn')
#     return f"GRN-{datetime.utcnow().strftime('%Y%m%d')}-{seq:04d}"


# def generate_order_ref():
#     seq = next_sequence('order_ref')
#     return f"ORD-{datetime.utcnow().strftime('%Y%m%d')}-{seq:04d}"


# # -------------------------------------------------------------------
# # PDF generation
# # -------------------------------------------------------------------
# def generate_pdf_for_receipt(grn_no, order_ref, supplier, supplier_email, condition, condition_notes, lines):
#     filename = f"{grn_no}.pdf"
#     path = os.path.join(UPLOAD_DIR, filename)
#     c = canvas.Canvas(path, pagesize=A4)

#     c.setFont("Helvetica-Bold", 14)
#     c.drawString(40, 800, f"Goods Received Note {grn_no}")

#     c.setFont("Helvetica", 10)
#     c.drawString(40, 782, f"Date: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
#     c.drawString(40, 767, f"Order Ref: {order_ref or ''}")
#     c.drawString(40, 752, f"Supplier: {supplier or ''}")
#     c.drawString(40, 737, f"Supplier Email: {supplier_email or ''}")
#     c.drawString(40, 722, f"Condition: {condition or 'good'}")
#     if condition_notes:
#         c.drawString(40, 707, f"Notes: {condition_notes[:90]}")

#     y = 680
#     c.setFont("Helvetica-Bold", 10)
#     c.drawString(40, y, "SKU")
#     c.drawString(220, y, "Qty")
#     c.drawString(280, y, "Unit")
#     c.drawString(340, y, "Location")
#     c.drawString(440, y, "Batch")
#     c.setFont("Helvetica", 10)
#     y -= 18

#     for ln in lines:
#         c.drawString(40, y, str(ln.get('sku', '')))
#         c.drawString(220, y, str(ln.get('qty', '')))
#         c.drawString(280, y, str(ln.get('unit', '') or ''))
#         c.drawString(340, y, str(ln.get('location', '') or ''))
#         c.drawString(440, y, str(ln.get('batch_id', '') or ''))
#         y -= 16
#         if y < 60:
#             c.showPage()
#             y = 800

#     c.showPage()
#     c.save()
#     return path


# # -------------------------------------------------------------------
# # Email
# # -------------------------------------------------------------------
# def send_receipt_email(to_email, grn_no, order_ref, pdf_path, receipt_id):
#     """Send the receipt PDF to the supplier and record success/failure on the receipt row."""
#     def _send():
#         error = None
#         try:
#             if not MAIL_HOST:
#                 raise RuntimeError("Mail server is not configured (BUSIA_SMTP_HOST missing)")

#             msg = EmailMessage()
#             msg['Subject'] = f"Goods Received Confirmation {grn_no}"
#             msg['From'] = f"{MAIL_FROM_NAME} <{MAIL_FROM}>"
#             msg['To'] = to_email
#             msg.set_content(
#                 f"Dear Supplier,\n\n"
#                 f"We confirm receipt of goods for GRN {grn_no} (Order {order_ref}).\n"
#                 f"Please find the goods received note attached for your records.\n\n"
#                 f"Regards,\n{MAIL_FROM_NAME}"
#             )
#             with open(pdf_path, 'rb') as f:
#                 msg.add_attachment(
#                     f.read(), maintype='application', subtype='pdf',
#                     filename=os.path.basename(pdf_path)
#                 )

#             if MAIL_USE_TLS:
#                 s = smtplib.SMTP(MAIL_HOST, MAIL_PORT, timeout=20)
#                 s.starttls()
#             else:
#                 s = smtplib.SMTP_SSL(MAIL_HOST, MAIL_PORT, timeout=20)
#             if MAIL_USER:
#                 s.login(MAIL_USER, MAIL_PASS)
#             s.send_message(msg)
#             s.quit()
#         except Exception as exc:
#             error = str(exc)
#             app.logger.exception("Failed to email receipt %s to %s", grn_no, to_email)

#         conn = get_conn()
#         try:
#             conn.execute(
#                 "UPDATE receipts SET email_sent = ?, email_error = ? WHERE id = ?",
#                 (0 if error else 1, error, receipt_id)
#             )
#             conn.commit()
#         finally:
#             conn.close()

#     threading.Thread(target=_send, daemon=True).start()


# # -------------------------------------------------------------------
# # Dashboard / index
# # -------------------------------------------------------------------
# @app.route('/')
# def index():
#     conn = get_conn()
#     try:
#         cur = conn.cursor()
#         cur.execute("""
#             SELECT sku, name, unit, category,
#                    COALESCE(on_hand, 0) AS on_hand,
#                    COALESCE(reorder_level, ?) AS reorder_level
#             FROM items
#             ORDER BY name ASC
#         """, (LOW_STOCK_THRESHOLD,))
#         items = [dict(r) for r in cur.fetchall()]

#         cur.execute("""
#             SELECT when_ts AS `when`, sku, qty, txn_type
#             FROM stock_ledger
#             ORDER BY when_ts DESC
#             LIMIT 5
#         """)
#         recent_movements = [dict(r) for r in cur.fetchall()]
#     except Exception:
#         app.logger.exception("index() failed")
#         items, recent_movements = [], []
#     finally:
#         conn.close()

#     low_alerts = [it for it in items if it['on_hand'] <= it['reorder_level']]
#     stats = {
#         'active_skus': len(items),
#         'units_on_hand': sum(it['on_hand'] for it in items),
#         'recent_movements': len(recent_movements),
#     }

#     return render_template(
#         'index.html', items=items, user=session.get('username'),
#         low_alerts=low_alerts, recent_movements=recent_movements,
#         stats=stats, now=datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')
#     )


# @app.route('/api/dashboard', methods=['GET'])
# def api_dashboard():
#     conn = get_conn()
#     payload = {'active_skus': 0, 'units_on_hand': 0, 'low_stock_alerts': 0,
#                'reorder_alerts': [], 'recent_movements': []}
#     try:
#         cur = conn.cursor()
#         cur.execute("SELECT COUNT(DISTINCT sku) AS n FROM items")
#         payload['active_skus'] = cur.fetchone()['n']

#         cur.execute("SELECT COALESCE(SUM(on_hand),0) AS n FROM items")
#         payload['units_on_hand'] = cur.fetchone()['n']

#         cur.execute("""
#             SELECT sku, name, unit, category, on_hand,
#                    COALESCE(reorder_level, ?) AS reorder_level
#             FROM items
#             WHERE on_hand <= COALESCE(reorder_level, ?)
#             ORDER BY (COALESCE(reorder_level, ?) - on_hand) DESC
#             LIMIT 10
#         """, (LOW_STOCK_THRESHOLD, LOW_STOCK_THRESHOLD, LOW_STOCK_THRESHOLD))
#         alerts = [dict(r) for r in cur.fetchall()]
#         payload['reorder_alerts'] = alerts
#         payload['low_stock_alerts'] = len(alerts)

#         cur.execute("""
#             SELECT when_ts AS ts, sku, qty, txn_type, created_by AS user
#             FROM stock_ledger ORDER BY when_ts DESC LIMIT 10
#         """)
#         payload['recent_movements'] = [dict(r) for r in cur.fetchall()]
#         return jsonify(payload), 200
#     except Exception:
#         app.logger.exception("api_dashboard failed")
#         return jsonify({'status': 'error', 'message': 'Server error fetching dashboard',
#                          'reorder_alerts': [], 'recent_movements': []}), 500
#     finally:
#         conn.close()


# # -------------------------------------------------------------------
# # Items
# # -------------------------------------------------------------------
# def parse_reorder_level(val):
#     if val in (None, ''):
#         return None
#     try:
#         return int(val)
#     except (TypeError, ValueError):
#         return None


# @app.route('/items')
# def items():
#     selected_category = (request.args.get('category') or '').strip() or None
#     q = (request.args.get('q') or '').strip() or None

#     conn = get_conn()
#     try:
#         cur = conn.cursor()
#         sql = """
#             SELECT sku, name, unit, category, COALESCE(on_hand,0) AS on_hand,
#                    COALESCE(reorder_level, ?) AS reorder_level
#             FROM items
#         """
#         params = [LOW_STOCK_THRESHOLD]
#         clauses = []
#         if selected_category:
#             clauses.append("category = ?")
#             params.append(selected_category)
#         if q:
#             clauses.append("(sku LIKE ? OR name LIKE ?)")
#             params += [f"%{q}%", f"%{q}%"]
#         if clauses:
#             sql += " WHERE " + " AND ".join(clauses)
#         sql += " ORDER BY name ASC"

#         cur.execute(sql, params)
#         item_rows = [dict(r) for r in cur.fetchall()]

#         low_alerts = [it for it in item_rows if it['on_hand'] <= it['reorder_level']]

#         cur.execute("""
#             SELECT when_ts AS `when`, sku, qty, txn_type
#             FROM stock_ledger ORDER BY when_ts DESC LIMIT 10
#         """)
#         recent_movements = [dict(r) for r in cur.fetchall()]

#         cur.execute("""
#             SELECT category, COUNT(1) AS cnt FROM items
#             WHERE category IS NOT NULL AND category != ''
#             GROUP BY category ORDER BY category ASC
#         """)
#         cat_rows = cur.fetchall()
#         categories = [{'name': r['category'], 'count': r['cnt']} for r in cat_rows] \
#             or [{'name': c, 'count': ''} for c in DEFAULT_CATEGORIES]

#         stats = {
#             'active_skus': len(item_rows),
#             'units_on_hand': sum(it['on_hand'] for it in item_rows),
#             'recent_movements': len(recent_movements),
#         }
#     except Exception:
#         app.logger.exception("Failed to load items dashboard")
#         item_rows, low_alerts, recent_movements = [], [], []
#         categories = [{'name': c, 'count': ''} for c in DEFAULT_CATEGORIES]
#         stats = {'active_skus': 0, 'units_on_hand': 0, 'recent_movements': 0}
#     finally:
#         conn.close()

#     return render_template(
#         'items.html', items=item_rows, low_alerts=low_alerts, stats=stats,
#         recent_movements=recent_movements, units=DEFAULT_UNITS, categories=categories,
#         selected_category=selected_category, user=session.get('username')
#     )


# @app.route('/export_items')
# def export_items():
#     conn = get_conn()
#     try:
#         cur = conn.cursor()
#         cur.execute("""
#             SELECT sku, name, unit, category, COALESCE(on_hand,0) AS on_hand,
#                    COALESCE(reorder_level, ?) AS reorder_level
#             FROM items ORDER BY name ASC
#         """, (LOW_STOCK_THRESHOLD,))
#         rows = cur.fetchall()

#         si = io.StringIO()
#         writer = csv.writer(si)
#         writer.writerow(['SKU', 'Name', 'Unit', 'Category', 'On hand', 'Reorder level'])
#         for r in rows:
#             writer.writerow([r['sku'], r['name'], r['unit'], r['category'], r['on_hand'], r['reorder_level']])

#         output = make_response(si.getvalue())
#         output.headers["Content-Disposition"] = "attachment; filename=items.csv"
#         output.headers["Content-type"] = "text/csv"
#         return output
#     except Exception:
#         app.logger.exception("Failed to export items")
#         return redirect(url_for('items'))
#     finally:
#         conn.close()


# @app.route('/api/items', methods=['POST'])
# def api_create_item():
#     data = request.get_json(silent=True) or request.form.to_dict()
#     if not data:
#         return jsonify({'status': 'error', 'message': 'Invalid or empty payload'}), 400

#     sku = (data.get('sku') or '').strip()
#     name = (data.get('name') or '').strip()
#     unit = (data.get('unit') or '').strip() or 'pcs'
#     category = (data.get('category') or '').strip()
#     reorder_level = parse_reorder_level(data.get('reorder_level'))

#     if not sku or not name:
#         return jsonify({'status': 'error', 'message': 'SKU and name are required'}), 400

#     conn = get_conn()
#     try:
#         cur = conn.cursor()
#         cur.execute("SELECT COUNT(1) AS cnt FROM items WHERE sku = ?", (sku,))
#         if cur.fetchone()['cnt'] > 0:
#             return jsonify({'status': 'error', 'message': 'SKU already exists'}), 400

#         cur.execute("""
#             INSERT INTO items (sku, name, unit, category, on_hand, reorder_level, created_at, updated_at)
#             VALUES (?, ?, ?, ?, 0, ?, datetime('now'), datetime('now'))
#         """, (sku, name, unit, category, reorder_level))
#         conn.commit()

#         return jsonify({'status': 'ok', 'item': {
#             'sku': sku, 'name': name, 'unit': unit, 'category': category,
#             'on_hand': 0, 'reorder_level': reorder_level if reorder_level is not None else LOW_STOCK_THRESHOLD
#         }}), 200
#     except Exception:
#         conn.rollback()
#         app.logger.exception("Failed to create item")
#         return jsonify({'status': 'error', 'message': 'Server error creating item'}), 500
#     finally:
#         conn.close()


# @app.route('/create_item', methods=['POST'])
# def create_item():
#     sku = (request.form.get('sku') or '').strip()
#     name = (request.form.get('name') or '').strip()
#     unit = (request.form.get('unit_final') or request.form.get('unit_select') or 'pcs').strip()
#     category = (request.form.get('category_final') or request.form.get('category_select') or '').strip()
#     reorder_level = parse_reorder_level(request.form.get('reorder_level'))

#     if not sku or not name:
#         flash('SKU and Name are required.', 'danger')
#         return redirect(url_for('items'))

#     conn = get_conn()
#     try:
#         cur = conn.cursor()
#         cur.execute("SELECT COUNT(1) AS cnt FROM items WHERE sku = ?", (sku,))
#         if cur.fetchone()['cnt'] > 0:
#             flash('SKU already exists.', 'danger')
#             return redirect(url_for('items'))

#         cur.execute("""
#             INSERT INTO items (sku, name, unit, category, on_hand, reorder_level, created_at, updated_at)
#             VALUES (?, ?, ?, ?, 0, ?, datetime('now'), datetime('now'))
#         """, (sku, name, unit, category, reorder_level))
#         conn.commit()
#         flash(f'Item {sku} created.', 'success')
#     except Exception:
#         conn.rollback()
#         app.logger.exception("Failed to create item (form)")
#         flash('Server error while creating item.', 'danger')
#     finally:
#         conn.close()

#     return redirect(url_for('items'))


# @app.route('/item/<sku>')
# def item_detail(sku):
#     conn = get_conn()
#     try:
#         cur = conn.cursor()
#         cur.execute("""
#             SELECT sku, name, unit, category, COALESCE(on_hand,0) AS on_hand,
#                    COALESCE(reorder_level, ?) AS reorder_level
#             FROM items WHERE sku = ?
#         """, (LOW_STOCK_THRESHOLD, sku))
#         item = cur.fetchone()

#         cur.execute("""
#             SELECT when_ts AS `when`, qty, txn_type, location_id
#             FROM stock_ledger WHERE sku = ? ORDER BY when_ts DESC LIMIT 10
#         """, (sku,))
#         movements = cur.fetchall()
#     except Exception:
#         app.logger.exception("Failed to load item detail")
#         item, movements = None, []
#     finally:
#         conn.close()

#     return render_template('item_detail.html', sku=sku, item=item, movements=movements, user=session.get('username'))


# @app.route('/item/<sku>/adjust', methods=['GET', 'POST'])
# def adjust_stock(sku):
#     if request.method == 'GET':
#         return render_template('adjust_stock.html', sku=sku, user=session.get('username'))

#     data = request.get_json(silent=True) or {}
#     qty = float(data.get('qty', 0) or 0)
#     txn_type = data.get('txn_type', 'adjust')
#     location = data.get('location') or None
#     note = data.get('note') or None

#     if qty == 0:
#         return jsonify({'status': 'error', 'message': 'Quantity required'}), 400

#     conn = get_conn()
#     try:
#         cur = conn.cursor()
#         cur.execute("""
#             INSERT INTO stock_ledger (sku, item_sku, qty, txn_type, reference, notes, created_by, when_ts)
#             VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
#         """, (sku, sku, qty, txn_type, location, note, session.get('username', 'system')))
#         cur.execute("UPDATE items SET on_hand = COALESCE(on_hand,0) + ?, updated_at = datetime('now') WHERE sku = ?", (qty, sku))
#         conn.commit()
#         return jsonify({'status': 'ok'})
#     except Exception:
#         conn.rollback()
#         app.logger.exception("Failed to adjust stock")
#         return jsonify({'status': 'error', 'message': 'Failed to adjust stock'}), 500
#     finally:
#         conn.close()


# # -------------------------------------------------------------------
# # Receive goods (GRN) — auto GRN/order ref + PDF + email on confirm
# # -------------------------------------------------------------------
# @app.route('/receive', methods=['GET'])
# def receive():
#     conn = get_conn()
#     items_list, locations_list, receipt = [], [], None
#     try:
#         cur = conn.cursor()
#         cur.execute("SELECT sku, name, unit FROM items ORDER BY name ASC")
#         items_list = [dict(r) for r in cur.fetchall()]

#         cur.execute("SELECT name FROM locations ORDER BY name ASC")
#         locations_list = [r['name'] for r in cur.fetchall()]

#         receipt_id = request.args.get('id')
#         if receipt_id:
#             cur.execute("SELECT * FROM receipts WHERE id = ?", (receipt_id,))
#             row = cur.fetchone()
#             if row:
#                 receipt = dict(row)

#         if not receipt:
#             receipt_no = f"RCPT-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
#             cur.execute(
#                 "INSERT INTO receipts (receipt_no, created_at) VALUES (?, datetime('now'))",
#                 (receipt_no,)
#             )
#             conn.commit()
#             receipt = {'id': cur.lastrowid, 'receipt_no': receipt_no}
#     except Exception:
#         app.logger.exception("Failed to load receive page data")
#         receipt = receipt or {'id': ''}
#     finally:
#         conn.close()

#     return render_template(
#         'receive.html', items=items_list, locations=locations_list,
#         user=session.get('username'), receipt=receipt, csrf_token=generate_csrf()
#     )


# @app.route('/api/receive', methods=['POST'])
# def api_receive():
#     """
#     Confirms a GRN in one shot:
#       1. auto-generates grn_no / order_ref if not already set on the receipt
#       2. saves the received lines
#       3. updates item on_hand + writes stock_ledger entries
#       4. generates the GRN PDF
#       5. emails the PDF to the supplier (async, only if condition == 'good')
#     """
#     data = request.get_json(silent=True) or {}

#     supplier = (data.get('supplier') or '').strip()
#     supplier_email = (data.get('supplier_email') or '').strip()
#     po_ref = (data.get('po_ref') or '').strip()
#     condition = (data.get('condition') or 'good').strip().lower()
#     condition_notes = (data.get('condition_notes') or '').strip()
#     confirmed_by = (data.get('confirmed_by') or session.get('username') or 'web').strip()
#     lines = data.get('lines') or []

#     if not supplier:
#         return jsonify({'status': 'error', 'message': 'Supplier is required'}), 400
#     if not supplier_email:
#         return jsonify({'status': 'error', 'message': 'Supplier email is required'}), 400
#     if not lines or not any(l.get('sku') and float(l.get('qty', 0) or 0) > 0 for l in lines):
#         return jsonify({'status': 'error', 'message': 'At least one valid line is required'}), 400

#     conn = get_conn()
#     try:
#         cur = conn.cursor()

#         # find or create the receipt header
#         receipt_id = data.get('receipt_id') or None
#         if receipt_id:
#             cur.execute("SELECT * FROM receipts WHERE id = ?", (receipt_id,))
#             hdr = cur.fetchone()
#             if not hdr:
#                 receipt_id = None
#         if not receipt_id:
#             receipt_no = f"RCPT-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
#             cur.execute("INSERT INTO receipts (receipt_no, created_at) VALUES (?, datetime('now'))", (receipt_no,))
#             receipt_id = cur.lastrowid
#             cur.execute("SELECT * FROM receipts WHERE id = ?", (receipt_id,))
#             hdr = cur.fetchone()

#         grn_no = hdr['grn_no'] or generate_grn_no()
#         order_ref = hdr['order_ref'] or po_ref or generate_order_ref()

#         # insert lines + update stock
#         clean_lines = []
#         for ln in lines:
#             sku = (ln.get('sku') or '').strip()
#             try:
#                 qty = float(ln.get('qty') or 0)
#             except (TypeError, ValueError):
#                 qty = 0
#             if not sku or qty <= 0:
#                 continue

#             unit = (ln.get('unit') or '').strip() or None
#             location = (ln.get('location') or '').strip() or None
#             batch_id = (ln.get('batch') or ln.get('batch_id') or '').strip() or None
#             expiry = (ln.get('expiry') or '').strip() or None

#             cur.execute("""
#                 INSERT INTO receipt_lines (receipt_id, sku, qty, unit, location, batch_id, expiry)
#                 VALUES (?, ?, ?, ?, ?, ?, ?)
#             """, (receipt_id, sku, qty, unit, location, batch_id, expiry))

#             cur.execute("UPDATE items SET on_hand = COALESCE(on_hand,0) + ?, updated_at = datetime('now') WHERE sku = ?", (qty, sku))
#             if cur.rowcount == 0:
#                 cur.execute("""
#                     INSERT INTO items (sku, name, unit, category, on_hand, reorder_level, created_at, updated_at)
#                     VALUES (?, ?, ?, '', ?, NULL, datetime('now'), datetime('now'))
#                 """, (sku, sku, unit or 'pcs', qty))

#             cur.execute("""
#                 INSERT INTO stock_ledger (sku, item_sku, qty, txn_type, reference, batch_no, created_by, when_ts, notes)
#                 VALUES (?, ?, ?, 'RECEIVE', ?, ?, ?, datetime('now'), ?)
#             """, (sku, sku, qty, grn_no, batch_id, confirmed_by, f"GRN {grn_no}"))

#             clean_lines.append({'sku': sku, 'qty': qty, 'unit': unit, 'location': location, 'batch_id': batch_id})

#         # finalize header
#         cur.execute("""
#             UPDATE receipts
#             SET grn_no = ?, order_ref = ?, supplier = ?, supplier_email = ?, po_no = ?,
#                 condition_status = ?, condition_notes = ?, received_by = ?,
#                 confirmed = 1, confirmed_at = datetime('now')
#             WHERE id = ?
#         """, (grn_no, order_ref, supplier, supplier_email, po_ref,
#               condition, condition_notes, confirmed_by, receipt_id))
#         conn.commit()

#         # generate the PDF
#         pdf_path = None
#         try:
#             pdf_path = generate_pdf_for_receipt(
#                 grn_no, order_ref, supplier, supplier_email, condition, condition_notes, clean_lines
#             )
#             cur.execute("UPDATE receipts SET document_path = ? WHERE id = ?", (pdf_path, receipt_id))
#             conn.commit()
#         except Exception:
#             app.logger.exception("PDF generation failed for receipt %s", receipt_id)

#         # email the supplier — only when goods were confirmed in good condition
#         emailed = False
#         if pdf_path and condition == 'good':
#             send_receipt_email(supplier_email, grn_no, order_ref, pdf_path, receipt_id)
#             emailed = True

#         return jsonify({
#             'status': 'ok',
#             'receipt_id': receipt_id,
#             'grn_no': grn_no,
#             'order_ref': order_ref,
#             'pdf_generated': bool(pdf_path),
#             'email_queued': emailed,
#         }), 200

#     except Exception:
#         conn.rollback()
#         app.logger.exception("Failed to process GRN")
#         return jsonify({'status': 'error', 'message': 'Failed to process GRN'}), 500
#     finally:
#         conn.close()


# @app.route('/receive/<int:receipt_id>/pdf')
# def download_receipt_pdf(receipt_id):
#     conn = get_conn()
#     try:
#         cur = conn.cursor()
#         cur.execute("SELECT document_path, grn_no FROM receipts WHERE id = ?", (receipt_id,))
#         row = cur.fetchone()
#     finally:
#         conn.close()

#     if not row or not row['document_path'] or not os.path.exists(row['document_path']):
#         abort(404)

#     from flask import send_file
#     return send_file(row['document_path'], as_attachment=False,
#                       download_name=f"{row['grn_no'] or receipt_id}.pdf")




# @app.route('/api/receive/confirm', methods=['POST'])
# def api_receive_confirm():
#     """
#     Confirm an existing receipt (receipt_id) and generate the GRN PDF.
#     Expects JSON: { "receipt_id": 123, "confirmed_by": "username" }
#     """
#     app.logger.debug("Incoming /api/receive/confirm headers: %s", dict(request.headers))
#     app.logger.debug("Incoming /api/receive/confirm raw body: %s", request.get_data(as_text=True))

#     data = request.get_json(silent=True) or {}
#     receipt_id = data.get('receipt_id')
#     confirmed_by = data.get('confirmed_by') or session.get('username') or 'web'

#     if not receipt_id:
#         return jsonify({'status': 'error', 'message': 'receipt_id is required'}), 400

#     conn = None
#     cur = None
#     try:
#         conn = get_conn()
#         cur = conn.cursor()

#         # load receipt header
#         cur.execute("SELECT id, receipt_no, grn_no, order_ref, supplier_email, confirmed FROM receipts WHERE id = ?", (receipt_id,))
#         hdr = cur.fetchone()
#         if not hdr:
#             return jsonify({'status': 'error', 'message': 'Receipt not found'}), 404

#         # sqlite3.Row does not support .get(); use indexing with fallback
#         def row_val(row, key, default=None):
#             try:
#                 v = row[key]
#                 return v if v is not None else default
#             except Exception:
#                 return default

#         confirmed_flag = row_val(hdr, 'confirmed', 0)
#         if confirmed_flag:
#             return jsonify({'status': 'error', 'message': 'Receipt already confirmed'}), 400

#         # ensure grn_no exists
#         grn_no = row_val(hdr, 'grn_no')
#         if not grn_no:
#             grn_no = f"GRN-{datetime.utcnow().strftime('%Y%m%d')}-{int(datetime.utcnow().timestamp())}"
#             cur.execute("UPDATE receipts SET grn_no = ? WHERE id = ?", (grn_no, receipt_id))

#         # ensure order_ref exists
#         order_ref = row_val(hdr, 'order_ref')
#         if not order_ref:
#             order_ref = f"ORD-{int(datetime.utcnow().timestamp())}"
#             cur.execute("UPDATE receipts SET order_ref = ? WHERE id = ?", (order_ref, receipt_id))

#         # load lines from DB; fallback to any lines provided in request
#         try:
#             cur.execute("SELECT sku, qty, unit, location, batch_id, expiry FROM receipt_lines WHERE receipt_id = ?", (receipt_id,))
#             rows = cur.fetchall() or []
#             if rows:
#                 pdf_lines = [{'sku': r['sku'], 'qty': r['qty'], 'unit': r.get('unit') if isinstance(r, dict) else r['unit'],
#                               'location': r.get('location') if isinstance(r, dict) else r['location'],
#                               'batch_id': r.get('batch_id') if isinstance(r, dict) else r['batch_id'],
#                               'expiry': r.get('expiry') if isinstance(r, dict) else r['expiry']} for r in rows]
#             else:
#                 pdf_lines = data.get('lines') or []
#         except Exception:
#             pdf_lines = data.get('lines') or []

#         # mark confirmed
#         cur.execute("UPDATE receipts SET confirmed = 1, confirmed_at = datetime('now'), received_by = ? WHERE id = ?",
#                     (confirmed_by, receipt_id))
#         conn.commit()

#         # generate PDF from persisted rows (or fallback)
#         try:
#             supplier_email = row_val(hdr, 'supplier_email') or data.get('supplier_email') or None
#             pdf_path = generate_pdf_for_receipt(grn_no, order_ref, supplier_email, pdf_lines)
#             app.logger.info("PDF generated at: %s", pdf_path)
#             try:
#                 cur.execute("UPDATE receipts SET document_path = ? WHERE id = ?", (pdf_path, receipt_id))
#                 conn.commit()
#             except Exception:
#                 app.logger.exception("Failed to update receipts.document_path for %s", receipt_id)
#         except Exception:
#             app.logger.exception("PDF generation failed for receipt %s", receipt_id)
#             pdf_path = None

#         return jsonify({
#             'status': 'ok',
#             'receipt_id': receipt_id,
#             'grn_no': grn_no,
#             'order_ref': order_ref,
#             'document_path': pdf_path
#         }), 200

#     except Exception:
#         if conn:
#             try:
#                 conn.rollback()
#             except Exception:
#                 pass
#         app.logger.exception("Failed to confirm receipt %s", receipt_id)
#         return jsonify({'status': 'error', 'message': 'Failed to confirm receipt'}), 500
#     finally:
#         try:
#             if cur: cur.close()
#         except Exception:
#             pass
#         try:
#             if conn: conn.close()
#         except Exception:
#             pass

# # -------------------------------------------------------------------
# # Issue goods
# # -------------------------------------------------------------------
# @app.route('/issue')
# def issue():
#     conn = get_conn()
#     try:
#         cur = conn.cursor()
#         cur.execute("SELECT sku, name, unit FROM items ORDER BY name ASC")
#         items_list = [dict(r) for r in cur.fetchall()]
#         cur.execute("SELECT name FROM locations ORDER BY name ASC")
#         locations_list = [r['name'] for r in cur.fetchall()]
#     except Exception:
#         app.logger.exception("Failed to load issue page data")
#         items_list, locations_list = [], []
#     finally:
#         conn.close()

#     return render_template('issue.html', items=items_list, locations=locations_list, user=session.get('username'))


# @app.route('/api/issue', methods=['POST'])
# def api_issue():
#     data = request.get_json(silent=True) or {}
#     now = datetime.utcnow()
#     delivery_no = (data.get('delivery_no') or '').strip() or f"DN-{now.strftime('%Y%m%d%H%M%S')}"
#     order_ref = (data.get('order_ref') or '').strip() or f"PO-{now.strftime('%Y%m%d%H%M%S')}"
#     recipient = (data.get('recipient') or '').strip()
#     address = (data.get('address') or '').strip()
#     vehicle_reg = (data.get('vehicle_reg') or '').strip()
#     driver_name = (data.get('driver_name') or '').strip()
#     remarks = (data.get('remarks') or '').strip()
#     lines = data.get('lines') or []

#     if not recipient:
#         return jsonify({'status': 'error', 'message': 'Recipient name is required'}), 400
#     if not lines or not any(l.get('sku') and float(l.get('qty', 0) or 0) > 0 for l in lines):
#         return jsonify({'status': 'error', 'message': 'At least one valid line is required'}), 400

#     conn = get_conn()
#     try:
#         cur = conn.cursor()
#         cur.execute("""
#             INSERT INTO deliveries (delivery_no, order_ref, recipient, address, vehicle_reg, driver_name, remarks, created_by, created_at, updated_at)
#             VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))
#         """, (delivery_no, order_ref, recipient, address, vehicle_reg, driver_name, remarks, session.get('username', 'web')))

#         for ln in lines:
#             sku = (ln.get('sku') or '').strip()
#             try:
#                 qty = float(ln.get('qty') or 0)
#             except (TypeError, ValueError):
#                 qty = 0
#             if not sku or qty <= 0:
#                 continue
#             location = (ln.get('location') or '').strip() or None
#             batch_id = (ln.get('batch_id') or '').strip() or None

#             cur.execute("""
#                 INSERT INTO stock_ledger (sku, item_sku, qty, txn_type, reference, batch_no, created_by, when_ts)
#                 VALUES (?, ?, ?, 'ISSUE', ?, ?, ?, datetime('now'))
#             """, (sku, sku, -abs(qty), delivery_no, batch_id, session.get('username', 'web')))
#             cur.execute("UPDATE items SET on_hand = COALESCE(on_hand,0) - ?, updated_at = datetime('now') WHERE sku = ?", (abs(qty), sku))

#         conn.commit()
#         return jsonify({'status': 'ok', 'delivery_no': delivery_no, 'order_ref': order_ref})
#     except Exception:
#         conn.rollback()
#         app.logger.exception("Failed to create issue")
#         return jsonify({'status': 'error', 'message': 'Failed to issue goods'}), 500
#     finally:
#         conn.close()


# # -------------------------------------------------------------------
# # Ledger
# # -------------------------------------------------------------------
# @app.route('/ledger')
# def ledger():
#     conn = get_conn()
#     try:
#         cur = conn.cursor()
#         cur.execute("""
#             SELECT when_ts AS created_at, txn_type AS movement_type, sku, qty, reference, created_by AS username
#             FROM stock_ledger ORDER BY when_ts DESC LIMIT 200
#         """)
#         rows = [dict(r) for r in cur.fetchall()]
#     except Exception:
#         app.logger.exception("Failed to load ledger")
#         rows = []
#     finally:
#         conn.close()
#     return render_template('ledger.html', rows=rows, user=session.get('username'))


# # -------------------------------------------------------------------
# # Locations
# # -------------------------------------------------------------------
# @app.route('/locations')
# def locations():
#     conn = get_conn()
#     try:
#         cur = conn.cursor()
#         cur.execute("SELECT id, code, name, type, capacity, description FROM locations ORDER BY name ASC")
#         rows = [dict(r) for r in cur.fetchall()]
#     except Exception:
#         app.logger.exception("Failed to load locations")
#         rows = []
#     finally:
#         conn.close()
#     return render_template('locations.html', locations=rows, user=session.get('username'))


# @app.route('/api/locations', methods=['POST'])
# def api_create_location():
#     data = request.get_json(silent=True) or {}
#     code = (data.get('code') or '').strip()
#     name = (data.get('name') or '').strip()
#     loc_type = (data.get('type') or '').strip() or 'Storage'
#     try:
#         capacity = int(data.get('capacity') or 0)
#     except (TypeError, ValueError):
#         capacity = 0
#     description = (data.get('description') or '').strip()

#     if not name:
#         return jsonify({'status': 'error', 'message': 'Name is required'}), 400

#     conn = get_conn()
#     try:
#         cur = conn.cursor()
#         if code:
#             cur.execute("SELECT COUNT(1) AS cnt FROM locations WHERE code = ?", (code,))
#             if cur.fetchone()['cnt'] > 0:
#                 return jsonify({'status': 'error', 'message': 'Location code already exists'}), 400
#         else:
#             prefix = ''.join(c for c in loc_type.upper() if c.isalnum())[:4] or 'LOC'
#             code = f"{prefix}-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"

#         cur.execute("""
#             INSERT INTO locations (code, name, type, capacity, description, created_at, updated_at)
#             VALUES (?, ?, ?, ?, ?, datetime('now'), datetime('now'))
#         """, (code, name, loc_type, capacity, description))
#         conn.commit()

#         cur.execute("SELECT id, code, name, type, capacity, description FROM locations WHERE code = ?", (code,))
#         loc = dict(cur.fetchone())
#         return jsonify({'status': 'ok', 'location': loc}), 201
#     except Exception:
#         conn.rollback()
#         app.logger.exception("Failed to create location")
#         return jsonify({'status': 'error', 'message': 'Failed to create location'}), 500
#     finally:
#         conn.close()


# @app.route('/export_locations')
# def export_locations():
#     conn = get_conn()
#     try:
#         cur = conn.cursor()
#         cur.execute("SELECT code, name, type, capacity, description FROM locations ORDER BY name ASC")
#         rows = cur.fetchall()

#         si = io.StringIO()
#         writer = csv.writer(si)
#         writer.writerow(['Code', 'Name', 'Type', 'Capacity', 'Description'])
#         for r in rows:
#             writer.writerow([r['code'], r['name'], r['type'], r['capacity'], r['description']])

#         output = make_response(si.getvalue())
#         output.headers["Content-Disposition"] = "attachment; filename=locations.csv"
#         output.headers["Content-type"] = "text/csv"
#         return output
#     except Exception:
#         app.logger.exception("Failed to export locations")
#         return redirect(url_for('locations'))
#     finally:
#         conn.close()


# @app.route('/api/locations/<string:code>', methods=['PUT'])
# def api_update_location(code):
#     data = request.get_json(silent=True) or {}
#     new_code = (data.get('code') or '').strip()
#     name = (data.get('name') or '').strip()
#     loc_type = (data.get('type') or '').strip() or 'Storage'
#     try:
#         capacity = int(data.get('capacity') or 0)
#     except (TypeError, ValueError):
#         capacity = 0
#     description = (data.get('description') or '').strip()

#     if not name:
#         return jsonify({'status': 'error', 'message': 'Name is required'}), 400

#     conn = get_conn()
#     try:
#         cur = conn.cursor()
#         cur.execute("SELECT id FROM locations WHERE code = ?", (code,))
#         if not cur.fetchone():
#             return jsonify({'status': 'error', 'message': 'Location not found'}), 404

#         final_code = new_code or code
#         if new_code and new_code != code:
#             cur.execute("SELECT COUNT(1) AS cnt FROM locations WHERE code = ?", (new_code,))
#             if cur.fetchone()['cnt'] > 0:
#                 return jsonify({'status': 'error', 'message': 'New code already exists'}), 400

#         cur.execute("""
#             UPDATE locations SET code = ?, name = ?, type = ?, capacity = ?, description = ?, updated_at = datetime('now')
#             WHERE code = ?
#         """, (final_code, name, loc_type, capacity, description, code))
#         conn.commit()

#         cur.execute("SELECT id, code, name, type, capacity, description FROM locations WHERE code = ?", (final_code,))
#         loc = dict(cur.fetchone())
#         return jsonify({'status': 'ok', 'location': loc}), 200
#     except Exception:
#         conn.rollback()
#         app.logger.exception("Failed to update location")
#         return jsonify({'status': 'error', 'message': 'Failed to update location'}), 500
#     finally:
#         conn.close()


# @app.route('/api/locations/<string:code>', methods=['DELETE'])
# def api_delete_location(code):
#     conn = get_conn()
#     try:
#         cur = conn.cursor()
#         cur.execute("SELECT id FROM locations WHERE code = ?", (code,))
#         if not cur.fetchone():
#             return jsonify({'status': 'error', 'message': 'Location not found'}), 404

#         cur.execute("SELECT COUNT(1) AS cnt FROM stock_ledger WHERE location_id = (SELECT id FROM locations WHERE code = ?)", (code,))
#         if cur.fetchone()['cnt'] > 0:
#             return jsonify({'status': 'error', 'message': 'Cannot delete a location with stock movements.'}), 400

#         cur.execute("DELETE FROM locations WHERE code = ?", (code,))
#         conn.commit()
#         return jsonify({'status': 'ok'}), 200
#     except Exception:
#         conn.rollback()
#         app.logger.exception("Failed to delete location")
#         return jsonify({'status': 'error', 'message': 'Failed to delete location'}), 500
#     finally:
#         conn.close()


# # -------------------------------------------------------------------
# # Debug / seed (debug mode only)
# # -------------------------------------------------------------------
# @app.route('/debug_db')
# def debug_db():
#     if not app.debug:
#         abort(403)
#     conn = get_conn()
#     out = {'db_path': DB_PATH}
#     try:
#         cur = conn.cursor()
#         cur.execute("SELECT COUNT(*) AS cnt FROM items")
#         out['items_count'] = cur.fetchone()['cnt']
#         cur.execute("SELECT sku, name, unit, category, on_hand, reorder_level FROM items ORDER BY sku LIMIT 20")
#         out['sample_items'] = [dict(r) for r in cur.fetchall()]
#     except Exception as e:
#         out['error'] = str(e)
#     finally:
#         conn.close()
#     return app.response_class(response=json.dumps(out, default=str, indent=2), mimetype='application/json')


# if __name__ == '__main__':
#     app.run(host='0.0.0.0', port=int(os.getenv('PORT', 5000)), debug=True)












# # import os
# # import csv
# # import io
# # from datetime import datetime
# # from flask import Flask, render_template, request, jsonify, redirect, url_for, session, send_file, make_response, abort
# # from dotenv import load_dotenv
# # from werkzeug.security import generate_password_hash, check_password_hash
# # from functools import wraps
# # from flask_wtf import CSRFProtect
# # from flask_wtf.csrf import CSRFError, generate_csrf
# # from flask_session import Session
# # import smtplib
# # from email.message import EmailMessage
# # from weasyprint import HTML
# # import io as _io
# # from flask_wtf.csrf import generate_csrf
# # # existing imports remain
# # # put this once in app.py after app = Flask(...)
# # from flask import current_app
# # import logging



# # # dropdown defaults used by templates and routes
# # DEFAULT_UNITS = ['pcs', 'bag', 'bottle', 'pack', 'jerrycan']
# # DEFAULT_CATEGORIES = ['Food', 'NFI', 'Health', 'Beverages', 'General']


# # load_dotenv()
# # app = Flask(__name__, template_folder='templates', static_folder='static')

# # app.jinja_env.globals['generate_csrf'] = generate_csrf




# # app.logger.setLevel(logging.DEBUG)


# # @app.context_processor
# # def expose_helpers():
# #     def endpoint_exists(name):
# #         return name in current_app.view_functions
# #     return {'endpoint_exists': endpoint_exists}


# # # helper for templates: check if an endpoint exists
# # def endpoint_exists(name):
# #     return name in app.view_functions
# # app.jinja_env.globals['endpoint_exists'] = endpoint_exists
# # app.jinja_env.globals['generate_csrf'] = generate_csrf


# # # stable secret for CSRF/session
# # app.secret_key = os.getenv('FLASK_SECRET', 'devsecret')

# # # Session & cookie settings for local development
# # app.config.update({
# #     'SESSION_TYPE': 'filesystem',
# #     'SESSION_COOKIE_HTTPONLY': True,
# #     'SESSION_COOKIE_SAMESITE': 'Lax',
# #     'SESSION_COOKIE_SECURE': False
# # })

# # # initialize server-side session and CSRF protection
# # Session(app)
# # csrf = CSRFProtect(app)





# # @app.errorhandler(CSRFError)
# # def handle_csrf(e):
# #     app.logger.warning("CSRFError: %s; headers=%s; body=%s",
# #                        e.description, dict(request.headers), request.get_data(as_text=True))
# #     return jsonify({'status':'error','message':'CSRF token missing or invalid'}), 400

# # from flask_wtf.csrf import CSRFError

# # @app.errorhandler(CSRFError)
# # def handle_csrf(e):
# #     app.logger.warning("CSRFError: %s; headers=%s; body=%s",
# #                        e.description, dict(request.headers), request.get_data(as_text=True))
# #     return jsonify({'status':'error','message':'CSRF token missing or invalid'}), 400


# # DB_CONFIG = {
# #     'user': os.getenv('DB_USER','root'),
# #     'password': os.getenv('DB_PASS',''),
# #     'host': os.getenv('DB_HOST','127.0.0.1'),
# #     'database': os.getenv('DB_NAME','busia_wms'),
# #     'autocommit': False
# # }

# # MAIL_HOST = os.getenv('MAIL_HOST')
# # MAIL_PORT = int(os.getenv('MAIL_PORT', 587))
# # MAIL_USER = os.getenv('MAIL_USER')
# # MAIL_PASS = os.getenv('MAIL_PASS')
# # MAIL_FROM = os.getenv('MAIL_FROM', MAIL_USER)
# # LOW_STOCK_THRESHOLD = int(os.getenv('LOW_STOCK_THRESHOLD', 10))

# # # --- SQLite configuration and helpers (replace MySQL parts) ---
# # import sqlite3

# # # Path to sqlite DB file (use env or default to project file)
# # DB_PATH = os.getenv('SQLITE_PATH', os.path.join(os.getcwd(), 'busia_wms.sqlite3'))

# # def get_conn():
# #     """
# #     Return a sqlite3.Connection with row_factory set to sqlite3.Row.
# #     Use detect_types so DATETIME fields are returned as strings (or parsed if needed).
# #     """
# #     conn = sqlite3.connect(DB_PATH, detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES)
# #     conn.row_factory = sqlite3.Row
# #     # enable foreign keys
# #     conn.execute("PRAGMA foreign_keys = ON;")
# #     return conn

# # def init_db():
# #     """
# #     Create tables if they don't exist. Called once at startup.
# #     Note: stock_ledger.when_ts column added to match queries in your app.
# #     """
# #     ddl = """
# #     PRAGMA foreign_keys = ON;

# #     CREATE TABLE IF NOT EXISTS items (
# #       id INTEGER PRIMARY KEY AUTOINCREMENT,
# #       sku TEXT NOT NULL UNIQUE,
# #       name TEXT NOT NULL,
# #       unit TEXT DEFAULT 'pcs',
# #       category TEXT,
# #       reorder_level INTEGER DEFAULT 0,
# #       price REAL DEFAULT NULL,
# #       carton_price REAL DEFAULT NULL,
# #       description TEXT,
# #       created_at DATETIME DEFAULT (datetime('now')),
# #       updated_at DATETIME DEFAULT (datetime('now'))
# #     );

# #     CREATE TABLE IF NOT EXISTS locations (
# #       id INTEGER PRIMARY KEY AUTOINCREMENT,
# #       code TEXT UNIQUE,
# #       name TEXT NOT NULL,
# #       type TEXT,
# #       capacity INTEGER DEFAULT NULL,
# #       notes TEXT,
# #       created_at DATETIME DEFAULT (datetime('now')),
# #       updated_at DATETIME DEFAULT (datetime('now'))
# #     );

# #     CREATE TABLE IF NOT EXISTS suppliers (
# #       id INTEGER PRIMARY KEY AUTOINCREMENT,
# #       code TEXT UNIQUE,
# #       name TEXT NOT NULL,
# #       contact_name TEXT,
# #       phone TEXT,
# #       email TEXT,
# #       address TEXT,
# #       notes TEXT,
# #       created_at DATETIME DEFAULT (datetime('now'))
# #     );

# #     CREATE TABLE IF NOT EXISTS customers (
# #       id INTEGER PRIMARY KEY AUTOINCREMENT,
# #       code TEXT UNIQUE,
# #       name TEXT NOT NULL,
# #       contact_name TEXT,
# #       phone TEXT,
# #       email TEXT,
# #       address TEXT,
# #       notes TEXT,
# #       created_at DATETIME DEFAULT (datetime('now'))
# #     );

# #     CREATE TABLE IF NOT EXISTS users (
# #       id INTEGER PRIMARY KEY AUTOINCREMENT,
# #       username TEXT NOT NULL UNIQUE,
# #       full_name TEXT,
# #       email TEXT,
# #       password_hash TEXT,
# #       role TEXT DEFAULT 'user',
# #       is_active INTEGER DEFAULT 1,
# #       created_at DATETIME DEFAULT (datetime('now'))
# #     );

# #     CREATE TABLE IF NOT EXISTS batches (
# #       id INTEGER PRIMARY KEY AUTOINCREMENT,
# #       batch_no TEXT NOT NULL,
# #       item_sku TEXT NOT NULL,
# #       manufacture_date DATE DEFAULT NULL,
# #       expiry_date DATE DEFAULT NULL,
# #       qty INTEGER DEFAULT 0,
# #       created_at DATETIME DEFAULT (datetime('now')),
# #       updated_at DATETIME DEFAULT (datetime('now')),
# #       UNIQUE (batch_no, item_sku),
# #       FOREIGN KEY (item_sku) REFERENCES items(sku) ON DELETE CASCADE ON UPDATE CASCADE
# #     );

# #     CREATE TABLE IF NOT EXISTS receipts (
# #       id INTEGER PRIMARY KEY AUTOINCREMENT,
# #       receipt_no TEXT NOT NULL UNIQUE,
# #       supplier_id INTEGER DEFAULT NULL,
# #       po_no TEXT DEFAULT NULL,
# #       received_by TEXT DEFAULT NULL,
# #       received_at DATETIME DEFAULT (datetime('now')),
# #       notes TEXT,
# #       created_at DATETIME DEFAULT (datetime('now')),
# #       FOREIGN KEY (supplier_id) REFERENCES suppliers(id) ON DELETE SET NULL
# #     );

# #     CREATE TABLE IF NOT EXISTS receipt_lines (
# #       id INTEGER PRIMARY KEY AUTOINCREMENT,
# #       receipt_id INTEGER NOT NULL,
# #       item_sku TEXT NOT NULL,
# #       qty INTEGER NOT NULL,
# #       unit TEXT DEFAULT 'pcs',
# #       location_id INTEGER DEFAULT NULL,
# #       batch_no TEXT DEFAULT NULL,
# #       line_notes TEXT,
# #       created_at DATETIME DEFAULT (datetime('now')),
# #       FOREIGN KEY (receipt_id) REFERENCES receipts(id) ON DELETE CASCADE,
# #       FOREIGN KEY (item_sku) REFERENCES items(sku) ON DELETE RESTRICT,
# #       FOREIGN KEY (location_id) REFERENCES locations(id) ON DELETE SET NULL
# #     );

# #     CREATE TABLE IF NOT EXISTS deliveries (
# #       id INTEGER PRIMARY KEY AUTOINCREMENT,
# #       delivery_no TEXT NOT NULL UNIQUE,
# #       order_ref TEXT DEFAULT NULL,
# #       recipient TEXT DEFAULT NULL,
# #       address TEXT,
# #       vehicle_reg TEXT,
# #       driver_name TEXT,
# #       remarks TEXT,
# #       created_by TEXT DEFAULT NULL,
# #       created_at DATETIME DEFAULT (datetime('now')),
# #       updated_at DATETIME DEFAULT (datetime('now'))
# #     );

# #     CREATE TABLE IF NOT EXISTS delivery_lines (
# #       id INTEGER PRIMARY KEY AUTOINCREMENT,
# #       delivery_id INTEGER NOT NULL,
# #       item_sku TEXT NOT NULL,
# #       qty INTEGER NOT NULL,
# #       unit TEXT DEFAULT 'pcs',
# #       location_id INTEGER DEFAULT NULL,
# #       batch_no TEXT DEFAULT NULL,
# #       line_notes TEXT,
# #       created_at DATETIME DEFAULT (datetime('now')),
# #       FOREIGN KEY (delivery_id) REFERENCES deliveries(id) ON DELETE CASCADE,
# #       FOREIGN KEY (item_sku) REFERENCES items(sku) ON DELETE RESTRICT,
# #       FOREIGN KEY (location_id) REFERENCES locations(id) ON DELETE SET NULL
# #     );

# #     CREATE TABLE IF NOT EXISTS stock_ledger (
# #       id INTEGER PRIMARY KEY AUTOINCREMENT,
# #       sku TEXT DEFAULT NULL,
# #       item_sku TEXT DEFAULT NULL,
# #       qty INTEGER NOT NULL,
# #       txn_type TEXT NOT NULL,
# #       location_id INTEGER DEFAULT NULL,
# #       from_location_id INTEGER DEFAULT NULL,
# #       to_location_id INTEGER DEFAULT NULL,
# #       reference TEXT DEFAULT NULL,
# #       batch_no TEXT DEFAULT NULL,
# #       created_by TEXT DEFAULT NULL,
# #       when_ts DATETIME DEFAULT (datetime('now')),
# #       notes TEXT,
# #       FOREIGN KEY (from_location_id) REFERENCES locations(id) ON DELETE SET NULL,
# #       FOREIGN KEY (to_location_id) REFERENCES locations(id) ON DELETE SET NULL
# #     );

# #     CREATE TABLE IF NOT EXISTS inventory_counts (
# #       id INTEGER PRIMARY KEY AUTOINCREMENT,
# #       count_no TEXT NOT NULL UNIQUE,
# #       performed_by TEXT,
# #       location_id INTEGER DEFAULT NULL,
# #       notes TEXT,
# #       created_at DATETIME DEFAULT (datetime('now')),
# #       FOREIGN KEY (location_id) REFERENCES locations(id) ON DELETE SET NULL
# #     );

# #     CREATE TABLE IF NOT EXISTS inventory_count_lines (
# #       id INTEGER PRIMARY KEY AUTOINCREMENT,
# #       count_id INTEGER NOT NULL,
# #       item_sku TEXT NOT NULL,
# #       counted_qty INTEGER NOT NULL,
# #       expected_qty INTEGER DEFAULT NULL,
# #       variance INTEGER GENERATED ALWAYS AS (counted_qty - COALESCE(expected_qty,0)) VIRTUAL,
# #       notes TEXT,
# #       FOREIGN KEY (count_id) REFERENCES inventory_counts(id) ON DELETE CASCADE,
# #       FOREIGN KEY (item_sku) REFERENCES items(sku) ON DELETE RESTRICT
# #     );
# #     """
# #     conn = get_conn()
# #     try:
# #         conn.executescript(ddl)
# #         conn.commit()
# #     finally:
# #         conn.close()

# # # initialize DB at startup
# # init_db()

# # # --- upsert_items adapted for SQLite ---
# # def upsert_items(conn, rows):
# #     """
# #     Insert or update multiple items using SQLite ON CONFLICT (sku) DO UPDATE.
# #     Returns number of rows affected (sum of changes).
# #     """
# #     if not rows:
# #         return 0

# #     sql = """
# #     INSERT INTO items
# #       (sku, name, unit, category, reorder_level, price, carton_price, description, created_at, updated_at)
# #     VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))
# #     ON CONFLICT(sku) DO UPDATE SET
# #       name = excluded.name,
# #       unit = excluded.unit,
# #       category = excluded.category,
# #       reorder_level = excluded.reorder_level,
# #       price = excluded.price,
# #       carton_price = excluded.carton_price,
# #       description = excluded.description,
# #       updated_at = datetime('now')
# #     """
# #     params = []
# #     for r in rows:
# #         params.append((
# #             r.get('sku'),
# #             r.get('name'),
# #             r.get('unit') or 'pcs',
# #             r.get('category') or 'General',
# #             int(r.get('reorder_level') or 0),
# #             float(r.get('price')) if r.get('price') not in (None, '') else None,
# #             float(r.get('carton_price')) if r.get('carton_price') not in (None, '') else None,
# #             r.get('description') or ''
# #         ))

# #     cur = conn.cursor()
# #     try:
# #         cur.executemany(sql, params)
# #         conn.commit()
# #         # sqlite3.Cursor.rowcount is -1 for executemany in some versions; use total_changes
# #         return conn.total_changes
# #     except Exception:
# #         conn.rollback()
# #         raise
# #     finally:
# #         cur.close()




# # @app.route('/seed_items', methods=['POST'])
# # def seed_items():
# #     if not app.debug:
# #         return jsonify({'status': 'error', 'message': 'Seeding allowed only in debug mode'}), 403

# #     default_items = [
# #         {'sku':'SOAP-100G','name':'Soap 100g','unit':'bar','category':'Hygiene','reorder_level':50,'price':30.00,'carton_price':300.00,'description':'Generic soap bar','initial_qty':200},
# #         {'sku':'SOAP-100G','name':'Soap 1kg','unit':'bar','category':'Hygiene',
# #  'reorder_level':50,'price':150.00,'carton_price':1500.00,
# #  'description':'Generic 1kg soap bar','initial_qty':200},
# #         {'sku':'DOWNY-500ML','name':'Downy Fabric Softener 500ml','unit':'bottle','category':'Cleaning','reorder_level':20,'price':250.00,'carton_price':3000.00,'description':'Downy 500ml bottle (carton of 12)','initial_qty':60},
# #         {'sku':'DOWNY-1L','name':'Downy Fabric Softener 1L','unit':'bottle','category':'Cleaning','reorder_level':15,'price':450.00,'carton_price':5400.00,'description':'Downy 1L bottle (carton of 12)','initial_qty':40},
# #         {'sku':'WATER-500ML','name':'Bottled Water 500ml','unit':'bottle','category':'Beverages','reorder_level':100,'price':30.00,'carton_price':720.00,'description':'500ml bottled water (carton of 24)','initial_qty':240},
# #         {'sku':'WATER-1.5L','name':'Bottled Water 1.5L','unit':'bottle','category':'Beverages','reorder_level':60,'price':80.00,'carton_price':1920.00,'description':'1.5L bottled water (carton of 24)','initial_qty':120},

# #         # Cooking oil realistic KES prices
# #         {'sku':'COOK-OIL-20L','name':'Cooking Oil 20L','unit':'jerrycan','category':'Food','reorder_level':5,'price':4200.00,'carton_price':None,'description':'Cooking oil 20L jerrycan','initial_qty':10},
# #         {'sku':'COOK-OIL-10L','name':'Cooking Oil 10L','unit':'jerrycan','category':'Food','reorder_level':8,'price':2200.00,'carton_price':None,'description':'Cooking oil 10L jerrycan','initial_qty':12},
# #         {'sku':'COOK-OIL-5L','name':'Cooking Oil 5L','unit':'jerrycan','category':'Food','reorder_level':12,'price':1200.00,'carton_price':None,'description':'Cooking oil 5L jerrycan','initial_qty':20},
# #         {'sku':'COOK-OIL-2L','name':'Cooking Oil 2L','unit':'bottle','category':'Food','reorder_level':20,'price':350.00,'carton_price':None,'description':'Cooking oil 2L bottle','initial_qty':50},
# #         {'sku':'COOK-OIL-1L','name':'Cooking Oil 1L','unit':'bottle','category':'Food','reorder_level':40,'price':190.00,'carton_price':None,'description':'Cooking oil 1L bottle','initial_qty':80},

# #         # Staples and condiments
# #         {'sku':'SUGAR-1KG','name':'Sugar 1kg','unit':'kg','category':'Food','reorder_level':30,'price':150.00,'carton_price':None,'description':'Refined sugar 1kg pack','initial_qty':100},
# #         {'sku':'SALT-500G','name':'Salt 500g','unit':'pack','category':'Food','reorder_level':40,'price':40.00,'carton_price':None,'description':'Table salt 500g','initial_qty':150},
# #         {'sku':'SALT-1KG','name':'Salt 1kg','unit':'kg','category':'Food','reorder_level':20,'price':70.00,'carton_price':None,'description':'Table salt 1kg','initial_qty':80},

# #         # Seasoning and small groceries
# #         {'sku':'ROYCO-50G','name':'Royco Seasoning 50g','unit':'pack','category':'Food','reorder_level':50,'price':60.00,'carton_price':None,'description':'Royco seasoning small pack','initial_qty':200},

# #         # Snacks and biscuits
# #         {'sku':'BISCUITS-200G','name':'Biscuits 200g','unit':'pack','category':'Food','reorder_level':30,'price':120.00,'carton_price':None,'description':'Assorted biscuits 200g','initial_qty':120},
# #         {'sku':'KIDS-SNACKS-50G','name':'Kids Snacks 50g','unit':'pack','category':'Food','reorder_level':40,'price':50.00,'carton_price':None,'description':'Kids snack pack 50g','initial_qty':160},

# #         # Household smalls
# #         {'sku':'MATCHBOX-1','name':'Matchbox (single)','unit':'pc','category':'Household','reorder_level':200,'price':10.00,'carton_price':300.00,'description':'Single matchbox (carton of 60)','initial_qty':1000},
# #         {'sku':'CANDLES-1PC','name':'Candle (single)','unit':'pc','category':'Household','reorder_level':100,'price':15.00,'carton_price':600.00,'description':'Single candle (carton of 60)','initial_qty':400},

# #         # Baby care (Pampers) realistic KES prices
# #         {'sku':'PAMPERS-S','name':'Pampers Size S','unit':'pack','category':'Baby','reorder_level':20,'price':600.00,'carton_price':None,'description':'Pampers small pack','initial_qty':40},
# #         {'sku':'PAMPERS-M','name':'Pampers Size M','unit':'pack','category':'Baby','reorder_level':20,'price':750.00,'carton_price':None,'description':'Pampers medium pack','initial_qty':40},
# #         {'sku':'PAMPERS-L','name':'Pampers Size L','unit':'pack','category':'Baby','reorder_level':20,'price':900.00,'carton_price':None,'description':'Pampers large pack','initial_qty':40},
# #         {'sku':'PAMPERS-XL','name':'Pampers Size XL','unit':'pack','category':'Baby','reorder_level':15,'price':1100.00,'carton_price':None,'description':'Pampers XL pack','initial_qty':30}
# #     ]

# #     conn = None
# #     try:
# #         conn = get_conn()
# #         cur = conn.cursor()
# #         try:
# #             # 1) upsert items (ensures price fields are stored)
# #             inserted = upsert_items(conn, default_items)

# #             # 2) insert initial stock_ledger entries with price info in notes
# #             ledger_sql = """
# #                 INSERT INTO stock_ledger
# #                   (sku, item_sku, qty, txn_type, location_id, reference, batch_no, created_by, when_ts, notes)
# #                 VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), ?)
# #             """
# #             ledger_params = []
# #             for it in default_items:
# #                 sku = it['sku']
# #                 qty = int(it.get('initial_qty', int(it.get('reorder_level', 10)) * 2))
# #                 unit_price = float(it.get('price')) if it.get('price') not in (None, '') else 0.0
# #                 total_value = round(unit_price * qty, 2)
# #                 notes = f"unit_price={unit_price:.2f}; total_value={total_value:.2f}; seeded_by=seed"
# #                 ledger_params.append((sku, sku, qty, 'INITIAL', None, 'seed', None, 'seed', notes))

# #             cur.executemany(ledger_sql, ledger_params)
# #             conn.commit()
# #             return jsonify({
# #                 'status': 'ok',
# #                 'message': f'Seeded {len(default_items)} items and created {len(ledger_params)} initial ledger entries'
# #             })
# #         except Exception:
# #             conn.rollback()
# #             raise
# #         finally:
# #             cur.close()
# #     except Exception:
# #         if conn:
# #             conn.rollback()
# #         app.logger.exception("Failed to seed items and ledger")
# #         return jsonify({'status': 'error', 'message': 'Failed to seed items and ledger'}), 500
# #     finally:
# #         if conn:
# #             conn.close()


# # # Temporary debug route — add to app.py, restart Flask, then open /debug_db
# # @app.route('/debug_db')
# # def debug_db():
# #     import json
# #     conn = None
# #     cur = None
# #     out = {
# #         'db_path': None,
# #         'items_count': None,
# #         'sample_items': [],
# #         'ledger_totals_sample': []
# #     }
# #     try:
# #         out['db_path'] = DB_PATH if 'DB_PATH' in globals() else os.getenv('SQLITE_PATH', None)

# #         conn = get_conn()
# #         cur = conn.cursor(dictionary=True)

# #         # items count
# #         cur.execute("SELECT COUNT(*) AS cnt FROM items")
# #         row = cur.fetchone()
# #         out['items_count'] = row.get('cnt', 0) if row else 0

# #         # sample items (first 20)
# #         cur.execute("SELECT sku, name, COALESCE(is_seeded,0) AS is_seeded, unit, category, reorder_level FROM items ORDER BY sku LIMIT 20")
# #         out['sample_items'] = cur.fetchall() or []

# #         # ledger totals grouped by item_sku and sku (show top 20)
# #         cur.execute("""
# #             SELECT COALESCE(item_sku, sku) AS key_sku, SUM(qty) AS on_hand
# #             FROM stock_ledger
# #             GROUP BY COALESCE(item_sku, sku)
# #             ORDER BY on_hand DESC
# #             LIMIT 20
# #         """)
# #         out['ledger_totals_sample'] = cur.fetchall() or []

# #     except Exception as e:
# #         out['error'] = str(e)
# #     finally:
# #         try:
# #             if cur:
# #                 cur.close()
# #             if conn:
# #                 conn.close()
# #         except:
# #             pass

# #     return app.response_class(response=json.dumps(out, default=str, indent=2), mimetype='application/json')


# # # --- add to app.py (imports near top) ---
# # from datetime import datetime
# # from flask import jsonify, request


# # import traceback



# # # Add near your other imports
# # from datetime import datetime
# # from flask import jsonify

# # # Robust dashboard API
# # @app.route('/api/dashboard', methods=['GET'])
# # def api_dashboard():
# #     conn = None
# #     cur = None
# #     # default payload values
# #     payload = {
# #         'active_skus': 0,
# #         'units_on_hand': 0,
# #         'low_stock_alerts': 0,
# #         'reorder_alerts': [],
# #         'recent_movements': []
# #     }
# #     try:
# #         conn = get_conn()
# #         try:
# #             cur = conn.cursor(dictionary=True)
# #             dict_cursor = True
# #         except Exception:
# #             cur = conn.cursor()
# #             dict_cursor = False

# #         # Active SKUs
# #         try:
# #             cur.execute("SELECT COUNT(DISTINCT sku) AS active_skus FROM items")
# #             row = cur.fetchone()
# #             payload['active_skus'] = int((row['active_skus'] if dict_cursor else row[0]) if row else 0)
# #         except Exception as e:
# #             app.logger.exception("Failed active_skus query: %s", e)

# #         # Units on hand (sum from ledger)
# #         try:
# #             cur.execute("SELECT COALESCE(SUM(qty),0) AS units_on_hand FROM stock_ledger")
# #             row = cur.fetchone()
# #             payload['units_on_hand'] = int((row['units_on_hand'] if dict_cursor else row[0]) if row else 0)
# #         except Exception as e:
# #             app.logger.exception("Failed units_on_hand query: %s", e)

# #         # Low stock alerts (safe per-item computation)
# #         try:
# #             cur.execute("""
# #                 SELECT COUNT(1) AS low_stock_alerts
# #                 FROM (
# #                   SELECT i.sku, COALESCE(SUM(sl.qty),0) AS on_hand, COALESCE(i.reorder_level, ?) AS reorder_level
# #                   FROM items i
# #                   LEFT JOIN stock_ledger sl ON sl.sku = i.sku
# #                   GROUP BY i.sku, i.reorder_level
# #                 ) t
# #                 WHERE t.reorder_level IS NOT NULL AND t.on_hand <= t.reorder_level
# #             """, (LOW_STOCK_THRESHOLD,))
# #             row = cur.fetchone()
# #             payload['low_stock_alerts'] = int((row['low_stock_alerts'] if dict_cursor else row[0]) if row else 0)
# #         except Exception as e:
# #             app.logger.exception("Failed low_stock_alerts query: %s", e)

# #         # Reorder alerts details (limit 10)
# #         try:
# #             cur.execute("""
# #                 SELECT i.sku, i.name, i.unit, i.category, COALESCE(SUM(sl.qty),0) AS on_hand, COALESCE(i.reorder_level, ?) AS reorder_level
# #                 FROM items i
# #                 LEFT JOIN stock_ledger sl ON sl.sku = i.sku
# #                 GROUP BY i.sku, i.name, i.unit, i.category, i.reorder_level
# #                 HAVING reorder_level IS NOT NULL AND on_hand <= reorder_level
# #                 ORDER BY (reorder_level - on_hand) DESC
# #                 LIMIT 10
# #             """, (LOW_STOCK_THRESHOLD,))
# #             rows = cur.fetchall() or []
# #             alerts = []
# #             for r in rows:
# #                 if dict_cursor:
# #                     alerts.append({
# #                         'sku': r.get('sku'),
# #                         'name': r.get('name'),
# #                         'unit': r.get('unit'),
# #                         'category': r.get('category'),
# #                         'on_hand': int(r.get('on_hand') or 0),
# #                         'reorder_level': int(r.get('reorder_level') or 0)
# #                     })
# #                 else:
# #                     alerts.append({
# #                         'sku': r[0], 'name': r[1], 'unit': r[2],
# #                         'category': r[3], 'on_hand': int(r[4] or 0), 'reorder_level': int(r[5] or 0)
# #                     })
# #             payload['reorder_alerts'] = alerts
# #         except Exception as e:
# #             app.logger.exception("Failed reorder_alerts query: %s", e)
# #             payload['reorder_alerts'] = []

# #         # Recent movements (limit 10)
# #         try:
# #             cur.execute("""
# #                 SELECT when_ts AS ts, sku, qty, txn_type, user
# #                 FROM stock_ledger
# #                 ORDER BY when_ts DESC
# #                 LIMIT 10
# #             """)
# #             rows = cur.fetchall() or []
# #             moves = []
# #             for r in rows:
# #                 if dict_cursor:
# #                     moves.append({
# #                         'sku': r.get('sku'),
# #                         'qty': int(r.get('qty') or 0),
# #                         'type': r.get('txn_type'),
# #                         'user': r.get('user'),
# #                         'ts': str(r.get('ts'))
# #                     })
# #                 else:
# #                     moves.append({
# #                         'sku': r[1], 'qty': int(r[2] or 0), 'type': r[3], 'user': r[4], 'ts': str(r[0])
# #                     })
# #             payload['recent_movements'] = moves
# #         except Exception as e:
# #             app.logger.exception("Failed recent_movements query: %s", e)
# #             payload['recent_movements'] = []

# #         return jsonify(payload), 200

# #     except Exception as exc:
# #         app.logger.exception("api_dashboard top-level exception: %s", exc)
# #         # return safe error with no null lists
# #         return jsonify({'status': 'error', 'message': 'Server error fetching dashboard', 'reorder_alerts': [], 'recent_movements': []}), 500
# #     finally:
# #         try:
# #             if cur: cur.close()
# #         except Exception:
# #             pass
# #         try:
# #             if conn: conn.close()
# #         except Exception:
# #             pass


# # # Replace index() with ledger-based on_hand computation for initial render
# # @app.route('/')
# # def index():
# #     conn = None
# #     cur = None
# #     items = []
# #     recent_movements = []
# #     try:
# #         conn = get_conn()
# #         try:
# #             cur = conn.cursor(dictionary=True)
# #             dict_cursor = True
# #         except Exception:
# #             cur = conn.cursor()
# #             dict_cursor = False

# #         # Items: compute on_hand from stock_ledger
# #         try:
# #             cur.execute("""
# #                 SELECT
# #                   i.sku,
# #                   i.name,
# #                   i.unit,
# #                   i.category,
# #                   COALESCE(SUM(sl.qty), 0) AS on_hand,
# #                   COALESCE(i.reorder_level, %s) AS reorder_level
# #                 FROM items i
# #                 LEFT JOIN stock_ledger sl ON sl.sku = i.sku
# #                 GROUP BY i.sku, i.name, i.unit, i.category, i.reorder_level
# #                 ORDER BY i.name ASC
# #             """, (LOW_STOCK_THRESHOLD,))
# #             items = cur.fetchall() or []
# #         except Exception:
# #             app.logger.exception("Failed items query in index()")
# #             items = []

# #         # Recent movements for initial render
# #         try:
# #             cur.execute("""
# #                 SELECT when_ts AS `when`, sku, qty, txn_type
# #                 FROM stock_ledger
# #                 ORDER BY when_ts DESC
# #                 LIMIT 5
# #             """)
# #             recent_movements = cur.fetchall() or []
# #         except Exception:
# #             app.logger.exception("Failed recent_movements query in index()")
# #             recent_movements = []

# #     except Exception:
# #         app.logger.exception("index() top-level exception")
# #     finally:
# #         if cur:
# #             cur.close()
# #         if conn:
# #             conn.close()

# #     low_alerts = [it for it in items if int(it.get('on_hand', 0)) <= int(it.get('reorder_level', LOW_STOCK_THRESHOLD))]

# #     stats = {
# #         'active_skus': len(items),
# #         'units_on_hand': sum(int(it.get('on_hand', 0)) for it in items),
# #         'recent_movements': len(recent_movements)
# #     }

# #     return render_template(
# #         'index.html',
# #         items=items,
# #         user=session.get('username'),
# #         low_alerts=low_alerts,
# #         recent_movements=recent_movements,
# #         stats=stats,
# #         now=datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')
# #     )



# # # -------------------------
# # # Items page + API + export
# # # -------------------------


# # # --- Add near top of app.py (after imports) ---
# # from flask import (
# #     Flask, render_template, request, jsonify, redirect, url_for,
# #     flash, session, make_response, current_app
# # )
# # import io, csv, math
# # import logging
# # app.logger.setLevel(logging.DEBUG)



# # # Defaults used by templates
# # DEFAULT_UNITS = ['pcs', 'bag', 'bottle', 'pack', 'jerrycan']
# # DEFAULT_CATEGORIES = ['Food', 'NFI', 'Health', 'Beverages', 'General']

# # # Context helper exposed to Jinja templates
# # @app.context_processor
# # def expose_helpers():
# #     def endpoint_exists(name):
# #         return name in current_app.view_functions
# #     return {'endpoint_exists': endpoint_exists}

# # # Utility to parse reorder level safely
# # def parse_reorder_level(val):
# #     if val is None:
# #         return None
# #     s = str(val).strip()
# #     if s == '':
# #         return None
# #     try:
# #         return int(s)
# #     except ValueError:
# #         return None

# # # -------------------------
# # # Items dashboard (with category counts)
# # # -------------------------
# # @app.route('/items')
# # def items():
# #     conn = None
# #     cur = None
# #     items = []
# #     low_alerts = []
# #     recent_movements = []
# #     stats = {'active_skus': 0, 'units_on_hand': 0, 'recent_movements': 0}
# #     categories = []

# #     # read filters
# #     selected_category = (request.args.get('category') or '').strip() or None
# #     q = (request.args.get('q') or '').strip() or None

# #     try:
# #         conn = get_conn()
# #         # try dictionary cursor, fallback to normal cursor
# #         try:
# #             cur = conn.cursor(dictionary=True)
# #             dict_cursor = True
# #         except Exception:
# #             cur = conn.cursor()
# #             dict_cursor = False

# #         # Build base SQL with optional search and category filter
# #         base_sql = """
# #             SELECT
# #               i.sku,
# #               i.name,
# #               i.unit,
# #               i.category,
# #               COALESCE(i.reorder_level, %s) AS reorder_level,
# #               COALESCE(SUM(sl.qty), 0) AS on_hand
# #             FROM items i
# #             LEFT JOIN stock_ledger sl ON sl.sku = i.sku
# #         """
# #         params = [LOW_STOCK_THRESHOLD]

# #         where_clauses = []
# #         if selected_category:
# #             where_clauses.append("i.category = %s")
# #             params.append(selected_category)
# #         if q:
# #             # search by sku or name (case-insensitive for most DBs)
# #             where_clauses.append("(i.sku LIKE %s OR i.name LIKE %s)")
# #             like_q = f"%{q}%"
# #             params.extend([like_q, like_q])

# #         if where_clauses:
# #             base_sql += " WHERE " + " AND ".join(where_clauses)

# #         base_sql += """
# #             GROUP BY i.sku, i.name, i.unit, i.category, i.reorder_level
# #             ORDER BY i.name ASC
# #         """

# #         # Try execute with MySQL-style (%s) then sqlite (?) fallback
# #         executed = False
# #         try:
# #             cur.execute(base_sql, tuple(params))
# #             executed = True
# #         except Exception:
# #             try:
# #                 sqlite_sql = base_sql.replace('%s', '?')
# #                 cur.execute(sqlite_sql, tuple(params))
# #                 executed = True
# #             except Exception:
# #                 # last resort: interpolate LOW_STOCK_THRESHOLD only (safe if int)
# #                 try:
# #                     fallback_sql = base_sql.replace('%s', str(int(LOW_STOCK_THRESHOLD)))
# #                     cur.execute(fallback_sql)
# #                     executed = True
# #                 except Exception:
# #                     executed = False

# #         if not executed:
# #             raise RuntimeError("Failed to execute items query with available DB driver.")

# #         raw_rows = cur.fetchall() or []
# #         items = []
# #         for r in raw_rows:
# #             if dict_cursor or hasattr(r, 'keys'):
# #                 row = {k: r[k] for k in r.keys()}
# #             else:
# #                 cols = [c[0] for c in cur.description] if cur.description else []
# #                 row = {cols[i]: r[i] for i in range(len(cols))}
# #             # normalize
# #             row['on_hand'] = int(row.get('on_hand') or 0)
# #             rl = row.get('reorder_level')
# #             row['reorder_level'] = int(rl) if rl is not None and str(rl).strip() != '' else None
# #             items.append(row)

# #         # compute stats and low alerts
# #         stats['active_skus'] = len(items)
# #         stats['units_on_hand'] = sum(it.get('on_hand', 0) for it in items)
# #         low_alerts = [it for it in items if (it.get('reorder_level') is not None and it.get('on_hand', 0) <= it.get('reorder_level'))]

# #         # recent movements (10 latest)
# #         try:
# #             cur.execute("""
# #                 SELECT when_ts AS `when`, sku, qty, txn_type
# #                 FROM stock_ledger
# #                 ORDER BY when_ts DESC
# #                 LIMIT 10
# #             """)
# #             mv = cur.fetchall() or []
# #             recent_movements = []
# #             for r in mv:
# #                 if dict_cursor or hasattr(r, 'keys'):
# #                     recent_movements.append({'when': r.get('when'), 'sku': r.get('sku'), 'qty': r.get('qty'), 'txn_type': r.get('txn_type')})
# #                 else:
# #                     recent_movements.append({'when': r[0], 'sku': r[1], 'qty': r[2], 'txn_type': r[3]})
# #             stats['recent_movements'] = len(recent_movements)
# #         except Exception:
# #             recent_movements = []
# #             stats['recent_movements'] = 0

# #         # categories with counts (preferred: dynamic)
# #         try:
# #             # MySQL-style
# #             try:
# #                 cur.execute("SELECT category, COUNT(1) AS cnt FROM items WHERE category IS NOT NULL GROUP BY category ORDER BY category ASC")
# #             except Exception:
# #                 # sqlite fallback
# #                 cur.execute("SELECT category, COUNT(1) AS cnt FROM items WHERE category IS NOT NULL GROUP BY category ORDER BY category ASC")
# #             cat_rows = cur.fetchall() or []
# #             categories = []
# #             for r in cat_rows:
# #                 if dict_cursor or hasattr(r, 'keys'):
# #                     name = r.get('category')
# #                     cnt = r.get('cnt') or 0
# #                 else:
# #                     name = r[0] if len(r) > 0 else None
# #                     cnt = r[1] if len(r) > 1 else 0
# #                 if name:
# #                     categories.append({'name': name, 'count': int(cnt)})
# #             if not categories:
# #                 # fallback to defaults
# #                 categories = [{'name': c, 'count': ''} for c in DEFAULT_CATEGORIES]
# #         except Exception:
# #             categories = [{'name': c, 'count': ''} for c in DEFAULT_CATEGORIES]

# #     except Exception:
# #         app.logger.exception("Failed to load items dashboard")
# #         items = []
# #         low_alerts = []
# #         recent_movements = []
# #         categories = [{'name': c, 'count': ''} for c in DEFAULT_CATEGORIES]
# #     finally:
# #         try:
# #             if cur: cur.close()
# #         except: pass
# #         try:
# #             if conn: conn.close()
# #         except: pass

# #     return render_template('items.html',
# #                            items=items,
# #                            low_alerts=low_alerts,
# #                            stats=stats,
# #                            recent_movements=recent_movements,
# #                            units=DEFAULT_UNITS,
# #                            categories=categories,
# #                            selected_category=selected_category,
# #                            user=session.get('username'))

# # # -------------------------
# # # Export items CSV
# # # -------------------------
# # @app.route('/export_items')
# # def export_items():
# #     conn = None
# #     cur = None
# #     try:
# #         conn = get_conn()
# #         try:
# #             cur = conn.cursor(dictionary=True)
# #         except Exception:
# #             cur = conn.cursor()

# #         cur.execute("""
# #             SELECT
# #               i.sku,
# #               i.name,
# #               i.unit,
# #               i.category,
# #               COALESCE(i.reorder_level, %s) AS reorder_level,
# #               COALESCE(SUM(sl.qty), 0) AS on_hand
# #             FROM items i
# #             LEFT JOIN stock_ledger sl ON sl.sku = i.sku
# #             GROUP BY i.sku, i.name, i.unit, i.category, i.reorder_level
# #             ORDER BY i.name ASC
# #         """, (LOW_STOCK_THRESHOLD,))
# #         rows = cur.fetchall() or []

# #         si = io.StringIO()
# #         writer = csv.writer(si)
# #         writer.writerow(['SKU', 'Name', 'Unit', 'Category', 'On hand', 'Reorder level'])
# #         for r in rows:
# #             if hasattr(r, 'keys'):
# #                 sku = r.get('sku'); name = r.get('name'); unit = r.get('unit'); cat = r.get('category')
# #                 on_hand = r.get('on_hand', 0); rl = r.get('reorder_level')
# #             else:
# #                 cols = [c[0] for c in cur.description]
# #                 mapping = {cols[i]: r[i] for i in range(len(cols))}
# #                 sku = mapping.get('sku'); name = mapping.get('name'); unit = mapping.get('unit'); cat = mapping.get('category')
# #                 on_hand = mapping.get('on_hand', 0); rl = mapping.get('reorder_level')
# #             writer.writerow([sku, name, unit, cat, on_hand, rl])

# #         output = make_response(si.getvalue())
# #         output.headers["Content-Disposition"] = "attachment; filename=items.csv"
# #         output.headers["Content-type"] = "text/csv"
# #         return output
# #     except Exception:
# #         app.logger.exception("Failed to export items")
# #         return redirect(url_for('items'))
# #     finally:
# #         if cur: cur.close()
# #         if conn: conn.close()


# # @app.route('/api/items', methods=['POST'])
# # def api_create_item():
# #     """
# #     Robust handler for POST /api/items:
# #     - Logs request data inside request context
# #     - Accepts JSON (falls back to form)
# #     - Validates fields and parses reorder_level safely
# #     - Checks duplicate SKU (works with MySQL dict cursor and sqlite3.Row)
# #     - Inserts item with MySQL/SQLite placeholder fallbacks
# #     """
# #     # Debug (only inside request context)
# #     app.logger.debug("api_create_item: raw body: %r", request.get_data())
# #     app.logger.debug("api_create_item: parsed json: %r", request.get_json(silent=True))
# #     app.logger.debug("api_create_item: headers: %r", dict(request.headers))

# #     # Parse JSON or fallback to form
# #     data = request.get_json(silent=True)
# #     if data is None:
# #         form = request.form.to_dict()
# #         if form:
# #             data = form
# #             app.logger.debug("api_create_item: using form fallback: %r", data)
# #         else:
# #             return jsonify({'status': 'error', 'message': 'Invalid JSON payload or empty body'}), 400

# #     # Normalize fields
# #     sku = (data.get('sku') or '').strip()
# #     name = (data.get('name') or '').strip()
# #     unit = (data.get('unit') or '').strip() or 'pcs'
# #     category = (data.get('category') or '').strip()
# #     try:
# #         rl_raw = data.get('reorder_level')
# #         reorder_level = None if rl_raw in (None, '') else int(rl_raw)
# #     except Exception:
# #         return jsonify({'status': 'error', 'message': 'Reorder level must be an integer or omitted'}), 400

# #     if not sku or not name:
# #         return jsonify({'status': 'error', 'message': 'SKU and name are required'}), 400

# #     conn = None
# #     cur = None
# #     try:
# #         conn = get_conn()
# #         # Try to get a dict-style cursor; fall back to default cursor
# #         try:
# #             cur = conn.cursor(dictionary=True)
# #             dict_cursor = True
# #         except Exception:
# #             cur = conn.cursor()
# #             dict_cursor = False

# #         # Duplicate check (parameterized; try MySQL style then sqlite style)
# #         executed = False
# #         try:
# #             cur.execute("SELECT COUNT(1) AS cnt FROM items WHERE sku = %s", (sku,))
# #             executed = True
# #         except Exception:
# #             try:
# #                 cur.execute("SELECT COUNT(1) AS cnt FROM items WHERE sku = ?", (sku,))
# #                 executed = True
# #             except Exception:
# #                 # last resort: use sqlite-style with parameter (safer than string interpolation)
# #                 cur.execute("SELECT COUNT(1) AS cnt FROM items WHERE sku = ?", (sku,))
# #                 executed = True

# #         row = cur.fetchone()
# #         cnt = 0
# #         if row:
# #             # sqlite3.Row supports mapping access by key or index but not .get()
# #             try:
# #                 cnt = row['cnt']            # works for dict cursor and sqlite3.Row
# #             except Exception:
# #                 try:
# #                     cnt = row[0]           # fallback to index access
# #                 except Exception:
# #                     cnt = 0

# #         try:
# #             if int(cnt) > 0:
# #                 return jsonify({'status': 'error', 'message': 'SKU already exists'}), 400
# #         except Exception:
# #             # defensive: treat non-int as zero
# #             cnt = 0

# #         # Insert item (MySQL then sqlite fallback)
# #         insert_sql = """
# #             INSERT INTO items (sku, name, unit, category, reorder_level, created_at)
# #             VALUES (%s, %s, %s, %s, %s, NOW())
# #         """
# #         params = (sku, name, unit, category, reorder_level)
# #         inserted = False
# #         try:
# #             cur.execute(insert_sql, params)
# #             inserted = True
# #         except Exception:
# #             try:
# #                 insert_sql_sqlite = insert_sql.replace('%s', '?').replace('NOW()', "datetime('now')")
# #                 cur.execute(insert_sql_sqlite, params)
# #                 inserted = True
# #             except Exception as e:
# #                 app.logger.debug("api_create_item: insert fallback failed: %s", e)
# #                 inserted = False

# #         if not inserted:
# #             conn.rollback()
# #             return jsonify({'status': 'error', 'message': 'Database insert failed'}), 500

# #         conn.commit()

# #         item = {
# #             'sku': sku,
# #             'name': name,
# #             'unit': unit,
# #             'category': category,
# #             'on_hand': 0,
# #             'reorder_level': reorder_level if reorder_level is not None else LOW_STOCK_THRESHOLD
# #         }
# #         return jsonify({'status': 'ok', 'item': item}), 200

# #     except Exception as exc:
# #         if conn:
# #             try:
# #                 conn.rollback()
# #             except Exception:
# #                 pass
# #         app.logger.exception("Failed to create item (api_create_item): %s", exc)
# #         return jsonify({'status': 'error', 'message': 'Server error while creating item. Check server logs.'}), 500
# #     finally:
# #         try:
# #             if cur: cur.close()
# #         except Exception:
# #             pass
# #         try:
# #             if conn: conn.close()
# #         except Exception:
# #             pass

# # # -------------------------
# # # Non-AJAX fallback: create_item (form POST)
# # # -------------------------
# # @app.route('/create_item', methods=['POST'])
# # def create_item():
# #     sku = (request.form.get('sku') or '').strip()
# #     name = (request.form.get('name') or '').strip()
# #     unit = (request.form.get('unit_final') or request.form.get('unit_other') or request.form.get('unit_select') or 'pcs').strip()
# #     category = (request.form.get('category_final') or request.form.get('category_other') or request.form.get('category_select') or '').strip()
# #     reorder_level = parse_reorder_level(request.form.get('reorder_level'))

# #     if not sku or not name:
# #         flash('SKU and Name are required.', 'danger')
# #         return redirect(url_for('items'))

# #     conn = None
# #     cur = None
# #     try:
# #         conn = get_conn()
# #         cur = conn.cursor()
# #         cur.execute("SELECT COUNT(1) FROM items WHERE sku = %s", (sku,))
# #         if cur.fetchone()[0] > 0:
# #             flash('SKU already exists.', 'danger')
# #             return redirect(url_for('items'))

# #         cur.execute("""
# #             INSERT INTO items (sku, name, unit, category, reorder_level, created_at)
# #             VALUES (%s, %s, %s, %s, %s, NOW())
# #         """, (sku, name, unit, category, reorder_level))
# #         conn.commit()
# #         flash(f'Item {sku} created.', 'success')
# #     except Exception:
# #         if conn: conn.rollback()
# #         app.logger.exception("Failed to create item (create_item)")
# #         flash('Server error while creating item.', 'danger')
# #     finally:
# #         if cur: cur.close()
# #         if conn: conn.close()

# #     return redirect(url_for('items'))

# # # -------------------------
# # # Item detail (unchanged)
# # # -------------------------
# # @app.route('/item/<sku>')
# # def item_detail(sku):
# #     conn = None
# #     cur = None
# #     item = None
# #     movements = []
# #     try:
# #         conn = get_conn()
# #         try:
# #             cur = conn.cursor(dictionary=True)
# #         except Exception:
# #             cur = conn.cursor()

# #         cur.execute("""
# #             SELECT sku, name, unit, category, COALESCE(reorder_level, %s) AS reorder_level
# #             FROM items WHERE sku = %s
# #         """, (LOW_STOCK_THRESHOLD, sku))
# #         item = cur.fetchone()

# #         cur.execute("""
# #             SELECT when_ts AS `when`, qty, txn_type, location_id
# #             FROM stock_ledger
# #             WHERE sku = %s
# #             ORDER BY when_ts DESC
# #             LIMIT 10
# #         """, (sku,))
# #         movements = cur.fetchall() or []
# #     except Exception:
# #         app.logger.exception("Failed to load item detail")
# #     finally:
# #         if cur: cur.close()
# #         if conn: conn.close()

# #     return render_template('item_detail.html', sku=sku, item=item, movements=movements, user=session.get('username'))



# # @app.route('/item/<sku>/adjust', methods=['GET','POST'])
# # def adjust_stock(sku):
# #     if request.method == 'GET':
# #         return render_template('adjust_stock.html', sku=sku, user=session.get('username'))
# #     # POST: apply adjustment
# #     data = request.get_json(silent=True) or {}
# #     qty = int(data.get('qty', 0))
# #     txn_type = data.get('txn_type', 'adjust')
# #     location = data.get('location') or None
# #     note = data.get('note') or None

# #     if qty == 0:
# #         return jsonify({'status':'error','message':'Quantity required'}), 400

# #     conn = None
# #     cur = None
# #     try:
# #         conn = get_conn()
# #         cur = conn.cursor()
# #         cur.execute("""
# #             INSERT INTO stock_ledger (sku, qty, txn_type, location_id, reference, created_at)
# #             VALUES (%s, %s, %s, %s, %s, NOW())
# #         """, (sku, qty, txn_type, location, note))
# #         conn.commit()
# #         return jsonify({'status':'ok'})
# #     except Exception:
# #         if conn:
# #             conn.rollback()
# #         app.logger.exception("Failed to adjust stock")
# #         return jsonify({'status':'error','message':'Failed to adjust stock'}), 500
# #     finally:
# #         if cur:
# #             cur.close()
# #         if conn:
# #             conn.close()



# # # --- add near top of app.py (merge with existing imports) ---
# # import os
# # import threading
# # import sqlite3
# # from datetime import datetime
# # from flask import request, jsonify, render_template, session, current_app
# # from flask_wtf.csrf import generate_csrf

# # # reportlab for PDF generation (install with pip if missing)
# # from reportlab.lib.pagesizes import A4
# # from reportlab.pdfgen import canvas

# # # ensure generated_docs exists and is absolute
# # BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# # UPLOAD_DIR = os.path.join(BASE_DIR, 'generated_docs')
# # os.makedirs(UPLOAD_DIR, exist_ok=True)
# # # --- minimal PDF helper ---
# # def generate_pdf_for_receipt(grn_no, order_ref, supplier_email, lines):
# #     filename = f"{grn_no}.pdf"
# #     path = os.path.join(UPLOAD_DIR, filename)
# #     c = canvas.Canvas(path, pagesize=A4)
# #     c.setFont("Helvetica-Bold", 14)
# #     c.drawString(40, 800, f"Goods Received Note {grn_no}")
# #     c.setFont("Helvetica", 10)
# #     c.drawString(40, 780, f"Date: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
# #     c.drawString(40, 765, f"Order Ref: {order_ref or ''}")
# #     c.drawString(40, 750, f"Supplier Email: {supplier_email or ''}")
# #     y = 730
# #     c.setFont("Helvetica-Bold", 10)
# #     c.drawString(40, y, "SKU")
# #     c.drawString(200, y, "Qty")
# #     c.drawString(260, y, "Unit")
# #     c.drawString(320, y, "Location")
# #     c.setFont("Helvetica", 10)
# #     y -= 18
# #     for ln in lines:
# #         c.drawString(40, y, str(ln.get('sku', '')))
# #         c.drawString(200, y, str(ln.get('qty', '')))
# #         c.drawString(260, y, str(ln.get('unit', '')))
# #         c.drawString(320, y, str(ln.get('location', '')))
# #         y -= 16
# #         if y < 60:
# #             c.showPage()
# #             y = 800
# #     c.showPage()
# #     c.save()
# #     return path
# # # --- GET /receive (replace your existing receive function) ---
# # @app.route('/receive', methods=['GET'])
# # def receive():
# #     conn = None
# #     cur = None
# #     items = []
# #     locations = []
# #     receipt = None
# #     csrf_token = ''
# #     try:
# #         conn = get_conn()
# #         cur = conn.cursor()

# #         # items
# #         try:
# #             cur.execute("SELECT sku, name, unit FROM items ORDER BY name ASC")
# #             rows = cur.fetchall() or []
# #             items = [{'sku': r['sku'], 'name': r['name'], 'unit': r['unit']} for r in rows]
# #         except Exception:
# #             items = []

# #         # locations
# #         try:
# #             cur.execute("SELECT name FROM locations ORDER BY name ASC")
# #             rows = cur.fetchall() or []
# #             locations = [r['name'] for r in rows]
# #         except Exception:
# #             try:
# #                 cur.execute("SELECT DISTINCT location_id FROM stock_ledger WHERE location_id IS NOT NULL")
# #                 rows = cur.fetchall() or []
# #                 locations = [r['location_id'] for r in rows]
# #             except Exception:
# #                 locations = []

# #         # existing receipt if id provided
# #         receipt_id = request.args.get('id')
# #         if receipt_id:
# #             try:
# #                 cur.execute("SELECT id, grn_no, order_ref, supplier_email FROM receipts WHERE id = ?", (receipt_id,))
# #                 row = cur.fetchone()
# #                 if row:
# #                     receipt = {
# #                         'id': row['id'],
# #                         'grn_no': row.get('grn_no'),
# #                         'order_ref': row.get('order_ref'),
# #                         'supplier_email': row.get('supplier_email')
# #                     }
# #             except Exception:
# #                 receipt = None

# #         # create lightweight receipt header if none
# #         if not receipt:
# #             try:
# #                 receipt_no = f"RCPT-{datetime.utcnow().strftime('%Y%m%d')}-{int(datetime.utcnow().timestamp())}"
# #                 cur.execute(
# #                     "INSERT INTO receipts (receipt_no, created_at, po_no, supplier_email) VALUES (?, datetime('now'), ?, ?)",
# #                     (receipt_no, None, None)
# #                 )
# #                 conn.commit()
# #                 receipt_id = cur.lastrowid
# #                 receipt = {'id': receipt_id, 'receipt_no': receipt_no}
# #             except Exception:
# #                 app.logger.exception("Failed to create lightweight receipt header")
# #                 receipt = {'id': ''}

# #         csrf_token = generate_csrf()

# #     except Exception:
# #         app.logger.exception("Failed to load receive page data")
# #         receipt = receipt or {'id': ''}
# #         csrf_token = ''
# #     finally:
# #         try:
# #             if cur: cur.close()
# #         except Exception:
# #             pass
# #         try:
# #             if conn: conn.close()
# #         except Exception:
# #             pass

# #     return render_template('receive.html',
# #                            items=items,
# #                            locations=locations,
# #                            user=session.get('username'),
# #                            receipt=receipt,
# #                            csrf_token=csrf_token)
# # # --- POST /api/receive (replace your existing api_receive) ---
# # @app.route('/api/receive', methods=['POST'])
# # def api_receive():
# #     app.logger.debug("Incoming /api/receive headers: %s", dict(request.headers))
# #     app.logger.debug("Incoming /api/receive raw body: %s", request.get_data(as_text=True))

# #     data = request.get_json(silent=True) or {}
# #     grn_no = (data.get('grn_no') or '').strip()
# #     supplier = (data.get('supplier') or '').strip()
# #     po_ref = (data.get('po_ref') or '').strip()
# #     lines = data.get('lines') or []

# #     if not grn_no:
# #         grn_no = f"GRN-{datetime.utcnow().strftime('%Y%m%d')}-{int(datetime.utcnow().timestamp())}"

# #     if not supplier:
# #         return jsonify({'status': 'error', 'message': 'Supplier is required'}), 400
# #     if not lines or not any(l.get('sku') and float(l.get('qty', 0)) > 0 for l in lines):
# #         return jsonify({'status': 'error', 'message': 'At least one valid line is required'}), 400

# #     conn = None
# #     cur = None
# #     try:
# #         conn = get_conn()
# #         cur = conn.cursor()

# #         # ensure receipt header exists (use provided receipt_id or create new)
# #         receipt_id = data.get('receipt_id') or None
# #         if receipt_id:
# #             cur.execute("SELECT id FROM receipts WHERE id = ?", (receipt_id,))
# #             if not cur.fetchone():
# #                 receipt_id = None

# #         if not receipt_id:
# #             receipt_no = f"RCPT-{datetime.utcnow().strftime('%Y%m%d')}-{int(datetime.utcnow().timestamp())}"
# #             cur.execute(
# #                 "INSERT INTO receipts (receipt_no, created_at, po_no, supplier_email) VALUES (?, datetime('now'), ?, ?)",
# #                 (receipt_no, po_ref or None, data.get('supplier_email') or None)
# #             )
# #             receipt_id = cur.lastrowid

# #         # persist grn_no and order_ref if missing
# #         cur.execute("SELECT grn_no, order_ref FROM receipts WHERE id = ?", (receipt_id,))
# #         hdr = cur.fetchone()
# #         existing_grn = hdr['grn_no'] if hdr and 'grn_no' in hdr.keys() else None
# #         existing_order = hdr['order_ref'] if hdr and 'order_ref' in hdr.keys() else None

# #         if not existing_grn:
# #             cur.execute("UPDATE receipts SET grn_no = ? WHERE id = ?", (grn_no, receipt_id))
# #         if not existing_order:
# #             order_ref = f"ORD-{int(datetime.utcnow().timestamp())}"
# #             cur.execute("UPDATE receipts SET order_ref = ? WHERE id = ?", (order_ref, receipt_id))
# #         else:
# #             order_ref = existing_order

# #         # insert lines, update items and ledger
# #         for ln in lines:
# #             sku = (ln.get('sku') or '').strip()
# #             try:
# #                 qty = float(ln.get('qty') or 0)
# #             except Exception:
# #                 qty = 0
# #             unit = (ln.get('unit') or '').strip() or None
# #             batch_id = (ln.get('batch_id') or '').strip() or None
# #             location = (ln.get('location') or '').strip() or None
# #             expiry = (ln.get('expiry') or '').strip() or None
# #             name = (ln.get('name') or '').strip() or sku

# #             if not sku or qty <= 0:
# #                 continue

# #             try:
# #                 cur.execute("""
# #                     INSERT INTO receipt_lines (receipt_id, sku, qty, unit, location, batch_id, expiry)
# #                     VALUES (?, ?, ?, ?, ?, ?, ?)
# #                 """, (receipt_id, sku, qty, unit, location, batch_id, expiry))
# #             except Exception:
# #                 pass

# #             cur.execute("UPDATE items SET on_hand = COALESCE(on_hand,0) + ? WHERE sku = ?", (qty, sku))
# #             if cur.rowcount == 0:
# #                 cur.execute(
# #                     "INSERT INTO items (sku, name, unit, category, on_hand, reorder_level, created_at) VALUES (?,?,?,?,?,?,datetime('now'))",
# #                     (sku, name, unit, '', int(qty), None)
# #                 )

# #             to_location_id = None
# #             if location:
# #                 try:
# #                     cur.execute("SELECT id FROM locations WHERE code = ? OR name = ? LIMIT 1", (location, location))
# #                     lr = cur.fetchone()
# #                     to_location_id = lr['id'] if lr and 'id' in lr.keys() else None
# #                 except Exception:
# #                     to_location_id = None

# #             cur.execute("""
# #                 INSERT INTO stock_ledger (sku, item_sku, qty, txn_type, to_location_id, reference, batch_id, expiry_date, created_by, when_ts)
# #                 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
# #             """, (sku, sku, qty, 'RECEIVE', to_location_id, grn_no, batch_id, expiry, data.get('confirmed_by', 'system')))

# #         cur.execute("UPDATE receipts SET confirmed = 1, confirmed_at = datetime('now'), supplier_email = COALESCE(supplier_email, ?) WHERE id = ?",
# #                     (data.get('supplier_email') or None, receipt_id))

# #         conn.commit()

# #         # generate PDF and update receipts.document_path
# #         try:
# #             try:
# #                 cur.execute("SELECT sku, qty, unit, location FROM receipt_lines WHERE receipt_id = ?", (receipt_id,))
# #                 pdf_rows = cur.fetchall()
# #                 if pdf_rows:
# #                     pdf_lines = [{'sku': r['sku'], 'qty': r['qty'], 'unit': r.get('unit'), 'location': r.get('location')} for r in pdf_rows]
# #                 else:
# #                     pdf_lines = [{'sku': l.get('sku'), 'qty': l.get('qty'), 'unit': l.get('unit'), 'location': l.get('location')} for l in lines]
# #             except Exception:
# #                 pdf_lines = [{'sku': l.get('sku'), 'qty': l.get('qty'), 'unit': l.get('unit'), 'location': l.get('location')} for l in lines]

# #             supplier_for_email = data.get('supplier_email') or None
# #             pdf_path = generate_pdf_for_receipt(grn_no, order_ref if 'order_ref' in locals() else None, supplier_for_email, pdf_lines)
# #             app.logger.info("PDF generated at: %s", pdf_path)

# #             try:
# #                 cur.execute("UPDATE receipts SET document_path = ? WHERE id = ?", (pdf_path, receipt_id))
# #                 conn.commit()
# #             except Exception:
# #                 app.logger.exception("Failed to update receipts.document_path for %s", receipt_id)
# #         except Exception:
# #             app.logger.exception("PDF generation failed for receipt %s", receipt_id)
# #             pdf_path = None

# #         # optional async email (keeps app responsive)
# #         try:
# #             smtp_cfg = {
# #                 'host': current_app.config.get('SMTP_HOST', 'localhost'),
# #                 'port': int(current_app.config.get('SMTP_PORT', 25)),
# #                 'username': current_app.config.get('SMTP_USER', ''),
# #                 'password': current_app.config.get('SMTP_PASS', ''),
# #                 'use_tls': bool(current_app.config.get('SMTP_USE_TLS', False))
# #             }
# #             subject = f"Goods Received Confirmation {grn_no}"
# #             body = f"Goods received for GRN {grn_no} (Order {order_ref if 'order_ref' in locals() else ''})."
# #             if supplier_for_email and pdf_path and smtp_cfg['host']:
# #                 def _send():
# #                     try:
# #                         from email.message import EmailMessage
# #                         msg = EmailMessage()
# #                         msg['Subject'] = subject
# #                         msg['From'] = smtp_cfg['username'] or 'no-reply@example.com'
# #                         msg['To'] = supplier_for_email
# #                         msg.set_content(body)
# #                         with open(pdf_path, 'rb') as f:
# #                             data_bytes = f.read()
# #                         msg.add_attachment(data_bytes, maintype='application', subtype='pdf', filename=os.path.basename(pdf_path))
# #                         import smtplib
# #                         if smtp_cfg['use_tls']:
# #                             s = smtplib.SMTP(smtp_cfg['host'], smtp_cfg['port'])
# #                             s.starttls()
# #                         else:
# #                             s = smtplib.SMTP(smtp_cfg['host'], smtp_cfg['port'])
# #                         if smtp_cfg['username']:
# #                             s.login(smtp_cfg['username'], smtp_cfg['password'])
# #                         s.send_message(msg)
# #                         s.quit()
# #                     except Exception:
# #                         app.logger.exception("Async email send failed for receipt %s", receipt_id)
# #                 t = threading.Thread(target=_send, daemon=True)
# #                 t.start()
# #         except Exception:
# #             app.logger.exception("Failed to queue email for receipt %s", receipt_id)

# #         return jsonify({'status': 'ok', 'grn_no': grn_no, 'order_ref': order_ref if 'order_ref' in locals() else None, 'receipt_id': receipt_id}), 200

# #     except Exception:
# #         if conn:
# #             try:
# #                 conn.rollback()
# #             except Exception:
# #                 pass
# #         app.logger.exception("Failed to process GRN")
# #         return jsonify({'status': 'error', 'message': 'Failed to process GRN'}), 500
# #     finally:
# #         try:
# #             if cur: cur.close()
# #         except Exception:
# #             pass
# #         try:
# #             if conn: conn.close()
# #         except Exception:
# #             pass


# # # Add this import near your other imports if not already present
# # # required imports (add near the top of app.py if not already present)
# # # from datetime import datetime
# # # from flask import request, render_template, session
# # # from flask_wtf.csrf import generate_csrf

# # # # GET /receive handler (replace the broken receive() in app.py with this)
# # # @app.route('/receive', methods=['GET'])
# # # def receive():
# # #     """
# # #     Render the receive goods page. Supplies:
# # #       - items: list of {sku, name, unit}
# # #       - locations: list of location names/ids
# # #       - receipt: header dict with id (created if missing)
# # #       - csrf_token: token for POSTs
# # #     """
# # #     conn = None
# # #     cur = None
# # #     items = []
# # #     locations = []
# # #     receipt = None
# # #     csrf_token = ''

# # #     try:
# # #         conn = get_conn()
# # #         cur = conn.cursor()

# # #         # items for select
# # #         try:
# # #             cur.execute("SELECT sku, name, unit FROM items ORDER BY name ASC")
# # #             rows = cur.fetchall() or []
# # #             items = [{'sku': r['sku'], 'name': r['name'], 'unit': r['unit']} for r in rows]
# # #         except Exception:
# # #             items = []

# # #         # try locations table, fallback to distinct location_id in stock_ledger
# # #         try:
# # #             cur.execute("SELECT name FROM locations ORDER BY name ASC")
# # #             rows = cur.fetchall() or []
# # #             locations = [r['name'] for r in rows]
# # #         except Exception:
# # #             try:
# # #                 cur.execute("SELECT DISTINCT location_id FROM stock_ledger WHERE location_id IS NOT NULL")
# # #                 rows = cur.fetchall() or []
# # #                 locations = [r['location_id'] for r in rows]
# # #             except Exception:
# # #                 locations = []

# # #         # ensure a receipt header exists for the page (optional id param)
# # #         receipt_id = request.args.get('id')
# # #         if receipt_id:
# # #             try:
# # #                 cur.execute("SELECT id, grn_no, order_ref, supplier_email FROM receipts WHERE id = ?", (receipt_id,))
# # #                 row = cur.fetchone()
# # #                 if row:
# # #                     receipt = {
# # #                         'id': row['id'],
# # #                         'grn_no': row.get('grn_no'),
# # #                         'order_ref': row.get('order_ref'),
# # #                         'supplier_email': row.get('supplier_email')
# # #                     }
# # #             except Exception:
# # #                 receipt = None

# # #         # if no existing receipt, create a lightweight header and ensure required receipt_no
# # #         if not receipt:
# # #             try:
# # #                 receipt_no = f"RCPT-{datetime.utcnow().strftime('%Y%m%d')}-{int(datetime.utcnow().timestamp())}"
# # #                 cur.execute(
# # #                     "INSERT INTO receipts (receipt_no, created_at, po_no, supplier_email) VALUES (?, datetime('now'), ?, ?)",
# # #                     (receipt_no, None, None)
# # #                 )
# # #                 conn.commit()
# # #                 receipt_id = cur.lastrowid
# # #                 receipt = {'id': receipt_id, 'receipt_no': receipt_no}
# # #             except Exception:
# # #                 # if insert fails, return a blank receipt id so the page still renders
# # #                 app.logger.exception("Failed to create lightweight receipt header")
# # #                 receipt = {'id': ''}

# # #         # generate CSRF token for the page
# # #         csrf_token = generate_csrf()

# # #     except Exception:
# # #         app.logger.exception("Failed to load receive page data")
# # #         receipt = receipt or {'id': ''}
# # #         csrf_token = ''
# # #     finally:
# # #         try:
# # #             if cur:
# # #                 cur.close()
# # #         except Exception:
# # #             pass
# # #         try:
# # #             if conn:
# # #                 conn.close()
# # #         except Exception:
# # #             pass

# # #     return render_template(
# # #         'receive.html',
# # #         items=items,
# # #         locations=locations,
# # #         user=session.get('username'),
# # #         receipt=receipt,
# # #         csrf_token=csrf_token
# # #     )

# # # from flask_wtf.csrf import generate_csrf   # add near other imports

# # # @app.route('/receive', methods=['GET'])
# # # def receive():
# # #     """
# # #     Render the receive goods page. Supplies:
# # #       - items: list of {sku, name, unit}
# # #       - locations: list of location names/ids
# # #       - receipt: header dict with id (created if missing)
# # #       - csrf_token: token for POSTs
# # #     """
# # #     conn = None
# # #     cur = None
# # #     items = []
# # #     locations = []
# # #     receipt = None
# # #     csrf_token = ''
# # #     try:
# # #         conn = get_conn()
# # #         cur = conn.cursor()

# # #         # items for select
# # #         cur.execute("SELECT sku, name, unit FROM items ORDER BY name ASC")
# # #         rows = cur.fetchall() or []
# # #         items = [{'sku': r['sku'], 'name': r['name'], 'unit': r['unit']} for r in rows]

# # #         # try locations table, fallback to distinct location_id in stock_ledger
# # #         try:
# # #             cur.execute("SELECT name FROM locations ORDER BY name ASC")
# # #             rows = cur.fetchall() or []
# # #             locations = [r['name'] for r in rows]
# # #         except Exception:
# # #             cur.execute("SELECT DISTINCT location_id FROM stock_ledger WHERE location_id IS NOT NULL")
# # #             rows = cur.fetchall() or []
# # #             locations = [r['location_id'] for r in rows]

# # #         # ensure a receipt header exists for the page
# # #         receipt_id = request.args.get('id')
# # #         if receipt_id:
# # #             cur.execute("SELECT id, supplier, grn_no FROM receipts WHERE id = ?", (receipt_id,))
# # #             row = cur.fetchone()
# # #             if row:
# # #                 receipt = {'id': row['id'], 'supplier': row.get('supplier'), 'grn_no': row.get('grn_no')}
# # #         if not receipt:
# # #             cur.execute("INSERT INTO receipts (created_at, po_no, supplier_email) VALUES (datetime('now'), ?, ?)", (po_ref or None, data.get('supplier_email') or None))
# # # receipt_id = cur.lastrowid
# # #             receipt_id = cur.lastrowid
# # #             receipt = {'id': receipt_id}

# # #         # generate CSRF token for the page (Flask-WTF)
# # #         csrf_token = generate_csrf()

# # #     except Exception:
# # #         app.logger.exception("Failed to load receive page data")
# # #         receipt = receipt or {'id': ''}
# # #         csrf_token = ''
# # #     finally:
# # #         try:
# # #             if cur: cur.close()
# # #         except Exception:
# # #             pass
# # #         try:
# # #             if conn: conn.close()
# # #         except Exception:
# # #             pass

# # #     return render_template('receive.html',
# # #                            items=items,
# # #                            locations=locations,
# # #                            user=session.get('username'),
# # #                            receipt=receipt,
# # #                            csrf_token=csrf_token)

# # # from datetime import datetime
# # # from flask import request, jsonify, render_template, session

# # # def _exec(cur, query, params=()):
# # #     """
# # #     Execute a query trying sqlite3-style ? placeholders first,
# # #     then retry with %s placeholders for MySQL-style connectors.
# # #     Returns the cursor after successful execute.
# # #     """
# # #     try:
# # #         cur.execute(query, params)
# # #         return cur
# # #     except Exception:
# # #         # try converting ? -> %s and retry
# # #         try:
# # #             q2 = query.replace('?', '%s')
# # #             cur.execute(q2, params)
# # #             return cur
# # #         except Exception:
# # #             # re-raise original exception for outer handler to log
# # #             raise

# # # from datetime import datetime
# # # from flask import request, jsonify, render_template, session

# # # # NOTE: Do NOT use `from flask import current_app as app`
# # # # Ensure this file is executed after `app = Flask(__name__)` is defined.

# # # def _exec_sqlite(cur, query, params=()):
# # #     """Execute sqlite3 query using ? placeholders; raise on error."""
# # #     cur.execute(query, params)
# # #     return cur

# # # @app.route('/receive', methods=['GET'])
# # # def receive():
# # #     """
# # #     Render the receive goods page. Supplies:
# # #       - items: list of {sku, name, unit}
# # #       - locations: list of location names/ids
# # #     """
# # #     conn = None
# # #     cur = None
# # #     items = []
# # #     locations = []
# # #     try:
# # #         conn = get_conn()
# # #         cur = conn.cursor()

# # #         # items for select
# # #         cur.execute("SELECT sku, name, unit FROM items ORDER BY name ASC")
# # #         rows = cur.fetchall() or []
# # #         items = [{'sku': r['sku'], 'name': r['name'], 'unit': r['unit']} for r in rows]

# # #         # try locations table, fallback to distinct location_id in stock_ledger
# # #         try:
# # #             cur.execute("SELECT name FROM locations ORDER BY name ASC")
# # #             rows = cur.fetchall() or []
# # #             locations = [r['name'] for r in rows]
# # #         except Exception:
# # #             cur.execute("SELECT DISTINCT location_id FROM stock_ledger WHERE location_id IS NOT NULL")
# # #             rows = cur.fetchall() or []
# # #             locations = [r['location_id'] for r in rows]

# # #     except Exception:
# # #         app.logger.exception("Failed to load receive page data")
# # #     finally:
# # #         try:
# # #             if cur: cur.close()
# # #         except Exception:
# # #             pass
# # #         try:
# # #             if conn: conn.close()
# # #         except Exception:
# # #             pass

# # #     return render_template('receive.html', items=items, locations=locations, user=session.get('username'))

# # # @app.route('/api/receive', methods=['POST'])
# # # def api_receive():
# # #     app.logger.debug("Incoming /api/receive headers: %s", dict(request.headers))
# # #     app.logger.debug("Incoming /api/receive raw body: %s", request.get_data(as_text=True))

# # #     data = request.get_json(silent=True) or {}
# # #     grn_no = (data.get('grn_no') or '').strip()
# # #     supplier = (data.get('supplier') or '').strip()
# # #     po_ref = (data.get('po_ref') or '').strip()
# # #     lines = data.get('lines') or []

# # #     # generate GRN if not provided
# # #     if not grn_no:
# # #         grn_no = generate_grn_no()

# # #     # basic header validation
# # #     if not supplier:
# # #         return jsonify({'status': 'error', 'message': 'Supplier is required'}), 400
# # #     if not lines or not any(l.get('sku') and float(l.get('qty', 0)) > 0 for l in lines):
# # #         return jsonify({'status': 'error', 'message': 'At least one valid line is required'}), 400

# # #     conn = None
# # #     cur = None
# # #     try:
# # #         conn = get_conn()
# # #         cur = conn.cursor()

# # #         # ensure receipt header exists (use provided receipt_id or create new)
# # #         receipt_id = data.get('receipt_id') or None
# # #         if receipt_id:
# # #             cur.execute("SELECT id FROM receipts WHERE id = ?", (receipt_id,))
# # #             if not cur.fetchone():
# # #                 receipt_id = None

# # #         if not receipt_id:
# # #             # receipts table in your DB has columns: receipt_no, supplier_id, po_no, received_by, ...
# # #             # we don't have supplier_id mapping here, so store supplier_email and po_no
# # #             cur.execute("INSERT INTO receipts (created_at, po_no, supplier_email) VALUES (datetime('now'), ?, ?)",
# # #                         (po_ref, data.get('supplier_email') or None))
# # #             receipt_id = cur.lastrowid

# # #         # persist grn_no and order_ref on receipts if missing
# # #         cur.execute("SELECT grn_no, order_ref FROM receipts WHERE id = ?", (receipt_id,))
# # #         hdr = cur.fetchone()
# # #         existing_grn = hdr['grn_no'] if hdr and 'grn_no' in hdr.keys() else None
# # #         existing_order = hdr['order_ref'] if hdr and 'order_ref' in hdr.keys() else None

# # #         if not existing_grn:
# # #             cur.execute("UPDATE receipts SET grn_no = ? WHERE id = ?", (grn_no, receipt_id))
# # #         if not existing_order:
# # #             try:
# # #                 order_ref = generate_order_ref()
# # #             except Exception:
# # #                 order_ref = f"ORD-{int(datetime.utcnow().timestamp())}"
# # #             cur.execute("UPDATE receipts SET order_ref = ? WHERE id = ?", (order_ref, receipt_id))
# # #         else:
# # #             order_ref = existing_order

# # #         # collect unique SKUs from payload and validate existence
# # #         skus = sorted({(l.get('sku') or '').strip() for l in lines if l.get('sku')})
# # #         sku_exists = {}
# # #         if skus:
# # #             placeholders = ','.join('?' for _ in skus)
# # #             cur.execute(f"SELECT sku FROM items WHERE sku IN ({placeholders})", tuple(skus))
# # #             found_rows = cur.fetchall() or []
# # #             found = {r['sku'] for r in found_rows}
# # #             for s in skus:
# # #                 sku_exists[s] = (s in found)

# # #         # per-line validation
# # #         errors = {}
# # #         for idx, ln in enumerate(lines):
# # #             ln_sku = (ln.get('sku') or '').strip()
# # #             try:
# # #                 ln_qty = float(ln.get('qty') or 0)
# # #             except Exception:
# # #                 ln_qty = 0
# # #             line_errs = []
# # #             if not ln_sku:
# # #                 line_errs.append('SKU required')
# # #             elif ln_sku not in sku_exists:
# # #                 line_errs.append('SKU not provided')
# # #             elif not sku_exists.get(ln_sku, False):
# # #                 line_errs.append('Unknown SKU')
# # #             if ln_qty <= 0:
# # #                 line_errs.append('Quantity must be greater than zero')
# # #             if line_errs:
# # #                 errors[idx] = line_errs

# # #         if errors:
# # #             return jsonify({'status': 'error', 'message': 'Validation errors', 'errors': errors}), 400

# # #         # Insert receipt_lines, ledger rows and update/create items
# # #         for ln in lines:
# # #             sku = (ln.get('sku') or '').strip()
# # #             try:
# # #                 qty = float(ln.get('qty') or 0)
# # #             except Exception:
# # #                 qty = 0
# # #             unit = (ln.get('unit') or '').strip() or None
# # #             batch_id = (ln.get('batch_id') or '').strip() or None
# # #             location = (ln.get('location') or '').strip() or None
# # #             expiry = (ln.get('expiry') or '').strip() or None
# # #             name = (ln.get('name') or '').strip() or sku

# # #             if not sku or qty <= 0:
# # #                 continue

# # #             # insert receipt line if table exists
# # #             try:
# # #                 cur.execute("""
# # #                     INSERT INTO receipt_lines (receipt_id, sku, qty, unit, location, batch_id, expiry)
# # #                     VALUES (?, ?, ?, ?, ?, ?, ?)
# # #                 """, (receipt_id, sku, qty, unit, location, batch_id, expiry))
# # #             except Exception:
# # #                 # ignore if table missing
# # #                 pass

# # #             # update items.on_hand; if no row updated, insert item
# # #             cur.execute("UPDATE items SET on_hand = COALESCE(on_hand,0) + ? WHERE sku = ?", (qty, sku))
# # #             if cur.rowcount == 0:
# # #                 cur.execute(
# # #                     "INSERT INTO items (sku, name, unit, category, on_hand, reorder_level, created_at) VALUES (?,?,?,?,?,?,datetime('now'))",
# # #                     (sku, name, unit, '', int(qty), None)
# # #                 )

# # #             # resolve location id if locations table uses names/codes
# # #             to_location_id = None
# # #             if location:
# # #                 try:
# # #                     cur.execute("SELECT id FROM locations WHERE code = ? OR name = ? LIMIT 1", (location, location))
# # #                     lr = cur.fetchone()
# # #                     to_location_id = lr['id'] if lr and 'id' in lr.keys() else None
# # #                 except Exception:
# # #                     to_location_id = None

# # #             # insert ledger row
# # #             cur.execute("""
# # #                 INSERT INTO stock_ledger (sku, item_sku, qty, txn_type, to_location_id, reference, batch_id, expiry_date, created_by, when_ts)
# # #                 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
# # #             """, (sku, sku, qty, 'RECEIVE', to_location_id, grn_no, batch_id, expiry, data.get('confirmed_by','system')))

# # #         # mark receipt confirmed and save supplier email if provided
# # #         cur.execute("UPDATE receipts SET confirmed = 1, confirmed_at = datetime('now'), supplier_email = COALESCE(supplier_email, ?) WHERE id = ?",
# # #                     (data.get('supplier_email') or None, receipt_id))

# # #         conn.commit()

# # #         # generate PDF and update receipts.document_path
# # #         try:
# # #             # reload lines for PDF if receipt_lines table exists
# # #             try:
# # #                 cur.execute("SELECT sku, qty, unit, location FROM receipt_lines WHERE receipt_id = ?", (receipt_id,))
# # #                 pdf_rows = cur.fetchall()
# # #                 if pdf_rows:
# # #                     pdf_lines = [{'sku': r['sku'], 'qty': r['qty'], 'unit': r.get('unit'), 'location': r.get('location')} for r in pdf_rows]
# # #                 else:
# # #                     pdf_lines = [{'sku': l.get('sku'), 'qty': l.get('qty'), 'unit': l.get('unit'), 'location': l.get('location')} for l in lines]
# # #             except Exception:
# # #                 pdf_lines = [{'sku': l.get('sku'), 'qty': l.get('qty'), 'unit': l.get('unit'), 'location': l.get('location')} for l in lines]

# # #             supplier_for_email = data.get('supplier_email') or None
# # #             pdf_path = generate_pdf_for_receipt(grn_no, order_ref, supplier_for_email, pdf_lines)
# # #             app.logger.info("PDF generated at: %s", pdf_path)

# # #             try:
# # #                 cur.execute("UPDATE receipts SET document_path = ? WHERE id = ?", (pdf_path, receipt_id))
# # #                 conn.commit()
# # #             except Exception:
# # #                 app.logger.exception("Failed to update receipts.document_path for %s", receipt_id)
# # #         except Exception:
# # #             app.logger.exception("PDF generation failed for receipt %s", receipt_id)
# # #             pdf_path = None

# # #         # send email async if supplier email present and pdf created
# # #         smtp_cfg = {
# # #             'host': current_app.config.get('SMTP_HOST', 'smtp.example.com'),
# # #             'port': int(current_app.config.get('SMTP_PORT', 587)),
# # #             'username': current_app.config.get('SMTP_USER', 'user@example.com'),
# # #             'password': current_app.config.get('SMTP_PASS', 'secret'),
# # #             'use_tls': True
# # #         }
# # #         subject = f"Goods Received Confirmation {grn_no}"
# # #         body = f"Dear Supplier,\n\nWe confirm receipt of goods for GRN {grn_no} (Order {order_ref}). Please find attached the goods received note.\n\nRegards,\nWarehouse Team"
# # #         if supplier_for_email and pdf_path:
# # #             try:
# # #                 send_email_async(supplier_for_email, subject, body, pdf_path, smtp_cfg)
# # #             except Exception:
# # #                 app.logger.exception("Failed to start async email for receipt %s", receipt_id)

# # #         return jsonify({'status': 'ok', 'grn_no': grn_no, 'order_ref': order_ref, 'receipt_id': receipt_id}), 200

# # #     except Exception:
# # #         if conn:
# # #             try:
# # #                 conn.rollback()
# # #             except Exception:
# # #                 pass
# # #         app.logger.exception("Failed to process GRN")
# # #         return jsonify({'status': 'error', 'message': 'Failed to process GRN'}), 500
# # #     finally:
# # #         try:
# # #             if cur: cur.close()
# # #         except Exception:
# # #             pass
# # #         try:
# # #             if conn: conn.close()
# # #         except Exception:
# # #             pass

# # # @app.route('/api/receive', methods=['POST'])
# # # def api_receive():

# # #     app.logger.debug("Incoming /api/receive headers: %s", dict(request.headers))
# # #     app.logger.debug("Incoming /api/receive raw body: %s", request.get_data(as_text=True))
# # #     """
# # #     Accepts JSON:
# # #       { grn_no, supplier, po_ref, lines: [{ sku, qty, unit, batch_id, location, expiry, name, category, reorder_level }] }
# # #     Validates and inserts ledger rows, updates/creates items, and returns { status:'ok', grn_no:'...' }.
# # #     """
# # #     data = request.get_json(silent=True) or {}
# # #     grn_no = (data.get('grn_no') or '').strip()
# # #     supplier = (data.get('supplier') or '').strip()
# # #     po_ref = (data.get('po_ref') or '').strip()
# # #     lines = data.get('lines') or []

# # #     # generate GRN if not provided
# # #     if not grn_no:
# # #         grn_no = f"GRN-{datetime.utcnow().strftime('%Y%m%d')}-{int(datetime.utcnow().timestamp())}"

# # #     # basic header validation
# # #     if not supplier:
# # #         return jsonify({'status': 'error', 'message': 'Supplier is required'}), 400
# # #     if not lines or not any(l.get('sku') and float(l.get('qty', 0)) > 0 for l in lines):
# # #         return jsonify({'status': 'error', 'message': 'At least one valid line is required'}), 400

# # #     conn = None
# # #     cur = None
# # #     try:
# # #         conn = get_conn()
# # #         cur = conn.cursor()

# # #         # collect unique SKUs from payload
# # #         skus = sorted({(l.get('sku') or '').strip() for l in lines if l.get('sku')})
# # #         sku_exists = {}
# # #         if skus:
# # #             placeholders = ','.join('?' for _ in skus)
# # #             cur.execute(f"SELECT sku FROM items WHERE sku IN ({placeholders})", tuple(skus))
# # #             found_rows = cur.fetchall() or []
# # #             found = {r['sku'] for r in found_rows}
# # #             for s in skus:
# # #                 sku_exists[s] = (s in found)

# # #         # per-line validation
# # #         errors = {}
# # #         for idx, ln in enumerate(lines):
# # #             ln_sku = (ln.get('sku') or '').strip()
# # #             try:
# # #                 ln_qty = float(ln.get('qty') or 0)
# # #             except Exception:
# # #                 ln_qty = 0
# # #             line_errs = []
# # #             if not ln_sku:
# # #                 line_errs.append('SKU required')
# # #             elif ln_sku not in sku_exists:
# # #                 line_errs.append('SKU not provided')
# # #             elif not sku_exists.get(ln_sku, False):
# # #                 line_errs.append('Unknown SKU')
# # #             if ln_qty <= 0:
# # #                 line_errs.append('Quantity must be greater than zero')
# # #             if line_errs:
# # #                 errors[idx] = line_errs

# # #         if errors:
# # #             return jsonify({'status': 'error', 'message': 'Validation errors', 'errors': errors}), 400

# # #         # optional: insert GRN header if table exists (SQLite)
# # #         try:
# # #             cur.execute("INSERT INTO grn (grn_no, supplier, po_ref, created_at) VALUES (?, ?, ?, datetime('now'))", (grn_no, supplier, po_ref))
# # #         except Exception:
# # #             # ignore if grn table doesn't exist
# # #             pass

# # #         # Insert ledger rows and update/create items
# # #         for ln in lines:
# # #             sku = (ln.get('sku') or '').strip()
# # #             try:
# # #                 qty = float(ln.get('qty') or 0)
# # #             except Exception:
# # #                 qty = 0
# # #             unit = (ln.get('unit') or '').strip() or None
# # #             batch_id = (ln.get('batch_id') or '').strip() or None
# # #             location = (ln.get('location') or '').strip() or None
# # #             expiry = (ln.get('expiry') or '').strip() or None
# # #             name = (ln.get('name') or '').strip() or sku
# # #             category = (ln.get('category') or '').strip() or ''
# # #             reorder_level = ln.get('reorder_level') if ln.get('reorder_level') is not None else None

# # #             if not sku or qty <= 0:
# # #                 continue

# # #             # update cached on_hand; if no row updated, insert item
# # #             cur.execute("UPDATE items SET on_hand = on_hand + ? WHERE sku = ?", (qty, sku))
# # #             if cur.rowcount == 0:
# # #                 cur.execute(
# # #                     "INSERT INTO items (sku, name, unit, category, on_hand, reorder_level, created_at) VALUES (?,?,?,?,?,?,datetime('now'))",
# # #                     (sku, name, unit, category, int(qty), reorder_level)
# # #                 )

# # #             # insert ledger row
# # #             cur.execute("""
# # #                 INSERT INTO stock_ledger
# # #                   (sku, qty, txn_type, location_id, reference, batch_id, expiry_date, created_at)
# # #                 VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
# # #             """, (sku, qty, 'receive', location, grn_no, batch_id, expiry))

# # #         conn.commit()
# # #         return jsonify({'status': 'ok', 'grn_no': grn_no}), 200

# # #     except Exception:
# # #         if conn:
# # #             try:
# # #                 conn.rollback()
# # #             except Exception:
# # #                 pass
# # #         app.logger.exception("Failed to process GRN")
# # #         return jsonify({'status': 'error', 'message': 'Failed to process GRN'}), 500
# # #     finally:
# # #         try:
# # #             if cur: cur.close()
# # #         except Exception:
# # #             pass
# # #         try:
# # #             if conn: conn.close()
# # #         except Exception:
# # #             pass


# # # --- add these imports at top of app.py ---
# # # import sqlite3
# # # import threading
# # # import os
# # # from datetime import datetime
# # # from flask import current_app, request, jsonify
# # # from email.message import EmailMessage
# # # import smtplib
# # # from reportlab.lib.pagesizes import A4
# # # from reportlab.pdfgen import canvas
# # # import os




# # # # --- configuration (adjust if needed) ---
# # # DB_PATH = 'busia_wms.sqlite3'   # path to your sqlite DB
# # # BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# # # UPLOAD_DIR = os.path.join(BASE_DIR, 'generated_docs')
# # # os.makedirs(UPLOAD_DIR, exist_ok=True)

# # # # --- sequence helpers ---
# # # def next_sequence(name):
# # #     conn = sqlite3.connect(DB_PATH)
# # #     cur = conn.cursor()
# # #     cur.execute("BEGIN")
# # #     cur.execute("SELECT last FROM sequences WHERE name = ?", (name,))
# # #     row = cur.fetchone()
# # #     if row is None:
# # #         cur.execute("INSERT INTO sequences (name, last) VALUES (?, ?)", (name, 1))
# # #         val = 1
# # #     else:
# # #         val = row[0] + 1
# # #         cur.execute("UPDATE sequences SET last = ? WHERE name = ?", (val, name))
# # #     conn.commit()
# # #     conn.close()
# # #     return val

# # # def generate_grn_no():
# # #     seq = next_sequence('grn')
# # #     return f"GRN-{datetime.utcnow().strftime('%Y%m%d')}-{seq:04d}"

# # # def generate_order_ref():
# # #     seq = next_sequence('order_ref')
# # #     return f"ORD-{datetime.utcnow().strftime('%Y%m%d')}-{seq:04d}"

# # # # --- PDF generation ---
# # # def generate_pdf_for_receipt(grn_no, order_ref, supplier, lines):
# # #     filename = f"{grn_no}.pdf"
# # #     path = os.path.join(UPLOAD_DIR, filename)
# # #     c = canvas.Canvas(path, pagesize=A4)
# # #     c.setFont("Helvetica-Bold", 14)
# # #     c.drawString(40, 800, f"Goods Received Note {grn_no}")
# # #     c.setFont("Helvetica", 10)
# # #     c.drawString(40, 780, f"Date: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
# # #     c.drawString(40, 760, f"Order Ref: {order_ref}")
# # #     c.drawString(40, 740, f"Supplier: {supplier or ''}")
# # #     y = 720
# # #     c.setFont("Helvetica-Bold", 10)
# # #     c.drawString(40, y, "SKU")
# # #     c.drawString(200, y, "Qty")
# # #     c.drawString(260, y, "Unit")
# # #     c.drawString(320, y, "Location")
# # #     c.setFont("Helvetica", 10)
# # #     y -= 20
# # #     for line in lines:
# # #         c.drawString(40, y, str(line.get('sku','')))
# # #         c.drawString(200, y, str(line.get('qty','')))
# # #         c.drawString(260, y, str(line.get('unit','')))
# # #         c.drawString(320, y, str(line.get('location','')))
# # #         y -= 18
# # #         if y < 60:
# # #             c.showPage()
# # #             y = 800
# # #     c.showPage()
# # #     c.save()
# # #     return path

# # # # --- email helpers ---
# # # def send_email_with_attachment(to_email, subject, body, attachment_path, smtp_cfg):
# # #     msg = EmailMessage()
# # #     msg['Subject'] = subject
# # #     msg['From'] = smtp_cfg['username']
# # #     msg['To'] = to_email
# # #     msg.set_content(body)
# # #     with open(attachment_path, 'rb') as f:
# # #         data = f.read()
# # #     msg.add_attachment(data, maintype='application', subtype='pdf', filename=os.path.basename(attachment_path))

# # #     if smtp_cfg.get('use_tls', True):
# # #         server = smtplib.SMTP(smtp_cfg['host'], smtp_cfg['port'])
# # #         server.starttls()
# # #     else:
# # #         server = smtplib.SMTP_SSL(smtp_cfg['host'], smtp_cfg['port'])
# # #     server.login(smtp_cfg['username'], smtp_cfg['password'])
# # #     server.send_message(msg)
# # #     server.quit()

# # # def send_email_async(to_email, subject, body, attachment_path, smtp_cfg):
# # #     t = threading.Thread(target=send_email_with_attachment, args=(to_email, subject, body, attachment_path, smtp_cfg))
# # #     t.daemon = True
# # #     t.start()

# # # # --- confirm endpoint (receipts / receipt_lines) ---
# # # @app.route('/api/receive/confirm', methods=['POST'])
# # # def api_receive_confirm():
# # #     payload = request.get_json() or {}
# # #     receipt_id = payload.get('receipt_id') or payload.get('grn_id')
# # #     if not receipt_id:
# # #         return jsonify({'status':'error','message':'Missing receipt_id'}), 400

# # #     conn = sqlite3.connect(DB_PATH)
# # #     cur = conn.cursor()
# # #     try:
# # #         # load header
# # #         cur.execute("SELECT id, grn_no, order_ref, supplier_email, supplier FROM receipts WHERE id = ?", (receipt_id,))
# # #         row = cur.fetchone()
# # #         if not row:
# # #             return jsonify({'status':'error','message':'Receipt not found'}), 404
# # #         _, existing_grn_no, existing_order_ref, supplier_email, supplier_name = row

# # #         # generate numbers if missing
# # #         grn_no = existing_grn_no or generate_grn_no()
# # #         order_ref = existing_order_ref or generate_order_ref()
# # #         if not existing_grn_no:
# # #             cur.execute("UPDATE receipts SET grn_no = ? WHERE id = ?", (grn_no, receipt_id))
# # #         if not existing_order_ref:
# # #             cur.execute("UPDATE receipts SET order_ref = ? WHERE id = ?", (order_ref, receipt_id))

# # #         # load lines
# # #         cur.execute("SELECT sku, qty, unit, location, batch_id, expiry FROM receipt_lines WHERE receipt_id = ?", (receipt_id,))
# # #         lines = [{'sku':r[0],'qty':r[1],'unit':r[2],'location':r[3],'batch_id':r[4],'expiry':r[5]} for r in cur.fetchall()]

# # #         # insert ledger rows and update items.on_hand
# # #         for line in lines:
# # #             loc_code = line.get('location')
# # #             cur.execute("SELECT id FROM locations WHERE code = ? OR name = ? LIMIT 1", (loc_code, loc_code))
# # #             loc_row = cur.fetchone()
# # #             to_location_id = loc_row[0] if loc_row else None

# # #             cur.execute("""
# # #                 INSERT INTO stock_ledger (sku, item_sku, qty, txn_type, to_location_id, reference, batch_id, expiry_date, created_by, when_ts)
# # #                 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
# # #             """, (line['sku'], line['sku'], line['qty'], 'RECEIVE', to_location_id, grn_no, line.get('batch_id'), line.get('expiry'), payload.get('confirmed_by','system')))

# # #             cur.execute("UPDATE items SET on_hand = COALESCE(on_hand,0) + ? WHERE sku = ?", (line['qty'], line['sku']))

# # #         # mark confirmed
# # #         cur.execute("UPDATE receipts SET confirmed = 1, confirmed_at = datetime('now'), supplier_email = COALESCE(supplier_email, ?) WHERE id = ?", (supplier_email or payload.get('supplier_email'), receipt_id))
# # #         conn.commit()

# # #         # generate PDF and save path
# # #         supplier_for_email = supplier_email or payload.get('supplier_email') or supplier_name
# # #         pdf_path = generate_pdf_for_receipt(grn_no, order_ref, supplier_for_email, lines)
# # #         cur.execute("UPDATE receipts SET document_path = ? WHERE id = ?", (pdf_path, receipt_id))
# # #         conn.commit()

# # #         # send email async if supplier email present
# # #         smtp_cfg = {
# # #             'host': current_app.config.get('SMTP_HOST', 'smtp.example.com'),
# # #             'port': current_app.config.get('SMTP_PORT', 587),
# # #             'username': current_app.config.get('SMTP_USER', 'user@example.com'),
# # #             'password': current_app.config.get('SMTP_PASS', 'secret'),
# # #             'use_tls': True
# # #         }
# # #         subject = f"Goods Received Confirmation {grn_no}"
# # #         body = f"Dear Supplier,\n\nWe confirm receipt of goods for GRN {grn_no} (Order {order_ref}). Please find attached the goods received note.\n\nRegards,\nWarehouse Team"
# # #         if supplier_for_email:
# # #             send_email_async(supplier_for_email, subject, body, pdf_path, smtp_cfg)

# # #         return jsonify({'status':'ok','message':'Receipt confirmed','grn_no':grn_no,'order_ref':order_ref}), 200

# # #     except Exception as e:
# # #         conn.rollback()
# # #         current_app.logger.exception("Failed to confirm receipt")
# # #         return jsonify({'status':'error','message':'Failed to process confirmation','detail':str(e)}), 500
# # #     finally:
# # #         conn.close()
# # # import os
# # # import threading
# # # import sqlite3
# # # import smtplib
# # # from email.message import EmailMessage
# # # from datetime import datetime
# # # from flask import current_app

# # # # PDF library
# # # from reportlab.lib.pagesizes import A4
# # # from reportlab.pdfgen import canvas

# # # # Ensure upload dir is absolute and exists
# # # BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# # # UPLOAD_DIR = os.path.join(BASE_DIR, 'generated_docs')
# # # os.makedirs(UPLOAD_DIR, exist_ok=True)

# # # DB_PATH = os.path.join(BASE_DIR, 'busia_wms.sqlite3')  # adjust if your DB path differs

# # # # --- sequence helpers (sqlite) ---
# # # def next_sequence(name):
# # #     conn = sqlite3.connect(DB_PATH)
# # #     conn.row_factory = sqlite3.Row
# # #     cur = conn.cursor()
# # #     cur.execute("BEGIN")
# # #     cur.execute("SELECT last FROM sequences WHERE name = ?", (name,))
# # #     row = cur.fetchone()
# # #     if row is None:
# # #         cur.execute("INSERT INTO sequences (name, last) VALUES (?, ?)", (name, 1))
# # #         val = 1
# # #     else:
# # #         val = int(row['last']) + 1
# # #         cur.execute("UPDATE sequences SET last = ? WHERE name = ?", (val, name))
# # #     conn.commit()
# # #     conn.close()
# # #     return val

# # # def generate_grn_no():
# # #     seq = next_sequence('grn')
# # #     return f"GRN-{datetime.utcnow().strftime('%Y%m%d')}-{seq:04d}"

# # # def generate_order_ref():
# # #     seq = next_sequence('order_ref')
# # #     return f"ORD-{datetime.utcnow().strftime('%Y%m%d')}-{seq:04d}"

# # # # --- PDF generation helper ---
# # # def generate_pdf_for_receipt(grn_no, order_ref, supplier, lines):
# # #     filename = f"{grn_no}.pdf"
# # #     path = os.path.join(UPLOAD_DIR, filename)
# # #     c = canvas.Canvas(path, pagesize=A4)
# # #     c.setFont("Helvetica-Bold", 14)
# # #     c.drawString(40, 800, f"Goods Received Note {grn_no}")
# # #     c.setFont("Helvetica", 10)
# # #     c.drawString(40, 780, f"Date: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
# # #     c.drawString(40, 765, f"Order Ref: {order_ref}")
# # #     c.drawString(40, 750, f"Supplier: {supplier or ''}")
# # #     y = 730
# # #     c.setFont("Helvetica-Bold", 10)
# # #     c.drawString(40, y, "SKU")
# # #     c.drawString(200, y, "Qty")
# # #     c.drawString(260, y, "Unit")
# # #     c.drawString(320, y, "Location")
# # #     c.setFont("Helvetica", 10)
# # #     y -= 18
# # #     for line in lines:
# # #         c.drawString(40, y, str(line.get('sku','')))
# # #         c.drawString(200, y, str(line.get('qty','')))
# # #         c.drawString(260, y, str(line.get('unit','')))
# # #         c.drawString(320, y, str(line.get('location','')))
# # #         y -= 16
# # #         if y < 60:
# # #             c.showPage()
# # #             y = 800
# # #     c.showPage()
# # #     c.save()
# # #     return path

# # # # --- email helpers ---
# # # def send_email_with_attachment(to_email, subject, body, attachment_path, smtp_cfg):
# # #     msg = EmailMessage()
# # #     msg['Subject'] = subject
# # #     msg['From'] = smtp_cfg['username']
# # #     msg['To'] = to_email
# # #     msg.set_content(body)
# # #     with open(attachment_path, 'rb') as f:
# # #         data = f.read()
# # #     msg.add_attachment(data, maintype='application', subtype='pdf', filename=os.path.basename(attachment_path))

# # #     if smtp_cfg.get('use_tls', True):
# # #         server = smtplib.SMTP(smtp_cfg['host'], smtp_cfg['port'])
# # #         server.starttls()
# # #     else:
# # #         server = smtplib.SMTP_SSL(smtp_cfg['host'], smtp_cfg['port'])
# # #     server.login(smtp_cfg['username'], smtp_cfg['password'])
# # #     server.send_message(msg)
# # #     server.quit()

# # # def send_email_async(to_email, subject, body, attachment_path, smtp_cfg):
# # #     t = threading.Thread(target=send_email_with_attachment, args=(to_email, subject, body, attachment_path, smtp_cfg))
# # #     t.daemon = True
# # #     t.start()



# # # from datetime import datetime
# # # from flask import request, jsonify

# # # @app.route('/receive')
# # # def receive():
# # #     """
# # #     Render the receive goods page. Supplies:
# # #       - items: list of {sku, name, unit}
# # #       - locations: list of location names/ids
# # #     """
# # #     conn = None
# # #     cur = None
# # #     items = []
# # #     locations = []
# # #     try:
# # #         conn = get_conn()
# # #         cur = conn.cursor(dictionary=True)

# # #         # items for select
# # #         cur.execute("SELECT sku, name, unit FROM items ORDER BY name ASC")
# # #         items = cur.fetchall() or []

# # #         # try locations table, fallback to distinct location_id in stock_ledger
# # #         try:
# # #             cur.execute("SELECT name FROM locations ORDER BY name ASC")
# # #             locations = [r['name'] for r in cur.fetchall() or []]
# # #         except Exception:
# # #             cur.execute("SELECT DISTINCT location_id FROM stock_ledger WHERE location_id IS NOT NULL")
# # #             locations = [r['location_id'] for r in cur.fetchall() or []]

# # #     except Exception:
# # #         app.logger.exception("Failed to load receive page data")
# # #     finally:
# # #         if cur:
# # #             cur.close()
# # #         if conn:
# # #             conn.close()

# # #     return render_template('receive.html', items=items, locations=locations, user=session.get('username'))

# # # from datetime import datetime
# # # from flask import request, jsonify

# # # @app.route('/api/receive', methods=['POST'])
# # # def api_receive():
# # #     """
# # #     Accepts JSON:
# # #       { grn_no, supplier, po_ref, lines: [{ sku, qty, unit, batch_id, location, expiry }] }
# # #     Validates SKU existence per-line and returns per-line errors if any.
# # #     On success inserts stock_ledger rows and returns { status:'ok', grn_no:'...' }.
# # #     """
# # #     data = request.get_json(silent=True) or {}
# # #     grn_no = (data.get('grn_no') or '').strip()
# # #     supplier = (data.get('supplier') or '').strip()
# # #     po_ref = (data.get('po_ref') or '').strip()
# # #     lines = data.get('lines') or []

# # #     # generate GRN if not provided
# # #     if not grn_no:
# # #         grn_no = f"GRN-{datetime.utcnow().strftime('%Y%m%d')}-{int(datetime.utcnow().timestamp())}"

# # #     # basic header validation
# # #     if not supplier:
# # #         return jsonify({'status': 'error', 'message': 'Supplier is required'}), 400
# # #     if not lines or not any(l.get('sku') and float(l.get('qty', 0)) > 0 for l in lines):
# # #         return jsonify({'status': 'error', 'message': 'At least one valid line is required'}), 400

# # #     # Validate SKUs and other per-line checks
# # #     conn = None
# # #     cur = None
# # #     try:
# # #         conn = get_conn()
# # #         cur = conn.cursor(dictionary=True)

# # #         # collect unique SKUs from payload
# # #         skus = sorted({(l.get('sku') or '').strip() for l in lines if l.get('sku')})
# # #         sku_exists = {}
# # #         if skus:
# # #             # query items table for these SKUs
# # #             format_strings = ','.join(['%s'] * len(skus))
# # #             cur.execute(f"SELECT sku FROM items WHERE sku IN ({format_strings})", tuple(skus))
# # #             found = {r['sku'] for r in cur.fetchall() or []}
# # #             for s in skus:
# # #                 sku_exists[s] = (s in found)

# # #         # build per-line errors
# # #         errors = {}
# # #         for idx, ln in enumerate(lines):
# # #             ln_sku = (ln.get('sku') or '').strip()
# # #             ln_qty = 0
# # #             try:
# # #                 ln_qty = float(ln.get('qty') or 0)
# # #             except Exception:
# # #                 ln_qty = 0
# # #             line_errs = []
# # #             if not ln_sku:
# # #                 line_errs.append('SKU required')
# # #             elif ln_sku not in sku_exists:
# # #                 # SKU not present in payload unique set (empty) -> treat as missing
# # #                 line_errs.append('SKU not provided')
# # #             elif not sku_exists.get(ln_sku, False):
# # #                 line_errs.append('Unknown SKU')
# # #             if ln_qty <= 0:
# # #                 line_errs.append('Quantity must be greater than zero')
# # #             if line_errs:
# # #                 errors[idx] = line_errs

# # #         if errors:
# # #             return jsonify({'status': 'error', 'message': 'Validation errors', 'errors': errors}), 400

# # #         # optional: insert GRN header if table exists
# # #         try:
# # #             cur.execute("""
# # #                 INSERT INTO grn (grn_no, supplier, po_ref, created_at)
# # #                 VALUES (%s, %s, %s, NOW())
# # #             """, (grn_no, supplier, po_ref))
# # #         except Exception:
# # #             # ignore if grn table doesn't exist
# # #             pass

# # #         # Insert ledger rows for each line (positive qty)
# # #         for ln in lines:
# # #             sku = (ln.get('sku') or '').strip()
# # #             try:
# # #                 qty = float(ln.get('qty') or 0)
# # #             except Exception:
# # #                 qty = 0
# # #             unit = (ln.get('unit') or '').strip() or None
# # #             batch_id = (ln.get('batch_id') or '').strip() or None
# # #             location = (ln.get('location') or '').strip() or None
# # #             expiry = (ln.get('expiry') or '').strip() or None

# # #             if not sku or qty <= 0:
# # #                 continue

# # #             cur.execute("""
# # #                 INSERT INTO stock_ledger
# # #                   (sku, qty, txn_type, location_id, reference, batch_id, expiry_date, created_at)
# # #                 VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
# # #             """, (sku, qty, 'receive', location, grn_no, batch_id, expiry))

# # #         conn.commit()
# # #         return jsonify({'status': 'ok', 'grn_no': grn_no})
# # #     except Exception:
# # #         if conn:
# # #             conn.rollback()
# # #         app.logger.exception("Failed to process GRN")
# # #         return jsonify({'status': 'error', 'message': 'Failed to process GRN'}), 500
# # #     finally:
# # #         if cur:
# # #             cur.close()
# # #         if conn:
# # #             conn.close()



# # from datetime import datetime
# # from flask import request, jsonify

# # @app.route('/issue')
# # def issue():
# #     """
# #     Render the issue page with items and locations for selects.
# #     Template expects: items, locations, user
# #     """
# #     conn = None
# #     cur = None
# #     items = []
# #     locations = []
# #     try:
# #         conn = get_conn()
# #         cur = conn.cursor(dictionary=True)
# #         cur.execute("SELECT sku, name, unit FROM items ORDER BY name ASC")
# #         items = cur.fetchall() or []

# #         # try locations table first, fallback to distinct location_id
# #         try:
# #             cur.execute("SELECT name FROM locations ORDER BY name ASC")
# #             locations = [r['name'] for r in cur.fetchall() or []]
# #         except Exception:
# #             cur.execute("SELECT DISTINCT location_id FROM stock_ledger WHERE location_id IS NOT NULL")
# #             locations = [r['location_id'] for r in cur.fetchall() or []]
# #     except Exception:
# #         app.logger.exception("Failed to load issue page data")
# #     finally:
# #         if cur:
# #             cur.close()
# #         if conn:
# #             conn.close()

# #     return render_template('issue.html', items=items, locations=locations, user=session.get('username'))


# # @app.route('/api/issue', methods=['POST'])
# # def api_issue():
# #     """
# #     Accepts JSON payload:
# #       {
# #         delivery_no (optional),
# #         order_ref (optional),
# #         recipient, address, vehicle_reg, driver_name, remarks,
# #         lines: [{ sku, qty, unit, location, batch_id, expiry }]
# #       }

# #     Returns:
# #       - 200 { status:'ok', delivery_no: 'DN-20260818-1072', order_ref: 'PO-20260818-2500' }
# #       - 400 { status:'error', message:'...', errors: { line_index: ['err1','err2'] } }
# #       - 500 on server error
# #     """
# #     data = request.get_json(silent=True) or {}
# #     delivery_no = (data.get('delivery_no') or '').strip()
# #     order_ref = (data.get('order_ref') or '').strip()
# #     recipient = (data.get('recipient') or '').strip()
# #     address = (data.get('address') or '').strip()
# #     vehicle_reg = (data.get('vehicle_reg') or '').strip()
# #     driver_name = (data.get('driver_name') or '').strip()
# #     remarks = (data.get('remarks') or '').strip()
# #     lines = data.get('lines') or []

# #     # auto-generate delivery_no and order_ref if missing
# #     now = datetime.utcnow()
# #     if not delivery_no:
# #         delivery_no = f"DN-{now.strftime('%Y%m%d')}-{int(now.timestamp()) % 10000}"
# #     if not order_ref:
# #         order_ref = f"PO-{now.strftime('%Y%m%d')}-{int(now.timestamp()) % 10000}"

# #     # header validation
# #     if not recipient:
# #         return jsonify({'status':'error','message':'Recipient name is required'}), 400
# #     if not lines or not any(l.get('sku') and float(l.get('qty', 0)) > 0 for l in lines):
# #         return jsonify({'status':'error','message':'At least one valid line is required'}), 400

# #     conn = None
# #     cur = None
# #     try:
# #         conn = get_conn()
# #         cur = conn.cursor(dictionary=True)

# #         # Validate SKUs in bulk
# #         payload_skus = sorted({(l.get('sku') or '').strip() for l in lines if l.get('sku')})
# #         sku_exists = {}
# #         if payload_skus:
# #             placeholders = ','.join(['%s'] * len(payload_skus))
# #             cur.execute(f"SELECT sku FROM items WHERE sku IN ({placeholders})", tuple(payload_skus))
# #             found = {r['sku'] for r in cur.fetchall() or []}
# #             for s in payload_skus:
# #                 sku_exists[s] = (s in found)

# #         # per-line validation
# #         errors = {}
# #         for idx, ln in enumerate(lines):
# #             ln_sku = (ln.get('sku') or '').strip()
# #             try:
# #                 ln_qty = float(ln.get('qty') or 0)
# #             except Exception:
# #                 ln_qty = 0
# #             line_errs = []
# #             if not ln_sku:
# #                 line_errs.append('SKU required')
# #             elif ln_sku not in sku_exists:
# #                 line_errs.append('SKU not provided in payload')
# #             elif not sku_exists.get(ln_sku, False):
# #                 line_errs.append('Unknown SKU')
# #             if ln_qty <= 0:
# #                 line_errs.append('Quantity must be greater than zero')
# #             # optional: validate location exists if you want (skip for now)
# #             if line_errs:
# #                 errors[idx] = line_errs

# #         if errors:
# #             return jsonify({'status':'error','message':'Validation errors', 'errors': errors}), 400

# #         # Optional: insert delivery header into a delivery table if present
# #         try:
# #             cur.execute("""
# #                 INSERT INTO deliveries (delivery_no, order_ref, recipient, address, vehicle_reg, driver_name, remarks, created_at)
# #                 VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
# #             """, (delivery_no, order_ref, recipient, address, vehicle_reg, driver_name, remarks))
# #         except Exception:
# #             # ignore if deliveries table doesn't exist
# #             pass

# #         # Insert ledger rows for each line (negative qty)
# #         for ln in lines:
# #             sku = (ln.get('sku') or '').strip()
# #             try:
# #                 qty = float(ln.get('qty') or 0)
# #             except Exception:
# #                 qty = 0
# #             location = (ln.get('location') or '').strip() or None
# #             batch_id = (ln.get('batch_id') or '').strip() or None
# #             expiry = (ln.get('expiry') or '').strip() or None

# #             if not sku or qty <= 0:
# #                 continue

# #             cur.execute("""
# #                 INSERT INTO stock_ledger (sku, qty, txn_type, location_id, reference, batch_id, expiry_date, created_at)
# #                 VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
# #             """, (sku, -abs(qty), 'issue', location, delivery_no, batch_id, expiry))

# #         conn.commit()
# #         return jsonify({'status':'ok', 'delivery_no': delivery_no, 'order_ref': order_ref})
# #     except Exception:
# #         if conn:
# #             conn.rollback()
# #         app.logger.exception("Failed to create issue")
# #         return jsonify({'status':'error','message':'Failed to issue goods'}), 500
# #     finally:
# #         if cur:
# #             cur.close()
# #         if conn:
# #             conn.close()

# # # -------------------------
# # # Ledger page (recent)
# # # -------------------------
# # @app.route('/ledger')
# # def ledger():
# #     conn = None
# #     cur = None
# #     rows = []
# #     try:
# #         conn = get_conn()
# #         cur = conn.cursor(dictionary=True)
# #         cur.execute("""
# #             SELECT created_at, txn_type AS movement_type, sku, location_id, qty, reference, created_by AS username
# #             FROM stock_ledger
# #             ORDER BY created_at DESC
# #             LIMIT 200
# #         """)
# #         rows = cur.fetchall() or []
# #     except Exception:
# #         app.logger.exception("Failed to load ledger")
# #     finally:
# #         if cur:
# #             cur.close()
# #         if conn:
# #             conn.close()
# #     return render_template('ledger.html', rows=rows, user=session.get('username'))


# # import csv
# # import io
# # from flask import make_response

# # # @app.route('/locations')
# # # def locations():
# # #     """Render locations page with bin/rack mapping cards."""
# # #     conn = None
# # #     cur = None
# # #     rows = []
# # #     try:
# # #         conn = get_conn()
# # #         cur = conn.cursor(dictionary=True)
# # #         cur.execute("""
# # #             SELECT id, code, name, type, capacity, description
# # #             FROM locations
# # #             ORDER BY name ASC
# # #         """)
# # #         rows = cur.fetchall() or []
# # #     except Exception:
# # #         app.logger.exception("Failed to load locations")
# # #         rows = []
# # #     finally:
# # #         if cur:
# # #             cur.close()
# # #         if conn:
# # #             conn.close()

# # #     return render_template('locations.html', locations=rows, user=session.get('username'))


# # # @app.route('/api/locations', methods=['POST'])
# # # def api_create_location():
# # #     """
# # #     Create a new location.
# # #     Expects JSON: { code, name, type, capacity, description }
# # #     Returns: { status: 'ok', location: {...} } or { status:'error', message:'...' }
# # #     """
# # #     data = request.get_json(silent=True) or {}
# # #     code = (data.get('code') or '').strip()
# # #     name = (data.get('name') or '').strip()
# # #     loc_type = (data.get('type') or '').strip()
# # #     try:
# # #         capacity = int(data.get('capacity') or 0)
# # #     except Exception:
# # #         capacity = 0
# # #     description = (data.get('description') or '').strip()

# # #     if not code or not name:
# # #         return jsonify({'status': 'error', 'message': 'Code and name are required'}), 400

# # #     conn = None
# # #     cur = None
# # #     try:
# # #         conn = get_conn()
# # #         cur = conn.cursor(dictionary=True)

# # #         # prevent duplicate code
# # #         cur.execute("SELECT COUNT(1) AS cnt FROM locations WHERE code = %s", (code,))
# # #         if cur.fetchone().get('cnt', 0) > 0:
# # #             return jsonify({'status': 'error', 'message': 'Location code already exists'}), 400

# # #         cur.execute("""
# # #             INSERT INTO locations (code, name, type, capacity, description, created_at)
# # #             VALUES (%s, %s, %s, %s, %s, NOW())
# # #         """, (code, name, loc_type, capacity, description))
# # #         conn.commit()

# # #         # return created row (id may not be available without SELECT; do a select)
# # #         cur.execute("SELECT id, code, name, type, capacity, description FROM locations WHERE code = %s", (code,))
# # #         loc = cur.fetchone() or {
# # #             'id': None, 'code': code, 'name': name, 'type': loc_type, 'capacity': capacity, 'description': description
# # #         }

# # #         return jsonify({'status': 'ok', 'location': loc})
# # #     except Exception:
# # #         if conn:
# # #             conn.rollback()
# # #         app.logger.exception("Failed to create location")
# # #         return jsonify({'status': 'error', 'message': 'Failed to create location'}), 500
# # #     finally:
# # #         if cur:
# # #             cur.close()
# # #         if conn:
# # #             conn.close()


# # # @app.route('/export_locations')
# # # def export_locations():
# # #     """Export locations as CSV for download."""
# # #     conn = None
# # #     cur = None
# # #     try:
# # #         conn = get_conn()
# # #         cur = conn.cursor(dictionary=True)
# # #         cur.execute("SELECT code, name, type, capacity, description FROM locations ORDER BY name ASC")
# # #         rows = cur.fetchall() or []

# # #         si = io.StringIO()
# # #         writer = csv.writer(si)
# # #         writer.writerow(['Code', 'Name', 'Type', 'Capacity', 'Description'])
# # #         for r in rows:
# # #             writer.writerow([r.get('code',''), r.get('name',''), r.get('type',''), r.get('capacity',0), r.get('description','')])

# # #         output = make_response(si.getvalue())
# # #         output.headers["Content-Disposition"] = "attachment; filename=locations.csv"
# # #         output.headers["Content-type"] = "text/csv"
# # #         return output
# # #     except Exception:
# # #         app.logger.exception("Failed to export locations")
# # #         return redirect(url_for('locations'))
# # #     finally:
# # #         if cur:
# # #             cur.close()
# # #         if conn:
# # #             conn.close()
# # @app.route('/locations')
# # def locations():
# #     """Render locations page with bin/rack mapping cards."""
# #     conn = None
# #     cur = None
# #     rows = []
# #     try:
# #         conn = get_conn()
# #         cur = conn.cursor()
# #         cur.execute("""
# #             SELECT id, code, name, type, capacity, description
# #             FROM locations
# #             ORDER BY name ASC
# #         """)
# #         rows = cur.fetchall() or []
# #     except Exception:
# #         app.logger.exception("Failed to load locations")
# #         rows = []
# #     finally:
# #         if cur: cur.close()
# #         if conn: conn.close()

# #     return render_template('locations.html', locations=rows, user=session.get('username'))

# # @app.route('/api/locations', methods=['POST'])
# # def api_create_location():
# #     """
# #     Create a new location.
# #     Accepts JSON: { code, name, type, capacity, description }
# #     If code is empty, server generates a unique code.
# #     """
# #     data = request.get_json(silent=True) or {}
# #     code = (data.get('code') or '').strip()
# #     name = (data.get('name') or '').strip()
# #     loc_type = (data.get('type') or '').strip() or 'Storage'
# #     try:
# #         capacity = int(data.get('capacity') or 0)
# #     except Exception:
# #         capacity = 0
# #     description = (data.get('description') or '').strip()

# #     if not name:
# #         return jsonify({'status': 'error', 'message': 'Name is required'}), 400

# #     conn = None
# #     cur = None
# #     try:
# #         conn = get_conn()
# #         cur = conn.cursor()

# #         # If code provided, validate uniqueness
# #         if code:
# #             cur.execute("SELECT COUNT(1) AS cnt FROM locations WHERE code = ?", (code,))
# #             row = cur.fetchone()
# #             if row and row['cnt'] > 0:
# #                 return jsonify({'status': 'error', 'message': 'Location code already exists'}), 400
# #         else:
# #             # Auto-generate code: TYPE_ABBR-YYYYMMDDHHMMSS
# #             # Use first 2-4 letters of type (uppercase) as prefix
# #             prefix = ''.join([c for c in loc_type.upper() if c.isalnum()])[:4] or 'LOC'
# #             ts = datetime.utcnow().strftime('%Y%m%d%H%M%S')
# #             code = f"{prefix}-{ts}"

# #             # Ensure uniqueness (very unlikely collision, but check)
# #             cur.execute("SELECT COUNT(1) AS cnt FROM locations WHERE code = ?", (code,))
# #             row = cur.fetchone()
# #             if row and row['cnt'] > 0:
# #                 # fallback to include microseconds
# #                 ts = datetime.utcnow().strftime('%Y%m%d%H%M%S%f')
# #                 code = f"{prefix}-{ts}"

# #         # Insert row
# #         cur.execute("""
# #             INSERT INTO locations (code, name, type, capacity, description, created_at)
# #             VALUES (?, ?, ?, ?, ?, datetime('now'))
# #         """, (code, name, loc_type, capacity, description))
# #         conn.commit()

# #         # Return created row
# #         cur.execute("SELECT id, code, name, type, capacity, description FROM locations WHERE code = ?", (code,))
# #         loc = cur.fetchone()
# #         return jsonify({'status': 'ok', 'location': dict(loc) if loc else {
# #             'id': None, 'code': code, 'name': name, 'type': loc_type, 'capacity': capacity, 'description': description
# #         }}), 201

# #     except Exception:
# #         if conn:
# #             try:
# #                 conn.rollback()
# #             except Exception:
# #                 pass
# #         app.logger.exception("Failed to create location")
# #         return jsonify({'status': 'error', 'message': 'Failed to create location'}), 500
# #     finally:
# #         if cur: cur.close()
# #         if conn: conn.close()



# # @app.route('/export_locations')
# # def export_locations():
# #     """Export locations as CSV for download."""
# #     conn = None
# #     cur = None
# #     try:
# #         conn = get_conn()
# #         cur = conn.cursor()
# #         cur.execute("SELECT code, name, type, capacity, description FROM locations ORDER BY name ASC")
# #         rows = cur.fetchall() or []

# #         si = io.StringIO()
# #         writer = csv.writer(si)
# #         writer.writerow(['Code', 'Name', 'Type', 'Capacity', 'Description'])
# #         for r in rows:
# #             writer.writerow([r['code'], r['name'], r['type'], r['capacity'], r['description']])

# #         output = make_response(si.getvalue())
# #         output.headers["Content-Disposition"] = "attachment; filename=locations.csv"
# #         output.headers["Content-type"] = "text/csv"
# #         return output
# #     except Exception:
# #         app.logger.exception("Failed to export locations")
# #         return redirect(url_for('locations'))
# #     finally:
# #         if cur: cur.close()
# #         if conn: conn.close()

# # @app.route('/api/locations/<string:code>', methods=['PUT'])
# # def api_update_location(code):
# #     data = request.get_json(silent=True) or {}
# #     new_code = (data.get('code') or '').strip()
# #     name = (data.get('name') or '').strip()
# #     loc_type = (data.get('type') or '').strip() or 'Storage'
# #     try:
# #         capacity = int(data.get('capacity') or 0)
# #     except Exception:
# #         capacity = 0
# #     description = (data.get('description') or '').strip()

# #     if not name:
# #         return jsonify({'status': 'error', 'message': 'Name is required'}), 400

# #     conn = None
# #     cur = None
# #     try:
# #         conn = get_conn()
# #         cur = conn.cursor()

# #         # ensure original exists
# #         cur.execute("SELECT id FROM locations WHERE code = ?", (code,))
# #         row = cur.fetchone()
# #         if not row:
# #             return jsonify({'status': 'error', 'message': 'Location not found'}), 404

# #         # if new_code provided and different, ensure uniqueness
# #         if new_code and new_code != code:
# #             cur.execute("SELECT COUNT(1) AS cnt FROM locations WHERE code = ?", (new_code,))
# #             r = cur.fetchone()
# #             if r and r['cnt'] > 0:
# #                 return jsonify({'status': 'error', 'message': 'New code already exists'}), 400
# #         final_code = new_code or code

# #         # Try update including description; fallback if column missing
# #         try:
# #             cur.execute("""
# #                 UPDATE locations
# #                 SET code = ?, name = ?, type = ?, capacity = ?, description = ?
# #                 WHERE code = ?
# #             """, (final_code, name, loc_type, capacity, description, code))
# #         except sqlite3.OperationalError:
# #             # fallback without description
# #             cur.execute("""
# #                 UPDATE locations
# #                 SET code = ?, name = ?, type = ?, capacity = ?
# #                 WHERE code = ?
# #             """, (final_code, name, loc_type, capacity, code))

# #         conn.commit()
# #         cur.execute("SELECT id, code, name, type, capacity, description FROM locations WHERE code = ?", (final_code,))
# #         loc = cur.fetchone()
# #         return jsonify({'status': 'ok', 'location': dict(loc) if loc else {'code': final_code, 'name': name, 'type': loc_type, 'capacity': capacity, 'description': description}}), 200
# #     except Exception:
# #         if conn:
# #             try:
# #                 conn.rollback()
# #             except Exception:
# #                 pass
# #         app.logger.exception("Failed to update location")
# #         return jsonify({'status': 'error', 'message': 'Failed to update location'}), 500
# #     finally:
# #         if cur: cur.close()
# #         if conn: conn.close()


# # @app.route('/api/locations/<string:code>', methods=['DELETE'])
# # def api_delete_location(code):
# #     conn = None
# #     cur = None
# #     try:
# #         conn = get_conn()
# #         cur = conn.cursor()
# #         cur.execute("SELECT id FROM locations WHERE code = ?", (code,))
# #         if not cur.fetchone():
# #             return jsonify({'status': 'error', 'message': 'Location not found'}), 404

# #         # Optionally: check for dependent records (stock_ledger, items referencing location) before delete
# #         # Example: prevent delete if stock exists at location
# #         try:
# #             cur.execute("SELECT COUNT(1) AS cnt FROM stock_ledger WHERE location_id = ?", (code,))
# #             r = cur.fetchone()
# #             if r and r['cnt'] > 0:
# #                 return jsonify({'status': 'error', 'message': 'Cannot delete location with stock movements. Clear stock first.'}), 400
# #         except Exception:
# #             # if stock_ledger doesn't exist or query fails, continue with delete
# #             pass

# #         cur.execute("DELETE FROM locations WHERE code = ?", (code,))
# #         conn.commit()
# #         return jsonify({'status': 'ok'}), 200
# #     except Exception:
# #         if conn:
# #             try:
# #                 conn.rollback()
# #             except Exception:
# #                 pass
# #         app.logger.exception("Failed to delete location")
# #         return jsonify({'status': 'error', 'message': 'Failed to delete location'}), 500
# #     finally:
# #         if cur: cur.close()
# #         if conn: conn.close()


# # if __name__ == '__main__':
# #     app.run(host='0.0.0.0', port=int(os.getenv('PORT', 5000)), debug=True)






# # # # export FLASK_SECRET="replace-with-secret"
# # # export SQLITE_PATH="/path/to/busia_wms.sqlite3"
# # # export BUSIA_SMTP_HOST="smtp.example.com"
# # # export BUSIA_SMTP_PORT="587"
# # # export BUSIA_SMTP_USER="smtp-user"    help  add some onmy  code so as it can  GRN no. is automatically generated and the order reference is also automatically generated too and also once the goods received are checked and once confirm receipt and update stock button is clicked a document is automatically generated and a soft copy is sent to the supplier through email to confirm that is received
# # # export BUSIA_SMTP_PASS="smtp-pass"
# # # export BUSIA_EMAIL_FROM="no-reply@example.com"
# # # export BUSIA_EMAIL_FROM_NAME="Busia WMS"


# # # @app.route('/api/locations', methods=['POST'])
# # # def api_create_location():
# # #     """
# # #     Create a new location.
# # #     Expects JSON: { code, name, type, capacity, description }
# # #     Returns: { status: 'ok', location: {...} } or { status:'error', message:'...' }
# # #     """
# # #     data = request.get_json(silent=True) or {}
# # #     code = (data.get('code') or '').strip()
# # #     name = (data.get('name') or '').strip()
# # #     loc_type = (data.get('type') or '').strip()
# # #     try:
# # #         capacity = int(data.get('capacity') or 0)
# # #     except Exception:
# # #         capacity = 0
# # #     description = (data.get('description') or '').strip()

# # #     if not code or not name:
# # #         return jsonify({'status': 'error', 'message': 'Code and name are required'}), 400

# # #     conn = None
# # #     cur = None
# # #     try:
# # #         conn = get_conn()
# # #         cur = conn.cursor()

# # #         # prevent duplicate code
# # #         cur.execute("SELECT COUNT(1) AS cnt FROM locations WHERE code = ?", (code,))
# # #         row = cur.fetchone()
# # #         if row and row['cnt'] > 0:
# # #             return jsonify({'status': 'error', 'message': 'Location code already exists'}), 400

# # #         cur.execute("""
# # #             INSERT INTO locations (code, name, type, capacity, description, created_at)
# # #             VALUES (?, ?, ?, ?, ?, datetime('now'))
# # #         """, (code, name, loc_type, capacity, description))
# # #         conn.commit()

# # #         # return created row
# # #         cur.execute("SELECT id, code, name, type, capacity, description FROM locations WHERE code = ?", (code,))
# # #         loc = cur.fetchone() or {
# # #             'id': None, 'code': code, 'name': name, 'type': loc_type,
# # #             'capacity': capacity, 'description': description
# # #         }

# # #         return jsonify({'status': 'ok', 'location': loc})
# # #     except Exception:
# # #         if conn: conn.rollback()
# # #         app.logger.exception("Failed to create location")
# # #         return jsonify({'status': 'error', 'message': 'Failed to create location'}), 500
# # #     finally:
# # #         if cur: cur.close()
# # #         if conn: conn.close()



# # # @app.route('/')
# # # def index():
# # #     conn = None
# # #     cur = None
# # #     items = []
# # #     recent_movements = []
# # #     try:
# # #         conn = get_conn()
# # #         cur = conn.cursor(dictionary=True)
# # #         cur.execute("""
# # #             SELECT
# # #               i.sku,
# # #               i.name,
# # #               i.unit,
# # #               i.category,
# # #               COALESCE(SUM(sl.qty), 0) AS on_hand,
# # #               COALESCE(i.reorder_level, %s) AS reorder_level
# # #             FROM items i
# # #             LEFT JOIN stock_ledger sl ON sl.sku = i.sku
# # #             GROUP BY i.sku, i.name, i.unit, i.category, i.reorder_level
# # #             ORDER BY i.name ASC
# # #         """, (LOW_STOCK_THRESHOLD,))
# # #         items = cur.fetchall() or []

# # #         cur.execute("""
# # #             SELECT when_ts AS `when`, sku, qty, txn_type
# # #             FROM stock_ledger
# # #             ORDER BY when_ts DESC
# # #             LIMIT 5
# # #         """)
# # #         recent_movements = cur.fetchall() or []
# # #     except Exception:
# # #         app.logger.exception("Failed to load items or movements")
# # #     finally:
# # #         if cur:
# # #             cur.close()
# # #         if conn:
# # #             conn.close()

# # #     low_alerts = [it for it in items if int(it.get('on_hand', 0)) <= int(it.get('reorder_level', LOW_STOCK_THRESHOLD))]

# # #     stats = {
# # #         'active_skus': len(items),
# # #         'units_on_hand': sum(int(it.get('on_hand', 0)) for it in items),
# # #         'recent_movements': len(recent_movements)
# # #     }

# # #     return render_template(
# # #         'index.html',
# # #         items=items,
# # #         user=session.get('username'),
# # #         low_alerts=low_alerts,
# # #         recent_movements=recent_movements,
# # #         stats=stats,
# # #         now=datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')
# # #     )





# # # --- add this API endpoint ---
# # # @app.route('/api/dashboard', methods=['GET'])
# # # def api_dashboard():
# # #     conn = None
# # #     cur = None
# # #     try:
# # #         conn = get_conn()
# # #         try:
# # #             cur = conn.cursor(dictionary=True)
# # #             dict_cursor = True
# # #         except Exception:
# # #             cur = conn.cursor()
# # #             dict_cursor = False

# # #         # Active SKUs
# # #         cur.execute("SELECT COUNT(DISTINCT sku) AS active_skus FROM items")
# # #         row = cur.fetchone()
# # #         active_skus = (row['active_skus'] if dict_cursor else row[0]) if row else 0

# # #         # Units on hand (prefer on_hand column)
# # #         try:
# # #             cur.execute("SELECT COALESCE(SUM(on_hand),0) AS units_on_hand FROM items")
# # #             row = cur.fetchone()
# # #             units_on_hand = (row['units_on_hand'] if dict_cursor else row[0]) if row else 0
# # #         except Exception:
# # #             cur.execute("SELECT COALESCE(SUM(qty),0) AS units_on_hand FROM stock_ledger")
# # #             row = cur.fetchone()
# # #             units_on_hand = (row['units_on_hand'] if dict_cursor else row[0]) if row else 0

# # #         # Low stock alerts
# # #         cur.execute("SELECT COUNT(1) AS low_stock_alerts FROM items WHERE reorder_level IS NOT NULL AND on_hand <= reorder_level")
# # #         row = cur.fetchone()
# # #         low_stock_alerts = (row['low_stock_alerts'] if dict_cursor else row[0]) if row else 0

# # #         # Reorder alerts (limit 10)
# # #         cur.execute("""
# # #             SELECT sku, name, unit, category, on_hand, reorder_level
# # #             FROM items
# # #             WHERE reorder_level IS NOT NULL AND on_hand <= reorder_level
# # #             ORDER BY (reorder_level - on_hand) DESC
# # #             LIMIT 10
# # #         """)
# # #         rows = cur.fetchall() or []
# # #         reorder_alerts = []
# # #         for r in rows:
# # #             if dict_cursor:
# # #                 reorder_alerts.append({
# # #                     'sku': r.get('sku'),
# # #                     'name': r.get('name'),
# # #                     'unit': r.get('unit'),
# # #                     'category': r.get('category'),
# # #                     'on_hand': r.get('on_hand'),
# # #                     'reorder_level': r.get('reorder_level')
# # #                 })
# # #             else:
# # #                 reorder_alerts.append({
# # #                     'sku': r[0], 'name': r[1], 'unit': r[2],
# # #                     'category': r[3], 'on_hand': r[4], 'reorder_level': r[5]
# # #                 })

# # #         # Recent movements (limit 10) from stock_ledger
# # #         cur.execute("""
# # #             SELECT when_ts AS ts, sku, qty, txn_type, user
# # #             FROM stock_ledger
# # #             ORDER BY when_ts DESC
# # #             LIMIT 10
# # #         """)
# # #         rows = cur.fetchall() or []
# # #         recent_movements = []
# # #         for r in rows:
# # #             if dict_cursor:
# # #                 recent_movements.append({
# # #                     'sku': r.get('sku'),
# # #                     'qty': r.get('qty'),
# # #                     'type': r.get('txn_type'),
# # #                     'user': r.get('user'),
# # #                     'ts': str(r.get('ts'))
# # #                 })
# # #             else:
# # #                 recent_movements.append({
# # #                     'sku': r[1], 'qty': r[2], 'type': r[3], 'user': r[4], 'ts': str(r[0])
# # #                 })

# # #         payload = {
# # #             'active_skus': int(active_skus),
# # #             'units_on_hand': int(units_on_hand),
# # #             'low_stock_alerts': int(low_stock_alerts),
# # #             'reorder_alerts': reorder_alerts,
# # #             'recent_movements': recent_movements
# # #         }
# # #         return jsonify(payload), 200

# # #     except Exception as exc:
# # #         app.logger.exception("api_dashboard failed: %s", exc)
# # #         return jsonify({'status': 'error', 'message': 'Server error fetching dashboard'}), 500
# # #     finally:
# # #         try: 
# # #             if cur: cur.close()
# # #         except Exception:
# # #             pass
# # #         try:
# # #             if conn: conn.close()
# # #         except Exception:
# # #             pass
# # # Temporary debug version of /api/dashboard — paste in place of your current route.
# # # Remove this after debugging.

# # # -------------------------
# # # API: create item (AJAX)
# # # -------------------------
# # # @app.route('/api/items', methods=['POST'])
# # # def api_create_item():

# # #     app.logger.debug("api_create_item: raw body: %r", request.get_data())
# # #     app.logger.debug("api_create_item: parsed json: %r", request.get_json(silent=True))
# # #     app.logger.debug("api_create_item: headers: %r", dict(request.headers))

# # #     data = request.get_json(silent=True)
# # #     if not data:
# # #         return jsonify({'status':'error','message':'Invalid JSON payload'}), 400

# # #     sku = (data.get('sku') or '').strip()
# # #     name = (data.get('name') or '').strip()
# # #     unit = (data.get('unit') or '').strip() or 'pcs'
# # #     category = (data.get('category') or '').strip()
# # #     reorder_level = parse_reorder_level(data.get('reorder_level'))

# # #     if not sku or not name:
# # #         return jsonify({'status':'error','message':'SKU and name are required'}), 400

# # #     conn = None
# # #     cur = None
# # #     try:
# # #         conn = get_conn()
# # #         try:
# # #             cur = conn.cursor(dictionary=True)
# # #             dict_cursor = True
# # #         except Exception:
# # #             cur = conn.cursor()
# # #             dict_cursor = False

# # #         # duplicate check
# # #         cur.execute("SELECT COUNT(1) AS cnt FROM items WHERE sku = %s", (sku,))
# # #         row = cur.fetchone()
# # #         cnt = row.get('cnt') if dict_cursor or hasattr(row, 'keys') else (row[0] if row else 0)
# # #         if cnt > 0:
# # #             return jsonify({'status':'error','message':'SKU already exists'}), 400

# # #         # insert
# # #         cur.execute("""
# # #             INSERT INTO items (sku, name, unit, category, reorder_level, created_at)
# # #             VALUES (%s, %s, %s, %s, %s, NOW())
# # #         """, (sku, name, unit, category, reorder_level))
# # #         conn.commit()

# # #         item = {
# # #             'sku': sku,
# # #             'name': name,
# # #             'unit': unit,
# # #             'category': category,
# # #             'on_hand': 0,
# # #             'reorder_level': reorder_level if reorder_level is not None else LOW_STOCK_THRESHOLD
# # #         }
# # #         return jsonify({'status':'ok', 'item': item}), 200

# # #     except Exception as exc:
# # #         if conn: conn.rollback()
# # #         app.logger.exception("Failed to create item (api_create_item): %s", exc)
# # #         return jsonify({'status':'error','message':'Server error while creating item. Check server logs.'}), 500
# # #     finally:
# # #         if cur: cur.close()
# # #         if conn: conn.close()
# # # @app.route('/api/items', methods=['POST'])
# # # def api_create_item():
# # #     """
# # #     Single robust handler for POST /api/items.
# # #     - Logs request data inside request context only.
# # #     - Accepts JSON; falls back to form-encoded data.
# # #     - Validates sku and name, parses reorder_level safely.
# # #     - Inserts into DB with MySQL/SQLite placeholder fallbacks.
# # #     """
# # #     # Debug (only inside request context)
# # #     app.logger.debug("api_create_item: raw body: %r", request.get_data())
# # #     app.logger.debug("api_create_item: parsed json: %r", request.get_json(silent=True))
# # #     app.logger.debug("api_create_item: headers: %r", dict(request.headers))

# # #     # Try JSON first, then fallback to form data
# # #     data = request.get_json(silent=True)
# # #     if data is None:
# # #         form = request.form.to_dict()
# # #         if form:
# # #             data = form
# # #             app.logger.debug("api_create_item: using form fallback: %r", data)
# # #         else:
# # #             return jsonify({'status': 'error', 'message': 'Invalid JSON payload or empty body'}), 400

# # #     # Extract and normalize fields
# # #     sku = (data.get('sku') or '').strip()
# # #     name = (data.get('name') or '').strip()
# # #     unit = (data.get('unit') or '').strip() or 'pcs'
# # #     category = (data.get('category') or '').strip()
# # #     try:
# # #         rl_raw = data.get('reorder_level')
# # #         reorder_level = None if rl_raw in (None, '') else int(rl_raw)
# # #     except Exception:
# # #         return jsonify({'status': 'error', 'message': 'Reorder level must be an integer or omitted'}), 400

# # #     if not sku or not name:
# # #         return jsonify({'status': 'error', 'message': 'SKU and name are required'}), 400

# # #     conn = None
# # #     cur = None
# # #     try:
# # #         conn = get_conn()
# # #         # dictionary cursor if available
# # #         try:
# # #             cur = conn.cursor(dictionary=True)
# # #             dict_cursor = True
# # #         except Exception:
# # #             cur = conn.cursor()
# # #             dict_cursor = False

# # #         # Duplicate check (parameterized; fallback to sqlite style)
# # #         executed = False
# # #         try:
# # #             cur.execute("SELECT COUNT(1) AS cnt FROM items WHERE sku = %s", (sku,))
# # #             executed = True
# # #         except Exception:
# # #             try:
# # #                 cur.execute("SELECT COUNT(1) AS cnt FROM items WHERE sku = ?", (sku,))
# # #                 executed = True
# # #             except Exception:
# # #                 # last resort (avoid in production)
# # #                 cur.execute(f"SELECT COUNT(1) AS cnt FROM items WHERE sku = '{sku}'")
# # #                 executed = True

# # #         row = cur.fetchone()
# # #         cnt = 0
# # #         if row:
# # #             if dict_cursor or hasattr(row, 'keys'):
# # #                 cnt = row.get('cnt') or 0
# # #             else:
# # #                 cnt = row[0] if len(row) > 0 else 0

# # #         if int(cnt) > 0:
# # #             return jsonify({'status': 'error', 'message': 'SKU already exists'}), 400

# # #         # Insert item (MySQL then sqlite fallback)
# # #         insert_sql = """
# # #             INSERT INTO items (sku, name, unit, category, reorder_level, created_at)
# # #             VALUES (%s, %s, %s, %s, %s, NOW())
# # #         """
# # #         params = (sku, name, unit, category, reorder_level)
# # #         inserted = False
# # #         try:
# # #             cur.execute(insert_sql, params)
# # #             inserted = True
# # #         except Exception:
# # #             try:
# # #                 insert_sql_sqlite = insert_sql.replace('%s', '?').replace('NOW()', "datetime('now')")
# # #                 cur.execute(insert_sql_sqlite, params)
# # #                 inserted = True
# # #             except Exception as e:
# # #                 app.logger.debug("api_create_item: insert fallback failed: %s", e)
# # #                 inserted = False

# # #         if not inserted:
# # #             conn.rollback()
# # #             return jsonify({'status': 'error', 'message': 'Database insert failed'}), 500

# # #         conn.commit()

# # #         item = {
# # #             'sku': sku,
# # #             'name': name,
# # #             'unit': unit,
# # #             'category': category,
# # #             'on_hand': 0,
# # #             'reorder_level': reorder_level if reorder_level is not None else LOW_STOCK_THRESHOLD
# # #         }
# # #         return jsonify({'status': 'ok', 'item': item}), 200

# # #     except Exception as exc:
# # #         if conn:
# # #             try:
# # #                 conn.rollback()
# # #             except Exception:
# # #                 pass
# # #         app.logger.exception("Failed to create item (api_create_item): %s", exc)
# # #         return jsonify({'status': 'error', 'message': 'Server error while creating item. Check server logs.'}), 500
# # #     finally:
# # #         try:
# # #             if cur: cur.close()
# # #         except Exception:
# # #             pass
# # #         try:
# # #             if conn: conn.close()
# # #         except Exception:
# # #             pass

# # # Add or replace in app.py (merge with your existing imports)
# # # from collections import defaultdict

# # # @app.route('/items')
# # # def items():
# # #     """
# # #     Items dashboard: returns items, stats, low_alerts, recent_movements, units/categories if needed.
# # #     Works with sqlite3 or MySQL connectors (dictionary cursor fallback).
# # #     """
# # #     conn = None
# # #     cur = None
# # #     items = []
# # #     locations = []
# # #     low_alerts = []
# # #     stats = {'active_skus': 0, 'units_on_hand': 0, 'recent_movements': 0}
# # #     recent_movements = []

# # #     try:
# # #         conn = get_conn()
# # #         # dictionary cursor if supported
# # #         try:
# # #             cur = conn.cursor(dictionary=True)
# # #         except Exception:
# # #             cur = conn.cursor()

# # #         # Query items with aggregated on_hand
# # #         sql = """
# # #             SELECT
# # #               i.sku,
# # #               i.name,
# # #               i.unit,
# # #               i.category,
# # #               COALESCE(i.reorder_level, %s) AS reorder_level,
# # #               COALESCE(SUM(sl.qty), 0) AS on_hand
# # #             FROM items i
# # #             LEFT JOIN stock_ledger sl ON sl.sku = i.sku
# # #             GROUP BY i.sku, i.name, i.unit, i.category, i.reorder_level
# # #             ORDER BY i.name ASC
# # #         """
# # #         # Try MySQL-style first (most common in your app)
# # #         try:
# # #             cur.execute(sql, (LOW_STOCK_THRESHOLD,))
# # #         except Exception:
# # #             # fallback: try sqlite placeholder style
# # #             try:
# # #                 cur.execute(sql.replace('%s', '?'), (LOW_STOCK_THRESHOLD,))
# # #             except Exception:
# # #                 # last resort: interpolate integer (safe if LOW_STOCK_THRESHOLD is int)
# # #                 cur.execute(sql.replace('%s', str(int(LOW_STOCK_THRESHOLD))))

# # #         raw = cur.fetchall() or []
# # #         items = []
# # #         for r in raw:
# # #             if hasattr(r, 'keys'):
# # #                 row = {k: r[k] for k in r.keys()}
# # #             else:
# # #                 cols = [c[0] for c in cur.description] if cur.description else []
# # #                 row = {cols[i]: r[i] for i in range(len(cols))}
# # #             # normalize types
# # #             row['on_hand'] = int(row.get('on_hand') or 0)
# # #             rl = row.get('reorder_level')
# # #             row['reorder_level'] = int(rl) if rl is not None and str(rl).strip() != '' else None
# # #             items.append(row)

# # #         # Compute stats
# # #         stats['active_skus'] = len(items)
# # #         stats['units_on_hand'] = sum(it.get('on_hand', 0) for it in items)
# # #         # low alerts: on_hand <= reorder_level (only when reorder_level defined and > 0)
# # #         low_alerts = [it for it in items if (it.get('reorder_level') is not None and it.get('on_hand', 0) <= it.get('reorder_level'))]

# # #         # recent movements (10 latest)
# # #         try:
# # #             cur.execute("""
# # #                 SELECT when_ts AS `when`, sku, qty, txn_type
# # #                 FROM stock_ledger
# # #                 ORDER BY when_ts DESC
# # #                 LIMIT 10
# # #             """)
# # #             mv = cur.fetchall() or []
# # #             recent_movements = []
# # #             for r in mv:
# # #                 if hasattr(r, 'keys'):
# # #                     recent_movements.append({'when': r.get('when'), 'sku': r.get('sku'), 'qty': r.get('qty'), 'txn_type': r.get('txn_type')})
# # #                 else:
# # #                     recent_movements.append({'when': r[0], 'sku': r[1], 'qty': r[2], 'txn_type': r[3]})
# # #             stats['recent_movements'] = len(recent_movements)
# # #         except Exception:
# # #             recent_movements = []
# # #             stats['recent_movements'] = 0

# # #     except Exception:
# # #         app.logger.exception("Failed to load items dashboard")
# # #         items = []
# # #         low_alerts = []
# # #         recent_movements = []
# # #     finally:
# # #         try:
# # #             if cur: cur.close()
# # #         except: pass
# # #         try:
# # #             if conn: conn.close()
# # #         except: pass

# # #     return render_template('items.html',
# # #                            items=items,
# # #                            locations=locations,
# # #                            low_alerts=low_alerts,
# # #                            stats=stats,
# # #                            recent_movements=recent_movements,
# # #                            units=DEFAULT_UNITS,
# # #                            categories=DEFAULT_CATEGORIES,
# # #                            user=session.get('username'))



# # # # @app.route('/items')
# # # # def items():
# # # #     """
# # # #     Robust /items route compatible with SQLite and MySQL connectors.
# # # #     Normalizes DB rows to a list of dicts for the template.
# # # #     """
# # # #     conn = None
# # # #     cur = None
# # # #     items = []
# # # #     locations = []

# # # #     try:
# # # #         conn = get_conn()

# # # #         # Try to create a dictionary cursor if supported (MySQL connector)
# # # #         try:
# # # #             cur = conn.cursor(dictionary=True)
# # # #             dict_cursor_supported = True
# # # #         except Exception:
# # # #             # Fallback for sqlite3 and other drivers
# # # #             cur = conn.cursor()
# # # #             dict_cursor_supported = False

# # # #         # SQL uses a placeholder; we'll attempt SQLite-style first, then MySQL-style
# # # #         sql_sqlite = """
# # # #             SELECT
# # # #               i.sku,
# # # #               i.name,
# # # #               i.unit,
# # # #               i.category,
# # # #               COALESCE(i.reorder_level, ?) AS reorder_level,
# # # #               COALESCE(SUM(sl.qty), 0) AS on_hand
# # # #             FROM items i
# # # #             LEFT JOIN stock_ledger sl ON sl.item_sku = i.sku
# # # #             GROUP BY i.sku, i.name, i.unit, i.category, i.reorder_level
# # # #             ORDER BY i.name ASC
# # # #         """

# # # #         executed = False
# # # #         # Try SQLite-style parameter
# # # #         try:
# # # #             cur.execute(sql_sqlite, (LOW_STOCK_THRESHOLD,))
# # # #             executed = True
# # # #         except Exception:
# # # #             # Try MySQL-style (%s)
# # # #             try:
# # # #                 sql_mysql = sql_sqlite.replace('?', '%s')
# # # #                 cur.execute(sql_mysql, (LOW_STOCK_THRESHOLD,))
# # # #                 executed = True
# # # #             except Exception:
# # # #                 # Last resort: interpolate integer directly (safe if LOW_STOCK_THRESHOLD is an int)
# # # #                 try:
# # # #                     sql_fallback = sql_sqlite.replace('?', str(int(LOW_STOCK_THRESHOLD)))
# # # #                     cur.execute(sql_fallback)
# # # #                     executed = True
# # # #                 except Exception:
# # # #                     executed = False

# # # #         if not executed:
# # # #             raise RuntimeError("Failed to execute items query with available DB driver.")

# # # #         # Fetch rows and normalize to list of dicts
# # # #         raw_rows = cur.fetchall() or []
# # # #         items = []
# # # #         for r in raw_rows:
# # # #             # mapping-like row (MySQL dict cursor or sqlite3.Row)
# # # #             if hasattr(r, 'keys'):
# # # #                 # r behaves like a mapping
# # # #                 items.append({k: r[k] for k in r.keys()})
# # # #             else:
# # # #                 # tuple row: map using cursor.description
# # # #                 cols = [c[0] for c in cur.description] if cur.description else []
# # # #                 items.append({cols[i]: r[i] for i in range(len(cols))})

# # # #         # Load locations (try simple query; normalize results)
# # # #         try:
# # # #             cur.execute("SELECT name FROM locations ORDER BY name ASC")
# # # #             loc_rows = cur.fetchall() or []
# # # #             locations = []
# # # #             for r in loc_rows:
# # # #                 if hasattr(r, 'keys'):
# # # #                     locations.append(r.get('name'))
# # # #                 else:
# # # #                     locations.append(r[0] if len(r) > 0 else None)
# # # #             locations = [l for l in locations if l]
# # # #         except Exception:
# # # #             # fallback: distinct location_id from ledger
# # # #             try:
# # # #                 cur.execute("SELECT DISTINCT location_id FROM stock_ledger WHERE location_id IS NOT NULL")
# # # #                 loc_rows = cur.fetchall() or []
# # # #                 locations = []
# # # #                 for r in loc_rows:
# # # #                     if hasattr(r, 'keys'):
# # # #                         locations.append(r.get('location_id'))
# # # #                     else:
# # # #                         locations.append(r[0] if len(r) > 0 else None)
# # # #                 locations = [l for l in locations if l]
# # # #             except Exception:
# # # #                 locations = []

# # # #     except Exception:
# # # #         app.logger.exception("Failed to load items or locations")
# # # #         items = []
# # # #         locations = []
# # # #     finally:
# # # #         try:
# # # #             if cur:
# # # #                 cur.close()
# # # #         except:
# # # #             pass
# # # #         try:
# # # #             if conn:
# # # #                 conn.close()
# # # #         except:
# # # #             pass

# # # #     return render_template('items.html', items=items, locations=locations, user=session.get('username'))


# # # # @app.route('/items')
# # # # def items():
# # # #     conn = None
# # # #     cur = None
# # # #     items = []
# # # #     locations = []
# # # #     try:
# # # #         conn = get_conn()
# # # #         cur = conn.cursor(dictionary=True)

# # # #         cur.execute("""
# # # #             SELECT
# # # #               i.sku,
# # # #               i.name,
# # # #               i.unit,
# # # #               i.category,
# # # #               COALESCE(i.reorder_level, %s) AS reorder_level,
# # # #               COALESCE(SUM(sl.qty), 0) AS on_hand
# # # #             FROM items i
# # # #             LEFT JOIN stock_ledger sl ON sl.item_sku = i.sku
# # # #             GROUP BY i.sku, i.name, i.unit, i.category, i.reorder_level
# # # #             ORDER BY i.name ASC
# # # #         """, (LOW_STOCK_THRESHOLD,))
# # # #         items = cur.fetchall() or []

# # # #         try:
# # # #             cur.execute("SELECT name FROM locations ORDER BY name ASC")
# # # #             locations = [r['name'] for r in cur.fetchall() or []]
# # # #         except Exception:
# # # #             try:
# # # #                 cur.execute("SELECT DISTINCT location_id FROM stock_ledger WHERE location_id IS NOT NULL")
# # # #                 locations = [r['location_id'] for r in cur.fetchall() or []]
# # # #             except Exception:
# # # #                 locations = []

# # # #     except Exception:
# # # #         app.logger.exception("Failed to load items or locations")
# # # #         items = []
# # # #         locations = []
# # # #     finally:
# # # #         if cur:
# # # #             cur.close()
# # # #         if conn:
# # # #             conn.close()

# # # #     return render_template('items.html', items=items, locations=locations, user=session.get('username'))

# # # # @app.route('/items')
# # # # def items():
# # # #     """Render items list page with items and available locations."""
# # # #     conn = None
# # # #     cur = None
# # # #     items = []
# # # #     locations = []
# # # #     try:
# # # #         conn = get_conn()
# # # #         cur = conn.cursor(dictionary=True)

# # # #         # Load items with on_hand aggregated from stock_ledger
# # # #         cur.execute("""
# # # #             SELECT
# # # #               i.sku,
# # # #               i.name,
# # # #               i.unit,
# # # #               i.category,
# # # #               COALESCE(i.reorder_level, %s) AS reorder_level,
# # # #               COALESCE(SUM(sl.qty), 0) AS on_hand
# # # #             FROM items i
# # # #             LEFT JOIN stock_ledger sl ON sl.sku = i.sku
# # # #             GROUP BY i.sku, i.name, i.unit, i.category, i.reorder_level
# # # #             ORDER BY i.name ASC
# # # #         """, (LOW_STOCK_THRESHOLD,))
# # # #         items = cur.fetchall() or []

# # # #         # Try to load locations from a locations table; fallback to distinct locations in stock_ledger
# # # #         try:
# # # #             cur.execute("SELECT name FROM locations ORDER BY name ASC")
# # # #             locations = [r['name'] for r in cur.fetchall() or []]
# # # #         except Exception:
# # # #             # fallback: distinct location_id from stock_ledger
# # # #             try:
# # # #                 cur.execute("SELECT DISTINCT location_id FROM stock_ledger WHERE location_id IS NOT NULL")
# # # #                 locations = [r['location_id'] for r in cur.fetchall() or []]
# # # #             except Exception:
# # # #                 locations = []

# # # #     except Exception:
# # # #         app.logger.exception("Failed to load items or locations")
# # # #         items = []
# # # #         locations = []
# # # #     finally:
# # # #         if cur:
# # # #             cur.close()
# # # #         if conn:
# # # #             conn.close()

# # # #     return render_template('items.html', items=items, locations=locations, user=session.get('username'))


# # # @app.route('/export_items')
# # # def export_items():
# # #     """Export items as CSV for download."""
# # #     conn = None
# # #     cur = None
# # #     try:
# # #         conn = get_conn()
# # #         cur = conn.cursor(dictionary=True)
# # #         cur.execute("""
# # #             SELECT
# # #               i.sku,
# # #               i.name,
# # #               i.unit,
# # #               i.category,
# # #               COALESCE(i.reorder_level, %s) AS reorder_level,
# # #               COALESCE(SUM(sl.qty), 0) AS on_hand
# # #             FROM items i
# # #             LEFT JOIN stock_ledger sl ON sl.sku = i.sku
# # #             GROUP BY i.sku, i.name, i.unit, i.category, i.reorder_level
# # #             ORDER BY i.name ASC
# # #         """, (LOW_STOCK_THRESHOLD,))
# # #         rows = cur.fetchall() or []
# # #         # Build CSV in memory
# # #         si = io.StringIO()
# # #         writer = csv.writer(si)
# # #         writer.writerow(['SKU', 'Name', 'Unit', 'Category', 'On hand', 'Reorder level'])
# # #         for r in rows:
# # #             writer.writerow([r.get('sku'), r.get('name'), r.get('unit'), r.get('category'),
# # #                              r.get('on_hand', 0), r.get('reorder_level')])

# # #         output = make_response(si.getvalue())
# # #         output.headers["Content-Disposition"] = "attachment; filename=items.csv"
# # #         output.headers["Content-type"] = "text/csv"
# # #         return output

# # #     except Exception:
# # #         app.logger.exception("Failed to export items")
# # #         return redirect(url_for('items'))
# # #     finally:
# # #         if cur:
# # #             cur.close()
# # #         if conn:
# # #             conn.close()

# # # @app.route('/api/items', methods=['POST'])
# # # def api_create_item():
# # #     data = request.get_json(silent=True)
# # #     if not data:
# # #         return jsonify({'status':'error','message':'Invalid JSON payload'}), 400

# # #     sku = (data.get('sku') or '').strip()
# # #     name = (data.get('name') or '').strip()
# # #     unit = (data.get('unit') or '').strip() or 'pcs'
# # #     category = (data.get('category') or '').strip()
# # #     reorder_level_raw = data.get('reorder_level')

# # #     # validate
# # #     if not sku or not name:
# # #         return jsonify({'status':'error','message':'SKU and name are required'}), 400
# # #     try:
# # #         reorder_level = None
# # #         if reorder_level_raw is not None and str(reorder_level_raw).strip() != '':
# # #             reorder_level = int(reorder_level_raw)
# # #     except ValueError:
# # #         return jsonify({'status':'error','message':'Reorder level must be an integer'}), 400

# # #     conn = None
# # #     cur = None
# # #     try:
# # #         conn = get_conn()
# # #         # try dictionary cursor, fallback to normal cursor
# # #         try:
# # #             cur = conn.cursor(dictionary=True)
# # #         except Exception:
# # #             cur = conn.cursor()

# # #         # check duplicate SKU
# # #         cur.execute("SELECT COUNT(1) AS cnt FROM items WHERE sku = %s", (sku,))
# # #         row = cur.fetchone()
# # #         cnt = row.get('cnt') if isinstance(row, dict) else (row[0] if row else 0)
# # #         if cnt > 0:
# # #             return jsonify({'status':'error','message':'SKU already exists'}), 400

# # #         # insert
# # #         cur.execute("""
# # #             INSERT INTO items (sku, name, unit, category, reorder_level, created_at)
# # #             VALUES (%s, %s, %s, %s, %s, NOW())
# # #         """, (sku, name, unit, category, reorder_level))
# # #         conn.commit()

# # #         item = {
# # #             'sku': sku,
# # #             'name': name,
# # #             'unit': unit,
# # #             'category': category,
# # #             'on_hand': 0,
# # #             'reorder_level': reorder_level if reorder_level is not None else LOW_STOCK_THRESHOLD
# # #         }
# # #         return jsonify({'status':'ok', 'item': item}), 200

# # #     except Exception as exc:
# # #         if conn:
# # #             conn.rollback()
# # #         # log full exception for debugging
# # #         app.logger.exception("Failed to create item (api_create_item): %s", exc)
# # #         # return a helpful message to the client (not stack trace)
# # #         return jsonify({'status':'error','message':'Server error while creating item. Check server logs for details.'}), 500
# # #     finally:
# # #         if cur:
# # #             try: cur.close()
# # #             except: pass
# # #         if conn:
# # #             try: conn.close()
# # #             except: pass


# # # # @app.route('/api/items', methods=['POST'])
# # # # def api_create_item():
# # # #     data = request.get_json(silent=True)
# # # #     if not data:
# # # #         return jsonify({'status':'error','message':'Invalid JSON payload'}), 400

# # # #     sku = (data.get('sku') or '').strip()
# # # #     name = (data.get('name') or '').strip()
# # # #     unit = (data.get('unit') or '').strip() or 'pcs'
# # # #     category = (data.get('category') or '').strip()
# # # #     reorder_level = data.get('reorder_level')

# # # #     if not sku or not name:
# # # #         return jsonify({'status':'error','message':'SKU and name are required'}), 400

# # # #     try:
# # # #         if reorder_level is not None and str(reorder_level).strip() != '':
# # # #             reorder_level = int(reorder_level)
# # # #         else:
# # # #             reorder_level = None
# # # #     except ValueError:
# # # #         return jsonify({'status':'error','message':'Reorder level must be an integer'}), 400

# # # #     conn = None
# # # #     cur = None
# # # #     try:
# # # #         conn = get_conn()
# # # #         cur = conn.cursor(dictionary=True)

# # # #         cur.execute("SELECT COUNT(1) AS cnt FROM items WHERE sku = %s", (sku,))
# # # #         if cur.fetchone().get('cnt', 0) > 0:
# # # #             return jsonify({'status':'error','message':'SKU already exists'}), 400

# # # #         cur.execute("""
# # # #             INSERT INTO items (sku, name, unit, category, reorder_level, created_at)
# # # #             VALUES (%s, %s, %s, %s, %s, NOW())
# # # #         """, (sku, name, unit, category, reorder_level))
# # # #         conn.commit()

# # # #         item = {
# # # #             'sku': sku,
# # # #             'name': name,
# # # #             'unit': unit,
# # # #             'category': category,
# # # #             'on_hand': 0,
# # # #             'reorder_level': reorder_level if reorder_level is not None else LOW_STOCK_THRESHOLD
# # # #         }
# # # #         return jsonify({'status':'ok', 'item': item}), 200

# # # #     except Exception:
# # # #         if conn:
# # # #             conn.rollback()
# # # #         app.logger.exception("Failed to create item (api_create_item)")
# # # #         return jsonify({'status':'error','message':'Server error while creating item'}), 500
# # # #     finally:
# # # #         if cur:
# # # #             cur.close()
# # # #         if conn:
# # # #             conn.close()


# # # @app.route('/api/items/check', methods=['GET'])
# # # def api_check_item():
# # #     """
# # #     Query param: sku
# # #     Returns JSON: { exists: true/false }
# # #     """
# # #     sku = (request.args.get('sku') or '').strip()
# # #     if not sku:
# # #         return jsonify({'exists': False}), 200

# # #     conn = None
# # #     cur = None
# # #     try:
# # #         conn = get_conn()
# # #         cur = conn.cursor()
# # #         cur.execute("SELECT COUNT(1) FROM items WHERE sku = %s", (sku,))
# # #         exists = cur.fetchone()[0] > 0
# # #         return jsonify({'exists': exists}), 200
# # #     except Exception:
# # #         app.logger.exception("Failed to check SKU")
# # #         return jsonify({'exists': False}), 200
# # #     finally:
# # #         if cur:
# # #             cur.close()
# # #         if conn:
# # #             conn.close()



# # # # -------------------------
# # # # Item detail and adjust
# # # # -------------------------
# # # @app.route('/item/<sku>')
# # # def item_detail(sku):
# # #     conn = None
# # #     cur = None
# # #     item = None
# # #     movements = []
# # #     try:
# # #         conn = get_conn()
# # #         cur = conn.cursor(dictionary=True)
# # #         cur.execute("""
# # #             SELECT sku, name, unit, category, COALESCE(reorder_level, %s) AS reorder_level
# # #             FROM items WHERE sku = %s
# # #         """, (LOW_STOCK_THRESHOLD, sku))
# # #         item = cur.fetchone()

# # #         cur.execute("""
# # #             SELECT when_ts AS `when`, qty, txn_type, location_id
# # #             FROM stock_ledger
# # #             WHERE sku = %s
# # #             ORDER BY when_ts DESC
# # #             LIMIT 10
# # #         """, (sku,))
# # #         movements = cur.fetchall() or []
# # #     except Exception:
# # #         app.logger.exception("Failed to load item detail")
# # #     finally:
# # #         if cur:
# # #             cur.close()
# # #         if conn:
# # #             conn.close()

# # #     return render_template('item_detail.html', sku=sku, item=item, movements=movements, user=session.get('username'))





# # # # Temporary debug route — add to app.py, restart Flask, then open /debug_db
# # # @app.route('/debug_db')
# # # def debug_db():
# # #     import json
# # #     conn = None
# # #     cur = None
# # #     out = {'db_path': None, 'items_count': None, 'sample_items': [], 'ledger_totals_sample': []}
# # #     try:
# # #         # show which DB path the app is using
# # #         out['db_path'] = DB_PATH if 'DB_PATH' in globals() else os.getenv('SQLITE_PATH', None)

# # #         conn = get_conn()
# # #         cur = conn.cursor(dictionary=True)

# # #         # items count
# # #         cur.execute("SELECT COUNT(*) AS cnt FROM items")
# # #         out['items_count'] = cur.fetchone().get('cnt', 0)

# # #         # sample items (first 20)
# # #         cur.execute("SELECT sku, name, COALESCE(is_seeded,0) AS is_seeded, unit, category, reorder_level FROM items ORDER BY sku LIMIT 20")
# # #         out['sample_items'] = cur.fetchall() or []

# # #         # ledger totals grouped by item_sku and sku (show top 20)
# # #         cur.execute("""
# # #             SELECT COALESCE(item_sku, sku) AS key_sku, SUM(qty) AS on_hand
# # #             FROM stock_ledger
# # #             GROUP BY COALESCE(item_sku, sku)
# # #             ORDER BY on_hand DESC
# # #             LIMIT 20
# # #         """)
# # #         out['ledger_totals_sample'] = cur.fetchall() or []

# # #     except Exception as e:
# # #         out['error'] = str(e)
# # #     finally:
# # #         try:
# # #             if cur: cur.close()
# # #             if conn: conn.close()
# # #         except:
# # #             pass
# # #     return app.response_class(response=json.dumps(out, default=str, indent=2), mimetype='application/json')




# # # @app.route('/api/dashboard', methods=['GET'])
# # # def api_dashboard_debug():
# # #     conn = None
# # #     cur = None
# # #     try:
# # #         conn = get_conn()
# # #         try:
# # #             cur = conn.cursor(dictionary=True)
# # #             dict_cursor = True
# # #         except Exception:
# # #             cur = conn.cursor()
# # #             dict_cursor = False

# # #         # Active SKUs
# # #         cur.execute("SELECT COUNT(DISTINCT sku) AS active_skus FROM items")
# # #         row = cur.fetchone()
# # #         active_skus = (row['active_skus'] if dict_cursor else row[0]) if row else 0

# # #         # Units on hand
# # #         cur.execute("SELECT COALESCE(SUM(on_hand),0) AS units_on_hand FROM items")
# # #         row = cur.fetchone()
# # #         units_on_hand = (row['units_on_hand'] if dict_cursor else row[0]) if row else 0

# # #         # Low stock alerts
# # #         cur.execute("SELECT COUNT(1) AS low_stock_alerts FROM items WHERE reorder_level IS NOT NULL AND on_hand <= reorder_level")
# # #         row = cur.fetchone()
# # #         low_stock_alerts = (row['low_stock_alerts'] if dict_cursor else row[0]) if row else 0

# # #         # Reorder alerts (limit 10)
# # #         cur.execute("""
# # #             SELECT sku, name, unit, category, on_hand, reorder_level
# # #             FROM items
# # #             WHERE reorder_level IS NOT NULL AND on_hand <= reorder_level
# # #             ORDER BY (reorder_level - on_hand) DESC
# # #             LIMIT 10
# # #         """)
# # #         rows = cur.fetchall() or []
# # #         reorder_alerts = []
# # #         for r in rows:
# # #             if dict_cursor:
# # #                 reorder_alerts.append({
# # #                     'sku': r.get('sku'),
# # #                     'name': r.get('name'),
# # #                     'unit': r.get('unit'),
# # #                     'category': r.get('category'),
# # #                     'on_hand': r.get('on_hand'),
# # #                     'reorder_level': r.get('reorder_level')
# # #                 })
# # #             else:
# # #                 reorder_alerts.append({
# # #                     'sku': r[0], 'name': r[1], 'unit': r[2],
# # #                     'category': r[3], 'on_hand': r[4], 'reorder_level': r[5]
# # #                 })

# # #         # Recent movements
# # #         cur.execute("""
# # #             SELECT when_ts AS ts, sku, qty, txn_type, user
# # #             FROM stock_ledger
# # #             ORDER BY when_ts DESC
# # #             LIMIT 10
# # #         """)
# # #         rows = cur.fetchall() or []
# # #         recent_movements = []
# # #         for r in rows:
# # #             if dict_cursor:
# # #                 recent_movements.append({
# # #                     'sku': r.get('sku'),
# # #                     'qty': r.get('qty'),
# # #                     'type': r.get('txn_type'),
# # #                     'user': r.get('user'),
# # #                     'ts': str(r.get('ts'))
# # #                 })
# # #             else:
# # #                 recent_movements.append({
# # #                     'sku': r[1], 'qty': r[2], 'type': r[3], 'user': r[4], 'ts': str(r[0])
# # #                 })

# # #         payload = {
# # #             'active_skus': int(active_skus),
# # #             'units_on_hand': int(units_on_hand),
# # #             'low_stock_alerts': int(low_stock_alerts),
# # #             'reorder_alerts': reorder_alerts,
# # #             'recent_movements': recent_movements
# # #         }
# # #         return jsonify(payload), 200

# # #     except Exception as exc:
# # #         tb = traceback.format_exc()
# # #         app.logger.error("api_dashboard_debug exception: %s", tb)
# # #         # Return traceback in JSON for debugging (remove in production)
# # #         return jsonify({'status': 'error', 'message': 'Server error fetching dashboard', 'traceback': tb}), 500
# # #     finally:
# # #         try:
# # #             if cur: cur.close()
# # #         except Exception:
# # #             pass
# # #         try:
# # #             if conn: conn.close()
# # #         except Exception:
# # #             pass


# # # # --- replace your existing index() with this improved version ---
# # # @app.route('/')
# # # def index():
# # #     conn = None
# # #     cur = None
# # #     items = []
# # #     recent_movements = []
# # #     try:
# # #         conn = get_conn()
# # #         try:
# # #             cur = conn.cursor(dictionary=True)
# # #             dict_cursor = True
# # #         except Exception:
# # #             cur = conn.cursor()
# # #             dict_cursor = False

# # #         # Items: prefer stored on_hand, fallback to ledger sum
# # #         cur.execute("""
# # #             SELECT
# # #               i.sku,
# # #               i.name,
# # #               i.unit,
# # #               i.category,
# # #               COALESCE(i.on_hand, COALESCE(SUM(sl.qty), 0)) AS on_hand,
# # #               COALESCE(i.reorder_level, %s) AS reorder_level
# # #             FROM items i
# # #             LEFT JOIN stock_ledger sl ON sl.sku = i.sku
# # #             GROUP BY i.sku, i.name, i.unit, i.category, i.on_hand, i.reorder_level
# # #             ORDER BY i.name ASC
# # #         """, (LOW_STOCK_THRESHOLD,))
# # #         items = cur.fetchall() or []

# # #         # Recent movements for initial render
# # #         cur.execute("""
# # #             SELECT when_ts AS `when`, sku, qty, txn_type
# # #             FROM stock_ledger
# # #             ORDER BY when_ts DESC
# # #             LIMIT 5
# # #         """)
# # #         recent_movements = cur.fetchall() or []
# # #     except Exception:
# # #         app.logger.exception("Failed to load items or movements")
# # #     finally:
# # #         if cur:
# # #             cur.close()
# # #         if conn:
# # #             conn.close()

# # #     low_alerts = [it for it in items if int(it.get('on_hand', 0)) <= int(it.get('reorder_level', LOW_STOCK_THRESHOLD))]

# # #     stats = {
# # #         'active_skus': len(items),
# # #         'units_on_hand': sum(int(it.get('on_hand', 0)) for it in items),
# # #         'recent_movements': len(recent_movements)
# # #     }

# # #     return render_template(
# # #         'index.html',
# # #         items=items,
# # #         user=session.get('username'),
# # #         low_alerts=low_alerts,
# # #         recent_movements=recent_movements,
# # #         stats=stats,
# # #         now=datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')
# # #     )

# # # from flask import render_template, session, current_app as app, jsonify
# # # from decimal import Decimal

# # # LOW_STOCK_THRESHOLD = 5  # adjust as needed

# # # @app.route('/items')
# # # def items():
# # #     """Render items list page with items and available locations."""
# # #     conn = None
# # #     cur = None
# # #     items = []
# # #     locations = []

# # #     try:
# # #         conn = get_conn()
# # #         cur = conn.cursor(dictionary=True)

# # #         # Aggregate on_hand from stock_ledger. Support both sl.sku and sl.item_sku column names.
# # #         # Use COALESCE to fall back to LOW_STOCK_THRESHOLD when reorder_level is NULL.
# # #         cur.execute("""
# # #             SELECT
# # #               i.sku,
# # #               i.name,
# # #               i.unit,
# # #               i.category,
# # #               COALESCE(i.reorder_level, %s) AS reorder_level,
# # #               COALESCE(SUM(COALESCE(sl.qty, 0)), 0) AS on_hand
# # #             FROM items i
# # #             LEFT JOIN stock_ledger sl
# # #               ON ( (sl.sku IS NOT NULL AND sl.sku = i.sku)
# # #                    OR (sl.item_sku IS NOT NULL AND sl.item_sku = i.sku) )
# # #             GROUP BY i.sku, i.name, i.unit, i.category, i.reorder_level
# # #             ORDER BY i.name ASC
# # #         """, (LOW_STOCK_THRESHOLD,))
# # #         items = cur.fetchall() or []

# # #         # Normalize on_hand to int (some drivers return Decimal)
# # #         for r in items:
# # #             val = r.get('on_hand', 0)
# # #             if isinstance(val, Decimal):
# # #                 r['on_hand'] = int(val)
# # #             else:
# # #                 try:
# # #                     r['on_hand'] = int(val or 0)
# # #                 except Exception:
# # #                     r['on_hand'] = 0

# # #         # Try to load locations from locations table (preferred)
# # #         try:
# # #             cur.execute("SELECT id, COALESCE(name, code) AS name FROM locations ORDER BY name ASC")
# # #             loc_rows = cur.fetchall() or []
# # #             # return list of names (or dicts if you prefer)
# # #             locations = [r['name'] for r in loc_rows]
# # #         except Exception:
# # #             # fallback 1: distinct location_id/from_location_id/to_location_id from stock_ledger
# # #             try:
# # #                 cur.execute("""
# # #                     SELECT DISTINCT COALESCE(location_id, from_location_id, to_location_id) AS loc
# # #                     FROM stock_ledger
# # #                     WHERE COALESCE(location_id, from_location_id, to_location_id) IS NOT NULL
# # #                     ORDER BY loc
# # #                 """)
# # #                 locations = [r['loc'] for r in cur.fetchall() or []]
# # #             except Exception:
# # #                 # final fallback: empty list
# # #                 locations = []

# # #     except Exception:
# # #         app.logger.exception("Failed to load items or locations")
# # #         items = []
# # #         locations = []
# # #     finally:
# # #         if cur:
# # #             cur.close()
# # #         if conn:
# # #             conn.close()

# # #     return render_template('items.html', items=items, locations=locations, user=session.get('username'))





# # # @app.route('/api/items', methods=['POST'])
# # # def api_create_item():
# # #     """
# # #     Create a new item. Returns JSON:
# # #       { status: 'ok', item: { sku, name, unit, category, on_hand, reorder_level } }
# # #     or { status: 'error', message: '...' }
# # #     """
# # #     data = request.get_json(silent=True) or {}
# # #     sku = (data.get('sku') or '').strip()
# # #     name = (data.get('name') or '').strip()
# # #     unit = (data.get('unit') or '').strip() or 'pcs'
# # #     category = (data.get('category') or '').strip()
# # #     reorder_level = data.get('reorder_level')
# # #     try:
# # #         reorder_level = int(reorder_level) if reorder_level is not None and str(reorder_level).strip() != '' else None
# # #     except Exception:
# # #         reorder_level = None

# # #     if not sku or not name:
# # #         return jsonify({'status': 'error', 'message': 'SKU and name are required'}), 400

# # #     conn = None
# # #     cur = None
# # #     try:
# # #         conn = get_conn()
# # #         cur = conn.cursor(dictionary=True)

# # #         # check duplicate
# # #         cur.execute("SELECT COUNT(1) AS cnt FROM items WHERE sku = %s", (sku,))
# # #         if cur.fetchone().get('cnt', 0) > 0:
# # #             return jsonify({'status': 'error', 'message': 'SKU already exists'}), 400

# # #         # insert
# # #         cur.execute("""
# # #             INSERT INTO items (sku, name, unit, category, reorder_level, created_at)
# # #             VALUES (%s, %s, %s, %s, %s, NOW())
# # #         """, (sku, name, unit, category, reorder_level))
# # #         conn.commit()

# # #         # return created item (on_hand default 0)
# # #         item = {
# # #             'sku': sku,
# # #             'name': name,
# # #             'unit': unit,
# # #             'category': category,
# # #             'on_hand': 0,
# # #             'reorder_level': reorder_level if reorder_level is not None else LOW_STOCK_THRESHOLD
# # #         }
# # #         return jsonify({'status': 'ok', 'item': item})
# # #     except Exception:
# # #         if conn:
# # #             conn.rollback()
# # #         app.logger.exception("Failed to create item")
# # #         return jsonify({'status': 'error', 'message': 'Failed to create item'}), 500
# # #     finally:
# # #         if cur:
# # #             cur.close()
# # #         if conn:
# # #             conn.close()



# # # @app.route('/api/items', methods=['POST'])
# # # def api_create_item():
# # #     """
# # #     Simple API endpoint to create a new item.
# # #     Expects JSON: { sku, name, unit, category }
# # #     Returns JSON { status: 'ok' } or { status: 'error', message: '...' }
# # #     """
# # #     data = request.get_json(silent=True) or {}
# # #     sku = (data.get('sku') or '').strip()
# # #     name = (data.get('name') or '').strip()
# # #     unit = (data.get('unit') or '').strip() or 'pcs'
# # #     category = (data.get('category') or '').strip()

# # #     if not sku or not name:
# # #         return jsonify({'status': 'error', 'message': 'SKU and name are required'}), 400

# # #     conn = None
# # #     cur = None
# # #     try:
# # #         conn = get_conn()
# # #         cur = conn.cursor()
# # #         # Insert item if not exists
# # #         cur.execute("SELECT COUNT(1) FROM items WHERE sku = %s", (sku,))
# # #         if cur.fetchone()[0] > 0:
# # #             return jsonify({'status': 'error', 'message': 'SKU already exists'}), 400

# # #         cur.execute("""
# # #             INSERT INTO items (sku, name, unit, category, created_at)
# # #             VALUES (%s, %s, %s, %s, NOW())
# # #         """, (sku, name, unit, category))
# # #         conn.commit()
# # #         return jsonify({'status': 'ok'})
# # #     except Exception:
# # #         if conn:
# # #             conn.rollback()
# # #         app.logger.exception("Failed to create item")
# # #         return jsonify({'status': 'error', 'message': 'Failed to create item'}), 500
# # #     finally:
# # #         if cur:
# # #             cur.close()
# # #         if conn:
# # #             conn.close()




# # # -------------------------
# # # Receive / Issue pages + API
# # # -------------------------
# # # @app.route('/receive')
# # # def receive():
# # #     # simple page that can be extended; uses items and locations for selects
# # #     conn = None
# # #     cur = None
# # #     items = []
# # #     locations = []
# # #     try:
# # #         conn = get_conn()
# # #         cur = conn.cursor(dictionary=True)
# # #         cur.execute("SELECT sku, name FROM items ORDER BY name ASC")
# # #         items = cur.fetchall() or []
# # #         try:
# # #             cur.execute("SELECT name FROM locations ORDER BY name ASC")
# # #             locations = [r['name'] for r in cur.fetchall() or []]
# # #         except Exception:
# # #             cur.execute("SELECT DISTINCT location_id FROM stock_ledger WHERE location_id IS NOT NULL")
# # #             locations = [r['location_id'] for r in cur.fetchall() or []]
# # #     except Exception:
# # #         app.logger.exception("Failed to load receive page data")
# # #     finally:
# # #         if cur:
# # #             cur.close()
# # #         if conn:
# # #             conn.close()
# # #     return render_template('receive.html', items=items, locations=locations, user=session.get('username'))




# # # @app.route('/api/receive', methods=['POST'])
# # # def api_receive():
# # #     """
# # #     Accepts JSON:
# # #       { grn_no, supplier, po_ref, lines: [{ sku, qty, unit, batch_id, location, expiry }] }
# # #     Creates stock_ledger rows (positive qty) and returns { status:'ok', grn_no: '...' }.
# # #     """
# # #     data = request.get_json(silent=True) or {}
# # #     grn_no = (data.get('grn_no') or '').strip()
# # #     supplier = (data.get('supplier') or '').strip()
# # #     po_ref = (data.get('po_ref') or '').strip()
# # #     lines = data.get('lines') or []

# # #     # generate GRN if not provided
# # #     if not grn_no:
# # #         grn_no = f"GRN-{datetime.utcnow().strftime('%Y%m%d')}-{int(datetime.utcnow().timestamp())}"

# # #     if not supplier:
# # #         return jsonify({'status': 'error', 'message': 'Supplier is required'}), 400
# # #     if not lines or not any(l.get('sku') and float(l.get('qty', 0)) > 0 for l in lines):
# # #         return jsonify({'status': 'error', 'message': 'At least one valid line is required'}), 400

# # #     conn = None
# # #     cur = None
# # #     try:
# # #         conn = get_conn()
# # #         cur = conn.cursor()

# # #         # Insert a GRN header record if you have a grn table (optional)
# # #         try:
# # #             cur.execute("""
# # #                 INSERT INTO grn (grn_no, supplier, po_ref, created_at)
# # #                 VALUES (%s, %s, %s, NOW())
# # #             """, (grn_no, supplier, po_ref))
# # #         except Exception:
# # #             # ignore if grn table doesn't exist
# # #             pass

# # #         # Insert ledger rows for each line (positive qty)
# # #         for ln in lines:
# # #             sku = (ln.get('sku') or '').strip()
# # #             try:
# # #                 qty = float(ln.get('qty') or 0)
# # #             except Exception:
# # #                 qty = 0
# # #             unit = (ln.get('unit') or '').strip() or None
# # #             batch_id = (ln.get('batch_id') or '').strip() or None
# # #             location = (ln.get('location') or '').strip() or None
# # #             expiry = (ln.get('expiry') or '').strip() or None

# # #             if not sku or qty <= 0:
# # #                 continue

# # #             # Insert into stock_ledger: positive qty for receive
# # #             cur.execute("""
# # #                 INSERT INTO stock_ledger
# # #                   (sku, qty, txn_type, location_id, reference, batch_id, expiry_date, created_at)
# # #                 VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
# # #             """, (sku, qty, 'receive', location, grn_no, batch_id, expiry))

# # #         conn.commit()
# # #         return jsonify({'status': 'ok', 'grn_no': grn_no})
# # #     except Exception:
# # #         if conn:
# # #             conn.rollback()
# # #         app.logger.exception("Failed to process GRN")
# # #         return jsonify({'status': 'error', 'message': 'Failed to process GRN'}), 500
# # #     finally:
# # #         if cur:
# # #             cur.close()
# # #         if conn:
# # #             conn.close()



# # # @app.route('/issue')
# # # def issue():
# # #     # page with items and locations for the issue form
# # #     conn = None
# # #     cur = None
# # #     items = []
# # #     locations = []
# # #     try:
# # #         conn = get_conn()
# # #         cur = conn.cursor(dictionary=True)
# # #         cur.execute("SELECT sku, name FROM items ORDER BY name ASC")
# # #         items = cur.fetchall() or []
# # #         try:
# # #             cur.execute("SELECT name FROM locations ORDER BY name ASC")
# # #             locations = [r['name'] for r in cur.fetchall() or []]
# # #         except Exception:
# # #             cur.execute("SELECT DISTINCT location_id FROM stock_ledger WHERE location_id IS NOT NULL")
# # #             locations = [r['location_id'] for r in cur.fetchall() or []]
# # #     except Exception:
# # #         app.logger.exception("Failed to load issue page data")
# # #     finally:
# # #         if cur:
# # #             cur.close()
# # #         if conn:
# # #             conn.close()
# # #     return render_template('issue.html', items=items, locations=locations, user=session.get('username'))


# # # @app.route('/api/issue', methods=['POST'])
# # # def api_issue():
# # #     """
# # #     Accepts JSON payload:
# # #     { delivery_no, customer, lines: [{sku, qty, location}] }
# # #     Creates ledger entries and returns {status:'ok', delivery_no: '...'}
# # #     """
# # #     data = request.get_json(silent=True) or {}
# # #     delivery_no = data.get('delivery_no') or f"DN-{int(datetime.utcnow().timestamp())}"
# # #     customer = data.get('customer') or ''
# # #     lines = data.get('lines') or []

# # #     if not customer or not lines:
# # #         return jsonify({'status':'error','message':'Customer and lines required'}), 400

# # #     conn = None
# # #     cur = None
# # #     try:
# # #         conn = get_conn()
# # #         cur = conn.cursor()
# # #         # Insert ledger rows for each line (negative qty for issue)
# # #         for ln in lines:
# # #             sku = ln.get('sku')
# # #             qty = int(ln.get('qty') or 0)
# # #             loc = ln.get('location') or None
# # #             if not sku or qty <= 0:
# # #                 continue
# # #             cur.execute("""
# # #                 INSERT INTO stock_ledger (sku, qty, txn_type, location_id, reference, created_at)
# # #                 VALUES (%s, %s, %s, %s, %s, NOW())
# # #             """, (sku, -abs(qty), 'issue', loc, delivery_no))
# # #         conn.commit()
# # #         return jsonify({'status':'ok', 'delivery_no': delivery_no})
# # #     except Exception:
# # #         if conn:
# # #             conn.rollback()
# # #         app.logger.exception("Failed to create issue")
# # #         return jsonify({'status':'error','message':'Failed to issue goods'}), 500
# # #     finally:
# # #         if cur:
# # #             cur.close()
# # #         if conn:
# # #             conn.close()


























































# # # USE busia_wms;

# # # CREATE TABLE IF NOT EXISTS items (
# # #   id INT AUTO_INCREMENT PRIMARY KEY,
# # #   sku VARCHAR(128) NOT NULL UNIQUE,
# # #   name VARCHAR(255) NOT NULL,
# # #   unit VARCHAR(64) DEFAULT 'pcs',
# # #   category VARCHAR(128),
# # #   reorder_level INT DEFAULT 0,
# # #   price DECIMAL(10,2) DEFAULT NULL,
# # #   carton_price DECIMAL(10,2) DEFAULT NULL,
# # #   description TEXT,
# # #   created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
# # #   updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
# # # ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

# # # CREATE TABLE IF NOT EXISTS locations (
# # #   id INT AUTO_INCREMENT PRIMARY KEY,
# # #   code VARCHAR(64) UNIQUE,
# # #   name VARCHAR(128) NOT NULL,
# # #   type VARCHAR(64),
# # #   capacity INT DEFAULT NULL,
# # #   notes TEXT,
# # #   created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
# # #   updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
# # # ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

# # # CREATE TABLE IF NOT EXISTS suppliers (
# # #   id INT AUTO_INCREMENT PRIMARY KEY,
# # #   code VARCHAR(64) UNIQUE,
# # #   name VARCHAR(255) NOT NULL,
# # #   contact_name VARCHAR(255),
# # #   phone VARCHAR(64),
# # #   email VARCHAR(255),
# # #   address TEXT,
# # #   notes TEXT,
# # #   created_at DATETIME DEFAULT CURRENT_TIMESTAMP
# # # ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

# # # CREATE TABLE IF NOT EXISTS customers (
# # #   id INT AUTO_INCREMENT PRIMARY KEY,
# # #   code VARCHAR(64) UNIQUE,
# # #   name VARCHAR(255) NOT NULL,
# # #   contact_name VARCHAR(255),
# # #   phone VARCHAR(64),
# # #   email VARCHAR(255),
# # #   address TEXT,
# # #   notes TEXT,
# # #   created_at DATETIME DEFAULT CURRENT_TIMESTAMP
# # # ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

# # # CREATE TABLE IF NOT EXISTS users (
# # #   id INT AUTO_INCREMENT PRIMARY KEY,
# # #   username VARCHAR(128) NOT NULL UNIQUE,
# # #   full_name VARCHAR(255),
# # #   email VARCHAR(255),
# # #   password_hash VARCHAR(255),
# # #   role VARCHAR(64) DEFAULT 'user',
# # #   is_active TINYINT(1) DEFAULT 1,
# # #   created_at DATETIME DEFAULT CURRENT_TIMESTAMP
# # # ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

# # # CREATE TABLE IF NOT EXISTS batches (
# # #   id INT AUTO_INCREMENT PRIMARY KEY,
# # #   batch_no VARCHAR(128) NOT NULL,
# # #   item_sku VARCHAR(128) NOT NULL,
# # #   manufacture_date DATE DEFAULT NULL,
# # #   expiry_date DATE DEFAULT NULL,
# # #   qty INT DEFAULT 0,
# # #   created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
# # #   updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
# # #   UNIQUE KEY ux_batch_item (batch_no, item_sku),
# # #   CONSTRAINT fk_batches_item FOREIGN KEY (item_sku) REFERENCES items(sku) ON DELETE CASCADE ON UPDATE CASCADE
# # # ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

# # # CREATE TABLE IF NOT EXISTS receipts (
# # #   id INT AUTO_INCREMENT PRIMARY KEY,
# # #   receipt_no VARCHAR(128) NOT NULL UNIQUE,
# # #   supplier_id INT DEFAULT NULL,
# # #   po_no VARCHAR(128) DEFAULT NULL,
# # #   received_by VARCHAR(128) DEFAULT NULL,
# # #   received_at DATETIME DEFAULT CURRENT_TIMESTAMP,
# # #   notes TEXT,
# # #   created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
# # #   CONSTRAINT fk_receipts_supplier FOREIGN KEY (supplier_id) REFERENCES suppliers(id) ON DELETE SET NULL
# # # ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

# # # CREATE TABLE IF NOT EXISTS receipt_lines (
# # #   id INT AUTO_INCREMENT PRIMARY KEY,
# # #   receipt_id INT NOT NULL,
# # #   item_sku VARCHAR(128) NOT NULL,
# # #   qty INT NOT NULL,
# # #   unit VARCHAR(64) DEFAULT 'pcs',
# # #   location_id INT DEFAULT NULL,
# # #   batch_no VARCHAR(128) DEFAULT NULL,
# # #   line_notes TEXT,
# # #   created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
# # #   CONSTRAINT fk_receipt_lines_receipt FOREIGN KEY (receipt_id) REFERENCES receipts(id) ON DELETE CASCADE,
# # #   CONSTRAINT fk_receipt_lines_item FOREIGN KEY (item_sku) REFERENCES items(sku) ON DELETE RESTRICT,
# # #   CONSTRAINT fk_receipt_lines_loc FOREIGN KEY (location_id) REFERENCES locations(id) ON DELETE SET NULL
# # # ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

# # # CREATE TABLE IF NOT EXISTS deliveries (
# # #   id INT AUTO_INCREMENT PRIMARY KEY,
# # #   delivery_no VARCHAR(128) NOT NULL UNIQUE,
# # #   order_ref VARCHAR(128) DEFAULT NULL,
# # #   recipient VARCHAR(255) DEFAULT NULL,
# # #   address TEXT,
# # #   vehicle_reg VARCHAR(64),
# # #   driver_name VARCHAR(128),
# # #   remarks TEXT,
# # #   created_by VARCHAR(128) DEFAULT NULL,
# # #   created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
# # #   updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
# # # ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

# # # CREATE TABLE IF NOT EXISTS delivery_lines (
# # #   id INT AUTO_INCREMENT PRIMARY KEY,
# # #   delivery_id INT NOT NULL,
# # #   item_sku VARCHAR(128) NOT NULL,
# # #   qty INT NOT NULL,
# # #   unit VARCHAR(64) DEFAULT 'pcs',
# # #   location_id INT DEFAULT NULL,
# # #   batch_no VARCHAR(128) DEFAULT NULL,
# # #   line_notes TEXT,
# # #   created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
# # #   CONSTRAINT fk_delivery_lines_delivery FOREIGN KEY (delivery_id) REFERENCES deliveries(id) ON DELETE CASCADE,
# # #   CONSTRAINT fk_delivery_lines_item FOREIGN KEY (item_sku) REFERENCES items(sku) ON DELETE RESTRICT,
# # #   CONSTRAINT fk_delivery_lines_loc FOREIGN KEY (location_id) REFERENCES locations(id) ON DELETE SET NULL
# # # ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

# # # CREATE TABLE IF NOT EXISTS stock_ledger (
# # #   id BIGINT AUTO_INCREMENT PRIMARY KEY,
# # #   sku VARCHAR(128) DEFAULT NULL,
# # #   item_sku VARCHAR(128) DEFAULT NULL,
# # #   qty INT NOT NULL,
# # #   txn_type VARCHAR(32) NOT NULL,
# # #   location_id INT DEFAULT NULL,
# # #   from_location_id INT DEFAULT NULL,
# # #   to_location_id INT DEFAULT NULL,
# # #   reference VARCHAR(255) DEFAULT NULL,
# # #   batch_no VARCHAR(128) DEFAULT NULL,
# # #   created_by VARCHAR(128) DEFAULT NULL,
# # #   created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
# # #   notes TEXT,
# # #   INDEX idx_ledger_sku (sku),
# # #   INDEX idx_ledger_item_sku (item_sku),
# # #   CONSTRAINT fk_ledger_from_loc FOREIGN KEY (from_location_id) REFERENCES locations(id) ON DELETE SET NULL,
# # #   CONSTRAINT fk_ledger_to_loc FOREIGN KEY (to_location_id) REFERENCES locations(id) ON DELETE SET NULL
# # # ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

# # # CREATE TABLE IF NOT EXISTS inventory_counts (
# # #   id INT AUTO_INCREMENT PRIMARY KEY,
# # #   count_no VARCHAR(128) NOT NULL UNIQUE,
# # #   performed_by VARCHAR(128),
# # #   location_id INT DEFAULT NULL,
# # #   notes TEXT,
# # #   created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
# # #   CONSTRAINT fk_counts_loc FOREIGN KEY (location_id) REFERENCES locations(id) ON DELETE SET NULL
# # # ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

# # # CREATE TABLE IF NOT EXISTS inventory_count_lines (
# # #   id INT AUTO_INCREMENT PRIMARY KEY,
# # #   count_id INT NOT NULL,
# # #   item_sku VARCHAR(128) NOT NULL,
# # #   counted_qty INT NOT NULL,
# # #   expected_qty INT DEFAULT NULL,
# # #   variance INT AS (counted_qty - IFNULL(expected_qty,0)) STORED,
# # #   notes TEXT,
# # #   CONSTRAINT fk_count_lines_count FOREIGN KEY (count_id) REFERENCES inventory_counts(id) ON DELETE CASCADE,
# # #   CONSTRAINT fk_count_lines_item FOREIGN KEY (item_sku) REFERENCES items(sku) ON DELETE RESTRICT
# # # ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

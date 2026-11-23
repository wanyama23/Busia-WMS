#!/usr/bin/env python3
import sqlite3
from pathlib import Path

DB_PATH = Path('busia_wms.sqlite3')  # change to 'busia_wms.db' if you want that name

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  sku TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  unit TEXT DEFAULT 'pcs',
  category TEXT,
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
  name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS stock_ledger (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  sku TEXT DEFAULT NULL,
  item_sku TEXT DEFAULT NULL,
  qty INTEGER NOT NULL,
  txn_type TEXT NOT NULL,
  location_id INTEGER DEFAULT NULL,
  from_location_id INTEGER DEFAULT NULL,
  to_location_id INTEGER DEFAULT NULL,
  reference TEXT DEFAULT NULL,
  batch_no TEXT DEFAULT NULL,
  created_by TEXT DEFAULT NULL,
  notes TEXT,
  FOREIGN KEY (from_location_id) REFERENCES locations(id) ON DELETE SET NULL,
  FOREIGN KEY (to_location_id) REFERENCES locations(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_ledger_sku ON stock_ledger(sku);
CREATE INDEX IF NOT EXISTS idx_ledger_item_sku ON stock_ledger(item_sku);
"""

ITEMS = [
    {'sku':'SOAP-100G','name':'Soap 100g','unit':'bar','category':'Hygiene','reorder_level':50,'price':30.00,'carton_price':300.00,'description':'Generic soap bar','initial_qty':200},
    {'sku':'DOWNY-500ML','name':'Downy Fabric Softener 500ml','unit':'bottle','category':'Cleaning','reorder_level':20,'price':250.00,'carton_price':2800.00,'description':'Downy 500ml bottle (carton of 12)','initial_qty':60},
    {'sku':'DOWNY-1L','name':'Downy Fabric Softener 1L','unit':'bottle','category':'Cleaning','reorder_level':15,'price':450.00,'carton_price':5000.00,'description':'Downy 1L bottle (carton of 12)','initial_qty':40},
    {'sku':'SOAP-1KG','name':'Soap 1kg','unit':'bar','category':'Hygiene','reorder_level':50,'price':150.00,'carton_price':1500.00,'description':'Generic 1kg soap bar','initial_qty':200},
    {'sku':'WATER-500ML','name':'Bottled Water 500ml','unit':'bottle','category':'Beverages','reorder_level':100,'price':20.00,'carton_price':420.00,'description':'500ml bottled water (carton of 24)','initial_qty':240},
    {'sku':'WATER-1.5L','name':'Bottled Water 1.5L','unit':'bottle','category':'Beverages','reorder_level':60,'price':40.00,'carton_price':900.00,'description':'1.5L bottled water (carton of 24)','initial_qty':120},
    {'sku':'COOK-OIL-20L','name':'Cooking Oil 20L','unit':'jerrycan','category':'Food','reorder_level':5,'price':4200.00,'carton_price':None,'description':'Cooking oil 20L jerrycan','initial_qty':10},
    {'sku':'COOK-OIL-10L','name':'Cooking Oil 10L','unit':'jerrycan','category':'Food','reorder_level':8,'price':2200.00,'carton_price':None,'description':'Cooking oil 10L jerrycan','initial_qty':12},
    {'sku':'COOK-OIL-5L','name':'Cooking Oil 5L','unit':'jerrycan','category':'Food','reorder_level':12,'price':1200.00,'carton_price':None,'description':'Cooking oil 5L jerrycan','initial_qty':20},
    {'sku':'COOK-OIL-2L','name':'Cooking Oil 2L','unit':'bottle','category':'Food','reorder_level':20,'price':480.00,'carton_price':None,'description':'Cooking oil 2L bottle','initial_qty':50},
    {'sku':'COOK-OIL-1L','name':'Cooking Oil 1L','unit':'bottle','category':'Food','reorder_level':40,'price':250.00,'carton_price':None,'description':'Cooking oil 1L bottle','initial_qty':80},
    {'sku':'SUGAR-1KG','name':'Sugar 1kg','unit':'kg','category':'Food','reorder_level':30,'price':150.00,'carton_price':None,'description':'Refined sugar 1kg pack','initial_qty':100},
    {'sku':'SALT-500G','name':'Salt 500g','unit':'pack','category':'Food','reorder_level':40,'price':40.00,'carton_price':None,'description':'Table salt 500g','initial_qty':150},
    {'sku':'SALT-1KG','name':'Salt 1kg','unit':'kg','category':'Food','reorder_level':20,'price':75.00,'carton_price':None,'description':'Table salt 1kg','initial_qty':80},
    {'sku':'ROYCO-50G','name':'Royco Seasoning 50g','unit':'pack','category':'Food','reorder_level':50,'price':60.00,'carton_price':None,'description':'Royco seasoning small pack','initial_qty':200},
    {'sku':'BISCUITS-200G','name':'Biscuits 200g','unit':'pack','category':'Food','reorder_level':30,'price':120.00,'carton_price':None,'description':'Assorted biscuits 200g','initial_qty':120},
    {'sku':'KIDS-SNACKS-50G','name':'Kids Snacks 50g','unit':'pack','category':'Food','reorder_level':40,'price':45.00,'carton_price':None,'description':'Kids snack pack 50g','initial_qty':160},
    {'sku':'MATCHBOX-1','name':'Matchbox (single)','unit':'pc','category':'Household','reorder_level':200,'price':10.00,'carton_price':600.00,'description':'Single matchbox (carton of 60)','initial_qty':1000},
    {'sku':'CANDLES-1PC','name':'Candle (single)','unit':'pc','category':'Household','reorder_level':100,'price':15.00,'carton_price':900.00,'description':'Single candle (carton of 60)','initial_qty':400},
    {'sku':'PAMPERS-S','name':'Pampers Size S','unit':'pack','category':'Baby','reorder_level':20,'price':600.00,'carton_price':None,'description':'Pampers small pack','initial_qty':40},
    {'sku':'PAMPERS-M','name':'Pampers Size M','unit':'pack','category':'Baby','reorder_level':20,'price':800.00,'carton_price':None,'description':'Pampers medium pack','initial_qty':40},
    {'sku':'PAMPERS-L','name':'Pampers Size L','unit':'pack','category':'Baby','reorder_level':20,'price':1000.00,'carton_price':None,'description':'Pampers large pack','initial_qty':40},
    {'sku':'PAMPERS-XL','name':'Pampers Size XL','unit':'pack','category':'Baby','reorder_level':15,'price':1200.00,'carton_price':None,'description':'Pampers XL pack','initial_qty':30}
]

def upsert_item(cur, item):
    sql = """
    INSERT INTO items (sku, name, unit, category, reorder_level, price, carton_price, description, created_at, updated_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))
    ON CONFLICT(sku) DO UPDATE SET
      name = excluded.name,
      unit = excluded.unit,
      category = excluded.category,
      reorder_level = excluded.reorder_level,
      price = excluded.price,
      carton_price = excluded.carton_price,
      description = excluded.description,
      updated_at = datetime('now')
    """
    cur.execute(sql, (
        item['sku'],
        item['name'],
        item.get('unit') or 'pcs',
        item.get('category'),
        int(item.get('reorder_level') or 0),
        float(item.get('price')) if item.get('price') not in (None, '') else None,
        float(item.get('carton_price')) if item.get('carton_price') not in (None, '') else None,
        item.get('description') or ''
    ))

def seed():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        # ensure foreign keys are enforced for this connection
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.executescript(SCHEMA)
        cur = conn.cursor()

        # upsert items
        for it in ITEMS:
            upsert_item(cur, it)

        # insert initial stock_ledger entries (txn_type = INITIAL)
        # NOTE: do not reference created_at column so this works with existing schema
        ledger_sql = """
        INSERT INTO stock_ledger
          (sku, item_sku, qty, txn_type, location_id, reference, batch_no, created_by, notes)
        VALUES (?, ?, ?, 'INITIAL', NULL, 'seed', NULL, 'seed', ?)
        """

        # optional: remove previous INITIAL seed rows for these SKUs to avoid duplicates
        skus = [it['sku'] for it in ITEMS]
        if skus:
            placeholders = ",".join("?" for _ in skus)
            cur.execute(f"DELETE FROM stock_ledger WHERE txn_type = 'INITIAL' AND sku IN ({placeholders})", skus)

        ledger_params = []
        for it in ITEMS:
            sku = it['sku']
            qty = int(it.get('initial_qty', int(it.get('reorder_level', 10)) * 2))
            unit_price = float(it.get('price')) if it.get('price') not in (None, '') else 0.0
            total_value = round(unit_price * qty, 2)
            notes = f"unit_price={unit_price:.2f}; total_value={total_value:.2f}; currency=KES; seeded_by=seed"
            ledger_params.append((sku, sku, qty, notes))

        cur.executemany(ledger_sql, ledger_params)

        conn.commit()
        print(f"Seeded {len(ITEMS)} items and {len(ledger_params)} ledger entries into {DB_PATH}")
    finally:
        conn.close()

if __name__ == '__main__':
    seed()

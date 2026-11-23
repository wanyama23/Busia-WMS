CREATE TABLE delivery_lines (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      delivery_id INTEGER NOT NULL,
      item_sku TEXT NOT NULL,
      qty INTEGER NOT NULL,
      unit TEXT DEFAULT 'pcs',
      location_id INTEGER DEFAULT NULL,
      batch_no TEXT DEFAULT NULL,
      line_notes TEXT,
      created_at DATETIME DEFAULT (datetime('now')), sku TEXT, location TEXT, batch_id TEXT, expiry TEXT,
      FOREIGN KEY (delivery_id) REFERENCES deliveries(id) ON DELETE CASCADE,
      FOREIGN KEY (item_sku) REFERENCES items(sku) ON DELETE RESTRICT,
      FOREIGN KEY (location_id) REFERENCES locations(id) ON DELETE SET NULL
    );

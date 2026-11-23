#!/usr/bin/env python3
import sqlite3, os, sys
from datetime import datetime, timezone
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

DB = 'busia_wms.sqlite3'
OUT_DIR = os.path.join(os.path.dirname(__file__), 'generated_docs')
os.makedirs(OUT_DIR, exist_ok=True)

def pdf_from_rows(path, grn_no, order_ref, supplier_email, lines, created_at):
    c = canvas.Canvas(path, pagesize=A4)
    c.setTitle(f"GRN {grn_no}")
    c.setFont("Helvetica-Bold", 14)
    c.drawString(40, 800, f"Goods Received Note {grn_no}")
    c.setFont("Helvetica", 10)
    c.drawString(40, 785, f"Order Ref: {order_ref or ''}")
    c.drawString(300, 785, f"Date: {created_at or ''}")
    c.drawString(40, 770, f"Supplier: {supplier_email or ''}")
    y = 740
    c.setFont("Helvetica-Bold", 10)
    c.drawString(40, y, "SKU")
    c.drawString(180, y, "Qty")
    c.drawString(230, y, "Unit")
    c.drawString(290, y, "Batch")
    c.drawString(380, y, "Location")
    c.setFont("Helvetica", 10)
    y -= 16
    for ln in lines:
        c.drawString(40, y, str(ln.get('sku','')))
        c.drawString(180, y, str(ln.get('qty','')))
        c.drawString(230, y, str(ln.get('unit','')))
        c.drawString(290, y, str(ln.get('batch_no','')))
        c.drawString(380, y, str(ln.get('location','')))
        y -= 14
        if y < 60:
            c.showPage()
            y = 800
    c.showPage()
    c.save()

def resolve_location(cur, loc_id):
    if not loc_id:
        return ''
    try:
        cur.execute("SELECT name, code FROM locations WHERE id = ? LIMIT 1", (loc_id,))
        r = cur.fetchone()
        if r:
            return r['name'] or r['code'] or str(loc_id)
    except Exception:
        pass
    return str(loc_id)

def main(receipt_id):
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    cur.execute("SELECT id, receipt_no, grn_no, order_ref, supplier_email, created_at FROM receipts WHERE id = ?", (receipt_id,))
    hdr = cur.fetchone()
    if not hdr:
        print("Receipt not found:", receipt_id); conn.close(); sys.exit(1)

    grn_no = hdr['grn_no'] or f"GRN-{datetime.now(timezone.utc).strftime('%Y%m%d')}-{int(datetime.now(timezone.utc).timestamp())}"
    order_ref = hdr['order_ref']
    supplier_email = hdr['supplier_email']
    created_at = hdr['created_at']

    cur.execute("SELECT * FROM receipt_lines WHERE receipt_id = ?", (receipt_id,))
    rows = cur.fetchall()
    lines = []
    for r in rows:
        rd = dict(r)
        ln = {
            'sku': rd.get('item_sku') or rd.get('sku') or '',
            'qty': rd.get('qty') or 0,
            'unit': rd.get('unit') or '',
            'batch_no': rd.get('batch_no') or rd.get('batch_no') or '',
            'line_notes': rd.get('line_notes') or '',
            'location': resolve_location(cur, rd.get('location_id') or rd.get('location'))
        }
        lines.append(ln)

    out = os.path.join(OUT_DIR, f"{grn_no}.pdf")
    pdf_from_rows(out, grn_no, order_ref, supplier_email, lines, created_at)
    cur.execute("UPDATE receipts SET document_path = ? WHERE id = ?", (out, receipt_id))
    conn.commit()
    cur.close()
    conn.close()
    print("PDF written to:", out)

if __name__ == '__main__':
    rid = int(sys.argv[1]) if len(sys.argv) > 1 else 12
    main(rid)



# #!/usr/bin/env python3
# # regen_pdf_for_receipt.py
# import sqlite3, os, sys
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

#     cur.execute("SELECT id, receipt_no, grn_no, order_ref, supplier_email, created_at FROM receipts WHERE id = ?", (receipt_id,))
#     hdr = cur.fetchone()
#     if not hdr:
#         print("Receipt not found:", receipt_id); conn.close(); sys.exit(1)

#     grn_no = hdr['grn_no'] or f"GRN-{datetime.utcnow().strftime('%Y%m%d')}-{int(datetime.utcnow().timestamp())}"
#     order_ref = hdr['order_ref']
#     supplier_email = hdr['supplier_email']
#     receipt_meta = {'created_at': hdr['created_at']}

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
#         loc_id = rowd.get('location_id') or rowd.get('location') or None
#         ln['location'] = resolve_location(cur, loc_id)
#         lines.append(ln)

#     filename = f"{grn_no}.pdf"
#     out_path = os.path.join(OUT_DIR, filename)
#     pdf_from_rows(out_path, grn_no, order_ref, supplier_email, lines, receipt_meta)
#     print("PDF written to:", out_path)

#     try:
#         cur.execute("UPDATE receipts SET document_path = ? WHERE id = ?", (out_path, receipt_id))
#         conn.commit()
#         print("Updated receipts.document_path for id", receipt_id)
#     except Exception as e:
#         print("Failed to update receipts.document_path:", e)

#     cur.close()
#     conn.close()

# if __name__ == '__main__':
#     rid = int(sys.argv[1]) if len(sys.argv) > 1 else 12
#     main(rid)

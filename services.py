"""
Import / Export services for the Lab Inventory app.

Import is deliberately forgiving:
  - Finds the header row automatically (scans up to 10 rows)
  - Accepts many column name synonyms (Item / Description / Item Description,
    Quantity / Qty / Count, Units / Unit / UOM, Category / Catergory / Type)
  - Treats em-dashes, blanks, "N/A", "NA" as empty
  - Skips title rows, TOTAL rows, and empty rows silently
  - If no category column exists, defaults to Consumable
  - If no unit column exists, defaults to "pcs"
"""

import io
from datetime import datetime, date
import pandas as pd

from models import db, Item, Transaction, Area, CATEGORIES


# -----------------------------------------------------------------------------
# Category mapping (source word -> SOP category)
# -----------------------------------------------------------------------------
CATEGORY_MAP = {
    "glassware": "Apparatus",
    "glass": "Apparatus",
    "pipettes": "Apparatus",
    "pipette": "Apparatus",
    "accessories": "Apparatus",
    "accessory": "Apparatus",
    "apparatus": "Apparatus",
    "labware": "Apparatus",
    "equipment": "Equipment",
    "instrument": "Equipment",
    "instruments": "Equipment",
    "microbiology equipment": "Equipment",
    "machine": "Equipment",
    "machines": "Equipment",
    "chemical": "Chemical",
    "chemicals": "Chemical",
    "reagent": "Reagent",
    "reagents": "Reagent",
    "media": "Reagent",
    "medium": "Reagent",
    "stain": "Reagent",
    "stains": "Reagent",
    "consumable": "Consumable",
    "consumables": "Consumable",
    "plasticware": "Consumable",
    "ppe": "Consumable",
    "personal protective equipment": "Consumable",
    "cleaning material": "Consumable",
    "cleaning materials": "Consumable",
    "disposable": "Consumable",
    "disposables": "Consumable",
}

ID_PREFIX = {
    "Apparatus": "APP",
    "Equipment": "EQU",
    "Reagent": "RGT",
    "Chemical": "CHM",
    "Consumable": "CON",
}


def next_inventory_id(category):
    prefix = ID_PREFIX.get(category, "ITM")
    rows = Item.query.filter(Item.inventory_id.like(f"{prefix}-%")).all()
    max_n = 0
    for r in rows:
        try:
            max_n = max(max_n, int(r.inventory_id.split("-")[-1]))
        except (ValueError, IndexError):
            continue
    return f"{prefix}-{max_n + 1:03d}"


# -----------------------------------------------------------------------------
# Value helpers
# -----------------------------------------------------------------------------
EMPTY_TOKENS = {"", "—", "―", "-", "--", "n/a", "na", "none", "null", "nil"}


def _clean(v):
    if v is None:
        return None
    if isinstance(v, float) and pd.isna(v):
        return None
    s = str(v).strip()
    if s.lower() in EMPTY_TOKENS:
        return None
    return s


def _num(v, default=0.0):
    if v is None:
        return default
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        if pd.isna(v):
            return default
        return float(v)
    s = _clean(v)
    if s is None:
        return default
    try:
        return float(s)
    except (ValueError, TypeError):
        return default


def _date(v):
    if v is None:
        return None
    try:
        d = pd.to_datetime(v, errors="coerce")
        if pd.isna(d):
            return None
        return d.date()
    except Exception:
        return None


def _norm(c):
    return str(c or "").strip().lower()


# Column synonyms — any of these will be treated as the same logical field
COL_SYNONYMS = {
    "name": ["item description", "item / description", "item", "item name",
             "itemnumber", "item number", "description", "name",
             "material", "product", "product name"],
    "quantity": ["quantity", "qty", "count", "stock", "stock count",
                 "on hand", "onhand", "available"],
    "unit": ["unit", "units", "uom", "measure", "measurement unit"],
    "category": ["category", "catergory", "cat", "type", "class",
                 "group", "section"],
    "capacity": ["capacity", "size", "volume", "pack size"],
    "location": ["location", "storage", "stored at", "shelf", "room"],
    "lot": ["lot", "lot #", "lot number", "serial", "lot / serial",
            "lot_serial", "batch", "batch number"],
    "expiry": ["expiry", "expiry date", "expires", "expiration",
               "expiration date", "exp date", "best before"],
    "manufacturer": ["manufacturer", "supplier", "manufacturer / supplier",
                     "vendor", "brand"],
    "min": ["min", "min level", "minimum", "minimum level",
            "min stock", "minimum stock"],
    "reorder": ["reorder", "reorder level", "re-order level",
                "reorder point", "re-order point"],
    "critical": ["critical", "critical?", "critical item", "is critical"],
    "unit_value": ["unit value", "unit value (usd)", "price", "unit price",
                   "value", "cost", "unit cost"],
    "responsible": ["responsible", "responsible person", "owner",
                    "custodian", "assigned to"],
    "condition": ["condition", "condition / status", "condition_status",
                  "status", "state"],
    "notes": ["notes", "remarks", "comment", "comments", "observations"],
    "received": ["received", "received / acquired", "received_date",
                 "acquired", "date received", "date acquired"],
    "service_due": ["service due", "cal./service due", "cal service due",
                    "calibration due", "service_due", "next service"],
    "inventory_id": ["inventory id", "inv id", "id", "item id", "item_id",
                     "code", "item code"],
    "opening": ["opening qty", "opening", "opening quantity",
                "opening balance", "opening_qty"],
}


def _find_logical_col(row, field):
    keys = [_norm(c) for c in row.index]
    original = {_norm(c): c for c in row.index}
    for syn in COL_SYNONYMS[field]:
        if syn in keys:
            return original[syn]
    return None


def _val(row, field):
    col = _find_logical_col(row, field)
    if col is None:
        return None
    return row.get(col)


# -----------------------------------------------------------------------------
# Header detection
# -----------------------------------------------------------------------------
NAME_KEYS = COL_SYNONYMS["name"]
QTY_KEYS = COL_SYNONYMS["quantity"]


def _row_has_header_values(values):
    vals = [_norm(v) for v in values if pd.notna(v)]
    joined = " ".join(vals)
    has_name = any(k in joined for k in NAME_KEYS)
    has_qty = any(k in joined for k in QTY_KEYS)
    return has_name and has_qty


def _find_header_row(df_raw, max_scan=10):
    for i in range(min(max_scan, len(df_raw))):
        row_values = df_raw.iloc[i].tolist()
        if _row_has_header_values(row_values):
            return i
    return None


def _extract_table(df_raw):
    if df_raw is None or df_raw.empty:
        return None

    hdr = _find_header_row(df_raw)
    if hdr is None:
        return None

    header_vals = [_norm(v) if pd.notna(v) else "" for v in df_raw.iloc[hdr].tolist()]
    seen = {}
    clean_header = []
    for i, h in enumerate(header_vals):
        if h == "":
            h = f"__col_{i}"
        if h in seen:
            seen[h] += 1
            h = f"{h}__{seen[h]}"
        else:
            seen[h] = 0
        clean_header.append(h)

    body = df_raw.iloc[hdr + 1:].reset_index(drop=True)
    body.columns = clean_header
    body = body.dropna(how="all")
    return body


# -----------------------------------------------------------------------------
# Row helpers
# -----------------------------------------------------------------------------
def _row_name(row):
    return _clean(_val(row, "name"))


def _row_qty(row):
    return _num(_val(row, "quantity"), 0.0)


def _row_unit(row):
    u = _clean(_val(row, "unit"))
    return u or "pcs"


def _row_category(row):
    return _clean(_val(row, "category"))


def _looks_like_total(name):
    if not name:
        return False
    n_up = name.strip().upper()
    return (n_up.startswith("TOTAL")
            or n_up.startswith("SUBTOTAL")
            or n_up == "SUM"
            or n_up.startswith("GRAND TOTAL"))


# -----------------------------------------------------------------------------
# Import — one sheet (unified)
# -----------------------------------------------------------------------------
def _import_sheet(df, user_id, sheet_name):
    area_name = (sheet_name or "Imported").strip()
    area = Area.query.filter_by(name=area_name).first()
    if not area:
        area = Area(name=area_name,
                    sort_order=(Area.query.count() or 0) + 1)
        db.session.add(area)
        db.session.flush()

    added, updated, skipped = 0, 0, 0

    for _, row in df.iterrows():
        name = _row_name(row)
        if not name or _looks_like_total(name):
            skipped += 1
            continue

        raw_cat = _row_category(row)
        if raw_cat:
            mapped_cat = CATEGORY_MAP.get(_norm(raw_cat), "Consumable")
        else:
            mapped_cat = CATEGORY_MAP.get(_norm(sheet_name), "Consumable")

        if mapped_cat not in CATEGORIES:
            mapped_cat = "Consumable"

        qty = _row_qty(row)
        unit = _row_unit(row)

        cap = _clean(_val(row, "capacity"))
        full_name = f"{name} {cap}".strip() if cap else name

        existing = (Item.query
                    .filter_by(name=full_name,
                               category=mapped_cat,
                               area_id=area.id)
                    .first())
        if existing:
            updated += 1
            continue

        item = Item(
            inventory_id=_clean(_val(row, "inventory_id"))
                         or next_inventory_id(mapped_cat),
            name=full_name,
            category=mapped_cat,
            area_id=area.id,
            manufacturer=_clean(_val(row, "manufacturer")),
            lot_serial=_clean(_val(row, "lot")),
            opening_qty=qty,
            unit=unit,
            location=_clean(_val(row, "location")),
            received_date=_date(_val(row, "received")),
            expiry_date=_date(_val(row, "expiry")),
            min_level=_num(_val(row, "min"), 0.0),
            reorder_level=_num(_val(row, "reorder"), 0.0),
            critical=(_clean(_val(row, "critical")) or "").lower()
                     in ("yes", "true", "1", "y"),
            condition_status=_clean(_val(row, "condition")) or "Active",
            service_due=_date(_val(row, "service_due")),
            unit_value=_num(_val(row, "unit_value"), 0.0),
            responsible=_clean(_val(row, "responsible")),
            notes=_clean(_val(row, "notes")),
            is_archived=qty <= 0,
            created_by_id=user_id,
        )
        db.session.add(item)
        db.session.flush()

        added += 1

    return added, updated, skipped


# -----------------------------------------------------------------------------
# Public import
# -----------------------------------------------------------------------------
SKIP_SHEETS = {
    "instructions", "dashboard", "lists",
    "monthly count", "discrepancy capa", "disposal register",
}


def import_workbook(file_storage, user_id):
    filename = file_storage.filename.lower()

    summary = {
        "sheets": [],
        "totals": {"added": 0, "updated": 0, "skipped": 0},
    }

    # ---- CSV ----------------------------------------------------------------
    if filename.endswith(".csv"):
        try:
            df_raw = pd.read_csv(file_storage, header=None)
        except Exception as e:
            raise ValueError(f"Could not read CSV: {e}")

        table = _extract_table(df_raw)
        if table is None or table.empty:
            raise ValueError("Could not find a recognizable header row in the CSV.")

        a, u, s = _import_sheet(table, user_id, "CSV Import")
        summary["sheets"].append({"name": file_storage.filename,
                                  "added": a, "updated": u, "skipped": s})
        summary["totals"] = {"added": a, "updated": u, "skipped": s}
        db.session.commit()
        return summary

    # ---- Excel --------------------------------------------------------------
    try:
        raw_sheets = pd.read_excel(file_storage, sheet_name=None, header=None)
    except Exception as e:
        raise ValueError(f"Could not read Excel file: {e}")

    for sheet_name, df_raw in raw_sheets.items():
        if df_raw is None or df_raw.empty:
            continue
        if _norm(sheet_name) in SKIP_SHEETS:
            continue

        table = _extract_table(df_raw)
        if table is None or table.empty:
            summary["sheets"].append({
                "name": sheet_name, "added": 0, "updated": 0,
                "skipped": len(df_raw),
            })
            continue

        a, u, s = _import_sheet(table, user_id, sheet_name)

        summary["sheets"].append({
            "name": sheet_name, "added": a, "updated": u, "skipped": s
        })
        summary["totals"]["added"] += a
        summary["totals"]["updated"] += u
        summary["totals"]["skipped"] += s

    db.session.commit()
    return summary


# -----------------------------------------------------------------------------
# Export — Native
# -----------------------------------------------------------------------------
def export_native():
    buf = io.BytesIO()

    def rows_for(category):
        items = Item.query.filter_by(category=category, is_archived=False) \
                          .order_by(Item.name).all()
        out = []
        for i in items:
            out.append({
                "Inventory ID": i.inventory_id,
                "Item / Description": i.name,
                "Category": i.category,
                "Area": i.area.name if i.area else "",
                "Manufacturer / Supplier": i.manufacturer or "",
                "Lot / Serial": i.lot_serial or "",
                "Opening Qty": i.opening_qty,
                "Unit": i.unit,
                "Location": i.location or "",
                "Received / Acquired": i.received_date or "",
                "Expiry Date": i.expiry_date or "",
                "Min Level": i.min_level,
                "Reorder Level": i.reorder_level,
                "Critical?": "Yes" if i.critical else "No",
                "Condition / Status": i.condition_status or "",
                "Cal./Service Due": i.service_due or "",
                "Unit Value (USD)": i.unit_value,
                "Responsible Person": i.responsible or "",
                "Qty In (auto)": i.qty_in,
                "Qty Out (auto)": i.qty_out,
                "Current Qty (auto)": i.current_qty,
                "Stock Status (auto)": i.stock_status,
                "Days to Expiry": i.days_to_expiry if i.days_to_expiry is not None else "",
                "Expiry Status (auto)": i.expiry_status,
                "Service Status (auto)": i.service_status,
                "Current Value (auto)": i.current_value,
                "Notes": i.notes or "",
            })
        return out

    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        for cat in CATEGORIES:
            rows = rows_for(cat)
            df = pd.DataFrame(rows) if rows else pd.DataFrame(
                columns=["Inventory ID", "Item / Description", "Category"])
            df.to_excel(w, sheet_name=cat[:31], index=False)

        txs = Transaction.query.order_by(Transaction.entry_date.desc()).all()
        tx_rows = [{
            "Transaction Date": t.trans_date,
            "Transaction Ref.": t.trans_ref or "",
            "Inventory ID": t.item.inventory_id,
            "Item / Description": t.item.name,
            "Lot / Serial": t.item.lot_serial or "",
            "Transaction Type": t.trans_type,
            "Qty In": t.qty_in or "",
            "Qty Out": t.qty_out or "",
            "Unit": t.unit or "",
            "From Location": t.from_location or "",
            "To Location": t.to_location or "",
            "User": t.user.username if t.user else "",
            "Reference / Notes": t.reference_notes or "",
            "Entry Date": t.entry_date,
        } for t in txs]
        tx_df = pd.DataFrame(tx_rows) if tx_rows else pd.DataFrame(
            columns=["Transaction Date", "Inventory ID", "Transaction Type"])
        tx_df.to_excel(w, sheet_name="Transactions", index=False)

    buf.seek(0)
    return buf


# -----------------------------------------------------------------------------
# Export — SOP-compatible
# -----------------------------------------------------------------------------
SOP_MASTER_HEADERS = [
    "Inventory ID", "Item / Description", "Category", "Manufacturer / Supplier",
    "Lot / Serial", "Opening Qty", "Unit", "Location", "Received / Acquired",
    "Expiry Date", "Min Level", "Reorder Level", "Critical?", "Condition / Status",
    "Cal./Service Due", "Unit Value (USD)", "Responsible Person",
    "Qty In (auto)", "Qty Out (auto)", "Current Qty (auto)", "Stock Status (auto)",
    "Days to Expiry", "Expiry Status (auto)", "Service Status (auto)",
    "Current Value (auto)", "Last Updated",
]

SOP_STOCK_HEADERS = [
    "Transaction Date", "Transaction Ref.", "Inventory ID", "Item / Description",
    "Lot / Serial", "Transaction Type", "Qty In", "Qty Out", "Unit",
    "From Location", "To Location", "User", "Reference / Notes", "Entry Date",
]


def export_sop():
    buf = io.BytesIO()

    master_rows = []
    items = Item.query.filter_by(is_archived=False).order_by(
        Item.category, Item.name).all()
    for i in items:
        master_rows.append({
            "Inventory ID": i.inventory_id,
            "Item / Description": i.name,
            "Category": i.category,
            "Manufacturer / Supplier": i.manufacturer or "",
            "Lot / Serial": i.lot_serial or "",
            "Opening Qty": i.opening_qty,
            "Unit": i.unit,
            "Location": i.location or "",
            "Received / Acquired": i.received_date or "",
            "Expiry Date": i.expiry_date or "",
            "Min Level": i.min_level,
            "Reorder Level": i.reorder_level,
            "Critical?": "Yes" if i.critical else "No",
            "Condition / Status": i.condition_status or "",
            "Cal./Service Due": i.service_due or "",
            "Unit Value (USD)": i.unit_value,
            "Responsible Person": i.responsible or "",
            "Qty In (auto)": i.qty_in,
            "Qty Out (auto)": i.qty_out,
            "Current Qty (auto)": i.current_qty,
            "Stock Status (auto)": i.stock_status,
            "Days to Expiry": i.days_to_expiry if i.days_to_expiry is not None else "",
            "Expiry Status (auto)": i.expiry_status,
            "Service Status (auto)": i.service_status,
            "Current Value (auto)": i.current_value,
            "Last Updated": i.updated_at.strftime("%Y-%m-%d %H:%M") if i.updated_at else "",
        })

    master_df = pd.DataFrame(master_rows, columns=SOP_MASTER_HEADERS)

    tx_rows = []
    for t in Transaction.query.order_by(Transaction.entry_date.desc()).all():
        tx_rows.append({
            "Transaction Date": t.trans_date,
            "Transaction Ref.": t.trans_ref or "",
            "Inventory ID": t.item.inventory_id,
            "Item / Description": t.item.name,
            "Lot / Serial": t.item.lot_serial or "",
            "Transaction Type": t.trans_type,
            "Qty In": t.qty_in or "",
            "Qty Out": t.qty_out or "",
            "Unit": t.unit or "",
            "From Location": t.from_location or "",
            "To Location": t.to_location or "",
            "User": t.user.username if t.user else "",
            "Reference / Notes": t.reference_notes or "",
            "Entry Date": t.entry_date.strftime("%Y-%m-%d %H:%M") if t.entry_date else "",
        })
    tx_df = pd.DataFrame(tx_rows, columns=SOP_STOCK_HEADERS)

    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        master_df.to_excel(w, sheet_name="Master Inventory", index=False)
        tx_df.to_excel(w, sheet_name="Stock Movements", index=False)

    buf.seek(0)
    return buf

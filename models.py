from datetime import datetime, date
from decimal import Decimal
from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, InvalidHashError
from sqlalchemy import case, func, select
from sqlalchemy.ext.hybrid import hybrid_property

db = SQLAlchemy()
ph = PasswordHasher()


class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False, default="viewer")
    full_name = db.Column(db.String(120))
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    last_login_at = db.Column(db.DateTime)

    def set_password(self, raw):
        self.password_hash = ph.hash(raw)

    def check_password(self, raw):
        if not self.password_hash or not raw:
            return False
        try:
            return ph.verify(self.password_hash, raw)
        except (VerifyMismatchError, InvalidHashError):
            return False

    def has_role(self, *roles):
        return self.role in roles

    def __repr__(self):
        return f"<User {self.username} ({self.role})>"


class Area(db.Model):
    __tablename__ = "areas"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), unique=True, nullable=False)
    sort_order = db.Column(db.Integer, default=0)
    is_active = db.Column(db.Boolean, default=True, nullable=False)

    items = db.relationship("Item", backref="area", lazy="dynamic")

    def __repr__(self):
        return f"<Area {self.name}>"


CATEGORIES = ["Apparatus", "Equipment", "Reagent", "Chemical", "Consumable"]
EXPIRY_CATEGORIES = ["Reagent", "Chemical"]

TRANSACTION_TYPES = [
    "RECEIVE", "ISSUE / USE", "TRANSFER IN", "TRANSFER OUT",
    "RETURN", "QUARANTINE", "RELEASE", "DISPOSE",
    "ADJUSTMENT IN", "ADJUSTMENT OUT",
]

UNITS = ["each", "box", "bottle", "pack", "kit",
         "L", "mL", "µL", "g", "mg", "µg",
         "pair", "roll", "sheet", "set", "pcs", "litres", "Rolls", "Boxes", "Pack"]

CONDITION_STATUSES = [
    "Active", "New", "Good", "Fair", "Faulty", "Out of Service",
    "Quarantined", "Near Expiry", "Expired",
    "Awaiting Repair", "Awaiting Disposal", "Disposed / Decommissioned",
]

LOCATIONS = ["Drawer", "Bench", "-8 degree fridge", "-25 degree fridge", "Shelf", "Fridge"]


class Item(db.Model):
    __tablename__ = "items"

    id = db.Column(db.Integer, primary_key=True)
    inventory_id = db.Column(db.String(30), unique=True, nullable=False, index=True)
    name = db.Column(db.String(200), nullable=False, index=True)
    category = db.Column(db.String(30), nullable=False, index=True)
    area_id = db.Column(db.Integer, db.ForeignKey("areas.id"), index=True)

    manufacturer = db.Column(db.String(120))
    lot_serial = db.Column(db.String(120))
    opening_qty = db.Column(db.Float, default=0.0, nullable=False)
    unit = db.Column(db.String(20), default="each", nullable=False)
    location = db.Column(db.String(80))

    received_date = db.Column(db.Date)
    expiry_date = db.Column(db.Date)
    min_level = db.Column(db.Float, default=0.0)
    reorder_level = db.Column(db.Float, default=0.0)
    critical = db.Column(db.Boolean, default=False, nullable=False)

    condition_status = db.Column(db.String(40), default="Active")
    service_due = db.Column(db.Date)
    unit_value = db.Column(db.Float, default=0.0)
    responsible = db.Column(db.String(120))
    notes = db.Column(db.Text)

    is_archived = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow,
                           onupdate=datetime.utcnow, nullable=False)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))

    transactions = db.relationship(
        "Transaction", backref="item", cascade="all, delete-orphan",
        order_by="desc(Transaction.trans_date), desc(Transaction.id)"
    )

    @property
    def qty_in(self):
        return sum(t.qty_in or 0 for t in self.transactions)

    @property
    def qty_out(self):
        return sum(t.qty_out or 0 for t in self.transactions)

    @hybrid_property
    def current_qty(self):
        opening_qty = Decimal(str(self.opening_qty or 0))
        qty_in = sum(
            (Decimal(str(transaction.qty_in or 0)) for transaction in self.transactions),
            Decimal("0"),
        )
        qty_out = sum(
            (Decimal(str(transaction.qty_out or 0)) for transaction in self.transactions),
            Decimal("0"),
        )
        return float(opening_qty + qty_in - qty_out)

    @current_qty.expression
    def current_qty(cls):
        qty_in = (select(func.coalesce(func.sum(Transaction.qty_in), 0.0))
                  .where(Transaction.item_id == cls.id).scalar_subquery())
        qty_out = (select(func.coalesce(func.sum(Transaction.qty_out), 0.0))
                   .where(Transaction.item_id == cls.id).scalar_subquery())
        return func.coalesce(cls.opening_qty, 0.0) + qty_in - qty_out

    @hybrid_property
    def stock_status(self):
        q = self.current_qty
        if q <= (self.min_level or 0):
            return "CRITICAL" if self.critical else "BELOW MIN"
        if q <= (self.reorder_level or 0):
            return "REORDER"
        return "OK"

    @stock_status.expression
    def stock_status(cls):
        q = cls.current_qty
        minimum = func.coalesce(cls.min_level, 0.0)
        reorder = func.coalesce(cls.reorder_level, 0.0)
        return case(
            (q <= minimum, case((cls.critical.is_(True), "CRITICAL"), else_="BELOW MIN")),
            (q <= reorder, "REORDER"),
            else_="OK",
        )

    @property
    def days_to_expiry(self):
        if self.category not in EXPIRY_CATEGORIES or not self.expiry_date:
            return None
        return (self.expiry_date - date.today()).days

    @property
    def expiry_status(self):
        if self.category not in EXPIRY_CATEGORIES or not self.expiry_date:
            return "N/A"
        d = (self.expiry_date - date.today()).days
        if d < 0:
            return "EXPIRED"
        if d <= 90:
            return "NEAR EXPIRY"
        return "OK"

    @property
    def service_status(self):
        if not self.service_due:
            return "N/A"
        d = (self.service_due - date.today()).days
        if d < 0:
            return "OVERDUE"
        if d <= 30:
            return "DUE <=30 DAYS"
        return "OK"

    @property
    def current_value(self):
        return round(self.current_qty * (self.unit_value or 0), 2)

    @property
    def is_low(self):
        return self.current_qty <= (self.reorder_level or 0)

    @property
    def is_out(self):
        return self.current_qty <= 0

    def __repr__(self):
        return f"<Item {self.inventory_id} {self.name}>"


class Transaction(db.Model):
    __tablename__ = "transactions"

    id = db.Column(db.Integer, primary_key=True)
    item_id = db.Column(db.Integer, db.ForeignKey("items.id"), nullable=False, index=True)
    trans_date = db.Column(db.Date, nullable=False, default=date.today)
    trans_ref = db.Column(db.String(40))
    trans_type = db.Column(db.String(30), nullable=False)
    qty_in = db.Column(db.Float, default=0.0)
    qty_out = db.Column(db.Float, default=0.0)
    unit = db.Column(db.String(20))
    from_location = db.Column(db.String(80))
    to_location = db.Column(db.String(80))
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    reference_notes = db.Column(db.Text)
    entry_date = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    user = db.relationship("User", foreign_keys=[user_id])

    def __repr__(self):
        return f"<Tx {self.trans_type} item={self.item_id}>"


class AuditLog(db.Model):
    __tablename__ = "audit_logs"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    action = db.Column(db.String(30), nullable=False)
    entity_type = db.Column(db.String(30))
    entity_id = db.Column(db.Integer)
    before_json = db.Column(db.Text)
    after_json = db.Column(db.Text)
    ip_address = db.Column(db.String(64))
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    user = db.relationship("User", foreign_keys=[user_id])
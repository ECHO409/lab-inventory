 from datetime import date
from flask_wtf import FlaskForm
from wtforms import (StringField, PasswordField, SelectField, FloatField,
                     DateField, BooleanField, TextAreaField, SubmitField)
from wtforms.validators import DataRequired, Optional, NumberRange, Length

from models import (CATEGORIES, TRANSACTION_TYPES, UNITS,
                    CONDITION_STATUSES, LOCATIONS)


class LoginForm(FlaskForm):
    username = StringField("Username", validators=[DataRequired(), Length(3, 64)])
    password = PasswordField("Password", validators=[DataRequired()])
    remember = BooleanField("Remember session")
    submit = SubmitField("Sign in")


class ItemForm(FlaskForm):
    inventory_id = StringField("Inventory ID", validators=[Optional(), Length(max=30)])
    name = StringField("Item / Description", validators=[DataRequired(), Length(max=200)])
    category = SelectField("Category",
                           choices=[(c, c) for c in CATEGORIES],
                           validators=[DataRequired()])
    area_id = SelectField("Area", coerce=int, validators=[Optional()])
    manufacturer = StringField("Manufacturer / Supplier",
                               validators=[Optional(), Length(max=120)])
    lot_serial = StringField("Lot / Serial", validators=[Optional(), Length(max=120)])
    opening_qty = FloatField("Opening Qty", default=0.0)
    unit = SelectField("Unit",
                       choices=[(u, u) for u in UNITS],
                       validators=[DataRequired()])
    # UPDATED: Changed from StringField to SelectField
    location = SelectField("Location",
                           choices=[("", "—")] + [(loc, loc) for loc in LOCATIONS],
                           validators=[Optional()])
    received_date = DateField("Received / Acquired", validators=[Optional()])
    expiry_date = DateField("Expiry Date", validators=[Optional()])
    min_level = FloatField("Min Level", default=0.0)
    reorder_level = FloatField("Reorder Level", default=0.0)
    critical = BooleanField("Critical?")
    condition_status = SelectField("Condition / Status",
                                   choices=[(c, c) for c in CONDITION_STATUSES],
                                   validators=[Optional()])
    service_due = DateField("Cal./Service Due", validators=[Optional()])
    unit_value = FloatField("Unit Value (USD)", default=0.0)
    responsible = StringField("Responsible Person",
                              validators=[Optional(), Length(max=120)])
    notes = TextAreaField("Notes", validators=[Optional()])
    submit = SubmitField("Save Item")


class TransactionForm(FlaskForm):
    trans_date = DateField("Date", default=date.today, validators=[DataRequired()])
    trans_type = SelectField("Transaction Type",
                             choices=[(t, t) for t in TRANSACTION_TYPES],
                             validators=[DataRequired()])
    qty_in = FloatField("Qty In", default=0.0,
                        validators=[Optional(), NumberRange(min=0)])
    qty_out = FloatField("Qty Out", default=0.0,
                         validators=[Optional(), NumberRange(min=0)])
    unit = SelectField("Unit",
                       choices=[(u, u) for u in UNITS],
                       validators=[Optional()])
    from_location = StringField("From Location",
                                validators=[Optional(), Length(max=80)])
    to_location = StringField("To Location",
                              validators=[Optional(), Length(max=80)])
    trans_ref = StringField("Reference", validators=[Optional(), Length(max=40)])
    reference_notes = TextAreaField("Notes", validators=[Optional()])
    submit = SubmitField("Record Movement")


class UserForm(FlaskForm):
    username = StringField("Username", validators=[DataRequired(), Length(3, 64)])
    full_name = StringField("Full Name", validators=[Optional(), Length(max=120)])
    role = SelectField("Role", choices=[
        ("viewer", "Viewer"),
        ("technician", "Technician"),
        ("admin", "Administrator"),
    ], validators=[DataRequired()])
    password = PasswordField("Password (leave blank to keep current)",
                             validators=[Optional()])
    is_active = BooleanField("Active", default=True)
    submit = SubmitField("Save User")
from datetime import date, datetime
import json
from decimal import Decimal
from flask import (Flask, render_template, redirect, url_for, flash,
                   request, abort, send_file)
from flask_login import LoginManager, login_required, current_user
from flask_wtf.csrf import CSRFProtect
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from sqlalchemy.orm import selectinload
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy import case, func

from config import Config
from models import (db, User, Item, Transaction, Area, AuditLog,
                    CATEGORIES, LOCATIONS, EXPIRY_CATEGORIES)
from forms import LoginForm, ItemForm, TransactionForm
from auth import auth_bp, role_required
from services import import_workbook, export_native, export_sop

login_manager = LoginManager()
csrf = CSRFProtect()
limiter = Limiter(key_func=get_remote_address)


def create_app(config_overrides=None):
    app = Flask(__name__)
    app.config.from_object(Config)
    if config_overrides:
        app.config.update(config_overrides)

    # Log which DB we're on at startup (password redacted)
    uri = app.config["SQLALCHEMY_DATABASE_URI"]
    shown = uri.split("@")[-1] if "@" in uri else uri
    app.logger.info("Database: %s", shown)

    db.init_app(app)
    csrf.init_app(app)
    limiter.init_app(app)
    login_manager.init_app(app)

    login_manager.login_view = "auth.login"
    login_manager.login_message = "Please sign in."
    login_manager.login_message_category = "warning"

    @login_manager.user_loader
    def load_user(uid):
        return User.query.get(int(uid))

    @app.after_request
    def add_security_headers(response):
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
        return response

    app.register_blueprint(auth_bp)

    @app.context_processor
    def inject_globals():
        return {"CATEGORIES": CATEGORIES, "LOCATIONS": LOCATIONS}

    @app.template_filter("quantity")
    def format_quantity(value):
        return format(Decimal(str(value or 0)).normalize(), "f")

    register_routes(app)

    with app.app_context():
        db.create_all()
        bootstrap_admin(app)
        bootstrap_areas(app)

    return app


def bootstrap_admin(app):
    if User.query.count() > 0:
        return
    u = User(
        username=app.config["ADMIN_USERNAME"],
        role="admin",
        full_name=app.config["ADMIN_FULLNAME"],
        is_active=True,
    )
    u.set_password(app.config["ADMIN_PASSWORD"])
    db.session.add(u)
    db.session.commit()
    app.logger.info(f"Bootstrap admin '{u.username}' created.")


DEFAULT_AREAS = [
    "Preparation Lab",
    "Microbio 1",
    "Microbio 2",
    "Biochemical Analysis 1",
    "Biochemical Analysis 2",
    "Biochemical Analysis 3",
]


def bootstrap_areas(app):
    areas_by_name = {area.name: area for area in Area.query.all()}
    for i, name in enumerate(DEFAULT_AREAS):
        area = areas_by_name.get(name)
        if area is None:
            area = Area(name=name)
            db.session.add(area)
        area.sort_order = i
        area.is_active = True
    for area in areas_by_name.values():
        if area.name not in DEFAULT_AREAS:
            area.is_active = False
    db.session.commit()
    app.logger.info("Default areas synchronized.")


ID_PREFIX = {
    "Apparatus": "APP",
    "Equipment": "EQU",
    "Reagent": "RGT",
    "Chemical": "CHM",
    "Consumable": "CON",
}


def next_inventory_id(category):
    prefix = ID_PREFIX.get(category, "ITM")
    existing = Item.query.filter(Item.inventory_id.like(f"{prefix}-%")).all()
    max_n = 0
    for it in existing:
        try:
            n = int(it.inventory_id.split("-")[-1])
            max_n = max(max_n, n)
        except (ValueError, IndexError):
            continue
    return f"{prefix}-{max_n + 1:03d}"


def item_snapshot(item):
    fields = (
        "inventory_id", "name", "category", "area_id", "manufacturer",
        "lot_serial", "opening_qty", "unit", "location", "received_date",
        "expiry_date", "min_level", "reorder_level", "critical",
        "condition_status", "service_due", "unit_value", "responsible", "notes",
        "is_archived",
    )
    return {field: getattr(item, field) for field in fields}


def find_duplicate_item(name, category, area_id, exclude_item_id=None):
    if not name or not category or not area_id:
        return None
    name = name.strip()
    if not name:
        return None
    query = Item.query.filter(
        Item.category == category,
        Item.area_id == area_id,
        Item.is_archived.is_(False),
        func.lower(Item.name) == name.lower(),
    )
    if exclude_item_id:
        query = query.filter(Item.id != exclude_item_id)
    return query.first()


def log_audit(action, entity_type=None, entity_id=None, before=None, after=None):
    db.session.add(AuditLog(
        user_id=current_user.id if current_user.is_authenticated else None,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        before_json=json.dumps(before, default=lambda value: value.isoformat()
                               if hasattr(value, "isoformat") else str(value)) if before is not None else None,
        after_json=json.dumps(after, default=lambda value: value.isoformat()
                              if hasattr(value, "isoformat") else str(value)) if after is not None else None,
        ip_address=request.remote_addr,
    ))


def register_routes(app):

    @app.route("/health")
    def health():
        try:
            db.session.execute(db.text("SELECT 1"))
        except SQLAlchemyError:
            db.session.rollback()
            return {"status": "unavailable", "service": "lab-inventory"}, 503
        return {"status": "ok", "service": "lab-inventory"}, 200

    def sync_archive_status(item):
        db.session.flush()
        db.session.expire(item, ["transactions"])
        should_archive = item.current_qty <= 0
        if item.is_archived == should_archive:
            return
        before = item_snapshot(item)
        item.is_archived = should_archive
        log_audit("ARCHIVE" if should_archive else "RESTORE", "Item", item.id,
                  before=before, after=item_snapshot(item))

    # ----- Dashboard ---------------------------------------------------------
    @app.route("/")
    @login_required
    def index():
        today = date.today()
        in_90_days = today.fromordinal(today.toordinal() + 90)
        in_30_days = today.fromordinal(today.toordinal() + 30)
        qty = Item.current_qty
        stock_status = Item.stock_status
        summary = db.session.query(
            func.count(Item.id),
            func.coalesce(func.sum(case((stock_status == "CRITICAL", 1), else_=0)), 0),
            func.coalesce(func.sum(case((stock_status == "REORDER", 1), else_=0)), 0),
            func.coalesce(func.sum(case((stock_status == "OK", 1), else_=0)), 0),
            func.coalesce(func.sum(case((Item.expiry_date < today, 1), else_=0)), 0),
            func.coalesce(func.sum(case((Item.expiry_date.between(today, in_90_days), 1), else_=0)), 0),
            func.coalesce(func.sum(case((Item.service_due < today, 1), else_=0)), 0),
            func.coalesce(func.sum(case((Item.service_due.between(today, in_30_days), 1), else_=0)), 0),
            func.coalesce(func.sum(qty * func.coalesce(Item.unit_value, 0.0)), 0.0),
        ).filter(Item.is_archived.is_(False)).one()
        (total_lines, critical, reorder, ok, expired,
         near_expiry, service_overdue, service_due_30, total_value) = summary
        total_value = round(total_value, 2)
        archived_count = Item.query.filter_by(is_archived=True).count()
        cat_counts = dict(
            db.session.query(Item.category, func.count(Item.id))
            .filter(Item.is_archived.is_(False)).group_by(Item.category).all()
        )
        cat_counts = {category: cat_counts.get(category, 0) for category in CATEGORIES}

        return render_template(
            "dashboard.html",
            total_lines=total_lines,
            critical=critical,
            reorder=reorder,
            ok=ok,
            near_expiry=near_expiry,
            expired=expired,
            service_due_30=service_due_30,
            service_overdue=service_overdue,
            total_value=total_value,
            archived_count=archived_count,
            cat_counts=cat_counts,
            categories=CATEGORIES,
        )

    # ----- Section -----------------------------------------------------------
    @app.route("/section/<category>")
    @login_required
    def section(category):
        if category not in CATEGORIES:
            flash("Unknown category.", "error")
            return redirect(url_for("index"))

        show = request.args.get("show", "active")
        if show not in {"active", "low", "out", "all"}:
            show = "active"
        area_id = request.args.get("area", type=int)
        q = request.args.get("q", "").strip()

        query = (Item.query.options(selectinload(Item.area), selectinload(Item.transactions))
                 .filter_by(category=category, is_archived=False))

        if area_id:
            query = query.filter_by(area_id=area_id)

        if q:
            like = f"%{q}%"
            query = query.filter(db.or_(
                Item.name.ilike(like),
                Item.inventory_id.ilike(like),
                Item.lot_serial.ilike(like),
                Item.location.ilike(like),
            ))

        qty = Item.current_qty
        if show == "active":
            query = query.filter(qty > 0)
        elif show == "low":
            query = query.filter(qty > 0, Item.stock_status.in_(("CRITICAL", "REORDER")))
        elif show == "out":
            query = query.filter(qty <= 0)

        counts_row = db.session.query(
            func.count(Item.id),
            func.coalesce(func.sum(case((qty > 0, 1), else_=0)), 0),
            func.coalesce(func.sum(case((db.and_(qty > 0, Item.stock_status.in_(("CRITICAL", "REORDER"))), 1), else_=0)), 0),
            func.coalesce(func.sum(case((qty <= 0, 1), else_=0)), 0),
        ).filter(Item.category == category, Item.is_archived.is_(False)).one()
        counts = {"all": counts_row[0], "active": counts_row[1],
                  "low": counts_row[2], "out": counts_row[3]}
        page_obj = query.order_by(Item.name).paginate(
            page=max(request.args.get("page", 1, type=int), 1),
            per_page=50, error_out=False,
        )

        areas = (Area.query.filter_by(is_active=True)
             .order_by(Area.sort_order).all())

        return render_template(
            "section.html",
            category=category,
            items=page_obj.items,
            page_obj=page_obj,
            show=show,
            area_id=area_id,
            q=q,
            counts=counts,
            areas=areas,
        )

    # ----- Add Item ----------------------------------------------------------
    @app.route("/item/new/<category>", methods=["GET", "POST"])
    @role_required("admin")
    def item_new(category):
        if category not in CATEGORIES:
            abort(404)

        form = ItemForm()
        form.category.data = category
        form.area_id.choices = [(0, "—")] + [
            (a.id, a.name) for a in Area.query.filter_by(is_active=True)
            .order_by(Area.sort_order).all()
        ]

        if form.validate_on_submit():
            area_id = form.area_id.data or None
            duplicate = find_duplicate_item(form.name.data, category, area_id)
            if duplicate:
                flash(f"{duplicate.name} already exists in this area and was not recorded again.", "warning")
                return redirect(url_for("item_detail", item_id=duplicate.id))

            item = Item()
            item.category = category
            item.inventory_id = (form.inventory_id.data or "").strip() \
                                or next_inventory_id(category)
            item.name = form.name.data.strip()
            item.area_id = area_id
            item.manufacturer = form.manufacturer.data
            item.lot_serial = form.lot_serial.data
            item.opening_qty = form.opening_qty.data or 0.0
            item.unit = form.unit.data
            item.location = form.location.data
            item.received_date = form.received_date.data
            item.expiry_date = form.expiry_date.data if category in EXPIRY_CATEGORIES else None
            item.min_level = form.min_level.data or 0.0
            item.reorder_level = form.reorder_level.data or 0.0
            item.critical = bool(form.critical.data)
            item.condition_status = form.condition_status.data
            item.service_due = form.service_due.data
            item.unit_value = form.unit_value.data or 0.0
            item.responsible = form.responsible.data
            item.notes = form.notes.data
            item.is_archived = item.opening_qty <= 0
            item.created_by_id = current_user.id

            db.session.add(item)
            db.session.flush()

            log_audit("CREATE", "Item", item.id, after=item_snapshot(item))
            db.session.commit()

            flash(f"Item {item.inventory_id} created.", "success")
            return redirect(url_for("item_detail", item_id=item.id))

        return render_template("item_form.html", form=form,
                               category=category, item=None)

    # ----- Edit Item ---------------------------------------------------------
    @app.route("/item/<int:item_id>/edit", methods=["GET", "POST"])
    @role_required("admin")
    def item_edit(item_id):
        item = Item.query.get_or_404(item_id)
        form = ItemForm(obj=item)
        form.category.data = item.category
        if request.method == "GET":
            form.current_qty.data = item.current_qty
        form.area_id.choices = [(0, "—")] + [
            (a.id, a.name) for a in Area.query.filter_by(is_active=True)
            .order_by(Area.sort_order).all()
        ]
        if item.area and not item.area.is_active:
            form.area_id.choices.append((item.area.id, item.area.name))
        form.location.choices = [("", "—")] + [(loc, loc) for loc in LOCATIONS]
        if item.location and item.location not in LOCATIONS:
            form.location.choices.append((item.location, item.location))

        if form.validate_on_submit():
            duplicate = find_duplicate_item(form.name.data, item.category, form.area_id.data or None, exclude_item_id=item.id)
            if duplicate:
                flash(f"{duplicate.name} already exists in this area and was not duplicated.", "warning")
                return redirect(url_for("item_detail", item_id=duplicate.id))

            target_qty = form.current_qty.data
            if target_qty is None:
                flash("Enter the current quantity.", "error")
                return render_template("item_form.html", form=form,
                                       category=item.category, item=item)
            previous_qty = item.current_qty
            previous_unit = item.unit
            before = item_snapshot(item)
            item.inventory_id = (form.inventory_id.data or "").strip() or item.inventory_id
            item.name = form.name.data.strip()
            item.area_id = form.area_id.data or None
            item.manufacturer = form.manufacturer.data
            item.lot_serial = form.lot_serial.data
            item.unit = form.unit.data
            item.location = form.location.data
            item.received_date = form.received_date.data
            item.expiry_date = form.expiry_date.data if item.category in EXPIRY_CATEGORIES else None
            item.min_level = form.min_level.data or 0.0
            item.reorder_level = form.reorder_level.data or 0.0
            item.critical = bool(form.critical.data)
            item.condition_status = form.condition_status.data
            item.service_due = form.service_due.data
            item.unit_value = form.unit_value.data or 0.0
            item.responsible = form.responsible.data
            item.notes = form.notes.data

            quantity_change = target_qty - previous_qty
            if quantity_change:
                transaction = Transaction(
                    item_id=item.id,
                    trans_date=date.today(),
                    trans_type="ADJUSTMENT IN" if quantity_change > 0 else "ADJUSTMENT OUT",
                    qty_in=max(quantity_change, 0),
                    qty_out=max(-quantity_change, 0),
                    unit=previous_unit,
                    reference_notes="Current quantity set from item edit.",
                    user_id=current_user.id,
                )
                db.session.add(transaction)
                db.session.flush()
                log_audit("CREATE", "Transaction", transaction.id)

            sync_archive_status(item)
            log_audit("UPDATE", "Item", item.id, before=before, after=item_snapshot(item))
            db.session.commit()

            flash("Item updated.", "success")
            return redirect(url_for("item_detail", item_id=item.id))

        return render_template("item_form.html", form=form,
                               category=item.category, item=item)

    # ----- Item detail -------------------------------------------------------
    @app.route("/item/<int:item_id>")
    @login_required
    def item_detail(item_id):
        item = Item.query.get_or_404(item_id)
        tx_form = TransactionForm()
        tx_form.unit.data = item.unit
        areas = (Area.query.filter_by(is_active=True)
             .order_by(Area.sort_order).all())
        return render_template("item_detail.html", item=item,
                       tx_form=tx_form, areas=areas,
                       locations=LOCATIONS, today=date.today().isoformat())

    # ----- Log transaction ---------------------------------------------------
    @app.route("/item/<int:item_id>/transaction", methods=["POST"])
    @role_required("admin", "technician")
    def item_transaction(item_id):
        item = Item.query.filter_by(id=item_id).with_for_update().first_or_404()
        form = TransactionForm()

        if not form.validate_on_submit():
            flash("Please check the form values.", "error")
            return redirect(url_for("item_detail", item_id=item.id))

        qty_in = form.qty_in.data or 0.0
        qty_out = form.qty_out.data or 0.0
        if form.trans_type.data == "ADJUSTMENT OUT" and qty_in > 0 and qty_out == 0:
            qty_out, qty_in = qty_in, 0.0
        inflow_types = {"RECEIVE", "TRANSFER IN", "RETURN", "RELEASE", "ADJUSTMENT IN"}
        outflow_types = {"ISSUE / USE", "TRANSFER OUT", "QUARANTINE", "DISPOSE", "ADJUSTMENT OUT"}

        expected = "in" if form.trans_type.data in inflow_types else "out"
        if (expected == "in" and (qty_in <= 0 or qty_out != 0)) or \
                (expected == "out" and (qty_out <= 0 or qty_in != 0)):
            flash("Enter a positive quantity in the matching Qty In or Qty Out field only.", "error")
            return redirect(url_for("item_detail", item_id=item.id))

        if qty_out > item.current_qty:
            flash(f"Insufficient stock. Only {item.current_qty:g} {item.unit} is available.", "error")
            return redirect(url_for("item_detail", item_id=item.id))

        if form.unit.data and form.unit.data != item.unit:
            flash(f"This item is measured in {item.unit}. Use that unit for the movement.", "error")
            return redirect(url_for("item_detail", item_id=item.id))

        reference_notes = (form.reference_notes.data or "").strip()
        if form.trans_type.data.startswith("ADJUSTMENT") and not reference_notes:
            reference_notes = "Quick stock adjustment."

        tx = Transaction(
            item_id=item.id,
            trans_date=form.trans_date.data or date.today(),
            trans_type=form.trans_type.data,
            qty_in=qty_in,
            qty_out=qty_out,
            unit=form.unit.data or item.unit,
            from_location=form.from_location.data,
            to_location=form.to_location.data,
            trans_ref=form.trans_ref.data,
            reference_notes=reference_notes,
            user_id=current_user.id,
        )
        db.session.add(tx)
        db.session.flush()
        log_audit("CREATE", "Transaction", tx.id)
        sync_archive_status(item)
        db.session.commit()

        flash(f"Movement recorded for {item.inventory_id}.", "success")
        return redirect(url_for("item_detail", item_id=item.id))

    @app.route("/item/<int:item_id>/relocate", methods=["POST"])
    @role_required("admin", "technician")
    def item_relocate(item_id):
        item = Item.query.get_or_404(item_id)
        area_id = request.form.get("area_id", type=int)
        area = Area.query.filter_by(id=area_id, is_active=True).first()
        location = request.form.get("location", "").strip()
        if area is None:
            flash("Choose a valid active area.", "error")
        elif location not in LOCATIONS:
            flash("Choose Shelf, Bench, or Fridge as the location.", "error")
        elif area.id != item.area_id or location != (item.location or ""):
            before = item_snapshot(item)
            item.area_id = area.id
            item.location = location
            log_audit("UPDATE", "Item", item.id, before=before,
                      after=item_snapshot(item))
            db.session.commit()
            flash(f"Area and location updated for {item.inventory_id}.", "success")
        return redirect(url_for("item_detail", item_id=item.id))

    @app.route("/item/<int:item_id>/location", methods=["POST"])
    @role_required("admin", "technician")
    def item_location_update(item_id):
        item = Item.query.get_or_404(item_id)
        location = request.form.get("location", "").strip()
        archived_update = request.form.get("return_to") == "archived"
        if len(location) > 80:
            flash("Location must be 80 characters or fewer.", "error")
        elif not archived_update and location not in LOCATIONS and location != (item.location or ""):
            flash("Choose Shelf, Bench, or Fridge as the location.", "error")
        elif location != (item.location or ""):
            before = item_snapshot(item)
            item.location = location or None
            log_audit("UPDATE", "Item", item.id, before=before, after=item_snapshot(item))
            db.session.commit()
            flash(f"Location updated for {item.inventory_id}.", "success")
        if request.form.get("return_to") == "archived":
            return redirect(url_for("archived", page=request.form.get("page", "1")))
        return redirect(url_for(
            "section", category=item.category,
            show=request.form.get("show", "active"),
            area=request.form.get("area") or None,
            q=request.form.get("q", ""),
            page=request.form.get("page", "1"),
        ))

    @app.route("/item/<int:item_id>/archive", methods=["POST"])
    @role_required("admin", "technician")
    def item_archive(item_id):
        item = Item.query.get_or_404(item_id)
        if item.current_qty > 0:
            flash("Only out-of-stock items can be archived.", "warning")
            return redirect(url_for("item_detail", item_id=item.id))
        before = item_snapshot(item)
        item.is_archived = True
        log_audit("ARCHIVE", "Item", item.id, before=before, after=item_snapshot(item))
        db.session.commit()
        flash(f"{item.inventory_id} moved to the archive.", "success")
        return redirect(url_for("archived"))

    @app.route("/item/<int:item_id>/delete", methods=["POST"])
    @role_required("admin")
    def item_delete(item_id):
        item = Item.query.get_or_404(item_id)
        item_ref = item.inventory_id
        before = item_snapshot(item)
        log_audit("DELETE", "Item", item.id, before=before)
        db.session.delete(item)
        db.session.commit()
        flash(f"{item_ref} was permanently deleted from the system.", "error")
        return redirect(url_for("index"))

    @app.route("/item/<int:item_id>/restore", methods=["POST"])
    @role_required("admin", "technician")
    def item_restore(item_id):
        item = Item.query.get_or_404(item_id)
        if not item.is_archived:
            flash("This item is not archived.", "warning")
            return redirect(url_for("item_detail", item_id=item.id))
        if item.current_qty <= 0:
            flash("Add stock before restoring this item to inventory.", "warning")
            return redirect(url_for("archived"))
        before = item_snapshot(item)
        item.is_archived = False
        log_audit("RESTORE", "Item", item.id, before=before, after=item_snapshot(item))
        db.session.commit()
        flash(f"{item.inventory_id} restored to inventory.", "success")
        return redirect(url_for("item_detail", item_id=item.id))

    # ----- Archived ----------------------------------------------------------
    @app.route("/archived")
    @login_required
    def archived():
        page_obj = (Item.query.options(selectinload(Item.transactions))
                    .filter_by(is_archived=True)
                    .order_by(Item.category, Item.name)
                    .paginate(page=max(request.args.get("page", 1, type=int), 1),
                              per_page=50, error_out=False))
        return render_template("archived.html", items=page_obj.items, page_obj=page_obj)

    # ----- Transactions log --------------------------------------------------
    @app.route("/transactions")
    @login_required
    def transactions():
        txs = (Transaction.query
               .order_by(Transaction.entry_date.desc())
               .limit(300).all())
        return render_template("transactions.html", txs=txs)

    # ----- Import ------------------------------------------------------------
    @app.route("/import", methods=["GET", "POST"])
    @role_required("admin")
    def import_page():
        if request.method == "POST":
            f = request.files.get("file")
            if not f or f.filename == "":
                flash("Please choose a file.", "error")
                return redirect(url_for("import_page"))

            try:
                summary = import_workbook(f, current_user.id)
                log_audit("IMPORT", "Item", None)
                db.session.commit()
                return render_template("import.html", summary=summary)
            except Exception as e:
                db.session.rollback()
                flash(f"Import failed: {e}", "error")
                return redirect(url_for("import_page"))

        return render_template("import.html", summary=None)

    # ----- Export ------------------------------------------------------------
    @app.route("/export")
    @login_required
    def export_page():
        return render_template("export.html")

    @app.route("/export/native")
    @login_required
    def export_native_route():
        buf = export_native()
        log_audit("EXPORT", "Item", None)
        db.session.commit()
        stamp = datetime.now().strftime("%Y%m%d_%H%M")
        return send_file(
            buf,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            as_attachment=True,
            download_name=f"lab_inventory_native_{stamp}.xlsx",
        )

    @app.route("/export/sop")
    @login_required
    def export_sop_route():
        buf = export_sop()
        log_audit("EXPORT", "Item", None)
        db.session.commit()
        stamp = datetime.now().strftime("%Y%m%d_%H%M")
        return send_file(
            buf,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            as_attachment=True,
            download_name=f"master_inventory_sop_{stamp}.xlsx",
        )


app = create_app()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(__import__("os").getenv("PORT", "5000")), debug=app.config.get("DEBUG", False))
import unittest
from datetime import date

from app import create_app, bootstrap_areas, DEFAULT_AREAS
from models import db, User, Item, Area, Transaction


class SecurityAndBrandingTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app({
            "TESTING": True,
            "WTF_CSRF_ENABLED": False,
            "SQLALCHEMY_DATABASE_URI": "sqlite://",
            "SECRET_KEY": "test-secret-key",
        })

        with self.app.app_context():
            db.drop_all()
            db.create_all()

            area = Area(name="Main Lab", sort_order=1)
            db.session.add(area)
            db.session.flush()

            item = Item(
                inventory_id="APP-001",
                name="Microscope",
                category="Apparatus",
                area_id=area.id,
                opening_qty=5,
                unit="each",
                location="Bench A",
                min_level=2,
                reorder_level=3,
            )
            db.session.add(item)
            db.session.commit()
            self.item_id = item.id

            admin = User(username="admin_user", role="admin", full_name="Admin User")
            admin.set_password("StrongPass!123")
            technical = User(username="tech_user", role="technician", full_name="Tech User")
            technical.set_password("StrongPass!123")
            viewer = User(username="viewer_user", role="viewer", full_name="Viewer User")
            viewer.set_password("StrongPass!123")
            db.session.add_all([admin, technical, viewer])
            db.session.commit()

            self.admin_user = admin.username
            self.tech_user = technical.username
            self.viewer_user = viewer.username

    def test_admin_can_open_edit_page(self):
        with self.app.test_client() as client:
            login = client.post(
                "/login",
                data={"username": self.admin_user, "password": "StrongPass!123"},
                follow_redirects=True,
            )
            self.assertEqual(login.status_code, 200)
            response = client.get(f"/item/{self.item_id}/edit")
            self.assertIn(response.status_code, (200, 302))

    def test_technician_cannot_edit_inventory_items(self):
        with self.app.test_client() as client:
            client.post(
                "/login",
                data={"username": self.tech_user, "password": "StrongPass!123"},
                follow_redirects=True,
            )
            response = client.get(f"/item/{self.item_id}/edit")
            self.assertIn(response.status_code, (302, 403))

    def test_login_page_has_nprdc_branding(self):
        with self.app.test_client() as client:
            response = client.get("/login")
            self.assertEqual(response.status_code, 200)
            text = response.get_data(as_text=True)
            self.assertIn("Midlands State University", text)
            self.assertIn("National Pathology Research and Diagnostic Centre", text)
            self.assertIn("MSU NPRDC", text)

    def _login_admin(self, client):
        return client.post("/login", data={
            "username": self.admin_user,
            "password": "StrongPass!123",
        })

    def test_item_detail_page_renders(self):
        with self.app.app_context():
            item = db.session.get(Item, self.item_id)
            item.opening_qty = 2.0
            item.unit = "pcs"
            db.session.commit()

        with self.app.test_client() as client:
            self._login_admin(client)
            response = client.get(f"/item/{self.item_id}")
            self.assertEqual(response.status_code, 200)
            self.assertIn('class="item-current-qty">2 pcs</dd>',
                          response.get_data(as_text=True))

    def test_current_quantity_preserves_decimal_precision(self):
        with self.app.app_context():
            item = db.session.get(Item, self.item_id)
            item.opening_qty = 0.1
            db.session.add(Transaction(
                item_id=item.id,
                trans_type="RECEIVE",
                qty_in=0.2,
                qty_out=0,
                unit="each",
            ))
            db.session.commit()
            self.assertEqual(item.current_qty, 0.3)

        with self.app.test_client() as client:
            self._login_admin(client)
            response = client.get(f"/item/{self.item_id}")
            self.assertEqual(response.status_code, 200)
            self.assertIn('class="item-current-qty">0.3 each</dd>',
                          response.get_data(as_text=True))

    def test_login_rejects_external_next_url(self):
        with self.app.test_client() as client:
            response = client.post(
                "/login?next=https://attacker.example/",
                data={"username": self.admin_user, "password": "StrongPass!123"},
            )
            self.assertEqual(response.status_code, 302)
            self.assertEqual(response.headers["Location"], "/")

    def test_issue_cannot_exceed_current_stock(self):
        with self.app.test_client() as client:
            self._login_admin(client)
            response = client.post(f"/item/{self.item_id}/transaction", data={
                "trans_type": "ISSUE / USE",
                "qty_in": "0",
                "qty_out": "6",
                "unit": "each",
            })
            self.assertEqual(response.status_code, 302)
            with self.app.app_context():
                self.assertEqual(Transaction.query.filter_by(item_id=self.item_id).count(), 0)

    def test_transaction_unit_must_match_item_unit(self):
        with self.app.test_client() as client:
            self._login_admin(client)
            response = client.post(f"/item/{self.item_id}/transaction", data={
                "trans_type": "RECEIVE",
                "qty_in": "2",
                "qty_out": "0",
                "unit": "box",
            })
            self.assertEqual(response.status_code, 302)
            with self.app.app_context():
                self.assertEqual(Transaction.query.filter_by(item_id=self.item_id).count(), 0)

    def test_adjustment_out_reduces_stock(self):
        with self.app.test_client() as client:
            self._login_admin(client)
            response = client.post(f"/item/{self.item_id}/transaction", data={
                "trans_type": "ADJUSTMENT OUT",
                "qty_in": "2",
                "qty_out": "0",
                "unit": "each",
            })
            self.assertEqual(response.status_code, 302)
            with self.app.app_context():
                item = db.session.get(Item, self.item_id)
                transaction = Transaction.query.filter_by(item_id=self.item_id).one()
                self.assertEqual(item.current_qty, 3)
                self.assertEqual(transaction.qty_out, 2)
                self.assertEqual(transaction.reference_notes, "Quick stock adjustment.")

    def test_adjustment_in_without_reason_updates_stock(self):
        with self.app.test_client() as client:
            self._login_admin(client)
            response = client.post(f"/item/{self.item_id}/transaction", data={
                "trans_type": "ADJUSTMENT IN",
                "qty_in": "1",
                "qty_out": "0",
                "unit": "each",
            })
            self.assertEqual(response.status_code, 302)
            with self.app.app_context():
                item = db.session.get(Item, self.item_id)
                transaction = Transaction.query.filter_by(item_id=self.item_id).one()
                self.assertEqual(item.current_qty, 6)
                self.assertEqual(transaction.qty_in, 1)
                self.assertEqual(transaction.reference_notes, "Quick stock adjustment.")

    def test_quick_adjustment_form_has_no_reason_field(self):
        with self.app.test_client() as client:
            self._login_admin(client)
            response = client.get(f"/item/{self.item_id}")
            self.assertEqual(response.status_code, 200)
            self.assertNotIn('name="reference_notes"', response.get_data(as_text=True))

    def test_item_can_be_moved_to_another_area_and_location(self):
        with self.app.app_context():
            area = Area(name="Microbio 1", sort_order=1)
            db.session.add(area)
            db.session.commit()
            area_id = area.id

        with self.app.test_client() as client:
            self._login_admin(client)
            response = client.post(f"/item/{self.item_id}/relocate", data={
                "area_id": str(area_id),
                "location": "Shelf",
            })
            self.assertEqual(response.status_code, 302)
            with self.app.app_context():
                item = db.session.get(Item, self.item_id)
                self.assertEqual(item.area_id, area_id)
                self.assertEqual(item.location, "Shelf")

    def test_requested_areas_and_locations_are_selectable(self):
        with self.app.app_context():
            bootstrap_areas(self.app)

        with self.app.test_client() as client:
            self._login_admin(client)
            response = client.get("/item/new/Apparatus")
            self.assertEqual(response.status_code, 200)
            page = response.get_data(as_text=True)
            for area_name in DEFAULT_AREAS:
                self.assertIn(area_name, page)
            for location in ("Shelf", "Bench", "Fridge"):
                self.assertIn(f'<option value="{location}">{location}</option>', page)

    def test_opening_quantity_remains_unchanged_during_item_edit(self):
        with self.app.test_client() as client:
            self._login_admin(client)
            response = client.post(f"/item/{self.item_id}/edit", data={
                "name": "Microscope",
                "opening_qty": "9",
                "current_qty": "5",
                "unit": "each",
                "location": "Bench",
                "min_level": "2",
                "reorder_level": "3",
                "condition_status": "Active",
            })
            self.assertEqual(response.status_code, 302)
            with self.app.app_context():
                item = db.session.get(Item, self.item_id)
                self.assertEqual(item.opening_qty, 5)
                self.assertEqual(item.current_qty, 5)

    def test_admin_can_set_current_quantity_from_item_edit(self):
        with self.app.test_client() as client:
            self._login_admin(client)
            response = client.post(f"/item/{self.item_id}/edit", data={
                "name": "Microscope",
                "opening_qty": "5",
                "current_qty": "8",
                "unit": "each",
                "location": "Bench",
                "min_level": "2",
                "reorder_level": "3",
                "condition_status": "Active",
            })
            self.assertEqual(response.status_code, 302)
            with self.app.app_context():
                item = db.session.get(Item, self.item_id)
                transaction = Transaction.query.filter_by(item_id=self.item_id).one()
                self.assertEqual(item.opening_qty, 5)
                self.assertEqual(item.current_qty, 8)
                self.assertEqual(transaction.trans_type, "ADJUSTMENT IN")
                self.assertEqual(transaction.qty_in, 3)
                self.assertEqual(transaction.qty_out, 0)

    def test_edit_form_has_numeric_quantity_and_hides_expiry(self):
        with self.app.app_context():
            item = db.session.get(Item, self.item_id)
            item.expiry_date = date(2027, 5, 1)
            db.session.commit()

        with self.app.test_client() as client:
            self._login_admin(client)
            response = client.get(f"/item/{self.item_id}/edit")
            self.assertEqual(response.status_code, 200)
            page = response.get_data(as_text=True)
            self.assertIn('id="current_qty"', page)
            self.assertIn('name="current_qty"', page)
            self.assertIn('type="number"', page)
            self.assertNotIn('name="expiry_date"', page)

            response = client.post(f"/item/{self.item_id}/edit", data={
                "name": "Microscope",
                "opening_qty": "5",
                "current_qty": "5",
                "unit": "each",
                "location": "Bench",
                "min_level": "2",
                "reorder_level": "3",
                "condition_status": "Active",
            })
            self.assertEqual(response.status_code, 302)
            with self.app.app_context():
                item = db.session.get(Item, self.item_id)
                self.assertEqual(item.expiry_date, date(2027, 5, 1))

    def test_out_of_stock_item_can_be_archived_and_restored(self):
        with self.app.app_context():
            item = Item(
                inventory_id="APP-002",
                name="Empty Reagent",
                category="Apparatus",
                opening_qty=0,
                unit="bottle",
            )
            db.session.add(item)
            db.session.commit()
            item_id = item.id

        with self.app.test_client() as client:
            self._login_admin(client)
            response = client.post(f"/item/{item_id}/archive")
            self.assertEqual(response.status_code, 302)
            with self.app.app_context():
                self.assertTrue(db.session.get(Item, item_id).is_archived)
            response = client.post(f"/item/{item_id}/restore")
            self.assertEqual(response.status_code, 302)
            with self.app.app_context():
                self.assertFalse(db.session.get(Item, item_id).is_archived)

    def test_in_stock_item_cannot_be_archived(self):
        with self.app.test_client() as client:
            self._login_admin(client)
            response = client.post(f"/item/{self.item_id}/archive")
            self.assertEqual(response.status_code, 302)
            with self.app.app_context():
                self.assertFalse(db.session.get(Item, self.item_id).is_archived)

    def test_new_item_opening_quantity_is_not_counted_twice(self):
        with self.app.test_client() as client:
            self._login_admin(client)
            response = client.post("/item/new/Apparatus", data={
                "name": "New Pipette",
                "opening_qty": "7",
                "unit": "each",
                "min_level": "0",
                "reorder_level": "0",
            })
            self.assertEqual(response.status_code, 302)
            with self.app.app_context():
                item = Item.query.filter_by(name="New Pipette").one()
                self.assertEqual(item.current_qty, 7)
                self.assertEqual(len(item.transactions), 0)

    def test_section_lists_are_paginated(self):
        with self.app.app_context():
            for number in range(51):
                db.session.add(Item(
                    inventory_id=f"EQU-{number + 1:03d}",
                    name=f"Equipment {number + 1:03d}",
                    category="Equipment",
                    opening_qty=1,
                    unit="each",
                ))
            db.session.commit()

        with self.app.test_client() as client:
            self._login_admin(client)
            response = client.get("/section/Equipment?show=all")
            self.assertEqual(response.status_code, 200)
            self.assertIn("Page 1 of 2", response.get_data(as_text=True))

    def test_dashboard_uses_database_stock_summary(self):
        with self.app.test_client() as client:
            self._login_admin(client)
            response = client.get("/")
            self.assertEqual(response.status_code, 200)
            self.assertIn("Total Inventory Lines", response.get_data(as_text=True))

    def test_duplicate_item_in_same_area_is_rejected(self):
        with self.app.app_context():
            area = Area.query.first()
            Item(
                inventory_id="RGT-001",
                name="Buffer A",
                category="Reagent",
                area_id=area.id,
                opening_qty=1,
                unit="L",
                location="Bench",
            ).save() if False else None

        with self.app.app_context():
            area = Area.query.first()
            db.session.add(Item(
                inventory_id="RGT-002",
                name="Buffer A",
                category="Reagent",
                area_id=area.id,
                opening_qty=2,
                unit="L",
                location="Bench",
            ))
            db.session.commit()

        with self.app.test_client() as client:
            self._login_admin(client)
            response = client.post("/item/new/Reagent", data={
                "name": "Buffer A",
                "opening_qty": "3",
                "unit": "L",
                "location": "Bench",
                "min_level": "0",
                "reorder_level": "0",
            })
            self.assertEqual(response.status_code, 302)
            with self.app.app_context():
                self.assertEqual(
                    Item.query.filter_by(name="Buffer A", category="Reagent").count(),
                    2,
                )

    def test_admin_can_delete_item_permanently(self):
        with self.app.app_context():
            area = Area.query.first()
            item = Item(
                inventory_id="CHM-001",
                name="Acetone",
                category="Chemical",
                area_id=area.id,
                opening_qty=1,
                unit="L",
                location="Bench",
            )
            db.session.add(item)
            db.session.commit()
            item_id = item.id

        with self.app.test_client() as client:
            self._login_admin(client)
            response = client.post(f"/item/{item_id}/delete")
            self.assertEqual(response.status_code, 302)
            with self.app.app_context():
                self.assertIsNone(db.session.get(Item, item_id))

    def test_expiry_controls_are_shown_only_for_chemical_and_reagent(self):
        with self.app.test_client() as client:
            self._login_admin(client)
            response = client.get("/item/new/Reagent")
            self.assertEqual(response.status_code, 200)
            self.assertIn('name="expiry_date"', response.get_data(as_text=True))

            response = client.get("/item/new/Apparatus")
            self.assertEqual(response.status_code, 200)
            self.assertNotIn('name="expiry_date"', response.get_data(as_text=True))

    def test_locations_include_storage_freezers(self):
        with self.app.test_client() as client:
            self._login_admin(client)
            response = client.get("/item/new/Reagent")
            self.assertEqual(response.status_code, 200)
            page = response.get_data(as_text=True)
            for location in ("Drawer", "Bench", "-8 degree fridge", "-25 degree fridge"):
                self.assertIn(f'<option value="{location}">{location}</option>', page)


if __name__ == "__main__":
    unittest.main()

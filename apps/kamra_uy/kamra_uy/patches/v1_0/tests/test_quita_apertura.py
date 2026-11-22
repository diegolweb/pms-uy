"""HR-275 (D-HR-49): el patch que quita de la base la fecha de apertura y el cierre de ventas de HR-263 y HR-264.

Monta lo que dejó en una base el código viejo (el Custom Field `uy_opening_date` de la Property, con su columna, y una
Channel Restriction `UY-APERTURA-<propiedad>`), corre el patch dos veces y mira que no quede nada y que lo puesto a mano
siga ahí.

Con COMMITS de verdad, no en el savepoint de AuditTestCase: el patch y el montaje cambian el esquema (ADD y DROP
COLUMN) y en MariaDB todo DDL hace commit. Con el commit vuelto un no-op, Frappe corta el primer DDL que llega con
escrituras pendientes (ImplicitCommitError) y el savepoint no podría deshacerlo igual. Así corre el patch como en un
migrate, y cada test deja la base como la encontró (tearDown, con su propio commit).
"""

from __future__ import annotations

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, nowdate
from kamra.tests.audit_fixtures import make_property

from kamra_uy.patches.v1_0 import quita_apertura as patch

PROP = "UT Quita Apertura"
DT = patch.DOCTYPE


def _day(offset: int) -> str:
	return str(add_days(nowdate(), offset))


def _restriction(name: str, **rules) -> str:
	frappe.get_doc({"doctype": DT, "property": PROP, "date_from": _day(0), "date_to": _day(5), "stop_sell": 1,
	                **rules}).insert(ignore_permissions=True, set_name=name)
	return name


def _clean(rollback: bool = True) -> None:
	"""Lo que monta el test, fuera de la base (y confirmado): las restricciones y los ajustes de la propiedad, la
	propiedad, el campo viejo y su columna. Lo que ya no está, no se toca. Al acabar, primero deshace lo que el test
	dejara a medias."""
	if rollback:
		frappe.db.rollback()
	frappe.db.delete(DT, {"property": PROP})
	frappe.db.delete("Property Setting", {"property": PROP})
	if frappe.db.exists("Property", PROP):
		frappe.delete_doc("Property", PROP, force=True, ignore_permissions=True)
	if frappe.db.exists("Custom Field", patch.CUSTOM_FIELD):
		frappe.delete_doc("Custom Field", patch.CUSTOM_FIELD, force=True, ignore_permissions=True)
	if frappe.db.has_column("Property", patch.FIELD):
		frappe.db.sql_ddl(f"ALTER TABLE `tabProperty` DROP COLUMN `{patch.FIELD}`")
	frappe.db.commit()  # nosemgrep: frappe-manual-commit -- el test commitea (DDL); deja la base como la encontró
	frappe.clear_cache(doctype="Property")
	frappe.clear_document_cache("Property", PROP)


class TestQuitaApertura(IntegrationTestCase):
	def setUp(self):
		super().setUp()
		user = frappe.session.user
		self.addCleanup(frappe.set_user, user)
		frappe.set_user("Administrator")  # nosemgrep: frappe-setuser -- test fixture
		_clean(rollback=False)  # por si una corrida anterior se cortó antes de su tearDown
		self.addCleanup(_clean)
		# lo que creaba HR-263 (ensure_fields) y HR-264 (stop_sell.sync), tal cual
		from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

		create_custom_fields({"Property": [{"fieldname": patch.FIELD, "label": "Fecha de apertura del hotel",
		                                    "fieldtype": "Date", "insert_after": "booking_engine_enabled",
		                                    "module": "Kamra UY"}]}, update=True)
		make_property(PROP, country="Uruguay", currency="USD")
		frappe.db.set_value("Property", PROP, patch.FIELD, _day(30))
		self.ours = _restriction(f"{patch.PREFIX}{PROP}")
		self.manual = _restriction(f"UT manual {frappe.generate_hash(length=6)}", closed_to_arrival=1, stop_sell=0)
		frappe.db.commit()  # nosemgrep: frappe-manual-commit -- la base vieja, confirmada, como la encuentra el migrate

	def test_borra_el_campo_su_columna_y_el_cierre_de_kamra_uy(self):
		self.assertTrue(frappe.db.has_column("Property", patch.FIELD))
		self.assertEqual(str(frappe.db.get_value("Property", PROP, patch.FIELD)), _day(30))
		patch.execute()
		self.assertFalse(frappe.db.exists("Custom Field", patch.CUSTOM_FIELD))
		self.assertFalse(frappe.db.has_column("Property", patch.FIELD))
		self.assertFalse(frappe.get_meta("Property").has_field(patch.FIELD))
		self.assertFalse(frappe.db.exists(DT, self.ours))
		self.assertEqual(patch.restrictions(), [])
		# la propiedad se sigue guardando sin el campo (la meta no lo arrastra)
		make_property(PROP, country="Uruguay", currency="USD")

	def test_lo_puesto_a_mano_se_queda(self):
		patch.execute()
		self.assertTrue(frappe.db.exists(DT, self.manual))
		self.assertEqual(frappe.db.get_value(DT, self.manual, "closed_to_arrival"), 1)

	def test_es_idempotente(self):
		patch.execute()
		patch.execute()
		self.assertFalse(frappe.db.exists("Custom Field", patch.CUSTOM_FIELD))
		self.assertFalse(frappe.db.has_column("Property", patch.FIELD))
		self.assertEqual(patch.restrictions(), [])
		self.assertTrue(frappe.db.exists(DT, self.manual))

"""La carga de la planilla en Kamra (HR-33), con frappe y una planilla INVENTADA (`fabrica`): la prueba no escribe;
dos cargas dejan los mismos registros; un precio cambiado actualiza sin duplicar; y tras la carga la reserva web
cobra el TOTAL de la estancia (Full online, ninguna temporada con advance_percent: ADDENDUM de KICKOFF-HR-220). Con
las reservas ya tomadas: cada una entra una vez, con su ota_ref, el recuento cuadra con la planilla, una segunda carga
no crea nada, y una revisión posterior de Channex la modifica y no la duplica.

Lo corre `bench run-tests --app kamra_uy` (o `--module kamra_uy.setup.tests.test_carga`).
"""

import contextlib
import io
import os
import tempfile

import frappe
from kamra import public_api
from kamra.tests.audit_fixtures import AuditTestCase, make_property

from kamra_uy.setup import carga
from kamra_uy.setup.tests import fabrica

PROP = "UT UY Carga HR33"


class CargaCase(AuditTestCase):
	properties = (PROP,)

	def setUp(self):
		super().setUp()
		make_property(PROP, country="India", currency="INR", booking_payment_mode="Pay at hotel", deposit_pct=0)
		self._dir = tempfile.TemporaryDirectory()
		self.ruta = os.path.join(self._dir.name, "planilla.xlsx")
		fabrica.planilla_inventada(self.ruta)

	def tearDown(self):
		self._dir.cleanup()
		super().tearDown()

	# --- ayudas

	def corre(self, aplicar: int, ruta: str | None = None, reservas: int = 0) -> tuple[dict, str]:
		salida = io.StringIO()
		with contextlib.redirect_stdout(salida):
			out = carga.carga(planilla=ruta or self.ruta, propiedad=PROP, aplicar=aplicar, reservas=reservas)
		return out, salida.getvalue()

	def con_reservas(self) -> str:
		ruta = os.path.join(self._dir.name, "con-reservas.xlsx")
		fabrica.planilla_inventada(ruta, con_reservas=True)
		return ruta

	def reservas(self) -> dict:
		filas = frappe.get_all(
			"Reservation", filters={"property": PROP}, order_by="name",
			fields=["name", "ota_ref", "idempotency_key", "channel", "source", "status", "room", "check_in_date",
			        "check_out_date", "adults", "children", "amount_after_tax", "auto_price", "guest"],
		)
		return {r.ota_ref or r.idempotency_key: r for r in filas}

	def registros(self) -> dict:
		return {
			dt: sorted(frappe.get_all(dt, filters={"property": PROP}, pluck="name"))
			for dt in ("Room Type", "Room", "Season")
		}

	# --- pruebas

	def test_the_dry_run_writes_nothing_and_says_what_it_would_create(self):
		out, texto = self.corre(aplicar=0)
		self.assertEqual(self.registros(), {"Room Type": [], "Room": [], "Season": []})
		self.assertEqual(frappe.db.get_value("Property", PROP, "booking_payment_mode"), "Pay at hotel")
		self.assertEqual(out["aplicada"], 0)
		self.assertEqual(out["errores"], 0)
		self.assertIn("PRUEBA: no se ha escrito nada", texto)
		self.assertIn("RESULTADO: lista para cargar (crearía 18, cambiaría 1, igual 0, retiraría 0).", texto)

	def test_the_load_creates_rooms_types_and_seasons_and_sets_full_payment(self):
		out, texto = self.corre(aplicar=1)
		self.assertEqual(out["aplicada"], 1, texto)
		regs = self.registros()
		self.assertEqual([len(regs[dt]) for dt in ("Room Type", "Room", "Season")], [3, 6, 9])
		prop = frappe.db.get_value(
			"Property", PROP, ["country", "currency", "timezone", "booking_payment_mode", "deposit_pct"], as_dict=True
		)
		self.assertEqual(
			dict(prop),
			{"country": "Uruguay", "currency": "USD", "timezone": "America/Montevideo",
			 "booking_payment_mode": "Full online", "deposit_pct": 100},
		)
		# HR-290: la pestaña «Seña y cancelación» inventada de la fábrica
		cond = frappe.db.get_value(
			"Property", PROP, ["hold_minutes", "checkin_time", "checkout_time", "free_cancel_days", "cancellation_fee",
			                   "no_show_charge", "pets_allowed"], as_dict=True,
		)
		self.assertEqual(
			(cond.hold_minutes, str(cond.checkin_time), str(cond.checkout_time), cond.free_cancel_days,
			 cond.cancellation_fee, cond.no_show_charge, cond.pets_allowed),
			(45, "15:30:00", "10:15:00", 9, "Full Stay", "First Night", 0),
		)
		# la capacidad en personas: niños hasta la capacidad menos 1 y el total hasta la capacidad
		cap = frappe.db.get_value(
			"Room Type", {"property": PROP, "room_type_name": "Triple"},
			["adults_capacity", "children_capacity", "max_total_occupants", "children_within_base_occupancy"], as_dict=True,
		)
		self.assertEqual(tuple(cap.values()), (3, 2, 3, 1))
		triple = frappe.get_all(
			"Room Type", filters={"property": PROP, "room_type_name": "Triple"},
			fields=["name", "base_price", "adults_capacity", "base_occupancy"],
		)[0]
		self.assertEqual((triple.base_price, triple.adults_capacity, triple.base_occupancy), (702, 3, 3))
		self.assertEqual(frappe.db.count("Room", {"property": PROP, "room_type": triple.name}), 3)
		seasons = frappe.get_all(
			"Season", filters={"property": PROP}, fields=["advance_percent", "adjustment_type", "room_type"]
		)
		self.assertTrue(all(not s.advance_percent for s in seasons))
		self.assertEqual({s.adjustment_type for s in seasons}, {"Absolute"})
		self.assertTrue(all(s.room_type for s in seasons))

	def test_two_loads_leave_the_same_records_and_a_changed_price_updates_without_duplicating(self):
		self.corre(aplicar=1)
		primera = self.registros()
		out, texto = self.corre(aplicar=1)
		self.assertEqual(self.registros(), primera)
		self.assertIn("RESULTADO: cargada (creados 0, cambiados 0, igual 19, retirados 0).", texto)
		otra = os.path.join(self._dir.name, "otra.xlsx")
		fabrica.planilla_inventada(otra, precio_doble=1111)
		out, texto = self.corre(aplicar=1, ruta=otra)
		self.assertEqual(self.registros(), primera)
		self.assertIn("RESULTADO: cargada (creados 0, cambiados 1, igual 18, retirados 0).", texto)
		doble = frappe.db.get_value("Room Type", {"property": PROP, "room_type_name": "Doble"})
		self.assertEqual(
			frappe.db.get_value("Season", {"property": PROP, "room_type": doble, "season_name": "Alta inventada"},
			                    "adjustment_value"),
			1111,
		)

	def test_after_the_load_the_web_booking_charges_the_whole_stay(self):
		self.corre(aplicar=1)
		frappe.clear_document_cache("Property", PROP)
		# dos noches de «Alta inventada» (1001) y una de «Media inventada» (801), para una Doble
		filas = public_api.search_stay(PROP, "2030-01-09", "2030-01-12", adults=2)
		doble = frappe.db.get_value("Room Type", {"property": PROP, "room_type_name": "Doble"})
		(fila,) = [f for f in filas if f["room_type"] == doble]
		q = fila["quote"]
		self.assertEqual([n["rate"] for n in q["nightly"]], [1001, 1001, 801])
		self.assertEqual(q["room_total"], 2803)
		self.assertGreater(q["amount_after_tax"], 0)
		self.assertEqual(q["advance_due"], q["amount_after_tax"])
		self.assertEqual(q["balance_at_hotel"], 0)
		self.assertEqual(q["payment_policy"], "Full amount paid online")

	def test_after_the_load_the_hotel_asks_no_security_deposit_and_a_hand_set_one_stays(self):
		# HR-232 (D-HR-64): con el de fábrica de Kamra (5000), la carga lo pone a 0 y el presupuesto de la web no lo pide;
		# el patch hace lo mismo con una propiedad ya cargada, y ni el patch ni la carga pisan uno puesto a mano.
		from kamra_uy.patches.v1_0 import garantia_a_cero

		frappe.db.set_value("Property", PROP, "security_deposit_amount", 5000)
		self.corre(aplicar=1)
		frappe.clear_document_cache("Property", PROP)
		self.assertEqual(frappe.db.get_value("Property", PROP, "security_deposit_amount"), 0)
		for fila in public_api.search_stay(PROP, "2030-01-09", "2030-01-12", adults=2):
			self.assertEqual(fila["quote"]["totals"]["deposit_required"], 0)

		frappe.db.set_value("Property", PROP, "security_deposit_amount", 5000)
		garantia_a_cero.execute()
		self.assertEqual(frappe.db.get_value("Property", PROP, "security_deposit_amount"), 0)

		frappe.db.set_value("Property", PROP, "security_deposit_amount", 2500)
		garantia_a_cero.execute()
		_, texto = self.corre(aplicar=1)
		self.assertEqual(frappe.db.get_value("Property", PROP, "security_deposit_amount"), 2500)
		self.assertIn("depósito de garantía de 2500", texto)

	def test_after_the_load_a_triple_takes_three_people_in_any_mix_with_one_adult_and_no_more(self):
		# HR-290 (el fundador, 22-11): niños hasta la capacidad menos 1 y el total hasta la capacidad, al mismo precio
		self.corre(aplicar=1)
		triple = frappe.db.get_value("Room Type", {"property": PROP, "room_type_name": "Triple"})

		def fila(adultos, ninos):
			filas = public_api.search_stay(PROP, "2030-01-09", "2030-01-12", adults=adultos, children=ninos)
			(f,) = [f for f in filas if f["room_type"] == triple]
			return f

		for adultos, ninos in ((3, 0), (2, 1), (1, 2)):
			f = fila(adultos, ninos)
			self.assertTrue(f["available"], (adultos, ninos, f.get("reason")))
			self.assertEqual(f["quote"]["room_total"], fila(3, 0)["quote"]["room_total"], (adultos, ninos))
		for adultos, ninos in ((3, 1), (2, 2), (1, 3), (4, 0)):
			self.assertFalse(fila(adultos, ninos)["available"], (adultos, ninos))

	def test_a_planilla_with_errors_writes_nothing(self):
		tem = fabrica.temporadas()
		tem[1][4] = "caro"
		mala = os.path.join(self._dir.name, "mala.xlsx")
		fabrica.escribe_xlsx(mala, fabrica.pestanas(temporadas_=tem))
		out, texto = self.corre(aplicar=1, ruta=mala)
		self.assertEqual((out["aplicada"], out["errores"]), (0, 1))
		self.assertEqual(self.registros(), {"Room Type": [], "Room": [], "Season": []})
		self.assertIn("fila 5, columna E (Precio por noche (US$)): «caro» no es un precio.", texto)

	def test_an_unknown_property_is_a_clear_error(self):
		out = carga.carga(planilla=self.ruta, propiedad="UT UY Carga que no existe", aplicar=1)
		self.assertEqual((out["aplicada"], out["errores"]), (0, 1))

	# --- las reservas ya tomadas

	def test_reservations_load_once_with_their_ota_ref_and_the_count_squares_with_the_planilla(self):
		ruta = self.con_reservas()
		out, texto = self.corre(aplicar=1, ruta=ruta, reservas=1)
		self.assertEqual((out["aplicada"], out["errores"]), (1, 0), texto)
		res = self.reservas()
		self.assertEqual(len(res), out["planilla"]["reservas"])
		self.assertEqual(out["recuento"]["Reservation"], {"crear": 6})
		self.assertEqual(
			set(res), {"4100000001", "4100000002", "EXP-INV-77-0", "EXP-INV-77-1", "HMINVENT04", "planilla:T-0005"}
		)
		booking = res["4100000001"]
		self.assertEqual(
			(booking.channel, booking.source, booking.status, booking.auto_price, booking.amount_after_tax, booking.room),
			("Booking.com", "OTA", "Confirmed", 0, 2400, None),
		)
		self.assertEqual(frappe.db.get_value("Guest", booking.guest, "email"), "uno@example.test")
		directa = res["planilla:T-0005"]
		self.assertEqual(
			(directa.channel, directa.source, directa.ota_ref, directa.amount_after_tax,
			 frappe.db.get_value("Room", directa.room, "room_number")),
			(None, "Phone", None, 1000, "802"),
		)
		airbnb = res["HMINVENT04"]  # sin total: la precia Kamra con las temporadas
		self.assertEqual((airbnb.auto_price, frappe.db.get_value("Room", airbnb.room, "room_number")), (1, "806"))
		self.assertGreater(airbnb.amount_after_tax, 0)
		self.assertNotIn("Huésped Inventado", texto)

		nombres = {k: r.name for k, r in res.items()}
		out, texto = self.corre(aplicar=1, ruta=ruta, reservas=1)
		self.assertIn("RESULTADO: cargada (creados 0, cambiados 0, igual 25, retirados 0).", texto)
		self.assertEqual({k: r.name for k, r in self.reservas().items()}, nombres)

	def test_without_asking_for_them_no_reservation_is_loaded(self):
		out, _ = self.corre(aplicar=1, ruta=self.con_reservas())
		self.assertEqual(out["aplicada"], 1)
		self.assertEqual(self.reservas(), {})
		self.assertNotIn("reservas", out["planilla"])

	def test_a_later_channex_revision_modifies_the_loaded_booking_and_does_not_duplicate_it(self):
		from kamra import channel_manager as cm

		ruta = self.con_reservas()
		self.corre(aplicar=1, ruta=ruta, reservas=1)
		doble = frappe.db.get_value("Room Type", {"property": PROP, "room_type_name": "Doble"})
		conn = frappe.get_doc({"doctype": "Channel Manager Connection", "property": PROP, "provider": "AioSell",
		                       "active": 1, "external_property_id": "hr33-inventado"}).insert(ignore_permissions=True)
		frappe.get_doc({"doctype": "Channel Room Mapping", "connection": conn.name, "room_type": doble,
		                "external_room_id": "dbl"}).insert(ignore_permissions=True)
		cargada = self.reservas()["4100000001"].name
		cuantas = frappe.db.count("Reservation", {"property": PROP})
		revision = {"event": "modify", "booking_id": "4100000001", "ota_ref": "4100000001", "channel": "BookingCom",
		            "room_type_external_id": "dbl", "check_in": "2030-01-04", "check_out": "2030-01-07", "adults": 2,
		            "children": 0, "total": 2500}
		out = cm._apply_event(conn, dict(revision))
		self.assertEqual((out["result"], out["reservation"]), ("modified", cargada))
		out = cm._apply_event(conn, {**revision, "event": "book"})
		self.assertEqual((out["result"], out["reservation"]), ("duplicate_ignored", cargada))
		self.assertEqual(frappe.db.count("Reservation", {"property": PROP}), cuantas)
		movida = self.reservas()["4100000001"]
		self.assertEqual((movida.name, str(movida.check_in_date), movida.amount_after_tax), (cargada, "2030-01-04", 2500))

		# y una carga más con la planilla vieja no la devuelve a sus fechas: la avisa y la deja como la dejó Channex
		out, texto = self.corre(aplicar=1, ruta=ruta, reservas=1)
		self.assertIn("creados 0, cambiados 0", texto)
		self.assertIn(f"ya está en Kamra ({cargada}) con otros datos: la llegada y la salida", texto)
		self.assertEqual(str(self.reservas()["4100000001"].check_in_date), "2030-01-04")

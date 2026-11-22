"""Las plataformas tampoco venden antes de la apertura (HR-264): la Channel Restriction de kamra_uy y su salida a Channex.

Con `uy_opening_date` futura, la propiedad tiene UNA Channel Restriction stop sell de kamra_uy (todos los tipos y
planes) de hoy a la víspera de la apertura: la crea el guardado de la Property, la ajusta un cambio de fecha, la retira
vaciarla, y deja de existir cuando llega la apertura (el trabajo de cada hora). Una restricción puesta a mano no se
toca; la de kamra_uy no se edita ni se borra a mano. Y el outbox de ARI de Kamra la manda a Channex (aquí, el Channex
falso de kamra.tests.test_channex_outbox, sin HTTP): stop_sell de hoy a la víspera en los cuatro planes; vaciar la
fecha los reabre.

Las fechas son relativas al día del sitio (nowdate): el cierre empieza hoy. Lo corre `bench run-tests --app kamra_uy`
(o `--module kamra_uy.booking.tests.test_stop_sell`).
"""

from datetime import datetime, time
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, getdate, nowdate
from kamra.tests.audit_fixtures import AuditTestCase, make_property, make_room_type
from kamra.tests.test_channex_outbox import _AriWorkerMixin

from kamra_uy.booking import opening, stop_sell
from kamra_uy.pure import opening as regla

PROP = "UT Cierre Apertura HR264"
PROP_2 = "UT Cierre Apertura HR264 Dos"
PROP_NEW = "UT Cierre Apertura HR264 Nueva"
DT = "Channel Restriction"
PLANS = ("rp-double-bar", "rp-double-bb", "rp-twin-bar", "rp-twin-bb")


def day(n: int) -> str:
	return str(add_days(nowdate(), n))


def opens(property: str, offset: int | None) -> None:
	"""Guarda la fecha de apertura como el escritorio: con el documento (sus hooks), no con db.set_value."""
	doc = frappe.get_doc("Property", property)
	doc.set(opening.FIELD, day(offset) if offset is not None else None)
	doc.save(ignore_permissions=True)


def own(property: str) -> list[frappe._dict]:
	return frappe.get_all(DT, filters={"property": property, "name": ("like", regla.PREFIJO_CIERRE + "%")},
	                      fields=["*"], order_by="creation asc")


class TestStopSell(AuditTestCase):
	properties = (PROP, PROP_2, PROP_NEW)

	def setUp(self):
		super().setUp()
		for p in (PROP, PROP_2):
			make_property(p, country="Uruguay", currency="USD", **{opening.FIELD: None})
		self.rt = make_room_type(PROP, "CIE", base_price=100, adults_capacity=2)

	def manual(self, start: int, end: int, **rules) -> str:
		"""Una restricción de la pantalla (kamra.channel_restrictions, la API de HR-17)."""
		from kamra.channel_restrictions import save_restrictions

		row = {"room_type": "", "rate_plan": "", "date_from": day(start), "date_to": day(end), **rules}
		return save_restrictions(PROP, frappe.as_json([row]))["saved"][0]

	def at(self, today: str):
		"""El reloj de los dos lados (sitio y hotel) en `today`."""
		self._stack.enter_context(patch("kamra_uy.booking.stop_sell.nowdate", return_value=today))
		self._stack.enter_context(patch("kamra.no_show.property_now",
		                                return_value=datetime.combine(getdate(today), time(12))))

	def assertClosed(self, property: str, start: int, end: int):
		rows = own(property)
		self.assertEqual(len(rows), 1, rows)
		r = rows[0]
		self.assertEqual(r.name, stop_sell.restriction_name(property))
		self.assertEqual((str(r.date_from), str(r.date_to)), (day(start), day(end)))
		self.assertEqual((r.room_type, r.rate_plan, r.days_of_week), (None, None, None))
		self.assertEqual((r.stop_sell, r.closed_to_arrival, r.closed_to_departure, r.min_stay_arrival,
		                  r.min_stay_through, r.max_stay, r.disabled), (1, 0, 0, 0, 0, 0, 0))
		self.assertEqual(r.note, regla.NOTA_CIERRE)
		return r

	# --- crea, ajusta, retira ---

	def test_sin_fecha_no_hay_cierre(self):
		self.assertEqual(own(PROP), [])
		opens(PROP, None)
		self.assertEqual(own(PROP), [])

	def test_con_la_apertura_futura_crea_una_de_hoy_a_la_vispera(self):
		opens(PROP, 6)
		self.assertClosed(PROP, 0, 5)

	def test_guardar_otra_vez_no_la_duplica_ni_la_reescribe(self):
		opens(PROP, 6)
		before = self.assertClosed(PROP, 0, 5)
		opens(PROP, 6)
		frappe.get_doc("Property", PROP).save(ignore_permissions=True)
		after = self.assertClosed(PROP, 0, 5)
		self.assertEqual(after.modified, before.modified)

	def test_al_crear_la_propiedad_con_la_fecha(self):
		make_property(PROP_NEW, country="Uruguay", currency="USD", **{opening.FIELD: day(3)})
		self.assertClosed(PROP_NEW, 0, 2)

	def test_cambiar_la_fecha_la_ajusta(self):
		opens(PROP, 6)
		name = self.assertClosed(PROP, 0, 5).name
		opens(PROP, 3)
		self.assertEqual(self.assertClosed(PROP, 0, 2).name, name)
		opens(PROP, 10)
		self.assertEqual(self.assertClosed(PROP, 0, 9).name, name)
		opens(PROP, 1)
		self.assertClosed(PROP, 0, 0)

	def test_vaciar_la_fecha_la_retira(self):
		opens(PROP, 6)
		opens(PROP, None)
		self.assertEqual(own(PROP), [])
		self.assertFalse(frappe.db.exists(DT, stop_sell.restriction_name(PROP)))

	def test_la_apertura_hoy_o_pasada_no_cierra_nada(self):
		opens(PROP, 6)
		opens(PROP, 0)
		self.assertEqual(own(PROP), [])
		opens(PROP, -3)
		self.assertEqual(own(PROP), [])

	# --- el paso del tiempo (el trabajo de cada hora) ---

	def test_al_dia_siguiente_la_primera_noche_es_la_de_hoy(self):
		opens(PROP, 6)
		self.at(day(1))
		stop_sell.sync_all()
		self.assertClosed(PROP, 1, 5)

	def test_cuando_llega_la_apertura_deja_de_existir(self):
		opens(PROP, 6)
		self.at(day(6))
		stop_sell.sync_all()
		self.assertEqual(own(PROP), [])

	def test_la_fecha_puesta_sin_hooks_la_recoge_el_trabajo_de_cada_hora(self):
		frappe.db.set_value("Property", PROP, opening.FIELD, day(4))
		self.assertEqual(own(PROP), [])
		stop_sell.sync_all()
		self.assertClosed(PROP, 0, 3)

	def test_hoy_es_el_mas_temprano_del_sitio_y_del_hotel(self):
		with patch("kamra.no_show.property_now", return_value=datetime.combine(getdate(day(1)), time(1))):
			opens(PROP, 6)
		self.assertClosed(PROP, 0, 5)  # el hotel ya está mañana: el sitio todavía vende hoy
		with patch("kamra.no_show.property_now", return_value=datetime.combine(getdate(day(-1)), time(23))):
			stop_sell.sync(PROP)
		self.assertClosed(PROP, -1, 5)  # el hotel todavía está ayer: esa noche también

	def test_una_propiedad_que_falla_no_frena_a_las_demas(self):
		opens(PROP, 6)
		frappe.db.set_value("Property", PROP_2, opening.FIELD, day(4))
		errors = frappe.db.count("Error Log", {"method": stop_sell.ERROR_TITLE})
		real = stop_sell.sync

		def sync(property, *args, **kwargs):
			if property == PROP:
				raise RuntimeError("UT falla a propósito")
			return real(property, *args, **kwargs)

		with patch("kamra_uy.booking.stop_sell.sync", side_effect=sync):
			stop_sell.sync_all()
		self.assertClosed(PROP_2, 0, 3)
		self.assertClosed(PROP, 0, 5)  # la suya sigue como estaba
		self.assertEqual(frappe.db.count("Error Log", {"method": stop_sell.ERROR_TITLE}), errors + 1)

	# --- solo la suya ---

	def test_una_restriccion_puesta_a_mano_no_se_toca(self):
		manual = self.manual(2, 8, stop_sell=1, note="UT a mano")
		other = self.manual(20, 22, min_stay_arrival=3)
		before = {n: frappe.get_doc(DT, n).as_dict() for n in (manual, other)}
		opens(PROP, 6)
		opens(PROP, 4)
		opens(PROP, None)
		stop_sell.sync_all()
		for n in (manual, other):
			after = frappe.get_doc(DT, n).as_dict()
			self.assertEqual(after.modified, before[n].modified, n)
			self.assertEqual({k: after[k] for k in ("date_from", "date_to", "stop_sell", "min_stay_arrival", "note")},
			                 {k: before[n][k] for k in ("date_from", "date_to", "stop_sell", "min_stay_arrival",
			                                            "note")})
		self.assertFalse(regla.es_cierre(manual))
		self.assertEqual(own(PROP), [])

	def test_la_de_kamra_uy_no_se_edita_ni_se_borra_a_mano(self):
		from kamra.channel_restrictions import delete_restrictions, save_restrictions

		opens(PROP, 6)
		name = stop_sell.restriction_name(PROP)
		for change in ({"date_to": day(2)}, {"disabled": 1}, {"stop_sell": 0, "min_stay_arrival": 2}):
			with self.assertRaises(stop_sell.ManagedRestrictionError) as caught:
				save_restrictions(PROP, frappe.as_json([{"name": name, **change}]))
			self.assertIn(regla.CIERRE_BLOQUEADO, str(caught.exception))
		with self.assertRaises(stop_sell.ManagedRestrictionError):
			delete_restrictions(PROP, frappe.as_json([name]))
		with self.assertRaises(stop_sell.ManagedRestrictionError):
			frappe.delete_doc(DT, name)
		self.assertClosed(PROP, 0, 5)

	def test_nadie_crea_a_mano_una_con_el_nombre_de_kamra_uy(self):
		with self.assertRaises(stop_sell.ManagedRestrictionError):
			frappe.get_doc({"doctype": DT, "property": PROP, "date_from": day(1), "date_to": day(2),
			                "stop_sell": 1}).insert(set_name=stop_sell.restriction_name(PROP))

	def test_una_fila_vieja_con_el_prefijo_se_retira_y_queda_una(self):
		with stop_sell._own():
			frappe.get_doc({"doctype": DT, "property": PROP, "date_from": day(0), "date_to": day(9),
			                "stop_sell": 1}).insert(set_name=regla.PREFIJO_CIERRE + "UTVIEJA")
		opens(PROP, 6)
		self.assertClosed(PROP, 0, 5)
		self.assertFalse(frappe.db.exists(DT, regla.PREFIJO_CIERRE + "UTVIEJA"))

	# --- renombrar y borrar la propiedad ---

	def test_renombrar_la_propiedad_deja_la_suya(self):
		opens(PROP, 6)
		old = stop_sell.restriction_name(PROP)
		frappe.rename_doc("Property", PROP, PROP_NEW, force=True)
		self.assertClosed(PROP_NEW, 0, 5)
		self.assertFalse(frappe.db.exists(DT, old))

	def test_borrar_la_propiedad_retira_la_suya(self):
		opens(PROP, 6)
		stop_sell.on_property_trash(frappe.get_doc("Property", PROP))
		self.assertEqual(own(PROP), [])

	# --- el cableado de Frappe ---

	def test_los_hooks_llegan_a_frappe(self):
		events = frappe.get_hooks("doc_events")
		self.assertIn("kamra_uy.booking.stop_sell.on_property_update", events["Property"]["on_update"])
		self.assertIn("kamra_uy.booking.stop_sell.on_property_rename", events["Property"]["after_rename"])
		self.assertIn("kamra_uy.booking.stop_sell.on_property_trash", events["Property"]["on_trash"])
		self.assertIn("kamra_uy.booking.stop_sell.protect", events[DT]["validate"])
		self.assertIn("kamra_uy.booking.stop_sell.protect", events[DT]["on_trash"])
		# y las de Kamra siguen: el outbox de ARI se entera de la fila de kamra_uy
		self.assertIn("kamra.channel_ari.on_ari_source_change", events[DT]["after_insert"])
		self.assertIn("kamra.channel_ari.on_ari_source_change", events[DT]["on_update"])
		self.assertIn("kamra.channel_ari.on_ari_source_change", events[DT]["on_trash"])
		cron = frappe.get_hooks("scheduler_events")["cron"]
		self.assertIn("kamra_uy.booking.stop_sell.sync_all", cron["5 * * * *"])

	def test_la_web_ya_no_vende_esas_noches_tampoco_por_kamra(self):
		"""El cierre es una regla de estancia como las de la pantalla: Kamra la aplica igual en sus motores."""
		from kamra.pricing import stay_rules_by_day

		opens(PROP, 6)
		rules = stay_rules_by_day(PROP, self.rt, None, day(0), day(7))
		self.assertEqual([rules[day(i)]["stop_sell"] for i in range(8)], [True] * 6 + [False] * 2)


class TestStopSellChannex(_AriWorkerMixin, IntegrationTestCase):
	"""La fila de kamra_uy sale a Channex por el outbox de ARI de Kamra (la costura de HR-16/HR-17), sin HTTP."""

	def setUp(self):
		super().setUp()
		# la propiedad del fixture nace en Asia/Kolkata (el valor por defecto de Kamra): aquí, el reloj del sitio
		frappe.db.set_value("Property", self.property, "timezone", "")
		frappe.clear_document_cache("Property", self.property)

	def restrictions(self) -> list[list[dict]]:
		return [sorted(values, key=lambda v: v["rate_plan_id"]) for values in self.fake.posts("restrictions")]

	@staticmethod
	def closed(start: int, end: int, value: bool) -> list[dict]:
		return [{"property_id": "cx-test", "rate_plan_id": p, "date_from": day(start), "date_to": day(end),
		         "stop_sell": value} for p in PLANS]

	def test_la_apertura_cierra_las_plataformas_de_hoy_a_la_vispera(self):
		opens(self.property, 6)
		self.flush()
		self.assertEqual(self.restrictions(), [self.closed(0, 5, True)])
		self.assertEqual(self.fake.posts("availability"), [])
		self.assertTrue(self.sent("restrictions", "rp-twin-bb")[day(5)]["stop_sell"])
		self.assertFalse(self.sent("restrictions", "rp-twin-bb")[day(6)]["stop_sell"])
		self.flush()
		self.assertEqual(len(self.restrictions()), 1, "un flush sin cambios no vuelve a llamar")

	def test_mover_la_apertura_ajusta_lo_que_sale(self):
		opens(self.property, 6)
		self.flush()
		opens(self.property, 4)
		self.flush()
		self.assertEqual(self.restrictions()[-1], self.closed(4, 5, False))
		opens(self.property, 8)
		self.flush()
		self.assertEqual(self.restrictions()[-1], self.closed(4, 7, True))

	def test_vaciar_la_apertura_reabre_esas_noches(self):
		opens(self.property, 6)
		self.flush()
		opens(self.property, None)
		self.flush()
		self.assertEqual(self.restrictions(), [self.closed(0, 5, True), self.closed(0, 5, False)])
		self.assertEqual(own(self.property), [])

	def test_una_restriccion_a_mano_sigue_cerrando_sus_noches(self):
		from kamra.channel_restrictions import save_restrictions

		save_restrictions(self.property, frappe.as_json([{"room_type": self.twin, "rate_plan": "",
		                                                   "date_from": day(3), "date_to": day(9), "stop_sell": 1}]))
		self.flush()
		opens(self.property, 6)
		self.flush()
		opens(self.property, None)
		self.flush()
		last = self.restrictions()[-1]
		# solo se reabren las noches que no cierra la de a mano: las del Double del 0 al 5 y las del Twin del 0 al 2
		self.assertEqual(last, sorted(
			[{"property_id": "cx-test", "rate_plan_id": p, "date_from": day(0), "date_to": day(5), "stop_sell": False}
			 for p in ("rp-double-bar", "rp-double-bb")]
			+ [{"property_id": "cx-test", "rate_plan_id": p, "date_from": day(0), "date_to": day(2), "stop_sell": False}
			   for p in ("rp-twin-bar", "rp-twin-bb")], key=lambda v: v["rate_plan_id"]))
		for d in range(3, 10):
			self.assertTrue(self.sent("restrictions", "rp-twin-bar")[day(d)]["stop_sell"], d)

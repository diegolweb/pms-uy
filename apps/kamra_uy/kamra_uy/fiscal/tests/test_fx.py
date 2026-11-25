"""La cotización de la factura (HR-348, D-HR-74 respuesta 7): el dólar billete comprador del BCU del día hábil anterior,
pedida al BCU y nunca tomada del Exchange Rate de la caja; sin ella el CFE espera y sale solo."""

import time
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import frappe
from kamra import ledger, settings

from kamra_uy.fiscal import api, connector, fx, sync
from kamra_uy.fiscal.tests.test_connector import FiscalCase
from kamra_uy.localization.tests.test_uruguay_pack import _bcu_post
from kamra_uy.payments.tests.fixtures import PROP

# la respuesta real del BCU del martes 24-11-2026 a las 16:42 (del 19 al 23; del 24 todavía nada)
LIVE = (Path(__file__).resolve().parents[3] / "tests" / "unit" / "uy" / "fixtures"
        / "bcu-cotizaciones-2026-11-24.xml").read_bytes()
TUESDAY_3PM = datetime(2026, 11, 24, 15, 0)
# la misma respuesta cuando el BCU ya publicó, tarde, la del martes 24
LATE = LIVE.replace(b"\t\t\t\t</datoscotizaciones>", b"\t\t\t\t\t<datoscotizaciones.dato xmlns=\"Cotiza\"><Fecha>2026-11-24</Fecha>"
                    b"<Moneda>2225</Moneda><Nombre>DLS. USA BILLETE</Nombre><CodigoISO>DLS.</CodigoISO><Emisor>ESTADOS "
                    b"UNIDOS</Emisor><TCC>40.800000</TCC><TCV>40.800000</TCV><ArbAct>1.000000</ArbAct>"
                    b"<FormaArbitrar>0</FormaArbitrar></datoscotizaciones.dato>\n\t\t\t\t</datoscotizaciones>")


class FxCase(FiscalCase):
	def setUp(self):
		super().setUp()
		self.now = self._stack.enter_context(patch.object(fx, "_now", return_value=TUESDAY_3PM))
		for key in ("kamra_uy:fx_fiscal", "kamra:fx_market:", "kamra:fx_not_offered:"):
			frappe.cache.delete_keys(key)
			self.addCleanup(frappe.cache.delete_keys, key)

	def bcu(self, up: bool = True, body: bytes = LIVE):
		import requests

		return patch("requests.post", _bcu_post(body) if up else _bcu_post(fail=requests.ConnectionError("no route")))


class TestTheInvoiceRate(FxCase):
	def test_it_is_the_bcu_billete_buyer_rate_of_the_previous_business_day_not_the_desk_row(self):
		# FiscalCase deja en la caja un cambio tecleado a mano de 40 pesos por dólar (Manual): la factura no lo mira
		settings.write_kv(PROP, "fx_margin_pct", 2)
		with self.bcu() as post:
			self.assertEqual(fx.fiscal_rate(PROP, "USD"), fx.FiscalRate(Decimal("40.721000"), datetime(2026, 11, 23).date()))
			self.assertEqual(fx.quote(PROP, "USD"), ("40.721", "2026-11-23"))
			# un lunes: la del viernes
			self.assertEqual(fx.quote(PROP, "USD", "2026-11-23"), ("40.629", "2026-11-20"))
			self.assertEqual(fx.quote(PROP, "UYU"), ("1", None))
		body = post.call_args_list[0].kwargs["data"]
		self.assertIn(b"<cot:item>2225</cot:item>", body)
		self.assertNotIn(b"<cot:item>2222</cot:item>", body)
		self.assertIn(b"<cot:FechaHasta>2026-11-23</cot:FechaHasta>", body)
		# la del 24 ya está guardada: no se vuelve a preguntar
		with self.bcu() as again:
			self.assertEqual(fx.rate(PROP, "USD"), "40.721")
		again.assert_not_called()

	def test_without_the_bcu_there_is_no_rate_and_nothing_asks_to_type_it(self):
		with self.bcu(up=False) as post:
			self.assertIsNone(fx.fiscal_rate(PROP, "USD"))
			with self.assertRaises(connector.FiscalError) as cm:
				fx.quote(PROP, "USD")
			self.assertIsInstance(cm.exception, fx.FxPendingError)
		self.assertEqual(str(cm.exception), fx.FX_PENDING)
		self.assertNotIn("cárgue", fx.FX_PENDING.lower())
		self.assertNotIn("finanzas", fx.FX_PENDING.lower())
		# un BCU caído se vuelve a preguntar al cabo de unos minutos, no en cada pantalla
		self.assertEqual(post.call_count, 1)
		with self.bcu(up=False):
			self.assertIn("el BCU no contestó", fx.diagnose(PROP)["reason"])

	def test_before_noon_after_a_weekday_without_a_quote_it_waits(self):
		# miércoles 25 a las 9 sin la del martes 24: puede que el BCU no la haya publicado; desde las 12, feriado
		with self.bcu():
			self.assertIsNone(fx.fiscal_rate(PROP, "USD", "2026-11-25"))
		frappe.cache.delete_keys("kamra_uy:fx_fiscal")
		self.now.return_value = datetime(2026, 11, 25, 12, 0)
		with self.bcu():
			self.assertEqual(fx.quote(PROP, "USD", "2026-11-25"), ("40.721", "2026-11-23"))

	def test_a_quote_the_bcu_publishes_late_replaces_the_holiday_one_at_the_next_sweep(self):
		# HR-355: la del lunes 23 se tomó el miércoles a las 12 porque el martes 24 no tenía cotización (¿feriado?); si
		# el BCU publica tarde la del 24, la factura pasa a esa al volver a mirar, no al cabo de los 7 días de HIT_TTL
		self._stack.enter_context(patch.object(fx, "MISS_TTL", 1))
		self.now.return_value = datetime(2026, 11, 25, 9, 0)
		with self.bcu():
			self.assertIsNone(fx.fiscal_rate(PROP, "USD", "2026-11-25"))
		time.sleep(1.2)
		self.now.return_value = datetime(2026, 11, 25, 12, 0)
		with self.bcu():
			self.assertEqual(fx.quote(PROP, "USD", "2026-11-25"), ("40.721", "2026-11-23"))
		time.sleep(1.2)
		with self.bcu(body=LATE) as post:
			self.assertEqual(fx.quote(PROP, "USD", "2026-11-25"), ("40.8", "2026-11-24"))
			# y esa, la del día hábil anterior, sí se guarda: no se vuelve a preguntar
			time.sleep(1.2)
			self.assertEqual(fx.quote(PROP, "USD", "2026-11-25"), ("40.8", "2026-11-24"))
		self.assertEqual(post.call_count, 1)


class TestTheCfeWaitsForTheRate(FxCase):
	def setUp(self):
		super().setUp()
		# el cableado de HR-346: connector.fx_quote → fx.quote
		self._stack.enter_context(patch.object(connector, "fx_quote", fx.quote))

	def test_the_cfe_waits_for_the_bcu_and_goes_out_on_its_own(self):
		with self.bcu(up=False):
			res = self.stay()
			out = connector.process_reservation(res.name)
		self.assertEqual(out, {"sent": 0, "pending": 1, "attention": 0})
		self.assertEqual(self.core.bodies("/v1/fiscal/cfe"), [])
		self.assertEqual([t.error for t in self.waiting(res)], [fx.FX_PENDING])

		frappe.cache.delete_keys("kamra_uy:fx_fiscal")
		with self.bcu():
			sync.sweep()
		self.assertEqual(self.waiting(res), [])
		[sent] = self.core.bodies("/v1/fiscal/cfe")
		self.assertEqual((sent["currency"], sent["fx_rate"]), ("USD", "40.721"))
		# y el check-in dice de qué día es la cotización
		with self.bcu():
			notes = [o["note"] for o in api.invoice_currency(res.name)["options"]]
		self.assertTrue(all("cotización del BCU del 23-11-2026" in n for n in notes), notes)


class TestTheDeskLoadsThePeso(FxCase):
	def test_a_dollar_property_in_uruguay_gets_the_peso_from_the_daily_load(self):
		# HR-348: en una propiedad en dólares el peso es la moneda extranjera de la caja; antes no estaba en la lista y
		# la carga diaria no lo traía nunca
		frappe.db.delete("Exchange Rate", {"property": PROP})
		frappe.db.delete("Property Setting", {"property": PROP, "setting_key": ["like", "fx_%"]})
		settings.write_kv(PROP, "fx_auto_fetch", 1)
		settings.clear_cache(PROP)
		self.addCleanup(settings.clear_cache, PROP)
		self.assertEqual(settings.list_setting(PROP, "fx_currencies"), ["UYU", "ARS", "BRL", "EUR"])
		with self.bcu(), patch("kamra_uy.localization.uruguay.nowdate", return_value="2026-11-24"), \
				patch("frappe.db.commit"):
			ledger.auto_fetch_exchange_rates()
		row = frappe.db.get_value("Exchange Rate", {"property": PROP, "currency": "UYU"},
		                          ["market_rate", "source"], as_dict=True)
		self.assertEqual(row.source, "Online")
		self.assertAlmostEqual(row.market_rate, 1 / 40.721, places=9)

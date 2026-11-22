"""La marca del hotel en los ajustes de Kamra (HR-32): kamra_uy.branding.aplica.aplicar con el paquete que sube
deploy/prod/branding/aplica.sh, y lo que el huésped recibe por las APIs de Kamra que pintan /kamra/book, el
pre-check-in (/kamra/checkin/<token>) y la factura (folio_invoice).

Lo corre `bench run-tests --app kamra_uy`.
"""

from __future__ import annotations

import json
import os
import tempfile

import frappe
from frappe.core.doctype.file.utils import get_content_hash
from frappe.tests import IntegrationTestCase
from kamra import api, public_api
from kamra.folio import open_folio
from kamra.tests.audit_fixtures import d, make_property, make_reservation, make_room_type

from kamra_uy.branding import aplica, marca
from kamra_uy.branding.tests.fixtures import PNGS, WEB, paquete_de_prueba

PROP = "UT Marca Roosevelt"
PIE_PROPIO = "Cuenta para transferencias: UT 000-000"


class MarcaTestCase(IntegrationTestCase):
	def setUp(self):
		super().setUp()
		self.prop = make_property(PROP, booking_engine_enabled=1, invoice_footer=PIE_PROPIO, logo_url=None,
		                          brand_accent="#0f6b54")
		frappe.db.set_default(marca.CLAVE, "")
		marca.olvidar()

	def tearDown(self):
		marca.olvidar()
		frappe.local.outgoing_email_account = {}
		super().tearDown()

	def propiedad(self):
		frappe.clear_document_cache("Property", self.prop)
		return frappe.get_doc("Property", self.prop)


class TestAplicaEnLaPropiedad(MarcaTestCase):
	def test_la_propiedad_lleva_el_simbolo_y_el_morado(self):
		resumen = aplica.aplicar(paquete=paquete_de_prueba())
		prop = self.propiedad()
		self.assertEqual(prop.brand_accent, "#2D236D")
		self.assertEqual(prop.logo_url, resumen["activos"]["simbolo"])
		fichero = frappe.get_doc("File", {"file_url": prop.logo_url})
		self.assertFalse(fichero.is_private)
		with open(fichero.get_full_path(), "rb") as f:
			self.assertEqual(f.read(), PNGS["simbolo"])

	def test_el_pie_de_la_factura_conserva_lo_suyo_y_suma_la_privacidad(self):
		aplica.aplicar(paquete=paquete_de_prueba())
		lineas = self.propiedad().invoice_footer.splitlines()
		self.assertEqual(lineas[0], PIE_PROPIO)
		self.assertIn(f"Privacidad UT es: {WEB}/privacidad", lineas)
		self.assertFalse([linea for linea in lineas if "/condiciones" in linea],
		                 "las condiciones en borrador no se enlazan")

	def test_las_condiciones_salen_al_aprobarse_y_se_van_si_vuelven_a_borrador(self):
		aplica.aplicar(paquete=paquete_de_prueba(**{"idiomas.es.condiciones.aprobado": True}))
		self.assertIn(f"Condiciones UT es: {WEB}/condiciones", self.propiedad().invoice_footer.splitlines())
		aplica.aplicar(paquete=paquete_de_prueba())
		pie = self.propiedad().invoice_footer.splitlines()
		self.assertNotIn(f"Condiciones UT es: {WEB}/condiciones", pie)
		self.assertEqual(pie, [PIE_PROPIO, f"Privacidad UT es: {WEB}/privacidad"])

	def test_es_idempotente(self):
		aplica.aplicar(paquete=paquete_de_prueba())
		segunda = aplica.aplicar(paquete=paquete_de_prueba())
		self.assertEqual(segunda["cambios"], [])
		for datos in PNGS.values():
			self.assertEqual(frappe.db.count("File", {"content_hash": get_content_hash(datos), "is_private": 0}), 1)
		self.assertEqual(self.propiedad().invoice_footer.count("/privacidad"), 1)

	def test_desde_un_fichero_como_lo_sube_aplica_sh(self):
		with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
			json.dump(paquete_de_prueba(), f)
		self.addCleanup(lambda: os.path.exists(f.name) and os.unlink(f.name))
		resumen = aplica.aplicar(ruta=f.name)
		self.assertEqual(self.propiedad().logo_url, resumen["activos"]["simbolo"])
		self.assertFalse(os.path.exists(f.name), "el fichero subido se borra al leerlo")


class TestLoQueVeElHuesped(MarcaTestCase):
	"""Las mismas APIs que llaman las pantallas de React de Kamra (PublicBooking, PublicCheckin, FolioView)."""

	def setUp(self):
		super().setUp()
		aplica.aplicar(paquete=paquete_de_prueba())
		self.logo = self.propiedad().logo_url
		self.rt = make_room_type(self.prop, "MRC", base_price=1000)

	def test_book_pinta_el_simbolo_y_el_morado(self):
		escaparate = public_api.showcase(self.prop)["property"]
		self.assertEqual(escaparate["logo_url"], self.logo)
		self.assertEqual(escaparate["brand_accent"], "#2D236D")

	def test_el_precheckin_pinta_el_simbolo(self):
		res = make_reservation(self.prop, self.rt, d(3), d(5), status="Confirmed")
		token = frappe.generate_hash(length=32)
		frappe.db.set_value("Reservation", res.name, "precheckin_token", token)
		self.assertEqual(public_api.precheckin_info(token)["property"]["logo_url"], self.logo)

	def test_la_factura_lleva_el_simbolo_y_el_enlace_a_la_privacidad(self):
		res = make_reservation(self.prop, self.rt, d(3), d(5), status="Confirmed")
		factura = api.folio_invoice(open_folio(res))
		self.assertEqual(factura["property"]["logo_url"], self.logo)
		self.assertIn(f"{WEB}/privacidad", factura["document"]["invoice_footer"])


class TestAjustesDelSitioYDelCorreo(MarcaTestCase):
	def test_el_sitio_y_las_cuentas_salientes_llevan_el_logo(self):
		cuenta = frappe.get_doc({"doctype": "Email Account", "email_account_name": "UT Marca", "enable_outgoing": 1,
		                         "email_id": "marca@example.com", "smtp_server": "smtp.example.com",
		                         "no_smtp_authentication": 1}).insert(ignore_permissions=True)
		resumen = aplica.aplicar(paquete=paquete_de_prueba())
		self.assertEqual(frappe.db.get_single_value("Website Settings", "app_name"), "UT Hotel Marca")
		self.assertEqual(frappe.db.get_single_value("Website Settings", "app_logo"), resumen["activos"]["logo"])
		self.assertEqual(frappe.db.get_value("Email Account", cuenta.name, "brand_logo"), resumen["activos"]["logo"])
		self.assertEqual(public_api.site_info()["brand_name"], "UT Hotel Marca")

	def test_guarda_la_marca_para_los_correos(self):
		resumen = aplica.aplicar(paquete=paquete_de_prueba())
		guardada = marca.guardada()
		self.assertEqual(guardada["nombre"], "UT Hotel Marca")
		self.assertEqual(guardada["urls"], resumen["activos"])
		self.assertNotIn("base64", json.dumps(guardada))


class TestPaqueteQueNoVale(MarcaTestCase):
	def assert_no_toca_nada(self, paquete, motivo: str):
		antes = frappe.db.count("File")
		with self.assertRaises(frappe.ValidationError) as error:
			aplica.aplicar(paquete=paquete)
		self.assertIn(motivo, str(error.exception))
		prop = self.propiedad()
		self.assertEqual((prop.brand_accent, prop.logo_url, prop.invoice_footer), ("#0f6b54", None, PIE_PROPIO))
		self.assertEqual(frappe.db.count("File"), antes)
		self.assertIsNone(marca.guardada())

	def test_un_color_que_no_es_hex(self):
		self.assert_no_toca_nada(paquete_de_prueba(**{"color.morado": "morado"}), "color.morado")

	def test_un_png_que_no_cuadra_con_su_huella(self):
		self.assert_no_toca_nada(paquete_de_prueba(**{"activos.simbolo.sha256": "0" * 64}), "simbolo")

	def test_falta_un_idioma(self):
		paquete = paquete_de_prueba()
		del paquete["marca"]["idiomas"]["pt"]
		self.assert_no_toca_nada(paquete, "pt")

	def test_un_enlace_que_no_es_https(self):
		self.assert_no_toca_nada(paquete_de_prueba(**{"idiomas.en.privacidad.url": "http://x.example.com/p"}),
		                         "idiomas.en.privacidad.url")

	def test_el_texto_del_cta_sin_contraste(self):
		self.assert_no_toca_nada(paquete_de_prueba(**{"color.sobre-amarillo": "#F2C230"}), "contraste")

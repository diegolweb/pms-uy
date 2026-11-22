"""El correo con la marca del hotel (HR-32): kamra_uy.branding.correo envuelve el cuerpo que escribe quien manda el
correo (HR-227, la confirmación de la reserva en línea) con la cabecera del logo, la letra y los colores de la
identidad, y el pie de la web en el idioma del huésped (es, pt o en), con los enlaces a la privacidad y, cuando estén
aprobadas, a las condiciones de la web nueva.

Lo corre `bench run-tests --app kamra_uy`.
"""

from __future__ import annotations

import email
import re
from email import policy

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import get_url

from kamra_uy.branding import aplica, correo, marca
from kamra_uy.branding.tests.fixtures import MARCA, WEB, paquete_de_prueba

CUERPO = "<p>Cuerpo UT de la confirmación</p>"


def estilo(html: str, etiqueta_con: str) -> str:
	"""El style="" del primer elemento cuyo HTML de apertura contiene `etiqueta_con`."""
	m = re.search(r"<[^>]*" + re.escape(etiqueta_con) + r"[^>]*>", html)
	assert m, f"no encuentro {etiqueta_con!r}"
	# premailer reescribe el atributo con comillas simples si el valor lleva dobles
	s = re.search(r"""style=(["'])(.*?)\1""", m.group(0))
	return s.group(2) if s else ""


class CorreoTestCase(IntegrationTestCase):
	def setUp(self):
		super().setUp()
		frappe.db.set_default(marca.CLAVE, "")
		marca.olvidar()

	def tearDown(self):
		marca.olvidar()
		frappe.local.outgoing_email_account = {}
		super().tearDown()

	def aplicar(self, **cambios) -> dict:
		return aplica.aplicar(paquete=paquete_de_prueba(**cambios))


class TestSinMarca(CorreoTestCase):
	def test_sin_marca_aplicada_el_cuerpo_sale_tal_cual(self):
		self.assertEqual(correo.envolver(CUERPO, "es"), CUERPO)


class TestEnvolver(CorreoTestCase):
	def setUp(self):
		super().setUp()
		self.urls = self.aplicar()["activos"]

	def test_cada_idioma_lleva_su_pie_y_solo_el_suyo(self):
		for idioma in ("es", "pt", "en"):
			html = correo.envolver(CUERPO, idioma)
			datos = MARCA["idiomas"][idioma]
			otros = [MARCA["idiomas"][o] for o in ("es", "pt", "en") if o != idioma]
			with self.subTest(idioma=idioma):
				self.assertIn(CUERPO, html)
				self.assertIn(f'href="{datos["privacidad"]["url"]}"', html)
				self.assertIn(datos["privacidad"]["texto"], html)
				self.assertIn(datos["direccion"], html)
				self.assertIn(datos["derechos"], html)
				for otro in otros:
					self.assertNotIn(otro["privacidad"]["url"] + '"', html)
					self.assertNotIn(otro["derechos"], html)

	def test_el_logo_arriba_y_el_logo_blanco_en_el_pie_con_url_absoluta(self):
		html = correo.envolver(CUERPO, "es")
		self.assertIn(f'src="{get_url(self.urls["logo"])}"', html)
		self.assertIn(f'src="{get_url(self.urls["logo-blanco"])}"', html)
		self.assertIn('alt="UT Hotel Marca"', html)
		self.assertLess(html.index(get_url(self.urls["logo"])), html.index(CUERPO))
		self.assertGreater(html.index(get_url(self.urls["logo-blanco"])), html.index(CUERPO))

	def test_el_logo_lleva_a_la_web_en_su_idioma(self):
		html = correo.envolver(CUERPO, "pt")
		self.assertIn(f'href="{WEB}/pt"', html)

	def test_el_contacto_del_pie(self):
		html = correo.envolver(CUERPO, "en")
		for dato in MARCA["contacto"]:
			self.assertIn(f'href="{dato["href"]}"', html)
			self.assertIn(dato["texto"], html)
			# un teléfono o un correo no se parte en dos líneas (a 390 px, «+598» quedaba solo)
			self.assertIn("white-space:nowrap", estilo(html, f'href="{dato["href"]}"'))

	def test_idiomas_regionales_y_desconocidos(self):
		self.assertIn(MARCA["idiomas"]["pt"]["derechos"], correo.envolver(CUERPO, "pt-BR"))
		self.assertIn(MARCA["idiomas"]["en"]["derechos"], correo.envolver(CUERPO, "EN_us"))
		self.assertIn(MARCA["idiomas"]["es"]["derechos"], correo.envolver(CUERPO, "fr"))
		self.assertIn(MARCA["idiomas"]["es"]["derechos"], correo.envolver(CUERPO, None))

	def test_las_condiciones_solo_cuando_estan_aprobadas(self):
		self.assertNotIn("/conditions", correo.envolver(CUERPO, "en"))
		self.aplicar(**{"idiomas.en.condiciones.aprobado": True})
		html = correo.envolver(CUERPO, "en")
		self.assertIn(f'href="{WEB}/en/conditions"', html)
		self.assertIn("Condiciones UT en", html)
		self.assertNotIn("/condicoes", correo.envolver(CUERPO, "pt"))

	def test_la_letra_y_los_colores_de_la_identidad(self):
		html = correo.envolver(CUERPO, "es")
		cuerpo = estilo(html, 'data-marca="cuerpo"')
		self.assertIn("font-family:Lato, 'Helvetica Neue', Helvetica, Arial, sans-serif", cuerpo)
		self.assertIn("color:#1B1730", cuerpo)
		self.assertIn("background-color:#1E1749", estilo(html, 'data-marca="pie"'))
		self.assertIn("background-color:#ECE9F7", estilo(html, 'data-marca="fondo"'))
		self.assertIn("border-radius:16px", estilo(html, 'data-marca="tarjeta"'))

	def test_los_textos_del_paquete_van_escapados(self):
		self.aplicar(**{"idiomas.es.direccion": "<script>alert(1)</script> & Cía"})
		html = correo.envolver(CUERPO, "es")
		self.assertNotIn("<script>", html)
		self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt; &amp; Cía", html)


class TestBoton(CorreoTestCase):
	def setUp(self):
		super().setUp()
		self.aplicar()

	def test_el_cta_es_el_amarillo_de_la_b_sin_hover(self):
		html = correo.boton("Texto UT", "https://pago.example.com/x?a=1&b=2")
		s = estilo(html, 'data-marca="cta"')
		for pieza in ("background-color:#F2C230", "color:#1E1749", "border:1.5px solid #8A6500",
		              "border-bottom:3px solid #8A6500", "border-radius:6px", "text-transform:uppercase",
		              "letter-spacing:0.04em", "font-weight:700", "font-size:18px"):
			self.assertIn(pieza, s)
		self.assertIn('href="https://pago.example.com/x?a=1&amp;b=2"', html)
		self.assertIn(">Texto UT<", html)

	def test_el_rotulo_va_escapado(self):
		self.assertIn("&lt;b&gt;UT&lt;/b&gt;", correo.boton("<b>UT</b>", "https://x.example.com"))

	def test_solo_enlaces_web(self):
		with self.assertRaises(frappe.ValidationError):
			correo.boton("UT", "javascript:alert(1)")


class TestEnlace(CorreoTestCase):
	def setUp(self):
		super().setUp()
		self.aplicar()

	def test_el_enlace_del_cuerpo_va_en_morado_y_no_en_el_azul_de_frappe(self):
		html = correo.enlace("<UT>", "https://x.example.com/a?b=1&c=2")
		s = estilo(html, 'href="https://x.example.com/a?b=1&amp;c=2"')
		self.assertIn("color:#2D236D", s)
		self.assertIn("text-decoration:underline", s)
		self.assertIn(">&lt;UT&gt;<", html)

	def test_solo_enlaces_web_o_de_correo(self):
		self.assertIn('href="mailto:a@example.com"', correo.enlace("UT", "mailto:a@example.com"))
		with self.assertRaises(frappe.ValidationError):
			correo.enlace("UT", "javascript:alert(1)")

	def test_los_enlaces_del_logo_llevan_color_propio(self):
		html = correo.envolver(CUERPO, "es")
		self.assertIn("color:#2D236D", estilo(html, 'data-marca="enlace-logo"'))
		self.assertIn("color:#FFFFFF", estilo(html, 'data-marca="enlace-logo-pie"'))


class TestPorLaColaDeFrappe(CorreoTestCase):
	"""Lo que de verdad sale: frappe.sendmail lo mete en standard.html y lo pasa por premailer."""

	def setUp(self):
		super().setUp()
		self.urls = self.aplicar()["activos"]
		for name in frappe.get_all("Email Account", {"default_outgoing": 1}, pluck="name"):
			frappe.db.set_value("Email Account", name, "default_outgoing", 0)
		if frappe.db.exists("Email Account", "UT Marca Cola"):
			frappe.db.set_value("Email Account", "UT Marca Cola", {"enable_outgoing": 1, "default_outgoing": 1})
		else:
			frappe.get_doc({"doctype": "Email Account", "email_account_name": "UT Marca Cola",
			                "email_id": "marca-cola@example.com", "enable_outgoing": 1, "default_outgoing": 1,
			                "smtp_server": "smtp.example.com", "no_smtp_authentication": 1}).insert(ignore_permissions=True)
		frappe.local.outgoing_email_account = {}

	def test_el_correo_encolado_lleva_la_marca(self):
		destino = "huesped-marca@example.com"
		cuerpo = correo.envolver(CUERPO + correo.boton("Texto UT", "https://pago.example.com/x"), "pt")
		frappe.sendmail(recipients=[destino], subject="UT marca", message=cuerpo)
		cola = frappe.get_last_doc("Email Queue", filters={"sender": ("like", "%marca-cola@example.com%")})
		html = next(parte.get_content() for parte in email.message_from_string(cola.message, policy=policy.default)
		            .walk() if parte.get_content_type() == "text/html")
		self.assertIn(get_url(self.urls["logo"]), html)
		self.assertIn(get_url(self.urls["logo-blanco"]), html)
		self.assertIn(f'href="{WEB}/pt/privacidade"', html)
		self.assertIn(MARCA["idiomas"]["pt"]["derechos"], html)
		self.assertIn("background-color:#1E1749", estilo(html, 'data-marca="pie"').replace(" ", ""))
		self.assertIn("background-color:#F2C230", estilo(html, 'data-marca="cta"').replace(" ", ""))

	def test_los_enlaces_ganan_a_la_hoja_de_correo_de_frappe(self):
		"""standard.html deja un <style> que premailer no puede poner en línea (`.email-body a:not(.btn)`, con color,
		peso y subrayado !important): en el cliente de correo gana a un estilo en línea sin !important, y el pie quedaba
		en negro sobre el morado. Cada enlace de la marca lleva los suyos con !important."""
		cuerpo = correo.envolver(CUERPO + correo.enlace("UT", "https://x.example.com")
		                         + correo.boton("Texto UT", "https://pago.example.com/x"), "es")
		frappe.sendmail(recipients=["huesped-marca-enlaces@example.com"], subject="UT marca enlaces", message=cuerpo)
		cola = frappe.get_last_doc("Email Queue", filters={"sender": ("like", "%marca-cola@example.com%")})
		html = next(parte.get_content() for parte in email.message_from_string(cola.message, policy=policy.default)
		            .walk() if parte.get_content_type() == "text/html")
		self.assertIn("a:not(.btn)", html, "si Frappe quita esa regla, esta prueba sobra")
		enlaces = re.findall(r"<a [^>]*>", html)
		self.assertGreaterEqual(len(enlaces), 7)  # logo, cuerpo, CTA, logo del pie, contacto (2) y privacidad
		for etiqueta in enlaces:
			s = re.search(r"""style=(["'])(.*?)\1""", etiqueta).group(2).replace(" ", "")
			for propiedad in ("color", "text-decoration", "font-weight"):
				self.assertRegex(s, rf"(^|;){propiedad}:[^;]+!important", etiqueta)
		self.assertIn("color:#FFF", estilo(html, 'href="https://hotel-ut.example.com/privacidad"').replace(" ", ""))

"""El correo al huésped con la marca del hotel (HR-32).

Quien escribe un correo al huésped (HR-227, la confirmación de la reserva en línea) pone el cuerpo y el rótulo de su
botón, con sus textos aprobados; aquí solo va la identidad:

    from kamra_uy.branding import correo
    html = correo.envolver(cuerpo_html + correo.boton(rotulo, url_de_pago), idioma_de_la_reserva)
    frappe.sendmail(recipients=[...], subject=..., message=html)

- `envolver(cuerpo, idioma)`: el fondo lavanda, la tarjeta blanca con el canto del llavero, el logo arriba (enlaza a
  la web en ese idioma), el cuerpo en la letra y la tinta de la identidad, y el pie morado de la web en ese idioma: el
  logo blanco, el contacto, la dirección, el enlace a la privacidad y, cuando estén aprobadas, a las condiciones, y
  los derechos. Sin marca aplicada (aplica.py), el cuerpo sale tal cual.
- `enlace(texto, url)`: un enlace del cuerpo, en el morado (sin él, Frappe lo pinta en su azul).
- `boton(rotulo, url)`: el CTA de la identidad, con el amarillo y el canto de la B. Un correo no tiene hover ni foco:
  el reposo, con el borde y el canto como `border` y `border-bottom` (final/README.md de HR-54).

Todo va en estilos en línea y tablas, que es lo que respetan los clientes de correo; Frappe además lo pasa por
premailer (frappe.email.email_body.inline_style_in_html). Los textos del paquete van escapados.
"""

from __future__ import annotations

import re
from html import escape

import frappe
from frappe.utils import get_url

from kamra_uy.branding import marca as marca_

# El logo de la cabecera y el del pie, al alto del logo de la web en escritorio (tokens «pagina.logo-l», 36 px).
ALTO_LOGO = 36
ANCHO_TARJETA = 600


def _px(valor: str) -> float:
	return float(re.fullmatch(r"([0-9.]+)px", valor).group(1))


def _num(valor: float) -> str:
	return f"{valor:g}px"


def _img(url: str, meta: dict, alt: str) -> str:
	ancho = round(meta["ancho"] * ALTO_LOGO / meta["alto"])
	return (f'<img src="{escape(get_url(url))}" alt="{escape(alt)}" width="{ancho}" height="{ALTO_LOGO}" '
	        f'style="display:block;border:0;outline:none;width:{ancho}px;height:{ALTO_LOGO}px;">')


# standard.html de Frappe deja un <style> que premailer no puede poner en línea: `.email-body a:not(.btn)` con color,
# peso y subrayado !important. En el cliente de correo gana a un estilo en línea sin !important (el pie quedaba en negro
# sobre el morado), así que los enlaces de la marca llevan esas tres propiedades con !important.
def _a(color: str, subrayado: bool, peso: int) -> str:
	return (f"color:{color} !important;text-decoration:{'underline' if subrayado else 'none'} !important;"
	        f"font-weight:{peso} !important;")


def _enlace(href: str, texto: str, color: str, peso: int, entero: bool = False) -> str:
	"""`entero`: sin partirse en dos líneas (un correo o un teléfono)."""
	estilo = _a(color, True, peso) + ("white-space:nowrap;" if entero else "")
	return f'<a href="{escape(href)}" style="{estilo}">{escape(texto)}</a>'


def _pie(m: dict, datos: dict) -> str:
	c, pila = m["color"], m["fuente"]["pila"]
	peso = m["fuente"]["texto"]
	contacto = " · ".join(_enlace(d["href"], d["texto"], c["blanco"], peso, entero=True) for d in m["contacto"])
	legales = [_enlace(datos["privacidad"]["url"], datos["privacidad"]["texto"], c["blanco"], peso)]
	if datos["condiciones"]["aprobado"]:
		legales.append(_enlace(datos["condiciones"]["url"], datos["condiciones"]["texto"], c["blanco"], peso))
	suave = f"margin:0 0 8px 0;color:{c['lavanda-media']};"
	return (
		f'<td data-marca="pie" style="background-color:{c["morado-hondo"]};padding:32px;font-family:{pila};'
		f'font-size:14px;line-height:20px;color:{c["blanco"]};">'
		f'<a data-marca="enlace-logo-pie" href="{escape(datos["inicio"])}" style="{_a(c["blanco"], False, peso)}">'
		f'{_img(m["urls"]["logo-blanco"], m["activos"]["logo-blanco"], m["nombre"])}</a>'
		f'<p style="margin:24px 0 8px 0;color:{c["blanco"]};">{contacto}</p>'
		f'<p style="{suave}">{escape(datos["direccion"])}</p>'
		f'<p style="margin:16px 0 8px 0;color:{c["blanco"]};">{" · ".join(legales)}</p>'
		f'<p style="{suave}margin-bottom:0;">{escape(datos["derechos"])}</p>'
		"</td>"
	)


def envolver(cuerpo_html: str, idioma: str | None = None) -> str:
	"""El cuerpo (HTML de confianza: lo escribe el código que manda el correo) con la marca en ese idioma."""
	m = marca_.guardada()
	if not m:
		return cuerpo_html
	datos = m["idiomas"][marca_.idioma(idioma)]
	c, pila = m["color"], m["fuente"]["pila"]
	tabla = 'role="presentation" cellpadding="0" cellspacing="0" border="0"'
	return (
		f'<table {tabla} width="100%" data-marca="fondo" style="background-color:{c["lavanda"]};'
		'border-collapse:collapse;">'
		'<tr><td align="center" style="padding:32px 16px;">'
		f'<table {tabla} width="100%" data-marca="tarjeta" style="max-width:{ANCHO_TARJETA}px;'
		f'background-color:{c["blanco"]};border-radius:{m["radio"]["llavero"]};border-collapse:separate;'
		'overflow:hidden;">'
		'<tr><td data-marca="cabecera" style="padding:32px 32px 8px 32px;">'
		f'<a data-marca="enlace-logo" href="{escape(datos["inicio"])}" '
		f'style="{_a(c["morado"], False, m["fuente"]["texto"])}">{_img(m["urls"]["logo"], m["activos"]["logo"], m["nombre"])}</a></td></tr>'
		f'<tr><td data-marca="cuerpo" style="padding:24px 32px 32px 32px;font-family:{pila};font-size:16px;'
		f'line-height:24px;font-weight:{m["fuente"]["texto"]};color:{c["tinta"]};">{cuerpo_html}</td></tr>'
		f"<tr>{_pie(m, datos)}</tr>"
		"</table></td></tr></table>"
	)


def enlace(texto: str, url: str) -> str:
	"""Un enlace del cuerpo en el morado de la identidad (sin color propio, la hoja de correo de Frappe lo pinta en su
	azul). Solo a una web o a un correo."""
	if not isinstance(url, str) or not re.match(r"^(https?://|mailto:)", url):
		frappe.throw("marca: un enlace del correo solo va a una web o a un correo", frappe.ValidationError)
	m = marca_.guardada()
	if not m:
		return f'<a href="{escape(url)}">{escape(texto)}</a>'
	return _enlace(url, texto, m["color"]["morado"], m["fuente"]["texto"])


def boton(rotulo: str, url: str) -> str:
	"""El botón de acción del correo (rótulo aprobado, de quien llama). Solo enlaza a una web (http o https)."""
	if not isinstance(url, str) or not re.match(r"^https?://", url):
		frappe.throw("marca: el botón del correo solo enlaza a una web (http o https)", frappe.ValidationError)
	m = marca_.guardada()
	if not m:
		return f'<p><a href="{escape(url)}">{escape(rotulo)}</a></p>'
	c, cta, pila = m["color"], m["cta"], m["fuente"]["pila"]
	radio = m["radio"]["boton"]
	# El alto del CTA (52 px) con un rótulo de 20 px de línea: lo que falta, repartido arriba y abajo; el borde de
	# arriba es el fino y el de abajo, el canto.
	linea = 20
	libre = _px(cta["alto"]) - linea - _px(cta["borde"]) - _px(cta["canto"])
	arriba = round(libre / 2 + 0.25)
	abajo = libre - arriba
	estilo = (
		f"display:inline-block;background-color:{c['amarillo']};{_a(c['sobre-amarillo'], False, cta['peso'])}"
		f"border:{cta['borde']} solid {c['amarillo-canto']};border-bottom:{cta['canto']} solid {c['amarillo-canto']};"
		f"border-radius:{radio};padding:{_num(arriba)} {cta['relleno-x']} {_num(abajo)} {cta['relleno-x']};"
		f"font-family:{pila};font-size:{cta['tamano']};line-height:{linea}px;"
		f"text-transform:{cta['mayusculas']};letter-spacing:{cta['espaciado']};"
	)
	return (
		'<table role="presentation" cellpadding="0" cellspacing="0" border="0" style="margin:24px 0;">'
		f'<tr><td style="border-radius:{radio};background-color:{c["amarillo"]};">'
		f'<a data-marca="cta" href="{escape(url)}" style="{estilo}">{escape(rotulo)}</a></td></tr></table>'
	)

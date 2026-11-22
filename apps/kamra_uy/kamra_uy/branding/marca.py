"""El paquete de marca del hotel dentro de Kamra (HR-32): cómo se valida y dónde se guarda.

El paquete lo prepara deploy/prod/branding/marca.py desde la identidad aprobada (HR-54) y los textos aprobados de la
web nueva (HR-52), y lo sube deploy/prod/branding/aplica.sh. Aquí no vive ningún dato del hotel: kamra_uy se publica
como AGPL (deploy/publish-agpl.sh) y el logo, el contacto y los textos son del hotel. Lo que se guarda en Kamra es:

- los tres PNG como ficheros públicos (aplica.py);
- la marca sin los PNG, con las URL de esos ficheros, en el valor por defecto del sitio `kamra_uy_marca`
  (frappe.db.set_default): la lee correo.py al montar un correo.

Forma del paquete: {"marca": {version, nombre, web, color, fuente, radio, cta, idiomas{es,pt,en}, contacto,
activos{logo, logo-blanco, simbolo: {ancho, alto, sha256}}}, "activos": {clave: {nombre, sha256, base64}}}.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re

import frappe

CLAVE = "kamra_uy_marca"
VERSION = 1
IDIOMAS = ("es", "pt", "en")
# El idioma de Kamra UY (es-UY): el del correo de un huésped sin idioma o con uno que la web no tiene.
IDIOMA_POR_DEFECTO = "es"
ACTIVOS = ("logo", "logo-blanco", "simbolo")
COLORES = ("morado", "morado-hondo", "lavanda", "lavanda-media", "blanco", "tinta", "tinta-suave", "amarillo",
           "amarillo-canto", "sobre-amarillo")
RADIOS = ("boton", "campo", "llavero")
# Los pares (texto, fondo, mínimo WCAG 2.x) que pinta el correo. Un paquete que no los cumple no entra.
PARES = (
	("tinta", "blanco", 4.5),  # el cuerpo
	("morado", "blanco", 4.5),  # los enlaces del cuerpo
	("blanco", "morado-hondo", 4.5),  # el pie
	("lavanda-media", "morado-hondo", 4.5),  # la dirección y los derechos del pie
	("sobre-amarillo", "amarillo", 4.5),  # el rótulo del CTA
	("amarillo-canto", "blanco", 3.0),  # el borde y el canto del CTA sobre la tarjeta blanca (gráfico)
)

_HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")
_PX = re.compile(r"^\d+(\.\d+)?px$")
_EM = re.compile(r"^\d+(\.\d+)?em$")
_NOMBRE_PNG = re.compile(r"^[a-z0-9][a-z0-9-]*\.png$")


def _mal(ruta: str, problema: str):
	frappe.throw(f"marca: {ruta} {problema}", frappe.ValidationError)


def _texto(valor, ruta: str, maximo: int = 300) -> str:
	if not isinstance(valor, str) or not valor.strip() or len(valor) > maximo:
		_mal(ruta, "tiene que ser un texto no vacío")
	return valor


def _url(valor, ruta: str, esquemas=("https://",)) -> str:
	if not isinstance(valor, str) or not valor.startswith(esquemas) or any(c in valor for c in '"<> \n'):
		_mal(ruta, f"no es una URL {' o '.join(e.rstrip(':/') for e in esquemas)} válida")
	return valor


def luminancia(hex_: str) -> float:
	canales = []
	for i in (1, 3, 5):
		c = int(hex_[i:i + 2], 16) / 255
		canales.append(c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4)
	r, g, b = canales
	return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contraste(texto: str, fondo: str) -> float:
	claro, oscuro = sorted((luminancia(texto), luminancia(fondo)), reverse=True)
	return (claro + 0.05) / (oscuro + 0.05)


def _png(clave: str, meta: dict, activo) -> bytes:
	if not isinstance(activo, dict):
		_mal(f"activos.{clave}", "falta")
	_texto(activo.get("nombre"), f"activos.{clave}.nombre", 80)
	if not _NOMBRE_PNG.match(activo["nombre"]):
		_mal(f"activos.{clave}.nombre", "tiene que ser un nombre de fichero .png en minúsculas")
	try:
		datos = base64.b64decode(activo.get("base64") or "", validate=True)
	except ValueError:
		_mal(f"activos.{clave}.base64", "no es base64")
	if datos[:8] != b"\x89PNG\r\n\x1a\n":
		_mal(f"activos.{clave}", "no es un PNG")
	huella = hashlib.sha256(datos).hexdigest()
	if huella != activo.get("sha256") or huella != meta.get("sha256"):
		_mal(f"activos.{clave}", "no cuadra con su sha256")
	if not all(isinstance(meta.get(m), int) and meta[m] > 0 for m in ("ancho", "alto")):
		_mal(f"marca.activos.{clave}", "necesita ancho y alto")
	return datos


def validar(paquete) -> tuple[dict, dict[str, bytes]]:
	"""(la marca, {clave: bytes del PNG}) si el paquete vale; si no, frappe.ValidationError con la ruta del fallo.
	Valida todo ANTES de que aplica.py escriba nada."""
	if not isinstance(paquete, dict) or not isinstance(paquete.get("marca"), dict):
		_mal("paquete", "no trae «marca»")
	marca = paquete["marca"]
	if marca.get("version") != VERSION:
		_mal("version", f"tiene que ser {VERSION}")
	_texto(marca.get("nombre"), "nombre", 140)
	_url(marca.get("web"), "web")

	color = marca.get("color")
	if not isinstance(color, dict):
		_mal("color", "falta")
	for nombre in COLORES:
		if not isinstance(color.get(nombre), str) or not _HEX.match(color[nombre]):
			_mal(f"color.{nombre}", "no es un color #RRGGBB")
	for texto, fondo, minimo in PARES:
		medido = contraste(color[texto], color[fondo])
		if medido < minimo:
			_mal(f"color.{texto}", f"sobre color.{fondo}: contraste {medido:.2f}:1, menos de {minimo}:1")

	fuente = marca.get("fuente") or {}
	_texto(fuente.get("familia"), "fuente.familia", 60)
	if any(c in _texto(fuente.get("pila"), "fuente.pila", 200) for c in '";<>'):
		_mal("fuente.pila", "lleva caracteres que no caben en un estilo")
	for uso in ("texto", "enfasis", "titulos"):
		if not isinstance(fuente.get(uso), int) or not 100 <= fuente[uso] <= 900:
			_mal(f"fuente.{uso}", "tiene que ser un peso de 100 a 900")

	radio = marca.get("radio") or {}
	for nombre in RADIOS:
		if not isinstance(radio.get(nombre), str) or not _PX.match(radio[nombre]):
			_mal(f"radio.{nombre}", "tiene que ser una medida en px")
	cta = marca.get("cta") or {}
	for nombre in ("alto", "relleno-x", "borde", "canto", "tamano"):
		if not isinstance(cta.get(nombre), str) or not _PX.match(cta[nombre]):
			_mal(f"cta.{nombre}", "tiene que ser una medida en px")
	if not isinstance(cta.get("espaciado"), str) or not _EM.match(cta["espaciado"]):
		_mal("cta.espaciado", "tiene que ser una medida en em")
	if cta.get("mayusculas") not in ("uppercase", "none"):
		_mal("cta.mayusculas", "es uppercase o none")
	if not isinstance(cta.get("peso"), int):
		_mal("cta.peso", "tiene que ser un peso")

	idiomas = marca.get("idiomas")
	if not isinstance(idiomas, dict):
		_mal("idiomas", "falta")
	for idioma in IDIOMAS:
		datos = idiomas.get(idioma)
		if not isinstance(datos, dict):
			_mal(f"idiomas.{idioma}", "falta")
		_url(datos.get("inicio"), f"idiomas.{idioma}.inicio")
		for enlace in ("privacidad", "condiciones"):
			pieza = datos.get(enlace)
			if not isinstance(pieza, dict):
				_mal(f"idiomas.{idioma}.{enlace}", "falta")
			_texto(pieza.get("texto"), f"idiomas.{idioma}.{enlace}.texto")
			_url(pieza.get("url"), f"idiomas.{idioma}.{enlace}.url")
		if not isinstance(datos["condiciones"].get("aprobado"), bool):
			_mal(f"idiomas.{idioma}.condiciones.aprobado", "es true o false")
		_texto(datos.get("direccion"), f"idiomas.{idioma}.direccion")
		_texto(datos.get("derechos"), f"idiomas.{idioma}.derechos")

	contacto = marca.get("contacto")
	if not isinstance(contacto, list):
		_mal("contacto", "tiene que ser una lista")
	for i, dato in enumerate(contacto):
		if not isinstance(dato, dict):
			_mal(f"contacto.{i}", "no es un enlace")
		_texto(dato.get("texto"), f"contacto.{i}.texto", 120)
		_url(dato.get("href"), f"contacto.{i}.href", ("mailto:", "tel:", "https://"))

	metas = marca.get("activos")
	if not isinstance(metas, dict) or not isinstance(paquete.get("activos"), dict):
		_mal("activos", "faltan")
	pngs = {}
	for clave in ACTIVOS:
		if not isinstance(metas.get(clave), dict):
			_mal(f"marca.activos.{clave}", "falta")
		pngs[clave] = _png(clave, metas[clave], paquete["activos"].get(clave))
	return marca, pngs


def idioma(codigo: str | None) -> str:
	"""«pt-BR», «PT_br» o «pt» → pt; uno que la web no tiene, o ninguno → el de Kamra UY (es)."""
	base = re.split(r"[-_]", (codigo or "").strip().lower(), maxsplit=1)[0]
	return base if base in IDIOMAS else IDIOMA_POR_DEFECTO


def guardar(marca: dict, urls: dict[str, str]) -> None:
	frappe.db.set_default(CLAVE, json.dumps({"marca": marca, "urls": urls}, ensure_ascii=False, sort_keys=True))
	olvidar()


def guardada() -> dict | None:
	"""La marca aplicada, con `urls` (la del fichero público de cada activo); None si nunca se aplicó."""
	if not hasattr(frappe.local, "kamra_uy_marca"):
		crudo = frappe.db.get_default(CLAVE)
		valor = None
		if crudo:
			try:
				leido = json.loads(crudo)
				valor = {**leido["marca"], "urls": leido["urls"]}
			except (ValueError, KeyError, TypeError):
				frappe.log_error(title="kamra_uy: la marca guardada no se puede leer", message=str(crudo)[:2000])
		frappe.local.kamra_uy_marca = valor
	return frappe.local.kamra_uy_marca


def olvidar() -> None:
	"""Que la próxima lectura vaya a la base (tras guardar, y en los tests tras su rollback)."""
	if hasattr(frappe.local, "kamra_uy_marca"):
		del frappe.local.kamra_uy_marca
	from frappe.cache_manager import clear_defaults_cache

	clear_defaults_cache("__default")

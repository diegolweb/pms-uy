"""Aplica la marca del hotel a los ajustes de Kamra (HR-32). Lo arranca deploy/prod/branding/aplica.sh:

    bench --site <sitio> execute kamra_uy.branding.aplica.aplicar --kwargs "{'ruta': '/tmp/hr-marca-….json'}"

No es un método de la API (sin @frappe.whitelist): solo se llama desde bench. Valida el paquete entero antes de
escribir nada (marca.validar) y es idempotente: con el mismo paquete, la segunda vez no cambia nada.

Qué toca, todo con los ajustes que Kamra y Frappe ya tienen (ninguna pantalla de Kamra cambia de código):
- cada Property: `logo_url` = el símbolo (Kamra lo pinta en un cuadrado en /kamra/book, en el pre-check-in y en la
  factura), `brand_accent` = el morado (los botones, enlaces y realces de /kamra/book) y, en `invoice_footer`, una
  línea por enlace legal de la web nueva (la privacidad y, si están aprobadas, las condiciones) en el idioma de la
  página de reservas de la propiedad. Las demás líneas del pie (las del hotel) se conservan;
- Website Settings: `app_name` y `app_logo` (el nombre y el logo que Frappe pone en la cabecera de un correo con
  cabecera, y los que Kamra muestra junto al suyo en la pantalla del personal, public_api.site_info);
- cada Email Account con salida: `brand_logo` (el logo de la cabecera de sus correos);
- la marca, para los correos (marca.guardar, la lee correo.py).

Lo que estos ajustes NO alcanzan (la letra y el CTA amarillo en las páginas de React de Kamra, el color del
pre-check-in y los enlaces legales en /kamra/book) necesita código fuera de kamra_uy/branding: ver
deploy/prod/branding/README.md.
"""

from __future__ import annotations

import json
import os

import frappe
from frappe.core.doctype.file.utils import get_content_hash

from kamra_uy.branding import marca as marca_


def aplicar(ruta: str | None = None, paquete: dict | None = None) -> dict:
	"""Aplica el paquete (dado, o leído de `ruta`, que se borra al leerlo). Devuelve {propiedades, activos, cambios}."""
	if paquete is None:
		paquete = _leer(ruta)
	marca, pngs = marca_.validar(paquete)
	anterior = marca_.guardada()
	cambios: list[str] = []
	urls = {clave: _fichero_publico(paquete["activos"][clave]["nombre"], pngs[clave], cambios)
	        for clave in marca_.ACTIVOS}
	propiedades = frappe.get_all("Property", pluck="name", order_by="name asc")
	for nombre in propiedades:
		_propiedad(nombre, marca, anterior, urls, cambios)
	_sitio(marca, urls, cambios)
	_cuentas_de_correo(urls, cambios)
	if anterior != {**marca, "urls": urls}:
		marca_.guardar(marca, urls)
		cambios.append("marca guardada para los correos")
	return {"propiedades": propiedades, "activos": urls, "cambios": cambios}


def _leer(ruta: str | None) -> dict:
	if not ruta or not os.path.isabs(ruta) or not ruta.endswith(".json"):
		frappe.throw("marca: hace falta la ruta absoluta del paquete .json", frappe.ValidationError)
	try:
		with open(ruta, encoding="utf-8") as f:  # nosemgrep: frappe-security-file-traversal -- lo llama bench execute con el fichero que acaba de subir aplica.sh
			paquete = json.load(f)
	except (OSError, ValueError) as e:
		frappe.throw(f"marca: no puedo leer {ruta}: {e}", frappe.ValidationError)
	os.remove(ruta)
	return paquete


def _fichero_publico(nombre: str, datos: bytes, cambios: list[str]) -> str:
	"""La URL pública del PNG: la del File que ya tiene ese contenido (y está en disco) o la de uno nuevo."""
	for fila in frappe.get_all("File", {"content_hash": get_content_hash(datos), "is_private": 0, "is_folder": 0},
	                           ["name", "file_url"], order_by="creation asc"):
		if frappe.get_doc("File", fila.name).exists_on_disk():
			return fila.file_url
	fichero = frappe.get_doc({"doctype": "File", "file_name": nombre, "content": datos, "is_private": 0})
	fichero.insert(ignore_permissions=True)
	cambios.append(f"fichero {fichero.file_url}")
	return fichero.file_url


def _enlaces_legales(marca: dict | None) -> set[str]:
	if not marca:
		return set()
	return {datos[enlace]["url"] for datos in marca.get("idiomas", {}).values()
	        for enlace in ("privacidad", "condiciones") if isinstance(datos.get(enlace), dict)}


def pie_de_factura(actual: str | None, marca: dict, idioma: str, anterior: dict | None = None) -> str:
	"""El pie de la factura con sus líneas propias intactas y, al final, «<texto>: <url>» por enlace legal. Las líneas
	de un enlace de esta marca o de la anterior se quitan antes, así que cambiar la URL o el idioma no las duplica."""
	gestionadas = _enlaces_legales(marca) | _enlaces_legales(anterior)
	lineas = [linea for linea in (actual or "").splitlines() if not any(url in linea for url in gestionadas)]
	while lineas and not lineas[-1].strip():
		lineas.pop()
	datos = marca["idiomas"][idioma]
	lineas.append(f"{datos['privacidad']['texto']}: {datos['privacidad']['url']}")
	if datos["condiciones"]["aprobado"]:
		lineas.append(f"{datos['condiciones']['texto']}: {datos['condiciones']['url']}")
	return "\n".join(lineas)


def _propiedad(nombre: str, marca: dict, anterior: dict | None, urls: dict, cambios: list[str]) -> None:
	doc = frappe.get_doc("Property", nombre)
	idioma = marca_.idioma(doc.get("booking_language"))
	nuevo = {
		"logo_url": urls["simbolo"],
		"brand_accent": marca["color"]["morado"],
		"invoice_footer": pie_de_factura(doc.get("invoice_footer"), marca, idioma, anterior),
	}
	distinto = {campo: valor for campo, valor in nuevo.items() if (doc.get(campo) or "") != valor}
	if not distinto:
		return
	# Sin validar la Property entera: solo cambian tres campos de presentación y su validación (tarifas, políticas…)
	# no es de esta ficha.
	frappe.db.set_value("Property", nombre, distinto)
	frappe.clear_document_cache("Property", nombre)
	cambios.append(f"Property {nombre}: {', '.join(sorted(distinto))}")


def _sitio(marca: dict, urls: dict, cambios: list[str]) -> None:
	for campo, valor in (("app_name", marca["nombre"]), ("app_logo", urls["logo"])):
		if (frappe.db.get_single_value("Website Settings", campo) or "") != valor:
			frappe.db.set_single_value("Website Settings", campo, valor)
			cambios.append(f"Website Settings: {campo}")


def _cuentas_de_correo(urls: dict, cambios: list[str]) -> None:
	for cuenta in frappe.get_all("Email Account", {"enable_outgoing": 1}, ["name", "brand_logo"]):
		if (cuenta.brand_logo or "") != urls["logo"]:
			frappe.db.set_value("Email Account", cuenta.name, "brand_logo", urls["logo"])
			cambios.append(f"Email Account {cuenta.name}: brand_logo")

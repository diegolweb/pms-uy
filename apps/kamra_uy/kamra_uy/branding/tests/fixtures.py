"""Un paquete de marca de prueba con la forma del que sube deploy/prod/branding/aplica.sh (HR-32).

La paleta es la de una identidad real con el contraste medido (texto sobre fondo ≥ 4,5:1 en los pares que pinta el
correo); el nombre, la web, el contacto y los textos son de prueba (example.com). Los PNG son de verdad (cabecera,
IHDR, IDAT e IEND), pequeños y distintos por activo.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import struct
import zlib

WEB = "https://hotel-ut.example.com"


def png(ancho: int, alto: int, rgba: tuple[int, int, int, int]) -> bytes:
	def trozo(tipo: bytes, datos: bytes) -> bytes:
		return struct.pack(">I", len(datos)) + tipo + datos + struct.pack(">I", zlib.crc32(tipo + datos))

	fila = b"\x00" + bytes(rgba) * ancho
	return (b"\x89PNG\r\n\x1a\n" + trozo(b"IHDR", struct.pack(">IIBBBBB", ancho, alto, 8, 6, 0, 0, 0))
	        + trozo(b"IDAT", zlib.compress(fila * alto)) + trozo(b"IEND", b""))


PNGS = {
	"logo": png(6, 2, (45, 35, 109, 255)),
	"logo-blanco": png(6, 2, (255, 255, 255, 255)),
	"simbolo": png(4, 4, (45, 35, 109, 255)),
}


def _idioma(sufijo: str, ruta_privacidad: str, ruta_condiciones: str) -> dict:
	return {
		"inicio": f"{WEB}/{sufijo}".rstrip("/"),
		"privacidad": {"texto": f"Privacidad UT {sufijo or 'es'}", "url": f"{WEB}{ruta_privacidad}"},
		"condiciones": {"texto": f"Condiciones UT {sufijo or 'es'}", "url": f"{WEB}{ruta_condiciones}",
		                "aprobado": False},
		"direccion": f"Calle de prueba 1 ({sufijo or 'es'})",
		"derechos": f"© UT Hotel ({sufijo or 'es'})",
	}


MARCA = {
	"version": 1,
	"nombre": "UT Hotel Marca",
	"web": WEB,
	"color": {
		"morado": "#2D236D", "morado-hondo": "#1E1749", "lavanda": "#ECE9F7", "lavanda-media": "#D6D0EE",
		"blanco": "#FFFFFF", "tinta": "#1B1730", "tinta-suave": "#4A4566", "amarillo": "#F2C230",
		"amarillo-canto": "#8A6500", "sobre-amarillo": "#1E1749",
	},
	"fuente": {"familia": "Lato", "pila": "Lato, 'Helvetica Neue', Helvetica, Arial, sans-serif",
	           "texto": 400, "enfasis": 700, "titulos": 900},
	"radio": {"boton": "6px", "campo": "10px", "llavero": "16px"},
	"cta": {"alto": "52px", "relleno-x": "28px", "borde": "1.5px", "canto": "3px", "mayusculas": "uppercase",
	        "espaciado": "0.04em", "tamano": "18px", "peso": 700},
	"idiomas": {
		"es": _idioma("", "/privacidad", "/condiciones"),
		"pt": _idioma("pt", "/pt/privacidade", "/pt/condicoes"),
		"en": _idioma("en", "/en/privacy", "/en/conditions"),
	},
	"contacto": [
		{"texto": "reservas@hotel-ut.example.com", "href": "mailto:reservas@hotel-ut.example.com"},
		{"texto": "+598 0000 0000", "href": "tel:+59800000000"},
	],
}


def paquete_de_prueba(**cambios) -> dict:
	"""El paquete, con `cambios` aplicados por ruta con puntos: paquete_de_prueba(**{"color.morado": "#000000"})."""
	marca = copy.deepcopy(MARCA)
	marca["activos"] = {}
	activos = {}
	for clave, datos in PNGS.items():
		sha = hashlib.sha256(datos).hexdigest()
		ancho, alto = struct.unpack(">II", datos[16:24])
		marca["activos"][clave] = {"ancho": ancho, "alto": alto, "sha256": sha}
		activos[clave] = {"nombre": f"ut-hotel-marca-{clave}.png", "sha256": sha,
		                  "base64": base64.b64encode(datos).decode("ascii")}
	paquete = {"marca": marca, "activos": activos}
	for ruta, valor in cambios.items():
		destino = paquete
		*camino, ultima = ruta.split(".")
		if camino and camino[0] not in ("marca", "activos"):
			camino = ["marca", *camino]
		elif not camino:
			camino = ["marca"]
		for paso in camino:
			destino = destino[paso]
		destino[ultima] = valor
	return paquete

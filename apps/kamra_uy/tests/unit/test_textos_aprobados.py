"""HR-282 (D-HR-48, 22-11): el fundador aprobó todos los textos, así que en kamra_uy ninguno queda a la espera de su OK.
Los del correo de confirmación llevan cada uno su fuente: «HR-175» los que el correo copia literales de la web
(web/content/*/reservar.md) y «D-HR-48» los nuevos del correo y el aviso del IVA de D-HR-47. Lo que dependía de que
hubiera textos pendientes (las listas del correo y su guarda contra la web) sigue en pie con la lista de los de la web."""

import re
from pathlib import Path

from kamra_uy.notifications import texts

PAQUETE = Path(__file__).resolve().parents[2] / "kamra_uy"
NUEVOS_DEL_CORREO = ("asunto", "saludo", "intro", "horario", "ayuda", "despedida", "pagado_total", "factura",
                     "recibo")

# 23-11, lo pidió el fundador: el correo es el aviso del pago, no un comprobante fiscal; la factura se hace siempre en el
# check-in. «pagado_total» solo sale si se pagó todo (confirmation.render); «factura», siempre.
PAGO_Y_FACTURA = {
	"es": ("Pagaste el total de tu estadía.", "La factura te la entregamos en el check-in."),
	"pt": ("Você pagou o total da sua estadia.", "A fatura é entregue no check-in."),
	"en": ("You have paid the full amount of your stay.", "We will give you the invoice at check-in."),
}


def test_ningun_texto_del_correo_queda_pendiente() -> None:
	assert set(texts.APROBADOS) == set(texts.TEXTOS["es"])
	assert not set(texts.BORRADORES["es"]) & set(texts.TEXTOS["es"]), "un borrador no se mezcla con los aprobados"


# HR-374 (D-HR-44): los textos nuevos de los dos correos nuevos al huésped y el de WhatsApp en pt y en, BORRADORES para
# la tanda de OK del fundador. Hasta su OK no sale ninguno: BORRADORES_OK lo apaga todo.
BORRADORES_HR374 = {"whatsapp", "intro_recepcion", "asunto_cancelada", "cancelada", "intro_cancelada",
                    "numero_cancelacion", "cargo_cancelacion", "ayuda_cancelada", "despedida_cancelada"}


def test_los_borradores_de_hr374_estan_en_los_tres_idiomas_y_apagados_hasta_su_ok() -> None:
	assert texts.BORRADORES_OK is False, "con el OK del fundador a la tanda de HR-374, cambia esta línea también"
	for idioma in ("es", "pt", "en"):
		assert set(texts.BORRADORES[idioma]) == BORRADORES_HR374, idioma
		assert all(v.strip() for v in texts.BORRADORES[idioma].values()), idioma


def test_cada_texto_del_correo_lleva_su_fuente() -> None:
	assert set(texts.FUENTES) == set(texts.TEXTOS["es"])
	assert set(texts.FUENTES.values()) == {"HR-175", "D-HR-48"}
	for clave in (*NUEVOS_DEL_CORREO, "iva"):
		assert texts.FUENTES[clave] == "D-HR-48", clave
	assert {k for k, v in texts.FUENTES.items() if v == "HR-175"} == set(texts.DE_LA_WEB) - {"iva"}


def test_los_de_la_web_siguen_siendo_los_que_el_correo_copia_de_reservar_md() -> None:
	# la guarda literal contra la web vive en kamra_uy/tests/test_confirmation_mail.py; aquí, que la lista no se mezcle
	# con los nuevos del correo
	assert not set(texts.DE_LA_WEB) & set(NUEVOS_DEL_CORREO)
	assert set(texts.DE_LA_WEB) | set(NUEVOS_DEL_CORREO) == set(texts.TEXTOS["es"])


# HR-374 (D-HR-44): solo el fichero que declara los borradores de su tanda; se quita cuando BORRADORES_OK pase a True. En
# los demás, el nombre del interruptor (texts.BORRADORES, texts.BORRADORES_OK) no es una marca: un texto pendiente sí.
BORRADORES_DECLARADOS = {"notifications/texts.py"}
INTERRUPTOR = re.compile(r"\btexts\.BORRADORES(_OK)?\b")


def test_ningun_fichero_de_kamra_uy_marca_un_texto_como_pendiente_del_fundador() -> None:
	marca = re.compile(r"borrador|hasta el OK del fundador|para el OK del fundador|esperan el OK", re.I)
	malos = []
	for f in sorted(PAQUETE.rglob("*")):
		if f.suffix not in (".py", ".md") or "tests" in f.relative_to(PAQUETE).parts:
			continue
		if f.relative_to(PAQUETE).as_posix() in BORRADORES_DECLARADOS:
			continue
		for n, linea in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
			if marca.search(INTERRUPTOR.sub("", linea)):
				malos.append(f"{f.relative_to(PAQUETE)}:{n}: {linea.strip()}")
	assert malos == []


def test_el_correo_dice_que_la_factura_se_entrega_en_el_check_in() -> None:
	for idioma, (pagado_total, factura) in PAGO_Y_FACTURA.items():
		assert texts.TEXTOS[idioma]["pagado_total"] == pagado_total, idioma
		assert texts.TEXTOS[idioma]["factura"] == factura, idioma
		assert texts.FUENTES["pagado_total"] == texts.FUENTES["factura"] == "D-HR-48"

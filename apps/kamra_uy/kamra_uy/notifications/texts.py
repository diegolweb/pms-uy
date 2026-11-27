"""Los textos del correo de confirmación de la reserva en línea (HR-227), en es, pt y en.

Todos están aprobados (HR-282: el fundador aprobó todos los textos el 22-11, D-HR-48), y cada uno lleva su fuente en
FUENTES:
- DE_LA_WEB: los que el correo repite, literales de web/content/{es,pt,en}/reservar.md: el título y el resumen de la
  página de confirmada (fuente «HR-175») y el aviso del IVA a residentes, que con D-HR-47 (HR-266: el 10 % se paga al
  reservar) es el `iva.aviso` nuevo de reservar.md (fuente «D-HR-48»). Si cambian allí, cambian aquí (la guarda es
  kamra_uy/tests/test_confirmation_mail.py).
- Los nuevos del correo (asunto, saludo, entrada, horario, ayuda, despedida, y pagado_total y factura, que pidió el
  fundador el 23-11: el correo es el aviso del pago y la factura se entrega en el check-in; y recibo, HR-349: el enlace
  al recibo del hotel de ese pago, D-HR-74): fuente «D-HR-48». Lo que
  no le guste al fundador se cambia después.

Aquí no va ningún dato del hotel (kamra_uy se publica como AGPL): el nombre, la dirección y el contacto salen de la
Property. Español neutro, portugués de Brasil con «você», inglés internacional; «check-in» y «check-out» se quedan así.
"""

from __future__ import annotations

DE_LA_WEB = ("confirmada", "referencia", "llegada", "salida", "noches", "habitacion", "huespedes", "total", "pagado",
             "direccion", "contacto", "iva")

TEXTOS = {
	"es": {
		# de la web (HR-175): estado.confirmada, estado.referencia, resumen.*, estado.pagado, estado.direccion,
		# estado.contacto, iva.aviso
		"confirmada": "Reserva confirmada",
		"referencia": "Referencia",
		"llegada": "Llegada",
		"salida": "Salida",
		"noches": "Noches",
		"habitacion": "Habitación",
		"huespedes": "Huéspedes",
		"total": "Total",
		"pagado": "Pagado",
		"direccion": "Dirección",
		"contacto": "Contacto",
		"iva": "Los huéspedes con documento uruguayo deberán abonar el 10% de IVA adicional al momento de la reserva "
		       "por las noches entre el 16 de noviembre y Semana de Turismo.",
		# los nuevos del correo (aprobados con D-HR-48)
		"asunto": "Tu reserva en {hotel} está confirmada",
		"saludo": "Hola, {nombre}:",
		"intro": "Recibimos tu pago y tu reserva está confirmada. Estos son los datos de tu estadía.",
		"horario": "Check-in desde las {entrada} · Check-out hasta las {salida}",
		"ayuda": "Si necesitas cambiar algo de tu reserva, escríbenos.",
		"despedida": "Te esperamos en {hotel}.",
		# el aviso del pago no es un comprobante fiscal (23-11, el fundador): «pagado_total» solo si se pagó todo
		"pagado_total": "Pagaste el total de tu estadía.",
		"factura": "La factura te la entregamos en el check-in.",
		"recibo": "El recibo de tu pago es el {numero}.",
	},
	"pt": {
		"confirmada": "Reserva confirmada",
		"referencia": "Referência",
		"llegada": "Chegada",
		"salida": "Saída",
		"noches": "Noites",
		"habitacion": "Quarto",
		"huespedes": "Hóspedes",
		"total": "Total",
		"pagado": "Pago",
		"direccion": "Endereço",
		"contacto": "Contato",
		"iva": "Os hóspedes com documento uruguaio deverão pagar 10% de IVA adicional no momento da reserva pelas "
		       "noites entre 16 de novembro e a Semana de Turismo (Semana Santa).",
		"asunto": "Sua reserva no {hotel} está confirmada",
		"saludo": "Olá, {nombre},",
		"intro": "Recebemos o seu pagamento e a sua reserva está confirmada. Estes são os dados da sua estadia.",
		"horario": "Check-in a partir das {entrada} · Check-out até as {salida}",
		"ayuda": "Se você precisar alterar algo na sua reserva, fale com a gente.",
		"despedida": "Esperamos você no {hotel}.",
		"pagado_total": "Você pagou o total da sua estadia.",
		"factura": "A fatura é entregue no check-in.",
		"recibo": "O recibo do seu pagamento é o {numero}.",
	},
	"en": {
		"confirmada": "Booking confirmed",
		"referencia": "Reference",
		"llegada": "Arrival",
		"salida": "Departure",
		"noches": "Nights",
		"habitacion": "Room",
		"huespedes": "Guests",
		"total": "Total",
		"pagado": "Paid",
		"direccion": "Address",
		"contacto": "Contact",
		"iva": "Guests with a Uruguayan ID or passport must pay an additional 10% VAT when booking for nights between "
		       "November 16 and Tourism Week (Holy Week).",
		"asunto": "Your booking at {hotel} is confirmed",
		"saludo": "Hello {nombre},",
		"intro": "We have received your payment and your booking is confirmed. Here are the details of your stay.",
		"horario": "Check-in from {entrada} · Check-out by {salida}",
		"ayuda": "If you need to change anything in your booking, get in touch.",
		"despedida": "We look forward to welcoming you at {hotel}.",
		"pagado_total": "You have paid the full amount of your stay.",
		"factura": "We will give you the invoice at check-in.",
		"recibo": "Your payment receipt is {numero}.",
	},
}

# De dónde sale el OK de cada texto: «HR-175», los literales de la web de HR-175; «D-HR-48», el aviso del IVA de D-HR-47
# y los nuevos del correo, que el fundador aprobó en bloque el 22-11.
FUENTES = {k: "HR-175" if k in DE_LA_WEB and k != "iva" else "D-HR-48" for k in TEXTOS["es"]}
APROBADOS = tuple(FUENTES)  # todos: con D-HR-48 ya no hay lista de los que esperan al fundador

# La cancelación (HR-291): la arma kamra_uy.notifications.cancelacion con los campos de la Property
# (`free_cancel_days`, `cancellation_fee`, `no_show_charge`), sin cifras fijas: `{dias}` es «5 días» o «1 día» (`dias` o `dia`, el singular). Los de
# CANCELACION_DE_LA_WEB, con 5 días y «Full Stay», son literales de la Sección 1 de web/content/{es,pt,en}/condiciones.md
# (el titular h2 «Cancelación» y sus dos textos; la guarda es kamra_uy/tests/unit/test_correo_cancelacion.py); los
# demás son los otros valores de Kamra, nuevos del correo. Todos aprobados con D-HR-48.
CANCELACION_DE_LA_WEB = ("titulo", "gratis_hasta", "despues_full_stay", "no_show_full_stay")

CANCELACION = {
	"es": {
		"titulo": "Cancelación",
		"dia": "{n} día",
		"dias": "{n} días",
		"gratis_hasta": "Puedes cancelar sin costo hasta {dias} antes de la llegada: te devolvemos el total.",
		"gratis_hasta_la_llegada": "Puedes cancelar sin costo hasta el día de la llegada: te devolvemos el total.",
		"despues_full_stay": "Si cancelas después, se cobra el 100% de la estadía y no hay devolución.",
		"despues_first_night": "Si cancelas después, se cobra la primera noche.",
		"no_reembolsable": "Esta reserva no es reembolsable: si cancelas, se cobra el 100% de la estadía y no hay "
		                   "devolución.",
		"no_show_full_stay": "Si no llegas y no nos avisas, se cobra el 100% de la estadía y no hay devolución.",
		"no_show_first_night": "Si no llegas y no nos avisas, se cobra la primera noche.",
		"no_show_none": "Si no llegas y no nos avisas, no se cobra nada.",
		# los tramos de Property.cancellation_tiers (HR-301): {dias} = «7 días», {cobro} = uno de los cobro_*
		"tramos_intro": "Lo que se cobra si cancelas depende de cuántos días antes de la llegada lo hagas:",
		"tramo_desde": "{dias} o más antes: {cobro}.",
		"tramo_entre": "De {desde} a {hasta} antes: {cobro}.",
		"tramo_exacto": "{dias} antes: {cobro}.",
		"tramo_menos": "Menos de {dias} antes: {cobro}.",
		"tramo_llegada": "El día de la llegada: {cobro}.",
		"tramo_siempre": "Si cancelas, en cualquier momento, {cobro}.",
		"cobro_nada": "no se cobra nada y te devolvemos el total",
		"cobro_parte": "se cobra el {pct} de la estadía",
		"cobro_total": "se cobra el {pct} de la estadía y no hay devolución",
	},
	"pt": {
		"titulo": "Cancelamento",
		"dia": "{n} dia",
		"dias": "{n} dias",
		"gratis_hasta": "Você pode cancelar sem custo até {dias} antes da chegada: devolvemos o valor total.",
		"gratis_hasta_la_llegada": "Você pode cancelar sem custo até o dia da chegada: devolvemos o valor total.",
		"despues_full_stay": "Se cancelar depois, é cobrado 100% da estadia e não há devolução.",
		"despues_first_night": "Se cancelar depois, é cobrada a primeira noite.",
		"no_reembolsable": "Esta reserva não é reembolsável: se cancelar, é cobrado 100% da estadia e não há "
		                   "devolução.",
		"no_show_full_stay": "Se você não chegar e não nos avisar, é cobrado 100% da estadia e não há devolução.",
		"no_show_first_night": "Se você não chegar e não nos avisar, é cobrada a primeira noite.",
		"no_show_none": "Se você não chegar e não nos avisar, nada é cobrado.",
		"tramos_intro": "O valor cobrado se você cancelar depende de quantos dias antes da chegada você cancelar:",
		"tramo_desde": "{dias} ou mais antes: {cobro}.",
		"tramo_entre": "De {desde} a {hasta} antes: {cobro}.",
		"tramo_exacto": "{dias} antes: {cobro}.",
		"tramo_menos": "Menos de {dias} antes: {cobro}.",
		"tramo_llegada": "No dia da chegada: {cobro}.",
		"tramo_siempre": "Se você cancelar, a qualquer momento, {cobro}.",
		"cobro_nada": "nada é cobrado e devolvemos o valor total",
		"cobro_parte": "é cobrado {pct} da estadia",
		"cobro_total": "é cobrado {pct} da estadia e não há devolução",
	},
	"en": {
		"titulo": "Cancellation",
		"dia": "{n} day",
		"dias": "{n} days",
		"gratis_hasta": "You can cancel free of charge up to {dias} before arrival: we refund the full amount.",
		"gratis_hasta_la_llegada": "You can cancel free of charge until the day of arrival: we refund the full amount.",
		"despues_full_stay": "If you cancel later, 100% of the stay is charged and there is no refund.",
		"despues_first_night": "If you cancel later, the first night is charged.",
		"no_reembolsable": "This booking is non-refundable: if you cancel, 100% of the stay is charged and there is "
		                   "no refund.",
		"no_show_full_stay": "If you do not arrive and do not let us know, 100% of the stay is charged and there is no "
		                     "refund.",
		"no_show_first_night": "If you do not arrive and do not let us know, the first night is charged.",
		"no_show_none": "If you do not arrive and do not let us know, nothing is charged.",
		"tramos_intro": "What you are charged if you cancel depends on how many days before arrival you do it:",
		"tramo_desde": "{dias} or more before: {cobro}.",
		"tramo_entre": "{desde} to {hasta} before: {cobro}.",
		"tramo_exacto": "{dias} before: {cobro}.",
		"tramo_menos": "Less than {dias} before: {cobro}.",
		"tramo_llegada": "On the day of arrival: {cobro}.",
		"tramo_siempre": "If you cancel, at any time, {cobro}.",
		"cobro_nada": "nothing is charged and we refund the full amount",
		"cobro_parte": "{pct} of the stay is charged",
		"cobro_total": "{pct} of the stay is charged and there is no refund",
	},
}

# Los días (de domingo a sábado) y los meses de las fechas, los mismos que `fechas.dias` y `fechas.meses` de
# web/content/*/reservar.md y DIAS/MESES de web/src/booking/reglas.ts (aprobados con D-HR-48): «Vie 8 ene 2027».
DIAS = {
	"es": ("Dom", "Lun", "Mar", "Mié", "Jue", "Vie", "Sáb"),
	"pt": ("Dom", "Seg", "Ter", "Qua", "Qui", "Sex", "Sáb"),
	"en": ("Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"),
}
MESES = {
	"es": ("ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"),
	"pt": ("jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez"),
	"en": ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"),
}

# HR-374: el nombre de cada habitación en portugués y en inglés, el titular h2 de su sección en
# web/content/{pt,en}/habitaciones.md (aprobados con la web, D-HR-30), por el código del tipo en Kamra (room_type_code, el
# que casa la web con Kamra: CODIGOS_TIPO de web/src/booking/reglas.ts, HR-273). En español va el nombre de Kamra, como
# hasta ahora; un tipo que no está aquí, también. La guarda contra la web es tests/unit/test_textos_aprobados.py.
HABITACIONES_DE_LA_WEB = {
	"pt": {"doble-camas-separadas": "Quarto Duplo Solteiro", "doble-matrimonial": "Quarto Duplo Superior",
	       "triple": "Quarto Triplo", "apartamento": "Apartamento para 5 pessoas"},
	"en": {"doble-camas-separadas": "Twin Room", "doble-matrimonial": "Superior Double Room",
	       "triple": "Triple Room", "apartamento": "Apartment for 5 people"},
}
CODIGOS_TIPO = {"doble-camas-separadas": ("DOBLE", "TWN"), "doble-matrimonial": ("DOBLESUPERIOR", "DBL"),
                "triple": ("TRIPLE",), "apartamento": ("APARTAMENTO",)}

# HR-374, los textos NUEVOS de los correos al huésped (el fundador, 26-11, pidió la confirmación de las reservas que carga
# recepción y el aviso de cancelación; y el texto del enlace de WhatsApp del pie en su idioma, que la web solo tiene en
# español). Son BORRADORES para su tanda de OK (D-HR-44): mientras BORRADORES_OK sea False no sale ninguno (ni esos dos
# correos, ni el texto de WhatsApp en pt y en, que sigue el de la web). Con su OK, el hub pone BORRADORES_OK = True.
BORRADORES_OK = True
BORRADORES = {
	"es": {
		"whatsapp": "¡Hola! Para solicitar una reserva:",  # el de la web (inicio.md), aprobado: no es borrador en es
		"intro_recepcion": "Tu reserva está confirmada. Estos son los datos de tu estadía.",
		"asunto_cancelada": "Tu reserva en {hotel} está cancelada",
		"cancelada": "Reserva cancelada",
		"intro_cancelada": "Cancelamos tu reserva. Estos eran sus datos.",
		"numero_cancelacion": "Número de cancelación",
		"cargo_cancelacion": "Cargo por la cancelación",
		"ayuda_cancelada": "Si tienes una duda sobre la cancelación, escríbenos.",
		"despedida_cancelada": "Esperamos verte pronto en {hotel}.",
	},
	"pt": {
		"whatsapp": "Olá! Para solicitar uma reserva:",
		"intro_recepcion": "A sua reserva está confirmada. Estes são os dados da sua estadia.",
		"asunto_cancelada": "Sua reserva no {hotel} está cancelada",
		"cancelada": "Reserva cancelada",
		"intro_cancelada": "Cancelamos a sua reserva. Estes eram os dados dela.",
		"numero_cancelacion": "Número do cancelamento",
		"cargo_cancelacion": "Valor cobrado pelo cancelamento",
		"ayuda_cancelada": "Se você tiver alguma dúvida sobre o cancelamento, fale com a gente.",
		"despedida_cancelada": "Esperamos ver você em breve no {hotel}.",
	},
	"en": {
		"whatsapp": "Hello! To request a booking:",
		"intro_recepcion": "Your booking is confirmed. Here are the details of your stay.",
		"asunto_cancelada": "Your booking at {hotel} has been cancelled",
		"cancelada": "Booking cancelled",
		"intro_cancelada": "We have cancelled your booking. These were its details.",
		"numero_cancelacion": "Cancellation number",
		"cargo_cancelacion": "Cancellation charge",
		"ayuda_cancelada": "If you have any question about the cancellation, get in touch.",
		"despedida_cancelada": "We hope to see you soon at {hotel}.",
	},
}


def habitacion(nombre_kamra: str, codigo: str | None, idioma: str) -> str:
	"""El nombre de la habitación en el idioma del correo: en pt y en, el de la web por su código; si no, el de Kamra."""
	codigo = (codigo or "").upper()
	for clave, codigos in CODIGOS_TIPO.items():
		if codigo in codigos:
			return HABITACIONES_DE_LA_WEB.get(idioma, {}).get(clave) or nombre_kamra
	return nombre_kamra

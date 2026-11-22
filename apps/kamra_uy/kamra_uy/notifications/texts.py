"""Los textos del correo de confirmación de la reserva en línea (HR-227), en es, pt y en.

- APROBADOS: los de la web que el correo repite, literales de web/content/{es,pt,en}/reservar.md (HR-175, fuera de
  «borradores»): el título y el resumen de la página de confirmada y el aviso del IVA a residentes (D-HR-43). Si cambian
  allí, cambian aquí (la guarda es kamra_uy/tests/test_confirmation_mail.py).
- BORRADORES: los nuevos del correo (asunto, saludo, entrada, horario, ayuda, despedida). Esperan el OK del fundador
  (D-HR-44); hasta entonces salen tal cual, como los borradores de la web en dev y en el gate.

Aquí no va ningún dato del hotel (kamra_uy se publica como AGPL): el nombre, la dirección y el contacto salen de la
Property. Español neutro, portugués de Brasil con «você», inglés internacional; «check-in» y «check-out» se quedan así.
"""

from __future__ import annotations

APROBADOS = ("confirmada", "referencia", "llegada", "salida", "noches", "habitacion", "huespedes", "total", "pagado",
             "direccion", "contacto", "iva")

TEXTOS = {
	"es": {
		# aprobados (HR-175): estado.confirmada, estado.referencia, resumen.*, estado.pagado, estado.direccion,
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
		"iva": "Los huéspedes residentes en Uruguay deberán abonar el 10% de IVA adicional al momento del check-in "
		       "entre el 16 de noviembre y Semana de Turismo.",
		# borradores (D-HR-44)
		"asunto": "Tu reserva en {hotel} está confirmada",
		"saludo": "Hola, {nombre}:",
		"intro": "Recibimos tu pago y tu reserva está confirmada. Estos son los datos de tu estadía.",
		"horario": "Check-in desde las {entrada} · Check-out hasta las {salida}",
		"ayuda": "Si necesitas cambiar algo de tu reserva, escríbenos.",
		"despedida": "Te esperamos en {hotel}.",
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
		"iva": "Os hóspedes residentes no Uruguai deverão pagar 10% de IVA adicional no momento do check-in entre 16 "
		       "de novembro e a Semana de Turismo (Semana Santa).",
		"asunto": "Sua reserva no {hotel} está confirmada",
		"saludo": "Olá, {nombre},",
		"intro": "Recebemos o seu pagamento e a sua reserva está confirmada. Estes são os dados da sua estadia.",
		"horario": "Check-in a partir das {entrada} · Check-out até as {salida}",
		"ayuda": "Se você precisar alterar algo na sua reserva, fale com a gente.",
		"despedida": "Esperamos você no {hotel}.",
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
		"iva": "Guests residing in Uruguay must pay an additional 10% VAT at check-in between November 16 and Tourism "
		       "Week (Holy Week).",
		"asunto": "Your booking at {hotel} is confirmed",
		"saludo": "Hello {nombre},",
		"intro": "We have received your payment and your booking is confirmed. Here are the details of your stay.",
		"horario": "Check-in from {entrada} · Check-out by {salida}",
		"ayuda": "If you need to change anything in your booking, get in touch.",
		"despedida": "We look forward to welcoming you at {hotel}.",
	},
}

BORRADORES = tuple(k for k in TEXTOS["es"] if k not in APROBADOS)

# Los días (de domingo a sábado) y los meses de las fechas, los mismos que `fechas.dias` y `fechas.meses` de
# web/content/*/reservar.md y DIAS/MESES de web/src/booking/reglas.ts (en borrador en HR-175): «Vie 8 ene 2027».
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

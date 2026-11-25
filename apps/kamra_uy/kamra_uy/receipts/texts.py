"""Los textos del recibo del hotel (HR-349, D-HR-74), en es, pt y en. Sin frappe.

El recibo es lo que el hotel entrega al cobrar una reserva antes del check-in (la web, el enlace de pago o recepción):
no es un CFE, y la factura se emite en el check-in (D-HR-74, respuesta 4 del estudio contable). La devolución de un
pago con recibo deja su constancia, con el importe negativo.

Aprobados con todos los textos (D-HR-48, 22-11): lo que no le guste al fundador se cambia después. Aquí no va ningún
dato del hotel (kamra_uy se publica como AGPL): el emisor, su RUT y la reserva salen de la Property y de la reserva.
Español neutro, portugués de Brasil con «você», inglés internacional; «check-in» se queda así.
"""

from __future__ import annotations

TEXTOS = {
	"es": {
		"recibo": "Recibo",
		"devolucion": "Constancia de devolución",
		"numero": "Número",
		"fecha": "Fecha",
		"emisor": "Emisor",
		"rut": "RUT",
		"pagador": "Recibimos de",
		"devuelto_a": "Devolvimos a",
		"concepto": "Concepto",
		"concepto_pago": "Pago de la reserva {reserva}, estadía del {llegada} al {salida}",
		"concepto_devolucion": "Devolución del pago de la reserva {reserva} (recibo {recibo}), estadía del {llegada} "
		                       "al {salida}",
		"importe": "Importe",
		"cobrado": "Cobrado en {moneda}",
		"aviso_recibo": "Este recibo no es una factura. La factura se emite en el check-in.",
		"aviso_devolucion": "Esta constancia no es un comprobante fiscal.",
		"no_encontrado": "No encontramos este recibo. Revisa el enlace del correo o escríbenos.",
	},
	"pt": {
		"recibo": "Recibo",
		"devolucion": "Comprovante de devolução",
		"numero": "Número",
		"fecha": "Data",
		"emisor": "Emitente",
		"rut": "RUT",
		"pagador": "Recebemos de",
		"devuelto_a": "Devolvemos a",
		"concepto": "Referente a",
		"concepto_pago": "Pagamento da reserva {reserva}, estadia de {llegada} a {salida}",
		"concepto_devolucion": "Devolução do pagamento da reserva {reserva} (recibo {recibo}), estadia de {llegada} "
		                       "a {salida}",
		"importe": "Valor",
		"cobrado": "Cobrado em {moneda}",
		"aviso_recibo": "Este recibo não é uma fatura. A fatura é emitida no check-in.",
		"aviso_devolucion": "Este comprovante não é um documento fiscal.",
		"no_encontrado": "Não encontramos este recibo. Confira o link do e-mail ou fale com a gente.",
	},
	"en": {
		"recibo": "Receipt",
		"devolucion": "Refund note",
		"numero": "Number",
		"fecha": "Date",
		"emisor": "Issued by",
		"rut": "RUT",
		"pagador": "Received from",
		"devuelto_a": "Refunded to",
		"concepto": "For",
		"concepto_pago": "Payment for booking {reserva}, stay from {llegada} to {salida}",
		"concepto_devolucion": "Refund of the payment for booking {reserva} (receipt {recibo}), stay from {llegada} "
		                       "to {salida}",
		"importe": "Amount",
		"cobrado": "Charged in {moneda}",
		"aviso_recibo": "This receipt is not an invoice. The invoice is issued at check-in.",
		"aviso_devolucion": "This note is not a tax document.",
		"no_encontrado": "We couldn't find this receipt. Check the link in the email or contact us.",
	},
}

# el nombre de la moneda cobrada en otra que la del enlace (Mercado Pago cobra en pesos, D-HR-68)
MONEDAS = {
	"es": {"UYU": "pesos", "USD": "dólares"},
	"pt": {"UYU": "pesos", "USD": "dólares"},
	"en": {"UYU": "pesos", "USD": "dollars"},
}

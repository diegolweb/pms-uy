"""Los textos de cobros que ve el personal (HR-30), en español neutro. Borradores hasta el OK del fundador.

Sin frappe: los usan también los módulos que se prueban sin él.
"""

NOT_CONFIGURED = "Falta configurar la dirección del servicio de cobros (hr_core_base_url)."
SETTINGS_INCOMPLETE = (
    "Faltan el código de la propiedad o la clave del servicio de cobros en los ajustes de la pasarela Mercado Pago."
)
UNAVAILABLE = "Mercado Pago no responde. Prueba de nuevo en unos minutos."
LINK_FAILED = "No se pudo crear el enlace: {reason}."
CURRENCY = "Solo se puede cobrar en pesos o en dólares."
REFUND_REJECTED = "Mercado Pago rechazó la devolución: {reason}."
REFUND_REQUESTED = "Devolución pedida a Mercado Pago. Aparecerá en el folio cuando la confirme."

REFUND_REASON = "Devolución por Mercado Pago"
CHARGEBACK_REASON = "Contracargo de Mercado Pago"

# el correo a Finanzas cuando un movimiento de Mercado Pago no se puede asentar solo
REVIEW_SUBJECT = "Mercado Pago: un movimiento para revisar en {property}"
REVIEW_BODY = (
    "<p>Llegó {kind} de Mercado Pago que hay que revisar.</p>"
    "<p>Folio: {folio}<br>Importe: {amount}</p>"
    "<p>Qué hacer: {action}</p>"
)
KIND_PAYMENT = "un cobro"
KIND_REFUND = "una devolución"
KIND_CHARGEBACK = "un contracargo"
ACTION_FOLIO_CLOSED = "el folio está cerrado: regístralo con una nota de crédito en un folio nuevo."
ACTION_CHARGEBACK = "revisa el contracargo en el panel de Mercado Pago."
ACTION_CURRENCY = "la moneda del movimiento no es la de la propiedad: asiéntalo a mano con su tipo de cambio."
ACTION_NO_FOLIO = "el folio no existe en Kamra: búscalo por la referencia del pago en el panel de Mercado Pago."
ACTION_LATE_PAYMENT = (
    "la reserva ya estaba cancelada cuando llegó el pago (por ejemplo, venció la retención): si hay disponibilidad, "
    "reactívala a mano; si no, devuelve el pago desde el folio."
)

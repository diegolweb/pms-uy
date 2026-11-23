"""Textos del conector fiscal (HR-28). Los que ve un huésped o recepción están aprobados: el fundador aprobó todos los
textos el 22-11 (D-HR-48, HR-282; README.md, «Textos aprobados»). Los de las notas de crédito salen en el CFE."""

# líneas del CFE (las ve el huésped en el comprobante)
NIGHT = "Alojamiento {date} · {room_type}"
EXTRA_GUESTS = "Huéspedes adicionales {date}"
MEAL_PLAN = "Plan de comidas {date}"
DISCOUNT = "Descuento de la reserva"

# motivos de las notas de crédito (salen en el CFE)
REASON_CLOSE = "Ajuste de la estancia al cerrar la cuenta"
REASON_CHECK_IN_UNDONE = "Check-in anulado"
REASON_VOID = "Factura anulada: {reason}"

# recepción y Finanzas
NOT_CONFIGURED = ("La facturación electrónica no está configurada para esta propiedad (credenciales del servicio de "
                  "facturación en Payment Gateway Settings).")
NO_FX = "No hay tipo de cambio del peso uruguayo para hoy: cárguelo en Finanzas, Tipos de cambio."
BAD_CURRENCY = "La moneda {currency} no se puede facturar con CFE (solo UYU o USD)."
# HR-304: sin la residencia no se factura (nunca se supone); el comprobante espera y sale solo en cuanto se indica
RESIDENCE_MISSING = ("Falta la residencia del huésped: indique en el check-in si reside en Uruguay y, si no, su país. "
                     "El comprobante fiscal sale en cuanto esté.")
COPY_MISSING = ("Falta la copia del documento del huésped: sin ella no corresponde el IVA 0 de no residente.")
POSTED_AT_OTHER_RATE = "El folio tiene {count} cargos con otro IVA que el del CFE: revíselos con Finanzas."

# /facturas (HR-304)
NOT_IN_CORE_YET = ("Este comprobante todavía no llegó al servicio de facturación: se podrá registrar cuando llegue (se "
                   "reintenta solo).")
ALREADY_ISSUED = "Este comprobante ya está emitido: no hay nada que registrar."
TYPE_PENDING = "Comprobante (tipo por decidir)"

"""Textos del conector fiscal (HR-28). Los que ve un huésped o recepción son BORRADORES hasta el OK del fundador (README.md,
«Textos para el OK del fundador»); los de las notas de crédito salen en el CFE."""

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
RESIDENCE_MISSING = ("Falta el país de residencia del huésped: se factura como residente (IVA de alojamiento). "
                     "Cárguelo antes del check-in si el huésped no reside en Uruguay.")
COPY_MISSING = ("Falta la copia del documento del huésped: sin ella no corresponde el IVA 0 de no residente.")
POSTED_AT_OTHER_RATE = "El folio tiene {count} cargos con otro IVA que el del CFE: revíselos con Finanzas."

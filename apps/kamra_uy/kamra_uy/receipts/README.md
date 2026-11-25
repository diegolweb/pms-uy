# kamra_uy.receipts — el recibo del hotel al cobrar la reserva (HR-349, D-HR-74)

El estudio contable (D-HR-74, respuesta 4): «con la reserva corresponde recibo siempre»; la factura, al prestar el
servicio (en el check-in, D-HR-32). El recibo **no es un CFE** ni pasa por core (no es un CAE).

## Cuándo sale

Cada cobro es una línea nueva del folio (`Folio.payments`): el aviso `payment.approved` de core (web, tarjeta de la
web y enlace de pago, `kamra.payments.settle_payment_link`) y recepción. El on_update de Folio
(`issue.on_folio_update`) mira solo las líneas que trae ese guardado:

- **Recibo**: `Payment` o `Advance` con importe positivo y la reserva Retenida, Pago pendiente o Confirmada.
  Después del check-in el dinero lo cubre la factura; la fianza (`Security Deposit`) no lleva recibo; el pago tardío
  de una reserva ya cancelada lo revisa Finanzas (HR-31) y no lleva recibo.
- **Constancia de devolución**: una devolución (`Refund`) de un pago con recibo (por `refund_of` en recepción, o por
  la referencia `refund:<pago>:<id>` / `chargeback:<pago>` de la pasarela), antes del check-in o con la reserva
  cancelada o no presentada. El importe va negativo. Después del check-in, lo devuelto va en la nota de crédito.

## Numeración

`R-<n>`, correlativa por Property y sin huecos: pagos y devoluciones en la misma serie. El número sale bloqueando la
fila de la Property hasta el final de la transacción; si el cobro se deshace, el número se deshace con él. La base lo
guarda dos veces: `property`+`number` único y `payment_row` único (una línea de pago, un recibo: el mismo aviso dos
veces no hace otro). El documento se llama `<código corto de la Property>-R-<n>`. Nadie crea, cambia ni borra un recibo
a mano (el doctype solo se lee y se imprime).

## Qué lleva

El número y la fecha (la de la línea de pago), el emisor (`legal_name` y `gstin` de la Property: la razón social y el
RUT; sin razón social, el nombre de la Property), el pagador (el huésped de la reserva), el concepto («Pago de la
reserva N, estadía del dd/mm/aaaa al dd/mm/aaaa»), el importe y la moneda del enlace y, si Mercado Pago cobró en pesos
(D-HR-68), lo cobrado en pesos. Y el aviso: «Este recibo no es una factura. La factura se emite en el check-in.» Es una
foto del momento del cobro: si después cambia la Property o la reserva, el recibo no cambia.

## Dónde se ve

- El huésped: el correo de confirmación lo enlaza (`notifications/confirmation.py`, «El recibo de tu pago es el R-n.»)
  a `/recibo?n=…&k=…` (`www/recibo.py`), en el idioma de la reserva y lista para imprimir. Sin la clave del recibo
  (32 caracteres al azar) la página no dice nada de él.
- Recepción y Finanzas: en el escritorio, la reserva enseña sus recibos en sus conexiones
  (`override_doctype_dashboards`), y la lista de UY Receipt, por Property (`kamra.authz`, HR-152). El React de
  recepción de Kamra no los enseña (no llama a kamra_uy, como los CFE: HR-304).

## Si no puede salir

Nunca frena el cobro: se deshace solo el recibo (savepoint) y va una fila al Error Log del folio con el título
«kamra_uy: recibo del cobro de la reserva sin emitir» y solo el tipo del error.

## Si Gsoft trae recibos

Si su API los emite (pregunta del fundador a Gsoft), cambia `issue.issue` y nada más.

Pruebas: `tests/unit/receipts/test_receipt_model.py` (las reglas, sin base) y `receipts/tests/test_receipts.py` (la
numeración, el aviso repetido, la devolución y el correo, con `bench run-tests`).

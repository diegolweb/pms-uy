# Conector fiscal de Kamra (HR-28)

Kamra pide a core (`services/core`, HR-25 y HR-26) el IVA de cada línea y el CFE ante la DGI. Este paquete es el
módulo de Frappe «Fiscal» de kamra_uy (sus doctypes viven en `doctype/`).

## Cuándo pasa qué (D-HR-32: la factura sale en el CHECK-IN, por la estancia entera)

| Momento en Kamra | Gancho (hooks.py) | Qué hace |
|---|---|---|
| Check-in (`Reservation.validate`, con el huésped en casa) | `events.on_reservation_validate` | `determineIva` de cada noche con el país de residencia, el documento y si su copia está archivada; deja la tasa en `night_rates` (lo que Kamra cobra). Core caído: sigue el IVA del paquete, sin parar el check-in. |
| Check-in (`on_update`, el estado pasa a Checked In) | `events.on_reservation_update` | Tarea «Check-in» en UY CFE + cola tras el commit → CFE de la estancia, línea a línea. |
| Cierre de la cuenta (`Folio.on_update` → Closed) | `events.on_folio_update` | Tarea «Close»: lo de todas las cuentas cerradas de la reserva contra lo ya facturado → CFE de ajuste (prolongación) o nota de crédito (salida anticipada, descuento, IVA corregido). Con el huésped en casa (factura intermedia) solo añade. |
| Anular la factura (`Cancelled Invoice.after_insert`) | `events.on_invoice_cancelled` | Tarea «Void»: nota de crédito de lo que deja de estar facturado; al volver a cerrar, la tarea «Close» factura la cuenta corregida. |
| Deshacer el check-in (Checked In → Confirmed, Cancelled o No Show) | `events.on_reservation_update` | Tarea «Void»: nota de crédito. |
| Se corrige la residencia o el documento del huésped en casa | `events.on_guest_update` | El IVA de las noches que faltan; el CFE se corrige al cerrar. |
| Cada 5 minutos | `sync.sweep` (scheduler_events) | Reintenta lo pendiente y pregunta a core (`getCfe`) por los CFE en cola o en contingencia sin aviso en 10 minutos. |
| Core avisa `cfe.updated` | `webhook.core_event` | Serie, número, CAE, QR, estado en la DGI o contingencia en UY CFE. |

La conciliación (`model.reconcile`) es la misma para todo: lo que debería estar facturado (la estancia o las cuentas
cerradas) contra lo que cubren los CFE menos sus notas de crédito, tasa a tasa. Las notas de crédito salen antes que el
CFE nuevo. Las filas de una reserva se procesan en orden y se para en la primera que no sale.

## Estados de UY CFE

- `Pending`: falta mandarla (core caído, sin credenciales, en cola). Se ve en el folio como pendiente.
- `Sent`: core la aceptó; `core_status` sigue a core (`queued`, `issued`, `rejected`, `contingency`).
- `Done` / `Not needed`: una tarea que pidió CFE o notas de crédito, o que no hizo falta.
- `Needs attention`: core la rechazó (4xx) o falta algo que no se arregla solo (moneda que no es UYU ni USD, sin tipo
  de cambio del peso). Finanzas la revisa y la reintenta (`api.retry`) o registra lo emitido a mano (`api.record_manual`).

## Lo que lee de Kamra

- Huésped: `Guest.residence_country` (campo de HR-23), `id_type`, `id_number`; copia del documento =
  `Reservation.id_document` (sin descartar) o `Guest.id_file`. Sin país de residencia se factura como residente y queda
  marcado (`residence_assumed`).
- País del documento: Kamra no lo guarda. La cédula («National ID») es de Uruguay, el RG de Brasil; el DNI, el
  pasaporte y los demás toman el país de residencia (el DNI solo para AR, BR, CL y PY).
- Comprador: e-Factura si la reserva es de una empresa (`Corporate Account.gstin`) con un RUT válido; si no, e-Ticket.
- Tipo de cambio: la cotización del BCU que carga Kamra (`Exchange Rate` de UYU, `market_rate`); la propiedad en UYU
  manda `1`.
- Credenciales: el Payment Gateway Settings de la propiedad (HR-30): `key_id` (la propiedad en core), `key_secret`
  (clave con `fiscal:write`) y `webhook_secret`. `hr_core_base_url` en site_config.

## Para recepción y Finanzas (`api.py`)

`folio_cfe` (bloque CFE del folio o su contingencia), `preview` (el IVA que llevará cada noche y qué falta),
`process_now`, `retry` y `record_manual`. `api.invoice_print_block` tiene la firma de `invoice_print_block` de los
paquetes de Kamra: el paquete de Uruguay la puede exponer tal cual cuando la factura impresa pinte el bloque.

## Textos para el OK del fundador (borradores)

Los de `texts.py` y los de `model.py` (`CONSUMER_NAME`, `ADJUSTMENT_TEXT`, `VERIFY_TEXT`, `TYPE_TITLES`,
`STATUS_TEXTS`). Salen en el CFE (líneas y motivos de nota de crédito) o en el folio.

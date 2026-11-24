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
- Tipo de cambio: la cotización del BCU que carga Kamra (`Exchange Rate`, `market_rate`; `connector.fx_quote`): pesos
  por dólar. El CFE en pesos manda `1`.

## La moneda de la factura (HR-316, D-HR-57 y D-HR-60)

Los precios van siempre en la moneda de la propiedad (dólares): la tarifa, la web, la reserva, lo que cotiza recepción,
el folio y el IVA que decide `determineIva`. Solo el CFE puede ir en la otra moneda:
- Recepción la elige en la última pantalla del check-in de Kamra (kamra.localization, «the currency a stay is invoiced
  in»: `api.invoice_currency` da las dos monedas, la propuesta y la cotización de cada una; `kamra.api.check_in` pasa la
  elegida a `api.set_invoice_currency`, que la deja en `res.flags`). Por defecto, dólares si el check-in cae del 16 de
  noviembre al domingo de Turismo (`model.proposed_currency`, con `kamra_uy.pure.turismo`) y pesos el resto del año.
- La tarea «Check-in» de UY CFE guarda la moneda (`currency`); las de cierre y anulación siguen con esa
  (`connector.invoice_currency`). Se elige antes de emitir: después del check-in el CFE ya está pedido.
- En otra moneda que la de la reserva, `model.convert_lines` convierte línea a línea con la cotización del BCU
  (`model.conversion`, con los tipos de cambio a 6 decimales que lleva el CFE) y cada línea guarda en `source_amount` lo
  que era en la de la reserva: la conciliación (`model.coverage`, `model.reconcile`) cuenta en la de la reserva y la nota
  de crédito devuelve en la de su CFE (`invoice_amount`), a la proporción de cada línea. El CFE guarda
  `source_currency` y `source_fx_rate`; /facturas enseña el tipo de cambio que hay que copiar en el portal
  (`model.fx_note`). El CFE en pesos no lleva tipo de cambio (la DGI lo pide solo en moneda extranjera).
- Qué cotización pide la DGI (de qué día y de qué tipo) lo confirma el contador: HUECO H16 de
  `docs/operacion/huecos.md`.
- Credenciales: el Payment Gateway Settings de la propiedad (HR-30): `key_id` (la propiedad en core), `key_secret`
  (clave con `fiscal:write`) y `webhook_secret`. `hr_core_base_url` en site_config.

## Para recepción y Finanzas (`api.py`)

`folio_cfe` (bloque CFE del folio o su contingencia), `preview` (el IVA que llevará cada noche y qué falta),
`process_now`, `retry` y `record_manual`. `api.invoice_print_block` tiene la firma de `invoice_print_block` de los
paquetes de Kamra: el paquete de Uruguay la puede exponer tal cual cuando la factura impresa pinte el bloque.

En la pantalla de recepción (HR-307), el paquete de Uruguay expone `api.folio_fiscal_documents` (la tarjeta del CFE en
el folio: `model.desk_block`, con los estados de Kamra «pending», «contingency», «issued» y «attention», la serie y el
número, el PDF y el aviso de la residencia) y `api.desk_pages` («Facturas pendientes» en el menú de recepción). La
factura impresa de Kamra no cambia: sigue siendo el «Detalle de cuenta», que no es el comprobante fiscal.

## Textos aprobados

Los de `texts.py` y los de `model.py` (`CONSUMER_NAME`, `ADJUSTMENT_TEXT`, `VERIFY_TEXT`, `TYPE_TITLES`,
`STATUS_TEXTS`, y los de la moneda de HR-316: `CURRENCY_TITLE`, `CURRENCY_LABELS`, `PROPOSED_USD`, `PROPOSED_UYU`,
`FX_NOTE`, `CONVERTED_NOTE`, `FX_LINE`, `CONVERTED_LINE`). Salen en el CFE (líneas y motivos de nota de crédito) o en el folio. Fuente: D-HR-48 (22-11), con la
que el fundador aprobó todos los textos (HR-282); lo que no le guste se cambia después.

# Cobros con Mercado Pago (Uruguay)

La pasarela «Mercado Pago» del paquete de Uruguay. Kamra **no habla con Mercado Pago**: le pide el enlace a un
servicio de cobros aparte y recibe de él avisos firmados. Este módulo es la mitad de Kamra de ese recorrido.

## El recorrido

1. **Enlace.** Recepción pulsa «Enviar enlace de pago» (`kamra.api.folio_payment_link`) o pide la seña
   (`kamra.api.deposit_payment_link`). Kamra llama a `mercadopago.create_link`, que hace
   `POST /v1/payments/links` al servicio de cobros con:
   `property` (el código de la propiedad en el servicio), `folio`, `reservation`, `amount` (cadena con dos decimales),
   `currency` (`UYU` o `USD`), `purpose` (`deposit` antes de la llegada y sin cargos, `balance` con cargos o con el
   huésped en casa, `other` sin reserva), `payer` (`name` y, si hay un correo válido, `email`), `return_url` y, si la
   reserva tiene una retención entre 5 minutos y 30 días, `expires_at`. Cada llamada lleva su propia
   `Idempotency-Key`. El `link_id` y la `url` de la respuesta quedan en el folio (`payment_link_id`,
   `payment_link_url`).
2. **Pago.** El huésped paga en Mercado Pago y vuelve a `/pago`, que solo le dice en qué quedó (aprobado, en proceso o
   no completado), en español, portugués o inglés según su navegador. **No confirma nada**: lo confirma el aviso.
3. **Aviso.** El servicio de cobros manda un `POST` a `/api/method/kamra_uy.payments.webhook.core_event` con
   `X-HR-Signature: t=<segundos>,v1=<hex>` (HMAC-SHA256 de `"<t>." + cuerpo crudo` con el secreto de la propiedad,
   ±300 s), `X-HR-Event-Id` y `X-HR-Event-Type`. Tres tipos:
   - `payment.approved` → una fila de cobro en el folio con `reference` = `payment_id` y el `mode` del aviso
     (`Tarjeta local`, `Tarjeta extranjera` o `Payment Link`); antes de la llegada es una seña (`Advance`) y confirma
     una reserva retenida.
   - `payment.refunded` → una fila `Refund` negativa con `reference` = `refund:<payment_id>:<refund_id>`.
   - `payment.charged_back` → una fila `Refund` por lo que quedaba del pago (lo cobrado menos lo ya devuelto), con
     `reference` = `chargeback:<payment_id>`; siempre queda para que lo revise una persona.
4. **Folio.** Lo asienta Kamra (`kamra.payments.settle_payment_link` y `kamra.payments.record_gateway_refund`), sin
   caja (no pasó por el cajón) y sin el límite de saldo a favor de una devolución de mostrador: el dinero ya se movió.

## Deduplicación

- Cada aviso se guarda en **UY Core Event** con su `event_id` (`Applied`, `Duplicate`, `Ignored` o
  `Needs attention`). El mismo aviso otra vez responde `200 {"duplicate": true}` sin tocar nada.
- Además, cada pago se asienta **una vez por `payment_id`** (un enlace puede pagarse dos veces: son dos filas) y cada
  devolución una vez por su referencia.
- Respuestas: cuerpo que no es un aviso → `400`; firma mala, propiedad desconocida, `X-HR-Event-Id` distinto del del
  cuerpo o un folio de **otra** propiedad → `403`; un error inesperado → `500` sin guardar nada (el servicio reintenta
  todo lo que no sea 2xx).
- Lo que no tiene arreglo reintentando responde `200`, queda en `Needs attention` y manda un correo a los usuarios
  activos con el rol «Finance»: folio cerrado (va con una nota de crédito en un folio nuevo), moneda distinta de la de
  la propiedad, folio que no existe y todo contracargo.

## Devoluciones desde Kamra

`kamra.payments.refund_via_gateway(folio, reference, amount, reason)` (roles «Finance» y «Front Desk») comprueba que
el importe no pase de lo que queda de ese pago y llama a `mercadopago.refund`, que hace
`POST /v1/payments/{payment_id}/refunds`. **No asienta nada**: la fila la pone el aviso `payment.refunded`, así el
folio tiene un solo escritor para el dinero de la pasarela.

## Configuración

- `site_config.json`: `hr_core_base_url` (la dirección del servicio de cobros; sin ella, recepción lee qué falta) y,
  opcional, `hr_payment_return_url` (por defecto, `/pago` de este sitio).
- **Payment Gateway Settings** de la propiedad, pasarela «Mercado Pago»:
  - `key_id`: el código de la propiedad en el servicio de cobros;
  - `key_secret`: la clave del servicio, con permisos para cobrar y leer;
  - `webhook_secret`: el secreto con el que el servicio firma los avisos de esa propiedad.
- En modo prueba (`test_mode`) Kamra no pide enlace: lo simula y la página de vuelta dice que no se cobró nada.

## Qué queda para otras fichas

- El cobro de la seña en la reserva web (HR-31): usa `link_expires_at` con la retención y puede fijar su propia
  `return_url`.
- La prueba con el sandbox de Mercado Pago (HR-89) y el país de la tarjeta por su BIN (HR-91; hoy llega nulo y el
  modo es `Payment Link`).
- La ayuda de la tarjeta de ajustes de la pasarela (HR-123), el botón «Devolver por Mercado Pago» del folio (HR-124) y
  el comprobante de la seña (HR-125).

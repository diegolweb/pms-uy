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
  la propiedad, folio que no existe y todo contracargo. Si el correo no puede salir (sin cuenta de correo saliente,
  como en hr-gate), el aviso se guarda igual y el motivo queda en el Error Log del aviso (HR-224).

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

## La seña de la reserva web

Cuando la propiedad tiene la pasarela «Mercado Pago» habilitada, la reserva de `/kamra/book` cobra la seña en línea
(Kamra: `kamra.public_api.book`):

- La reserva queda en `Pending Payment` con su retención (`hold_minutes` de la propiedad, nunca menos de 10 minutos con
  cobro en línea) y el enlace caduca con ella (`link_expires_at`).
- Mercado Pago devuelve al huésped a `/kamra/book?booking=<token>`: Kamra pasa esa `return_url` a `create_link`, que la
  manda al servicio en lugar de la de `/pago`. El token solo deja leer el estado de esa reserva
  (`kamra.public_api.booking_status`), sin ningún dato del huésped.
- Una web propia que use el mismo `book` puede pedir su propia `return_url`, solo en un origen que liste la
  configuración del sitio (`booking_return_origins`, por ejemplo `["https://reservas.example.com"]`).
- La seña de una noche es el modo «First night» de la propiedad: el total de la estadía dividido por sus noches; una
  temporada con su propio porcentaje de seña manda sobre él.
- El pago que llega tarde (la retención ya venció y Kamra canceló la reserva, o la reserva quedó como no-show): se
  asienta si el folio sigue abierto, para poder devolverlo desde ahí, y el aviso queda en «Needs attention» con un
  correo a Finanzas. La reserva **no** se reactiva sola: su habitación pudo venderse otra vez.

## La tarjeta y el saldo dentro de la web del hotel (HR-222)

La web del hotel (HR-220) cobra la reserva retenida sin mandar al huésped a Mercado Pago: el formulario de tarjeta de
Mercado Pago (Card Payment Brick) crea en el navegador un **token de la tarjeta**, y la web se lo pasa a Kamra con el
token de estado de la reserva. Kamra pide el cobro a core (HR-221), que es el único que habla con Mercado Pago. Para el
saldo de la cuenta de Mercado Pago, que Mercado Pago solo cobra en su propia web, Kamra pide a core una preferencia que
solo admite el saldo y la web lleva al huésped allí. Lo que el hotel cobra al reservar es lo que da la regla de la
propiedad: con `booking_payment_mode = Full online` (D-HR-38), **el total**.

Tres métodos públicos (`/api/method/kamra_uy.payments.card.<método>`, sin sesión, solo `POST`):

| Método | Límite | Devuelve |
|---|---|---|
| `checkout(token)` | 240/h | `card` (bool), `public_key` (la de Mercado Pago de la propiedad, o null), `amount`, `currency`, `charge` (lo que cobra la tarjeta en pesos: `amount`, `currency`, `exchange_rate`; o null), `hold_expires_on`, `code` |
| `pay(token, card_token, payment_method_id, issuer_id, payer_email, identification_type, identification_number, device_id, charge_amount, charge_currency)` | 20/h | `status` (`approved`, `pending`, `in_process`, `rejected` o `error`), `status_detail` (el de Mercado Pago), `payment_id`, `three_ds` (`external_resource_url` y `creq` si el banco pide autorización), `code` |
| `wallet(token, return_url)` | 10/h | `url` (la de Mercado Pago para pagar con el saldo), `code` |

- **El importe lo pone el servidor.** Las tres calculan lo que falta (`advance_due − advance_paid`) y usan el enlace del
  folio si es por ese importe; si no, piden uno nuevo (`kamra.public_api._deposit_link`, como `booking_payment_link`).
  core cobra el importe del enlace. `pay` no tiene parámetro de importe: lo que mande el navegador se descarta.
- **La tarjeta cobra en pesos** (HR-366, D-HR-80): Mercado Pago Uruguay cobra la tarjeta en UYU, así que core cobra el
  importe en dólares del enlace en pesos, al cambio que da Mercado Pago, y el folio sigue en dólares con lo cobrado y el
  cambio (D-HR-68). `checkout` da esos pesos en `charge` para que la web los enseñe, y `pay` los devuelve en
  `charge_amount`/`charge_currency` (requeridos, `invalid` si faltan): core solo los compara y, si el cambio se movió,
  responde `charge_changed` sin cobrar. Sin `charge` (core no tuvo el cambio), `card` es false.
- **Un enlace, dos caminos.** El cobro con tarjeta y la preferencia del saldo cuelgan del mismo enlace que la preferencia
  de siempre, así que la confirmación de la reserva, el aviso firmado, la conciliación, el pago tardío y la devolución
  desde el folio (HR-124) valen igual.
- **Idempotencia.** `pay` manda `Idempotency-Key = sha256("<link_id>:<token de la tarjeta>")`: el mismo token, el mismo
  cobro (un doble clic no cobra dos veces). `wallet` manda una por enlace, y core devuelve la misma preferencia.
- **Códigos, no textos** (la web los traduce; `kamra_uy.pure.payments.CARD_CODES`):
  - `hold_expired`: la retención venció; no se pidió nada a core.
  - `link_not_payable`: no falta nada por pagar, o core dice que el enlace ya no está pendiente.
  - `card_not_enabled`: la propiedad no tiene Mercado Pago, o core no tiene su `public_key`.
  - `too_many_attempts`: core rechaza la tarjeta tras 5 rechazos en el mismo enlace (queda el saldo).
  - `charge_changed`: el importe en pesos ya no es el que se enseñó (Mercado Pago movió el cambio); no se cobró, la web
    vuelve a pedir `checkout` y lo enseña.
  - `unavailable`: core o Mercado Pago no responden; se puede reintentar.
  - `invalid`: la petición tiene algo mal (un campo vacío o largo, una `return_url` de otro sitio, un 4xx de core).
- `checkout` da `card: false` (y `code` nulo) si la propiedad no cobra con tarjeta: la web ofrece entonces el botón a
  Mercado Pago de siempre. Su `code` solo dice por qué no se puede pagar en absoluto.
- Un token de estado falso es `PermissionError` (HTTP 403), como en `booking_status`.
- **Ningún dato de tarjeta en los logs.** Kamra nunca ve la tarjeta, solo su token de un solo uso. El Error Log de un
  fallo lleva la reserva, el enlace y el estado y el código de core; un fallo inesperado, su traza sin variables y con el
  token y el documento tachados (lo prueba `test_card` con valores centinela).
- Lo que se importa de `kamra.public_api` (`_res_by_status_token`, `_hold_is_live`, `_guest_folio`, `_deposit_link`,
  `_return_url`) es código del hotel (HR-31); un test fija sus firmas.

## Qué queda para otras fichas

- La prueba con el sandbox de Mercado Pago (HR-89) y el país de la tarjeta por su BIN (HR-91; hoy llega nulo y el
  modo es `Payment Link`).
- La ayuda de la tarjeta de ajustes de la pasarela (HR-123), el botón «Devolver por Mercado Pago» del folio (HR-124) y
  el comprobante de la seña (HR-125).
- El paso de pago de la web con la tarjeta y el botón del saldo (HR-220) y su prueba con Mercado Pago de verdad (HR-223).

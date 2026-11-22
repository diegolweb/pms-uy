# kamra_uy.notifications — el correo de confirmación de la reserva en línea (HR-227)

La página de confirmada de la web promete «Te enviamos la confirmación por email»; Kamra no la manda (`book` no avisa a
nadie). La manda kamra_uy.

## Cuándo sale

Cuando una reserva con `source = Website` pasa de **Pending Payment** o **Held** a **Confirmed** con dinero cobrado
(`advance_paid > 0`). Es lo que hace el aviso firmado `payment.approved` de core (`kamra_uy.payments.webhook`): asienta
el pago y Kamra confirma (`kamra.api._confirm_status`). Sale **una vez**: lo marca el campo `uy_confirmation_mail_on`
al encolarlo. El mismo aviso repetido, otro pago, otro guardado o una vuelta a retenida con otra confirmación no lo
repiten. No sale si el huésped no dejó correo, si la reserva no es de la web, si recepción la confirma sin cobrar o si
llega un pago tardío (la reserva ya cancelada no se reactiva, HR-31).

## En qué idioma

En el de la página con que se reservó. La web manda `_lang` (es, pt o en) en cada llamada a Kamra. Al crear la
reserva web (`before_insert`) se guarda en `uy_booking_language`. Dentro de `book`, `form_dict` ya no lo tiene
(`kamra.authz.act_as` llama a `frappe.set_user`, que lo vacía), así que se lee de `frappe.local.lang`, que Frappe fijó
con él al empezar la petición. Con otro idioma, nada. Sin idioma guardado, el de la propiedad (`booking_language`) y,
sin ese, español.

## Qué lleva

- El título «Reserva confirmada», el saludo y la entrada.
- La referencia, la llegada, la salida, las noches, la habitación (el nombre del tipo en Kamra) y los huéspedes.
- El total y «Pagado», o el total y lo pagado si no se pagó todo, con las cifras y fechas de la web
  (`formato.py` = `importe` y `fechaCorta` de web/src/booking/reglas.ts).
- El aviso del IVA a residentes si **alguna noche** cae entre el 16 de noviembre y el domingo de Pascua siguiente
  (`kamra_uy.pure.turismo`, D-HR-40, los mismos vectores que la web).
- El horario de check-in y check-out, la dirección y el contacto de la Property. Responder va al correo de la Property
  (Reply-To).

Nunca lleva un dato del pago: ni el modo, ni la referencia, ni la tarjeta. La identidad (logo, letra, colores y pie) se
la pone HR-32 con `kamra_uy.branding.correo.envolver`; sin marca aplicada, el cuerpo sale tal cual.

## Los textos

Están en `texts.py`. Los APROBADOS son literales de HR-175 (web/content/*/reservar.md). Los BORRADORES (asunto,
saludo, entrada, horario, ayuda, despedida) esperan el OK del fundador (D-HR-44).

## Si no puede salir

Nunca frena la confirmación. Si no hay cuenta de correo saliente, `frappe.sendmail` lanza `OutgoingEmailError`. Pasa
en hr-gate y en producción hasta HR-75. Entonces se deshace solo el correo (savepoint), la reserva queda Confirmed y
sin marca, y va una fila al Error Log con el título «kamra_uy: correo de confirmación de la reserva en línea sin
enviar». La fila lleva la reserva y solo el tipo del error, sin destinatario ni traza. Ese correo no se reintenta.
Con `mute_emails`, Frappe sí lo encola y lo envía al quitarlo.

## Los campos

`uy_booking_language` (Data) y `uy_confirmation_mail_on` (Datetime) van en la Reservation, de solo lectura y del módulo
Kamra UY. Los crea `confirmation.ensure_fields` en `after_install` y `after_migrate` (idempotente), no
`fixtures/custom_field.json`.

Pruebas: `kamra_uy/tests/test_confirmation_mail.py`. Lo corre `bench run-tests --app kamra_uy`.

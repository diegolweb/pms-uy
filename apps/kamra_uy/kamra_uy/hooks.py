app_name = "kamra_uy"
app_title = "Kamra UY"
app_publisher = "Hotel Roosevelt"
app_description = "Localización uruguaya de Kamra PMS para el Hotel Roosevelt"
app_email = "kamra-uy@hotel-roosevelt.test"
app_license = "agpl-3.0"

# kamra_uy extiende Kamra: se instala después de kamra en el mismo sitio.
required_apps = ["kamra"]

# Paquete de país de Kamra (kamra.localization.pack_for_country): toma el ÚLTIMO valor del hook, y kamra_uy se instala
# después de kamra, así que este reclama «Uruguay» (vendor/kamra/kamra/hooks.py, kamra_localization).
kamra_localization = {"Uruguay": "kamra_uy.localization.uruguay"}

# Campos propios (tasas de IVA de la propiedad, país de residencia del huésped) y las opciones de modo de pago que
# añaden «Tarjeta local» y «Tarjeta extranjera». Solo lo del módulo Kamra UY: nunca lo de otra app.
fixtures = [
	{"dt": "Custom Field", "filters": [["module", "=", "Kamra UY"]]},
	{"dt": "Property Setter", "filters": [["module", "=", "Kamra UY"]]},
]

# Alcance por propiedad (HR-152): los doctypes de kamra_uy con Link `property` a Property siguen la regla de los de
# Kamra (_PROPERTY_SCOPED en vendor/kamra/kamra/hooks.py, kamra.authz). Con kamra_strict_property_scope, quien no tiene
# User Permission de Property (salvo Administrator y System Manager) no los lista ni los lee, y una fila sin propiedad
# solo la ve quien ve toda la cadena; sin la bandera no añaden nada y filtra el User Permission de Frappe por el Link.
# Frappe suma los hooks de todas las apps. Quien escribe con ignore_permissions (el receptor de core, sin sesión; el
# botón de /contingencia) y lee con get_all no pasa por aquí. Van como literales: los tests unitarios leen este fichero
# con ast.literal_eval, sin importar frappe.
permission_query_conditions = {
	"UY Contingency Settings": "kamra.authz.property_query_conditions",
	"UY Contingency Snapshot": "kamra.authz.property_query_conditions",
	"UY Core Event": "kamra.authz.property_query_conditions",
}
has_permission = {
	"UY Contingency Settings": "kamra.authz.property_has_permission",
	"UY Contingency Snapshot": "kamra.authz.property_has_permission",
	"UY Core Event": "kamra.authz.property_has_permission",
}

# Modo contingencia sin sincronización (HR-34). El informe diario sale a las 07:00 de la propiedad: Frappe evalúa los
# cron en la zona del SISTEMA, así que el trabajo corre cada hora y kamra_uy.pure.contingency.is_daily_report_hour
# decide. Cada cambio de reserva recalcula sus fechas y avisa solo de lo nuevo (kamra_uy/contingency/alerts.py).
# Conector fiscal (HR-28): el barrido reintenta los CFE pendientes y pone al día los que esperan su aviso.
# Monitoreo (HR-21): el latido del programador, en cada tick (Frappe 16 encola los cron cada 4 min); si envejece, la
# señal «programador» de /api/method/kamra_uy.health.status se pone roja y el vigía de deploy/monitoring avisa.
scheduler_events = {
	"cron": {
		"* * * * *": ["kamra_uy.health.beat.heartbeat"],
		"0 * * * *": ["kamra_uy.contingency.alerts.daily_report"],
		"*/5 * * * *": ["kamra_uy.fiscal.sync.sweep"],
		# Las plataformas tampoco venden antes de la apertura (HR-264): cada hora, el cierre de venta de cada propiedad
		# corre su primera noche al cambiar el día y se retira al llegar la apertura (kamra_uy/booking/stop_sell.py).
		"5 * * * *": ["kamra_uy.booking.stop_sell.sync_all"],
	},
}

# Conector fiscal (HR-28, D-HR-32): el IVA de la estancia y su CFE en el check-in, el ajuste al cerrar la cuenta y la
# nota de crédito al anularla o al deshacer el check-in (kamra_uy/fiscal/events.py; nunca paran a recepción).
# Correo de confirmación de la reserva en línea (HR-227): la reserva web guarda al crearse el idioma de la página
# (before_insert) y, cuando un pago la confirma, encola un solo correo al huésped en ese idioma; nunca frena la
# confirmación (kamra_uy/notifications/confirmation.py).
# No vender antes de la apertura (HR-263): una reserva Website con llegada anterior a la fecha de apertura de la
# propiedad no se crea (kamra_uy/booking/opening.py); va primero, así una reserva que no se crea no apunta nada.
# Las plataformas tampoco venden antes de la apertura (HR-264): guardar, renombrar o borrar la Property pone al día su
# Channel Restriction stop sell de hoy a la víspera de la apertura (la de kamra_uy; las puestas a mano no se tocan), que
# el outbox de ARI de Kamra manda a Channex por sus propios doc_events; y esa fila no se edita ni se borra a mano
# (kamra_uy/booking/stop_sell.py).
doc_events = {
	"Reservation": {
		"before_insert": [
			"kamra_uy.booking.opening.on_reservation_before_insert",
			"kamra_uy.notifications.confirmation.remember_language",
		],
		"after_insert": "kamra_uy.contingency.alerts.on_reservation_change",
		"validate": "kamra_uy.fiscal.events.on_reservation_validate",
		"on_update": [
			"kamra_uy.contingency.alerts.on_reservation_change",
			"kamra_uy.fiscal.events.on_reservation_update",
			"kamra_uy.notifications.confirmation.on_reservation_update",
		],
		"on_cancel": "kamra_uy.contingency.alerts.on_reservation_change",
		"on_trash": "kamra_uy.contingency.alerts.on_reservation_change",
	},
	"Folio": {"on_update": "kamra_uy.fiscal.events.on_folio_update"},
	"Cancelled Invoice": {"after_insert": "kamra_uy.fiscal.events.on_invoice_cancelled"},
	"Guest": {"on_update": "kamra_uy.fiscal.events.on_guest_update"},
	"Property": {
		"on_update": "kamra_uy.booking.stop_sell.on_property_update",
		"after_rename": "kamra_uy.booking.stop_sell.on_property_rename",
		"on_trash": "kamra_uy.booking.stop_sell.on_property_trash",
	},
	"Channel Restriction": {
		"validate": "kamra_uy.booking.stop_sell.protect",
		"on_trash": "kamra_uy.booking.stop_sell.protect",
	},
	# Monitoreo (HR-21): cada Error Log a Sentry, sin datos personales (kamra_uy/health/sentry.py)
	"Error Log": {"after_insert": "kamra_uy.health.sentry.on_error_log"},
}

# Monitoreo (HR-21): el cliente de Sentry de cada proceso, con el filtro de datos personales, también si lo creó Frappe
# (FRAPPE_SENTRY_DSN); y el escritorio sin el DSN, para que ningún error del navegador salga sin filtro.
before_request = ["kamra_uy.health.sentry.setup"]
before_job = ["kamra_uy.health.sentry.setup"]
extend_bootinfo = ["kamra_uy.health.sentry.extend_bootinfo"]

# Los campos de la Reservation del correo de confirmación (HR-227): el idioma de la reserva web y cuándo salió el correo.
# Y la fecha de apertura de la Property (HR-263, `uy_opening_date`, vacía = sin restricción). Van por código y no en
# fixtures/custom_field.json, y se ponen al día al instalar y en cada migrate (idempotente).
after_install = ["kamra_uy.notifications.confirmation.ensure_fields", "kamra_uy.booking.opening.ensure_fields"]
after_migrate = ["kamra_uy.notifications.confirmation.ensure_fields", "kamra_uy.booking.opening.ensure_fields"]

# No vender antes de la apertura (HR-263, D-HR-39): la búsqueda y la reserva de la página pública (las de la web y las de
# /book de Kamra) pasan por el guardián de kamra_uy, que rechaza una llegada anterior a la fecha de apertura de la
# propiedad y si no llama a la de Kamra tal cual; la vitrina añade esa fecha (`uy_opening_date`) para que la web acote
# la llegada y diga la apertura (kamra_uy/booking/opening.py). Frappe usa el último valor del hook: kamra no los toca.
override_whitelisted_methods = {
	"kamra.public_api.showcase": "kamra_uy.booking.opening.showcase",
	"kamra.public_api.search_stay": "kamra_uy.booking.opening.search_stay",
	"kamra.public_api.book": "kamra_uy.booking.opening.book",
}

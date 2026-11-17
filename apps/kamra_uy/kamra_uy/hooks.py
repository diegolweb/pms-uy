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

# Modo contingencia sin sincronización (HR-34). El informe diario sale a las 07:00 de la propiedad: Frappe evalúa los
# cron en la zona del SISTEMA, así que el trabajo corre cada hora y kamra_uy.pure.contingency.is_daily_report_hour
# decide. Cada cambio de reserva recalcula sus fechas y avisa solo de lo nuevo (kamra_uy/contingency/alerts.py).
scheduler_events = {
	"cron": {
		"0 * * * *": ["kamra_uy.contingency.alerts.daily_report"],
	},
}

doc_events = {
	"Reservation": {
		"after_insert": "kamra_uy.contingency.alerts.on_reservation_change",
		"on_update": "kamra_uy.contingency.alerts.on_reservation_change",
		"on_cancel": "kamra_uy.contingency.alerts.on_reservation_change",
		"on_trash": "kamra_uy.contingency.alerts.on_reservation_change",
	},
}

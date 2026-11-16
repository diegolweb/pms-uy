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

"""Las funciones públicas de Kamra que el hotel cierra del todo (HR-374).

`kamra.public_api.hosting_enquiry` es el formulario comercial de la empresa de Kamra («¿quieres Kamra en tu hotel?»):
cualquiera podía llamarlo y hacer que el sistema mandara, con el remitente del hotel, un texto suyo al correo comercial de Kamra,
gastando el cupo diario de Resend que necesitan las confirmaciones. El hotel no lo usa.

El borde lo niega en su ruta (deploy/prod/hardening/borde.plantilla.yml: hrprod-cerrado-alojamiento), pero Frappe
también ejecuta una función nombrada en el `cmd` de cualquier petición (la forma vieja, en frappe/app.py), que el borde
no ve. Por eso hooks.py (override_whitelisted_methods) la cambia por esta, que a propósito NO lleva @frappe.whitelist:
Frappe contesta 403 («is not whitelisted») por las tres vías (/api/method, /api/v2/method y el `cmd`), sin llegar a
ejecutarla.
"""

import frappe


def hosting_enquiry(**kwargs):
	"""Nunca se llega aquí desde una petición (no está en la lista blanca); si se llamara desde dentro, tampoco envía."""
	frappe.throw("El formulario de alojamiento de Kamra está cerrado en este sitio.", frappe.PermissionError)

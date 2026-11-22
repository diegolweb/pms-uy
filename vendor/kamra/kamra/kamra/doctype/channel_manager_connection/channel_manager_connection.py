import frappe
from frappe import _
from frappe.model.document import Document

# Providers whose every call names the provider's property: for Channex, the
# booking feed (filter[property_id]: without it the feed is the whole
# account's), each ARI value (property_id) and the mapping import
# (GET properties/<id>).
NEEDS_PROPERTY_ID = ("Channex",)


def missing_property_id(conn) -> bool:
	"""True when `conn` (a document or a row with provider and
	external_property_id) is of a provider that cannot work without the
	provider's property id, and has none."""
	return conn.provider in NEEDS_PROPERTY_ID and not (conn.external_property_id or "").strip()


def no_property_id_message() -> str:
	return _("This connection has no provider's property ID yet: enter your property's ID in Channex first.")


class ChannelManagerConnection(Document):
	def validate(self):
		if self.provider in NEEDS_PROPERTY_ID and self.external_property_id:
			# pasted with a space, the feed filter and every ARI value would miss the property
			self.external_property_id = self.external_property_id.strip()
		if not self.active:
			return
		if missing_property_id(self):
			frappe.throw(_("Enter the provider's property ID (your property's ID in Channex) to activate this "
			               "connection: without it, availability and rates do not reach Channex and its "
			               "bookings do not come in."))
		# Inbound webhooks are refused until the connection can authenticate
		# them - say so up front instead of letting bookings silently bounce.
		if self.provider == "AioSell":
			missing = not (self.api_username and self.api_key)
			what = "API username and API key"
		else:
			missing = not self.webhook_secret
			what = "webhook secret"
		if missing:
			frappe.msgprint(
				f"Set the {what}: inbound bookings from {self.provider} are "
				"rejected until it is configured.",
				indicator="orange", alert=True)

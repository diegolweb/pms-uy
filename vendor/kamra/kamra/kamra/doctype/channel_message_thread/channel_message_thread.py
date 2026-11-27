# One row per guest conversation Channex brings in from an OTA (Booking.com,
# Expedia, Airbnb): ids, counts, who spoke last and the reservation it belongs
# to. No message text, title or guest data: the conversation is read live from
# Channex when the desk opens it (kamra.channel_messages).

from frappe.model.document import Document


class ChannelMessageThread(Document):
	pass

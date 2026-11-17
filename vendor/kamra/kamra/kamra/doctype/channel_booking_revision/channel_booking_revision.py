# One row per Channex booking revision the feed worker has seen: its status
# (Received -> Applied -> Acked, or Failed), attempts and outcome. Ids only:
# no payload and no guest data - an unacked revision stays in Channex's feed
# with its data, so a retry reads it from there.

from frappe.model.document import Document


class ChannelBookingRevision(Document):
	pass

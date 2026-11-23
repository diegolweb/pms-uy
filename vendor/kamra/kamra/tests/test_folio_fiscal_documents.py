"""A country pack may show the desk the tax receipt of a folio.

In some countries the receipt the tax office counts is not the hotel's own
bill: Uruguay's CFE is issued by an e-invoicing provider at check-in, and it
can wait (no answer yet, contingency, the guest's residence missing). A pack
like that implements folio_fiscal_documents (kamra.localization) and the
folio shows it in a card of its own, open or settled - never as the printed
ZATCA block, which stays `statutory`. A pack may also give the desk pages of
its own to open from the menu (desk_pages). A pack without them shows
nothing, and a pack that fails never stops the folio from opening.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from kamra import api
from kamra import localization as loc
from kamra.localization import generic
from kamra.tests.audit_fixtures import (
	PROP_A,
	TODAY,
	AuditTestCase,
	d,
	guest_folio,
	make_property,
	make_reservation,
	make_room,
	make_room_type,
)

ISSUED = {
	"title": "Comprobante fiscal",
	"state": "issued",
	"text": "",
	"notice": "",
	"documents": [{"label": "e-Ticket", "state": "issued", "number": "A 1234", "text": "",
	               "url": "https://example.org/cfe/1.pdf", "date": "2026-12-07"}],
	"link": None,
}


class TestFolioFiscalDocuments(AuditTestCase):
	def test_a_pack_without_it_shows_nothing(self):
		self.assertIsNone(loc.folio_fiscal_documents(generic, "FOLIO-X"))
		self.assertEqual(loc.desk_pages(generic, "P"), [])

	def test_what_the_pack_gives_is_what_the_desk_sees(self):
		pack = MagicMock(spec=["folio_fiscal_documents"])
		pack.folio_fiscal_documents.return_value = ISSUED
		self.assertEqual(loc.folio_fiscal_documents(pack, "FOLIO-X"), ISSUED)
		pack.folio_fiscal_documents.assert_called_once_with("FOLIO-X")
		pack.folio_fiscal_documents.return_value = None
		self.assertIsNone(loc.folio_fiscal_documents(pack, "FOLIO-X"))

	def test_a_state_the_desk_does_not_know_never_reads_as_issued(self):
		pack = MagicMock(spec=["folio_fiscal_documents"])
		pack.folio_fiscal_documents.return_value = {
			"title": "Receipt", "state": "sent?", "notice": "Ask where the guest lives.",
			"documents": [{"label": "e-Ticket", "state": None}],
			"link": {"label": "Pending invoices", "url": "/facturas?property=P"},
		}
		out = loc.folio_fiscal_documents(pack, "FOLIO-X")
		self.assertEqual(out["state"], "pending")
		self.assertEqual(out["text"], "")
		self.assertEqual(out["notice"], "Ask where the guest lives.")
		self.assertEqual(out["documents"], [{"label": "e-Ticket", "state": "pending", "number": None, "text": "",
		                                     "url": None, "date": None}])
		self.assertEqual(out["link"], {"label": "Pending invoices", "url": "/facturas?property=P"})

	def test_only_links_to_this_site_or_https_are_kept(self):
		pack = MagicMock(spec=["folio_fiscal_documents", "desk_pages"])
		pack.folio_fiscal_documents.return_value = {
			"title": "Receipt", "state": "issued",
			"documents": [{"label": "e-Ticket", "state": "issued", "url": "javascript:alert(1)"}],
			"link": {"label": "Pending invoices", "url": "//elsewhere.example/x"},
		}
		out = loc.folio_fiscal_documents(pack, "FOLIO-X")
		self.assertIsNone(out["documents"][0]["url"])
		self.assertIsNone(out["link"])
		pack.desk_pages.return_value = [
			{"label": "Pending invoices", "url": "/facturas?property=P", "roles": ["Front Desk"]},
			{"label": "Bad", "url": "javascript:alert(1)"},
			{"label": "", "url": "/x"},
		]
		self.assertEqual(loc.desk_pages(pack, "P"),
		                 [{"label": "Pending invoices", "url": "/facturas?property=P", "roles": ["Front Desk"]}])
		pack.desk_pages.assert_called_once_with("P")

	def test_a_pack_that_fails_never_stops_the_folio(self):
		pack = MagicMock(spec=["folio_fiscal_documents", "desk_pages"])
		pack.folio_fiscal_documents.side_effect = RuntimeError("provider down")
		pack.desk_pages.side_effect = RuntimeError("broken")
		with patch("frappe.log_error") as logged:
			self.assertIsNone(loc.folio_fiscal_documents(pack, "FOLIO-X"))
			self.assertEqual(loc.desk_pages(pack, "P"), [])
		self.assertEqual(logged.call_count, 2)


class TestOnTheFolio(AuditTestCase):
	properties = (PROP_A,)

	def setUp(self):
		super().setUp()
		self.at(TODAY, "kamra.folio", "kamra.business_date", "kamra.ledger")
		make_property(PROP_A, business_date=TODAY)
		self.rt = make_room_type(PROP_A, "STD", base_price=1000)
		self.room = make_room(PROP_A, self.rt, "101")
		self.pack = loc.pack_for(PROP_A)

	def test_an_open_folio_carries_the_receipt_and_keeps_statutory_for_the_print(self):
		res = make_reservation(PROP_A, self.rt, d(-1), d(1), room=self.room, status="Checked In")
		folio = guest_folio(res.name)
		fake = MagicMock(return_value=ISSUED)
		with patch.object(self.pack, "folio_fiscal_documents", fake, create=True):
			bill = api.folio_invoice(folio)
		fake.assert_called_once_with(folio)
		self.assertEqual(bill["fiscal_documents"], ISSUED)
		# not settled: nothing for the printed bill, as before
		self.assertIsNone(bill["statutory"])
		self.assertFalse(bill["document"]["is_final"])

	def test_a_property_whose_pack_has_none_gets_none(self):
		res = make_reservation(PROP_A, self.rt, d(-1), d(1), room=self.room, status="Checked In")
		bill = api.folio_invoice(guest_folio(res.name))
		self.assertIn("fiscal_documents", bill)
		self.assertIsNone(bill["fiscal_documents"])

	def test_the_locale_lists_the_pack_pages_for_the_menu(self):
		self.assertEqual(api.property_locale(PROP_A)["desk_pages"], [])
		pages = MagicMock(return_value=[{"label": "Pending invoices", "url": "/facturas?property=X"}])
		with patch.object(self.pack, "desk_pages", pages, create=True):
			self.assertEqual(api.property_locale(PROP_A)["desk_pages"],
			                 [{"label": "Pending invoices", "url": "/facturas?property=X", "roles": []}])
		pages.assert_called_once_with(PROP_A)

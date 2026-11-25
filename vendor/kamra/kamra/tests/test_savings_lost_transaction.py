# Copyright (c) 2026, HeyKoala and contributors
# For license information, please see license.txt

"""A transaction the database dropped is not swallowed by the savings ledger.

MariaDB 11.8 runs InnoDB with snapshot isolation: a locking read of a row that
another connection changed after this transaction took its snapshot fails with
1020 ("Record has changed since last read") and the whole transaction is rolled
back, savepoint included. Frappe raises it as QueryDeadlockError. Every
Agent Action Log row locks its tabSeries counter, so a log_action can be where
that happens, after the business action it describes was already written. If
log_action swallowed it and wrote its row again in a fresh transaction, the
caller would carry on and commit as if its writes were still there.

This runs against the real database, with no test records (a lost transaction
takes them with it) and no AuditTestCase (its savepoint would be lost too).
"""

import inspect

import frappe
from frappe.tests import IntegrationTestCase

from kamra.savings import log_action


def _series_key() -> str:
	"""The tabSeries row an Agent Action Log takes its name from, measured: name
	one row, see which counter moved, and roll it all back (Frappe's key for a
	format: rule is not necessarily the name's visible prefix)."""
	counters = "SELECT `name`, `current` FROM `tabSeries`"
	frappe.db.rollback()
	before = dict(frappe.db.sql(counters))
	name = log_action("hr365_series_probe", channel="API")
	moved = [k for k, v in frappe.db.sql(counters) if before.get(k) != v]
	frappe.db.rollback()
	if len(moved) != 1:
		raise AssertionError(f"naming {name} moved {len(moved)} tabSeries rows: {moved[:5]}")
	return moved[0]


def _second_connection():
	"""Another connection to the site's database, as a second request would have."""
	from frappe.database import get_db

	conf = frappe.conf
	wanted = {"socket": conf.db_socket, "host": conf.db_host, "port": conf.db_port,
	          "user": conf.db_user or conf.db_name, "password": conf.db_password,
	          "cur_db_name": conf.db_name}
	accepted = inspect.signature(get_db).parameters
	db = get_db(**{k: v for k, v in wanted.items() if k in accepted})
	db.connect()
	return db


class TestSavingsLostTransaction(IntegrationTestCase):
	def test_a_transaction_lost_under_log_action_raises_and_keeps_nothing(self):
		series = _series_key()
		witness = f"HR365PROBE-{frappe.generate_hash(length=8)}-"
		rationale = f"HR-365 lost transaction probe {frappe.generate_hash(length=10)}"
		bump = ("INSERT INTO `tabSeries` (`name`, `current`) VALUES (%s, 0) "
		        "ON DUPLICATE KEY UPDATE `current` = `current` + %s")

		other = _second_connection()
		self.addCleanup(other.close)
		self.addCleanup(frappe.db.rollback)
		# the counter row exists before the snapshot (committing a counter is harmless)
		other.sql(bump, (series, 0))
		other.commit()

		frappe.db.rollback()
		# a consistent read: this transaction's snapshot starts here
		frappe.db.sql("SELECT `current` FROM `tabSeries` WHERE `name` = %s", (series,))
		# another request names a log row in between, and commits
		other.sql(bump, (series, 1))
		other.commit()
		# the business action this transaction already wrote
		frappe.db.sql("INSERT INTO `tabSeries` (`name`, `current`) VALUES (%s, 0)", (witness,))

		try:
			out, lost = log_action("hr365_probe", rationale=rationale, channel="API"), None
		except frappe.QueryDeadlockError as exc:
			out, lost = None, exc
		witness_kept = bool(frappe.db.sql("SELECT 1 FROM `tabSeries` WHERE `name` = %s", (witness,)))
		logged = frappe.db.sql(
			"SELECT `name` FROM `tabAgent Action Log` WHERE `rationale` LIKE %s", (f"{rationale}%",))
		cause = lost.__cause__ if lost else None
		print(f"HR-365 V1: raised={type(lost).__name__ if lost else None} "
		      f"cause={type(cause).__name__ if cause else None} {getattr(cause, 'args', ())[:1]} "
		      f"returned={out} witness_kept={witness_kept} log_rows={len(logged)}")

		# the database dropped the whole transaction: the business write is gone
		self.assertFalse(witness_kept, "the transaction was not lost: the cause is another one")
		# log_action says so instead of carrying on
		self.assertIsNotNone(lost, f"log_action swallowed the lost transaction and returned {out}")
		# and no log row was written for it in a fresh transaction
		self.assertFalse(logged)

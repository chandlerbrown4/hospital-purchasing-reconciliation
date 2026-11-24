import csv
import shutil
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from evaluation.score_holdout import AMOUNTS, FIXTURE, ROOT, read_csv, route
from purchasing.pipeline import run


class DuplicateInvoiceTests(unittest.TestCase):
    def test_current_engine_on_every_original_scorecard_line(self):
        fixture = ROOT / FIXTURE
        with TemporaryDirectory() as folder:
            output = Path(folder)
            run([fixture / "batch"], fixture, output)
            with closing(sqlite3.connect(output / "reconciliation.sqlite")) as db:
                rows = db.execute("""
                    SELECT event_id, decision, delta_cents, unbacked_eaches, unbacked_po_cents
                    FROM invoice_lines
                """).fetchall()
            evidence_ids = {r["invoice_event_id"] for r in read_csv(output / "device_price_evidence.csv")}
        expected = {r["event_id"]: r for r in read_csv(fixture / "expected.csv")}
        self.assertEqual(len(rows), len(expected))
        self.assertEqual({r[0] for r in rows}, set(expected))
        for event_id, decision, *amounts in rows:
            with self.subTest(event_id=event_id):
                actual = ("review" if decision == "possible_duplicate_invoice"
                          else route({"engine_decision": decision, "active": 1}))
                self.assertEqual(actual, expected[event_id]["decision"])
                self.assertEqual(amounts, [int(expected[event_id][k]) if expected[event_id][k] else None
                                           for k in AMOUNTS])
        self.assertTrue({"O01", "C01", "C02"} <= evidence_ids)
        self.assertNotIn("O02", evidence_ids)

    def test_hold_does_not_consume_coverage_and_reversal_releases_surviving_copy(self):
        # The first import has a later lexical ID. Both representations charge a dollar high.
        fixture = ROOT / FIXTURE
        with TemporaryDirectory() as folder:
            tmp = Path(folder)
            batch = Path(shutil.copytree(fixture / "batch", tmp / "batch"))
            invoices = batch / "invoices.csv"
            fields = list(read_csv(invoices)[0]) + ["operation", "target"]
            rows = [dict(event_id="Z", vendor="Birch Goods", invoice="Original", po="B01",
                         po_line="1", sku="LAMP", date="2025-03-03", qty="3", unit="EA", unit_price="47")]
            rows.append(dict(rows[0], event_id="A", invoice="ORIGINAL"))
            rows.append(dict(rows[0], event_id="L", invoice="Other", date="2025-03-04", qty="9", unit_price="46"))
            for reversal in (False, True):
                if reversal:
                    rows.append(dict(event_id="REV", vendor="Birch Goods", date="2025-03-05",
                                     operation="reverse", target="Z"))
                with invoices.open("w", newline="") as sink:
                    writer = csv.DictWriter(sink, fields)
                    writer.writeheader()
                    writer.writerows(rows)
                results = run([batch], fixture, tmp / "output")
                with closing(sqlite3.connect(tmp / "output/reconciliation.sqlite")) as db:
                    states = dict(db.execute("SELECT event_id,decision FROM invoice_lines"))
                self.assertEqual(states["L"], "matched")
                self.assertEqual(results[0]["priced_delta_cents"], 300)
                if reversal:
                    self.assertNotIn("Z", states)
                    self.assertEqual(states["A"], "price_variance")
                else:
                    self.assertEqual(states["Z"], "price_variance")
                    self.assertEqual(states["A"], "possible_duplicate_invoice")

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
    def test_one_day_window_keeps_the_first_import_and_separate_supplier_documents(self):
        fixture = ROOT / FIXTURE
        with TemporaryDirectory() as folder:
            tmp = Path(folder)
            batch = Path(shutil.copytree(fixture / "batch", tmp / "batch"))
            original = {r["event_id"]: r for r in read_csv(batch / "invoices.csv")}
            for second_date, expected in (("2025-03-30", "review"), ("2025-03-31", "review"),
                                          ("2025-04-01", "review"), ("2025-04-02", "matched")):
                with self.subTest(second_date=second_date):
                    # C01/C02 have equal amounts on the same PO line and different invoice numbers.
                    data = [dict(original["O01"], date="2025-03-31"), dict(original["O02"], date=second_date),
                            original["C01"], dict(original["C02"], date="2025-03-04")]
                    with (batch / "invoices.csv").open("w", newline="") as sink:
                        writer = csv.DictWriter(sink, data[0])
                        writer.writeheader()
                        writer.writerows(data)
                    out = tmp / "out"
                    run([batch], fixture, out)
                    ap = {r["event_id"]: r for r in read_csv(out / "ap_invoice_rows.csv")}
                    self.assertEqual([ap[k]["disposition"] for k in ("O01", "O02", "C01", "C02")],
                                     ["matched", expected, "matched", "matched"])
                    evidence = {r["invoice_event_id"] for r in read_csv(out / "device_price_evidence.csv")}
                    self.assertEqual("O02" in evidence, expected == "matched")
                    if expected == "review":
                        self.assertIn("O01 (2025-03-31)", ap["O02"]["reason"])
                        self.assertIn(second_date, ap["O02"]["reason"])
                        self.assertEqual((ap["O02"]["actual_cents"], ap["O02"]["unbacked_eaches"]), ("13800", ""))

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

    def test_original_duplicate_with_explicit_claims_on_the_same_shipment(self):
        fixture = ROOT / FIXTURE
        with TemporaryDirectory() as folder:
            tmp = Path(folder)
            application_file = tmp / "applications.csv"
            application_file.write_text(
                "vendor_id,invoice_event_id,voucher_id,voucher_line,invoice,receipt_event_id,applied_eaches,applied_cents,billed_eaches\n"
                "BIRCH,O01,V01,1,bg402,R06,3,13800,3\n"
                "BIRCH,O02,V01,2,BG402,R06,3,13800,3\n"
            )
            run([fixture / "batch"], fixture, tmp / "out", receipt_applications=application_file)
            with closing(sqlite3.connect(tmp / "out/reconciliation.sqlite")) as db:
                rows = dict(db.execute("SELECT event_id, decision FROM invoice_lines"))
            self.assertEqual([rows[k] for k in ("O01", "O02", "C01", "C02")],
                             ["matched", "possible_duplicate_invoice", "matched", "matched"])

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

import csv
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from purchasing.pipeline import run


def feed(path, header, rows):
    with path.open("w", newline="") as dest:
        writer = csv.writer(dest)
        writer.writerow(header.split())
        writer.writerows(rows)


class SplitDeliveryTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.batch = self.base / "batch"
        self.batch.mkdir()
        self.apps = self.base / "applications.csv"
        feed(self.base / "vendors.csv", "alias vendor_id", [("Demo", "DEMO")])
        feed(self.base / "units.csv", "vendor_id vendor_sku device_id unit eaches", [("DEMO", "T", "Tray", "EA", 1)])
        feed(self.batch / "po_lines.csv", "event_id vendor po po_line sku date qty unit unit_price",
             [("P", "Demo", "P1", 1, "T", "2025-01-01", 5, "EA", "17.00")])
        self.shipments()
        feed(self.batch / "invoices.csv", "event_id vendor invoice po po_line sku date qty unit unit_price", [
            ("A", "Demo", "SP-1", "P1", 1, "T", "2025-01-04", 1, "EA", "17"),
            ("B", "Demo", "SP-1", "P1", 1, "T", "2025-01-04", 1, "EA", "17"),
            ("C", "Demo", "OTHER", "P1", 1, "T", "2025-01-04", 1, "EA", "17"),
        ])
        self.links = [
            ["DEMO", "A", "V1", "10", "SP-1", "S1", 1, 1700],
            ["DEMO", "B", "V1", "20", "SP-1", "S2", 1, 1700],
            ["DEMO", "C", "V2", "10", "OTHER", "S1", 1, 1700],
        ]

    def shipments(self, second_date="2025-01-06"):
        feed(self.batch / "receipts.csv", "event_id vendor po po_line sku date qty unit", [
            ("S1", "Demo", "P1", 1, "T", "2025-01-02", 2, "EA"),
            ("S2", "Demo", "P1", 1, "T", second_date, 1, "EA"),
            ("SPARE", "Demo", "P1", 1, "T", "2025-01-02", 1, "EA"),
        ])

    def replay(self, links):
        feed(self.apps, "vendor_id invoice_event_id voucher_id voucher_line invoice receipt_event_id applied_eaches applied_cents",
             links)
        run([self.batch], self.base, self.base / "out", receipt_applications=self.apps)
        with closing(sqlite3.connect(self.base / "out/reconciliation.sqlite")) as db:
            return {r[0]: r[1:] for r in db.execute("""
                SELECT i.event_id, i.decision, i.unbacked_eaches, a.reason
                FROM invoice_lines i JOIN row_audit a USING (audit_id)""")}

    def test_certified_split_uses_assigned_shipments_on_invoice_date(self):
        results = self.replay(self.links)
        self.assertEqual(results["A"][:2], ("matched", 0))
        self.assertEqual(results["B"][:2], ("insufficient_receipts", 1))
        self.assertIn("distinct split deliveries confirmed", results["B"][2])
        self.assertIn("V1 line 20, shipments: S2", results["B"][2])
        self.assertEqual(results["C"][:2], ("matched", 0))
        self.shipments(second_date="2025-01-04")
        self.assertEqual(self.replay(self.links)["B"][:2], ("matched", 0))

    def test_ambiguous_evidence_keeps_the_hold(self):
        scenarios = {
            "shared shipment": (1, 5, "S1"), "unknown shipment": (1, 5, "NONE"),
            "different voucher": (1, 2, "V3"), "same voucher line": (1, 3, "10"),
            "bad amount": (1, 7, 1701), "missing amount": (1, 7, ""),
            "extra units": (1, 6, 2), "wrong supplier document": (1, 4, "SP-9"),
            "third line overdraws shipment": (2, 5, "S2"), "unresolved third line": (2, 6, ""),
        }
        for name, (row, col, value) in scenarios.items():
            with self.subTest(name=name):
                changed = [r.copy() for r in self.links]
                changed[row][col] = value
                self.assertEqual(self.replay(changed)["B"][:2], ("possible_duplicate_invoice", None))
        for name, changed in {
            "missing evidence for another billed line": self.links[:2],
            "one repeated application": self.links + self.links[:1],
            "unknown invoice claims the same shipment": self.links + [["DEMO", "X", "V9", "9", "X", "S2", 1, 1700]],
        }.items():
            with self.subTest(name=name):
                self.assertEqual(self.replay(changed)["B"][0], "possible_duplicate_invoice")

    def test_supplier_price_discrepancy_survives_split_confirmation(self):
        self.shipments(second_date="2025-01-04")
        path = self.batch / "po_lines.csv"
        path.write_text(path.read_text().replace("17.00", "16.00"))
        results = self.replay(self.links)
        self.assertEqual([results[item][0] for item in ("A", "B")], ["price_variance", "price_variance"])

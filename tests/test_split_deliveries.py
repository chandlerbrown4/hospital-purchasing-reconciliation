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

    def replay(self, links, *, include_billed=False):
        header = "vendor_id invoice_event_id voucher_id voucher_line invoice receipt_event_id applied_eaches applied_cents"
        if include_billed:
            header += " billed_eaches"
        feed(self.apps, header, links)
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

    def shared_links(self):
        records = [row + [1] for row in self.links]
        records[1][5] = "S1"
        records[2][5] = "S2"
        return records

    def test_complete_shared_shipment_preserves_dated_quantities_and_proof(self):
        records = self.shared_links()
        results = self.replay(records, include_billed=True)
        for key in ("A", "B"):
            self.assertEqual(results[key][:2], ("matched", 0))
            self.assertIn("shared shipment fully allocated", results[key][2])
        with closing(sqlite3.connect(self.base / "out/reconciliation.sqlite")) as db:
            self.assertEqual(db.execute("""
                SELECT voucher_line, receipt_event_id, billed_eaches, applied_eaches, applied_cents
                FROM verified_receipt_applications WHERE split_confirmed = 1 ORDER BY voucher_line
            """).fetchall(), [("10", "S1", 1, 1, 1700), ("20", "S1", 1, 1, 1700)])
        receipts = self.batch / "receipts.csv"
        receipts.write_text(receipts.read_text().replace("S1,Demo,P1,1,T,2025-01-02", "S1,Demo,P1,1,T,2025-01-05"))
        late = self.replay(records, include_billed=True)
        for key in ("A", "B"):
            self.assertEqual(late[key][:2], ("insufficient_receipts", 1))

    def test_balanced_total_requires_independent_line_level_billing_evidence(self):
        records = self.shared_links()
        self.assertEqual(self.replay([r[:-1] for r in records])["B"][0], "possible_duplicate_invoice")
        for column, value in ((8, 2), (8, ""), (8, "unknown"), (7, 1701), (3, "10"), (2, "OTHER")):
            with self.subTest(column=column, value=value):
                changed = [r.copy() for r in records]
                changed[1][column] = value
                self.assertEqual(self.replay(changed, include_billed=True)["B"][:2],
                                 ("possible_duplicate_invoice", None))
        receipts = self.batch / "receipts.csv"
        receipts.write_text(receipts.read_text().replace("2025-01-02,2,EA", "2025-01-02,3,EA"))
        self.assertEqual(self.replay(records, include_billed=True)["B"][0], "possible_duplicate_invoice")
        records[2][5] = "S1"  # The family leaves one unit for a third voucher.
        self.assertEqual(self.replay(records, include_billed=True)["B"][0], "possible_duplicate_invoice")

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

    def test_unresolved_sibling_only_blocks_a_plausible_order_line(self):
        target = self.batch / "invoices.csv"
        original = target.read_text()
        for po_line, expected in (("2", "insufficient_receipts"), ("1", "possible_duplicate_invoice"),
                                  ("", "possible_duplicate_invoice")):
            with self.subTest(po_line=po_line):
                target.write_text(original + f"X,Demo,EARLY,P1,{po_line},T,2024-12-29,1,EA,17\n")
                results = self.replay(self.links)
                self.assertEqual(results["B"][0], expected)
                self.assertIn(results["X"][0], ("before_po_date", "no_po_line"))

    def test_supplier_price_discrepancy_survives_split_confirmation(self):
        self.shipments(second_date="2025-01-04")
        path = self.batch / "po_lines.csv"
        path.write_text(path.read_text().replace("17.00", "16.00"))
        results = self.replay(self.links)
        self.assertEqual([results[item][0] for item in ("A", "B")], ["price_variance", "price_variance"])

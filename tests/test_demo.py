import csv
import json
import shutil
import sqlite3
import subprocess
import sys
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from purchasing.pipeline import run


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures"
INITIAL = FIXTURES / "01_initial"
FOLLOWUP = FIXTURES / "02_followup"
PRICE_HEADER = "device_id,effective_from,effective_to,unit_price_cents,currency,price_source"
PRICES = (
    PRICE_HEADER + "\n"
    "FIX-1,2025-01-01,2025-07-01,2100,USD,reconciled_invoice\n"
    "FIX-1,2025-07-01,,2400,USD,reconciled_invoice\n"
    "MESH-A,2025-01-01,,12000,USD,reconciled_invoice\n"
    "MESH-B,2025-01-01,,17500,USD,reconciled_invoice\n"
)


def csv_rows(path):
    with path.open(encoding="utf-8", newline="") as source:
        return list(csv.DictReader(source))


def invoice_line(output, event_id):
    with closing(sqlite3.connect(output / "reconciliation.sqlite")) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn.execute(
            """SELECT invoice_lines.*, row_audit.receipt_event_ids,
                      row_audit.uncertain_receipt_event_ids
               FROM invoice_lines JOIN row_audit USING (audit_id)
               WHERE invoice_lines.event_id = ?""",
            (event_id,),
        ).fetchone()


class DemoTest(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.temp = Path(folder.name)
        self.output = self.temp / "results"

    def test_default_demo_reports_before_and_after_and_exports_prices(self):
        result = subprocess.run(
            [sys.executable, "-m", "purchasing", "--output", str(self.output)],
            cwd=ROOT, text=True, capture_output=True, check=True,
        )
        self.assertIn("INV-480: price variance +10,000 cents", result.stdout)
        self.assertIn("INV-391: price variance +3,000 cents", result.stdout)
        before = csv_rows(self.output / "before/invoice_differences.csv")
        after = {row["invoice"]: row for row in csv_rows(self.output / "invoice_differences.csv")}
        self.assertEqual([(row["invoice"], row["priced_delta_cents"]) for row in before],
                         [("INV-480", "10000")])
        self.assertEqual(set(after), {"INV-391", "INV-499", "INV-610", "INV-611", "INV-620"})
        self.assertEqual(after["INV-391"]["priced_delta_cents"], "3000")
        self.assertEqual(after["INV-499"]["priced_delta_cents"], "")
        self.assertEqual((self.output / "device_prices.csv").read_text(encoding="utf-8"), PRICES)
        price_evidence = csv_rows(self.output / "device_price_evidence.csv")
        mesh = [row for row in price_evidence if row["device_id"] == "MESH-A"]
        self.assertEqual([(row["invoice"], row["invoice_event_id"], row["po_event_id"])
                          for row in mesh], [("INV-380", "I-A1", "P-A")])
        for row in price_evidence:
            source = csv_rows(ROOT / row["source_file"])[int(row["source_line"]) - 2]
            self.assertEqual(source.get("event_id", source.get("record_id")), row["invoice_event_id"])

        sql = (ROOT / "purchasing/invoice_differences.sql").read_text(encoding="utf-8")
        with closing(sqlite3.connect(self.output / "reconciliation.sqlite")) as conn:
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchall(), [("ok",)])
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual({row[1] for row in conn.execute(sql)}, set(after))

    def test_replay_preserves_partial_receipts_and_correction_and_reversal(self):
        run([INITIAL, FOLLOWUP], FIXTURES, self.output)
        replay = self.temp / "replay"
        run([INITIAL, FOLLOWUP, INITIAL, FOLLOWUP], FIXTURES, replay)
        for name in ("invoice_differences.csv", "device_prices.csv"):
            self.assertEqual((self.output / name).read_bytes(), (replay / name).read_bytes())

        audit = csv_rows(replay / "row_audit.csv")
        decisions = {(row["event_id"], row["decision"]) for row in audit}
        for expected in (
            ("P-A", "replay"), ("R-A1", "replay"), ("R-A2", "superseded"),
            ("R-A2-C", "receipt_linked"), ("I-B0", "reversed"), ("I-B0-V", "reversal_applied"),
            ("I-X", "ambiguous_po_line"), ("R-Q", "id_conflict"),
        ):
            self.assertIn(expected, decisions)
        self.assertEqual(len(audit), 2 * len(csv_rows(self.output / "row_audit.csv")))
        first = invoice_line(replay, "I-A1")
        second = invoice_line(replay, "I-A2")
        self.assertEqual((first["delivered_eaches"], first["cumulative_billed_eaches"]), (6, 6))
        self.assertEqual((second["delivered_eaches"], second["cumulative_billed_eaches"]), (12, 12))
        self.assertEqual(json.loads(second["receipt_event_ids"]), ["R-A1", "R-A2-C"])
        self.assertIsNone(invoice_line(replay, "I-B0"))
        self.assertEqual(invoice_line(replay, "I-B1")["decision"], "matched")

    def test_exposure_is_incremental_and_conflicting_receipts_remain_unknown(self):
        run([INITIAL, FOLLOWUP], FIXTURES, self.output)
        first = invoice_line(self.output, "I-S1")
        second = invoice_line(self.output, "I-S2")
        columns = ("cumulative_billed_eaches", "covered_eaches", "unbacked_eaches", "unbacked_po_cents", "delta_cents")
        self.assertEqual(tuple(first[name] for name in columns), (100, 80, 20, 20_000, 0))
        self.assertEqual(tuple(second[name] for name in columns), (110, 0, 10, 10_000, 0))

        disputed = invoice_line(self.output, "I-Q")
        self.assertEqual(disputed["decision"], "uncertain_receipts")
        self.assertEqual(disputed["delta_cents"], 0)
        for name in ("delivered_eaches", "covered_eaches", "unbacked_eaches", "unbacked_po_cents"):
            self.assertIsNone(disputed[name])
        self.assertEqual(json.loads(disputed["uncertain_receipt_event_ids"]), ["R-Q"])
        audit = csv_rows(self.output / "row_audit.csv")
        candidates = [json.loads(row["normalized_event"])["eaches"] for row in audit
                      if row["event_id"] == "R-Q"]
        self.assertEqual(candidates, [80, 100])

    def test_competing_replacements_quarantine_the_chain_in_either_order(self):
        initial = Path(shutil.copytree(INITIAL, self.temp / "initial"))
        followup = Path(shutil.copytree(FOLLOWUP, self.temp / "followup"))
        initial_invoices = initial / "invoices.csv"
        text = initial_invoices.read_text(encoding="utf-8").replace(",6,EA,120\n", ",6,EA,125\n")
        initial_invoices.write_text(text, encoding="utf-8")
        followup_invoices = followup / "invoices.csv"
        text = followup_invoices.read_text(encoding="utf-8").replace(",1,BX,750,,", ",1,BX,720,,")
        followup_invoices.write_text(text, encoding="utf-8")

        run([initial, followup], FIXTURES, self.output)
        self.assertEqual(invoice_line(self.output, "I-A2")["decision"], "matched")
        self.assertIn("MESH-A,", (self.output / "device_prices.csv").read_text(encoding="utf-8"))

        receipt_file = followup / "receipts.csv"
        base = receipt_file.read_text(encoding="utf-8")
        first = next(line for line in base.splitlines() if line.startswith("R-A2-C,"))
        second = first.replace("R-A2-C,", "R-A2-D,").replace(",1,BX,", ",4,EA,")
        child = first.replace("R-A2-C,", "R-A2-E,").replace(",replace,R-A2", ",replace,R-A2-C")
        own_outputs = []
        for order in ((first, second, child), (second, first, child)):
            receipt_file.write_text(base.replace(first + "\n", "\n".join(order) + "\n"), encoding="utf-8")
            run([initial, followup], FIXTURES, self.output)
            line = invoice_line(self.output, "I-A2")
            self.assertEqual(line["decision"], "uncertain_receipts")
            self.assertIsNone(line["unbacked_po_cents"])
            self.assertEqual(json.loads(line["uncertain_receipt_event_ids"]),
                             ["R-A2", "R-A2-C", "R-A2-D", "R-A2-E"])
            prices = (self.output / "device_prices.csv").read_text(encoding="utf-8")
            self.assertNotIn("MESH-A,", prices)
            self.assertIn("FIX-1,2025-07-01,,2400", prices)
            own_outputs.append((prices, (self.output / "invoice_differences.csv").read_bytes()))
        self.assertEqual(*own_outputs)

    def test_header_only_schema_drift_and_explicit_foreign_currency_fail(self):
        run([INITIAL], FIXTURES, self.output)
        previous = (self.output / "device_prices.csv").read_bytes()
        batch = Path(shutil.copytree(INITIAL, self.temp / "invalid"))
        receipt_file, invoice_file = batch / "receipts.csv", batch / "invoices.csv"
        receipts = receipt_file.read_text(encoding="utf-8")
        header = receipts.splitlines()[0]
        receipt_file.write_text(header + ",mystery\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, r"receipts\.csv: schema mismatch.*mystery"):
            run([batch], FIXTURES, self.output)

        receipt_file.write_text(receipts, encoding="utf-8")
        header, first, *others = invoice_file.read_text(encoding="utf-8").splitlines()
        edited = [header + ",currency", first + ",EUR", *(row + ",USD" for row in others)]
        invoice_file.write_text("\n".join(edited) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, r"invoices\.csv:2: unsupported currency 'EUR'"):
            run([batch], FIXTURES, self.output)
        self.assertEqual((self.output / "device_prices.csv").read_bytes(), previous)


if __name__ == "__main__":
    unittest.main()

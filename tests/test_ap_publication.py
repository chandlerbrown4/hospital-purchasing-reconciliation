import csv
import shutil
import sqlite3
import subprocess
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from purchasing.pipeline import EXPORTS, OUTPUT_NAMES, build_snapshot


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "evaluation/held-out-v1"


def rows(path):
    with path.open(newline="", encoding="utf-8") as stream:
        return list(csv.DictReader(stream))


def write_rows(path, header, data):
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, header)
        writer.writeheader()
        writer.writerows(data)


def interrupted_build(batch, output, name, when):
    # Exit inside the child without Python cleanup, on either side of the real rename.
    script = """
import os, sys
from pathlib import Path
from purchasing.pipeline import run
batch, mappings, output, name, when = sys.argv[1:]
replace = Path.replace
def interrupt(path, target):
    stop = target.name == name
    if stop and when == 'before':
        os._exit(91)
    result = replace(path, target)
    if stop and when == 'after':
        os._exit(91)
    return result
Path.replace = interrupt
run([Path(batch)], Path(mappings), Path(output))
"""
    return subprocess.run([sys.executable, "-c", script, str(batch), str(FIXTURE),
                           str(output), name, when], cwd=ROOT).returncode


class APTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.batch = self.base / "batch"
        self.batch.mkdir()
        self.out = self.base / "out"
        self.invoices = self.batch / "invoices.csv"
        for filename, keep in (("po_lines.csv", ["P06"]), ("receipts.csv", ["R06"]),
                               ("invoices.csv", ["O01", "O02"])):
            data = rows(FIXTURE / "batch" / filename)
            write_rows(self.batch / filename, data[0], [r for r in data if r["event_id"] in keep])
        self.original = rows(self.invoices)

    def cli(self):
        return subprocess.check_output([sys.executable, "-m", "purchasing", str(self.batch),
                                        "--mappings", str(FIXTURE), "--output", str(self.out)],
                                       cwd=ROOT, text=True)

    def assert_exports_agree(self, directory):
        with closing(sqlite3.connect(directory / "reconciliation.sqlite")) as db:
            for name, order in EXPORTS.items():
                with self.subTest(export=name):
                    result = db.execute(f"SELECT * FROM {name}" + (f" ORDER BY {order}" if order else ""))
                    expected = [[col[0] for col in result.description]]
                    expected.extend(["" if v is None else str(v) for v in row] for row in result)
                    with (directory / f"{name}.csv").open(newline="") as stream:
                        self.assertEqual(list(csv.reader(stream)), expected)

    def test_bad_quantity_and_missing_id_reach_ap_with_source_and_unknown_amount(self):
        row = dict(self.original[0], qty="3 units")
        write_rows(self.invoices, row, [row, dict(row, event_id="", invoice="")])
        message = self.cli()
        self.assertIn("Invoice rows received: 2 | matched: 0 | exception: 0 | review: 0 | rejected: 2", message)
        self.assertIn("REJECTED Birch Goods bg402 [O01]", message)
        self.assertIn("invalid positive integer quantity: '3 units'", message)
        self.assertIn(f"{self.invoices}:3", message)
        self.assertNotIn("No invoice rows require action", message)
        result = rows(self.out / "ap_invoice_rows.csv")
        self.assertEqual([(r["source_line"], r["actual_cents"], r["disposition"]) for r in result],
                         [("2", "", "rejected"), ("3", "", "rejected")])
        self.assert_exports_agree(self.out)

    def test_counts_cover_conflicts_history_and_an_empty_feed(self):
        row = self.original[0]
        header = [*row, "operation", "target"]
        void = dict(event_id="VOID", vendor=row["vendor"], date=row["date"], operation="reverse", target="O01")
        data = [row, row, void, dict(row, event_id="X"), dict(row, event_id="X", qty="4"),
                dict(void, event_id="BAD", target="MISSING")]
        write_rows(self.invoices, header, data)
        self.cli()
        decisions = [(r["decision"], r["disposition"]) for r in rows(self.out / "ap_invoice_rows.csv")]
        self.assertEqual(decisions, [("reversed", "history"), ("replay", "history"),
                                     ("reversal_applied", "history"), ("id_conflict", "review"),
                                     ("id_conflict", "review"), ("invalid_revision", "rejected")])
        self.assertEqual(list(rows(self.out / "ap_summary.csv")[0].values()), ["6", "0", "0", "2", "1", "3"])
        write_rows(self.invoices, header, [])
        self.cli()
        self.assertEqual(list(rows(self.out / "ap_summary.csv")[0].values()), ["0"] * 6)

    def test_interruption_either_side_of_switch_and_retry(self):
        prior = build_snapshot([self.batch], FIXTURE, self.out).directory
        before = {name: (self.out / name).read_bytes() for name in OUTPUT_NAMES}
        self.assertEqual(rows(prior / "ap_invoice_rows.csv")[1]["decision"], "possible_duplicate_invoice")
        first, second = self.original
        write_rows(self.invoices, first, [first, dict(second, invoice="BG403")])
        self.assertEqual(interrupted_build(self.batch, self.out, "current", "before"), 91)
        self.assertEqual({name: (self.out / name).read_bytes() for name in OUTPUT_NAMES}, before)
        self.assert_exports_agree(self.out)
        self.assertEqual(interrupted_build(self.batch, self.out, "current", "after"), 91)
        self.assertEqual([r["decision"] for r in rows(self.out / "ap_invoice_rows.csv")], ["matched"] * 2)
        self.assert_exports_agree(self.out)
        self.assertEqual({name: (prior / name).read_bytes() for name in OUTPUT_NAMES}, before)
        self.cli()
        self.assert_exports_agree(self.out)

    def test_interrupted_adoption_of_flat_files_and_first_build(self):
        prior = build_snapshot([self.batch], FIXTURE, self.base / "seed").directory
        self.out.mkdir()
        legacy = ("row_audit.csv", "invoice_differences.csv", "device_prices.csv",
                  "device_price_evidence.csv", "reconciliation.sqlite")
        for name in legacy:
            shutil.copyfile(prior / name, self.out / name)
        first = self.original[0]
        write_rows(self.invoices, first, [dict(first, qty="3 units")])
        self.assertEqual(interrupted_build(self.batch, self.out, "device_prices.csv", "after"), 91)
        for name in legacy:
            self.assertEqual((self.out / name).read_bytes(), (prior / name).read_bytes())
        self.assertFalse((self.out / "ap_invoice_rows.csv").exists())
        self.cli()
        self.assert_exports_agree(self.out)
        empty = self.base / "empty"
        self.assertEqual(interrupted_build(self.batch, empty, "current", "before"), 91)
        self.assertTrue(all(not (empty / name).exists() for name in OUTPUT_NAMES))
        build_snapshot([self.batch], FIXTURE, empty)
        self.assert_exports_agree(empty)

    def test_concurrent_builds_keep_both_pinned_results_and_one_complete_publication(self):
        distinct = Path(shutil.copytree(self.batch, self.base / "distinct"))
        first, second = self.original
        write_rows(distinct / "invoices.csv", first, [first, dict(second, invoice="BG403")])
        with ThreadPoolExecutor(max_workers=2) as pool:
            builds = [pool.submit(build_snapshot, [batch], FIXTURE, self.out) for batch in (self.batch, distinct)]
            directories = [job.result().directory for job in builds]
        for path, expected in zip(directories, ["possible_duplicate_invoice", "matched"]):
            self.assertEqual(rows(path / "ap_invoice_rows.csv")[1]["decision"], expected)
        live = (self.out / ".purchasing/current").resolve()
        self.assertIn(live, [d.resolve() for d in directories])
        self.assertTrue(all((self.out / name).resolve().parent == live for name in OUTPUT_NAMES))
        self.assert_exports_agree(self.out)

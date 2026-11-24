import csv
import sqlite3
from argparse import ArgumentParser
from contextlib import closing
from pathlib import Path

from .pipeline import build_snapshot


def report(title, snapshot):
    print(title)
    with closing(sqlite3.connect(snapshot.directory / "reconciliation.sqlite")) as db:
        db.row_factory = sqlite3.Row
        counts = db.execute("SELECT * FROM ap_summary").fetchone()
        print(f"  Invoice rows received: {counts['received']} | matched: {counts['matched']} | "
              f"exception: {counts['exception']} | review: {counts['review']} | "
              f"rejected: {counts['rejected']} | history: {counts['history']}")
        for row in db.execute("""SELECT * FROM ap_invoice_rows
                WHERE disposition IN ('exception', 'review', 'rejected') ORDER BY audit_id"""):
            amount = "" if row["actual_cents"] is None else f" (${row['actual_cents'] / 100:,.2f})"
            print(f"  {row['disposition'].upper()} {row['vendor']} {row['invoice']} "
                  f"[{row['event_id'] or 'no event ID'}]{amount}: {row['reason']} "
                  f"({row['source_file']}:{row['source_line']})")
        if not (counts["exception"] + counts["review"] + counts["rejected"]):
            print("  No invoice rows require action")
    for row in snapshot.differences:
        delta = row["priced_delta_cents"]
        amount = "price variance unknown" if delta is None else f"price variance {delta:+,} cents"
        exposure = row["unbacked_po_cents"]
        if exposure is None:
            quantity = "quantity exposure unknown"
        elif row["unknown_exposure_lines"]:
            quantity = (f"{row['unbacked_eaches']} known unbacked units, "
                        f"{exposure:,} cents at PO rate on quantified lines")
        else:
            quantity = f"{row['unbacked_eaches']} unbacked units, {exposure:,} cents at PO rate"
        print(f"  {row['vendor']} {row['invoice']}: {amount}, {quantity}"
              f" ({row['unpriced_lines']} unpriced, {row['unknown_exposure_lines']} unknown exposure line(s))")
    print(f"  Full invoice register: {snapshot.directory / 'ap_invoice_rows.csv'}")


def main():
    parser = ArgumentParser(description="Reconcile fictional hospital purchasing records.")
    parser.add_argument("batches", nargs="*", type=Path, help="Batch directories in ingestion order")
    parser.add_argument("--mappings", type=Path, default=Path("fixtures"))
    parser.add_argument("--output", type=Path, default=Path("build"))
    parser.add_argument("--receipt-applications", type=Path,
                        help="Complete voucher-to-shipment application snapshot for the supplied batches")
    args = parser.parse_args()
    if args.receipt_applications and not args.batches:
        parser.error("--receipt-applications requires explicit batch directories")

    try:
        if args.batches:
            report("AP invoice summary", build_snapshot(args.batches, args.mappings, args.output,
                                                       receipt_applications=args.receipt_applications))
        else:
            initial = Path("fixtures/01_initial")
            followup = Path("fixtures/02_followup")
            report("Before the follow-up batch", build_snapshot([initial], args.mappings, args.output / "before"))
            report("After the follow-up batch", build_snapshot([initial, followup], args.mappings, args.output))
    except (OSError, ValueError, csv.Error) as error:
        parser.exit(1, f"Reconciliation failed: {error}\n")


if __name__ == "__main__":
    main()

import csv
from argparse import ArgumentParser
from pathlib import Path

from .pipeline import OUTPUT_NAMES, run


def report(title, differences):
    print(title)
    if not differences:
        print("  No active invoice exceptions")
    for row in differences:
        delta = row["priced_delta_cents"]
        amount = "price variance unknown" if delta is None else f"price variance {delta:+,} cents"
        exposure = row["unbacked_po_cents"]
        if exposure is None:
            quantity = "quantity exposure unknown"
        else:
            quantity = f"{row['unbacked_eaches']} unbacked units, {exposure:,} cents at PO rate"
        print(f"  {row['vendor']} {row['invoice']}: {amount}, {quantity}"
              f" ({row['unpriced_lines']} unpriced, {row['unknown_exposure_lines']} unknown exposure line(s))")


def main():
    parser = ArgumentParser(description="Reconcile fictional hospital purchasing records.")
    parser.add_argument("batches", nargs="*", type=Path, help="Batch directories in ingestion order")
    parser.add_argument("--mappings", type=Path, default=Path("fixtures"))
    parser.add_argument("--output", type=Path, default=Path("build"))
    args = parser.parse_args()

    try:
        if args.batches:
            report("Active invoice exceptions", run(args.batches, args.mappings, args.output))
        else:
            initial = Path("fixtures/01_initial")
            followup = Path("fixtures/02_followup")
            report("Before the follow-up batch", run([initial], args.mappings, args.output / "before"))
            report("After the follow-up batch", run([initial, followup], args.mappings, args.output))
    except (OSError, ValueError, csv.Error) as error:
        parser.exit(1, f"Reconciliation failed: {error}\n")
    print("Wrote " + ", ".join(str(args.output / name) for name in OUTPUT_NAMES))


if __name__ == "__main__":
    main()

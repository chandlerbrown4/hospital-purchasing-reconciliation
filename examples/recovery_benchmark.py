"""Reproduce a process-interruption test on a generated purchasing batch (macOS/Linux)."""

import argparse
import csv
import hashlib
import os
import platform
import resource
import sqlite3
import subprocess
import sys
import time
from contextlib import ExitStack, closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from purchasing.pipeline import EXPORTS, build_snapshot

ROOT = Path(__file__).resolve().parents[1]


def make_batch(batch, orders):
    batch.mkdir(exist_ok=True)
    with ExitStack() as stack:
        feeds = {}
        for name in ("po_lines", "receipts", "invoices"):
            with (ROOT / "fixtures/01_initial" / f"{name}.csv").open(newline="") as template:
                columns = next(csv.reader(template))
            stream = stack.enter_context((batch / f"{name}.csv").open("w", newline=""))
            feeds[name] = csv.DictWriter(stream, fieldnames=columns)
            feeds[name].writeheader()
        for n in range(1, orders + 1):
            line = dict(vendor="Horizon Devices", po=f"PO-{n}", po_line="1", sku="HZ-B",
                        date="2025-01-01", qty=2, unit="EA")
            feeds["po_lines"].writerow(dict(line, event_id=f"P-{n}", unit_price="175.00"))
            for shipment in (1, 2):
                feeds["receipts"].writerow(dict(line, event_id=f"R-{n}-{shipment}", qty=1))
            invoice = dict(line, event_id=f"I-{n}", invoice=f"DOC-{n}",
                           unit_price="175.50" if n % 10 == 0 else "175.00")
            feeds["invoices"].writerow(invoice)
            if n % 20 == 0:
                feeds["invoices"].writerow(invoice)


def worker(batch, out, interrupt):
    replace = Path.replace

    def publish(path, target):
        if interrupt and Path(target).name == "current":
            os._exit(86)  # Simulate a stopped process at the final publication switch.
        return replace(path, target)

    with patch.object(Path, "replace", publish):
        build_snapshot([batch], ROOT / "fixtures", out)


def build(batch, out, interrupt=False):
    command = [sys.executable, "-m", "examples.recovery_benchmark", "--worker", str(batch), str(out)]
    if interrupt:
        command.append("--interrupt")
    start = time.perf_counter()
    run = subprocess.run(command, cwd=ROOT)
    if run.returncode != (86 if interrupt else 0):
        raise RuntimeError(f"unexpected worker exit {run.returncode}")
    return time.perf_counter() - start


def fingerprint(folder):
    names = [f"{table}.csv" for table in EXPORTS] + ["reconciliation.sqlite"]
    with ExitStack() as stack:
        return {name: hashlib.file_digest(stack.enter_context((folder / name).open("rb")),
                                          "sha256").hexdigest() for name in names}


def run(orders):
    with TemporaryDirectory() as directory:
        work = Path(directory)
        batch, live, clean = work / "batch", work / "live", work / "clean"
        make_batch(batch, 1)
        build(batch, live)
        baseline = fingerprint(live)
        make_batch(batch, orders)
        build(batch, live, interrupt=True)
        if fingerprint(live) != baseline:
            raise RuntimeError("the interrupted build changed the published snapshot")
        seconds = build(batch, live)
        build(batch, clean)
        if fingerprint(live) != fingerprint(clean):
            raise RuntimeError("retry and clean build disagree")
        with closing(sqlite3.connect(live / "reconciliation.sqlite")) as db:
            counts = tuple(db.execute(
                "SELECT received, matched, exception, review, rejected, history FROM ap_summary"
            ).fetchone())
            price_delta = db.execute("SELECT SUM(delta_cents) FROM invoice_lines").fetchone()[0] or 0
        extra = orders // 20
        exceptions = orders // 10
        if counts != (orders + extra, orders - exceptions, exceptions, 0, 0, extra):
            raise RuntimeError(f"unexpected invoice dispositions {counts}")
        if price_delta != 100 * exceptions:
            raise RuntimeError(f"unexpected price variance {price_delta} cents")
        rss = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
        peak_mib = rss / (1024 * 1024 if sys.platform == "darwin" else 1024)
        print(f"{4 * orders + extra:,} input rows; {counts[0]:,} invoice rows; "
              f"{counts[1]:,} matched, {exceptions:,} exceptions, {extra:,} replays.")
        print(f"Verified ${price_delta / 100:,.2f} price variance and seven identical output files.")
        print(f"Retry: {seconds:.2f} s. Maximum worker RSS: {peak_mib:.1f} MiB. "
              f"Python {platform.python_version()} / {platform.system()} {platform.machine()}.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--orders", type=int, default=10000)
    parser.add_argument("--worker", nargs=2, type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--interrupt", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        worker(*args.worker, args.interrupt)
    elif args.orders < 20:
        parser.error("use at least 20 orders to exercise every case")
    else:
        run(args.orders)

import csv
import json
import sqlite3
from contextlib import closing
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory

from .applications import load_applications
from .inputs import load
from .reconcile import price_book, reconcile


AUDIT_COLUMNS = (
    "audit_id", "batch", "source_file", "source_line", "kind", "event_id", "vendor",
    "operation", "target_event_id", "decision", "reason", "po_event_id",
    "receipt_event_ids", "uncertain_receipt_event_ids", "source_fields", "normalized_event",
)
PRICE_COLUMNS = (
    "device_id", "effective_from", "effective_to", "unit_price_cents", "currency", "price_source",
)
PRICE_EVIDENCE_COLUMNS = ("device_id", "effective_from", "unit_price_cents", "vendor", "invoice",
                          "invoice_event_id", "po_event_id", "source_file", "source_line")
OUTPUT_NAMES = ("row_audit.csv", "invoice_differences.csv", "device_prices.csv",
                "device_price_evidence.csv", "reconciliation.sqlite")
SCHEMA = """
CREATE TABLE row_audit (
    audit_id INTEGER PRIMARY KEY,
    batch INTEGER NOT NULL,
    source_file TEXT NOT NULL,
    source_line INTEGER NOT NULL,
    kind TEXT NOT NULL,
    event_id TEXT NOT NULL,
    vendor TEXT NOT NULL,
    operation TEXT NOT NULL,
    target_event_id TEXT NOT NULL,
    decision TEXT NOT NULL CHECK (decision <> ''),
    reason TEXT NOT NULL,
    po_event_id TEXT NOT NULL,
    receipt_event_ids TEXT NOT NULL,
    uncertain_receipt_event_ids TEXT NOT NULL,
    source_fields TEXT NOT NULL,
    normalized_event TEXT
);

CREATE TABLE invoice_lines (
    audit_id INTEGER PRIMARY KEY REFERENCES row_audit(audit_id),
    vendor TEXT NOT NULL,
    invoice TEXT NOT NULL,
    event_id TEXT NOT NULL,
    invoice_date TEXT NOT NULL,
    po_number TEXT NOT NULL,
    po_line TEXT,
    po_event_id TEXT,
    device_id TEXT NOT NULL,
    invoiced_eaches INTEGER NOT NULL,
    ordered_eaches INTEGER,
    delivered_eaches INTEGER,
    cumulative_billed_eaches INTEGER,
    actual_cents INTEGER NOT NULL,
    covered_eaches INTEGER,
    unbacked_eaches INTEGER,
    unbacked_po_cents INTEGER,
    expected_cents INTEGER,
    delta_cents INTEGER,
    decision TEXT NOT NULL
);

CREATE TABLE verified_receipt_applications (
    invoice_audit_id INTEGER NOT NULL REFERENCES invoice_lines(audit_id),
    voucher_id TEXT NOT NULL,
    voucher_line TEXT NOT NULL,
    receipt_event_id TEXT NOT NULL,
    applied_eaches INTEGER NOT NULL,
    applied_cents INTEGER NOT NULL,
    split_confirmed INTEGER NOT NULL,
    PRIMARY KEY (invoice_audit_id, receipt_event_id)
);
"""


def write_csv(path, header, data):
    with path.open("w", encoding="utf-8", newline="") as sink:
        writer = csv.writer(sink, lineterminator="\n")
        writer.writerow(header)
        writer.writerows(data)


def json_value(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def audit_record(audit_id, row):
    event = row.event
    return (
        audit_id, row.batch_number, str(row.path), row.line, row.kind,
        event.event_id if event else row.raw.get("event_id", ""),
        event.vendor if event else row.raw.get("vendor", ""),
        event.operation if event else row.raw.get("operation", ""),
        event.target if event else row.raw.get("target", ""),
        row.decision, row.reason, row.po_event, json_value(row.receipts), json_value(row.uncertain_receipts),
        json_value(row.raw), json_value(asdict(event)) if event else None,
    )


def invoice_record(audit_id, line):
    invoice, po = line.source.event, line.po
    return (
        audit_id, invoice.vendor, invoice.invoice, invoice.event_id, invoice.date,
        invoice.po, po.po_line if po else invoice.po_line or None, po.event_id if po else None,
        invoice.device, invoice.eaches, po.eaches if po else None, line.delivered_eaches,
        line.cumulative_billed_eaches, invoice.amount_cents, line.covered_eaches,
        line.unbacked_eaches, line.unbacked_po_cents, line.expected_cents, line.delta_cents,
        line.source.decision,
    )


def run(batches, mappings, output, *, receipt_applications=None):
    rows = load(batches, mappings)
    applications = load_applications(receipt_applications) if receipt_applications is not None else None
    lines = reconcile(rows, applications)
    prices, price_evidence = price_book(lines)
    audit = [audit_record(audit_id, row) for audit_id, row in enumerate(rows, start=1)]
    audit_ids = {id(row): audit_id for audit_id, row in enumerate(rows, start=1)}
    invoice_records = [invoice_record(audit_ids[id(line.source)], line) for line in lines]
    sql = Path(__file__).with_name("invoice_differences.sql").read_text(encoding="utf-8")

    output.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".reconciliation-", dir=output) as folder:
        staging = Path(folder)
        with closing(sqlite3.connect(staging / "reconciliation.sqlite")) as conn:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.executescript(SCHEMA)
            with conn:
                conn.executemany("INSERT INTO row_audit VALUES (" + ",".join("?" for _ in AUDIT_COLUMNS) + ")", audit)
                conn.executemany(
                    "INSERT INTO invoice_lines VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    invoice_records,
                )
                conn.executemany(
                    "INSERT INTO verified_receipt_applications VALUES (?, ?, ?, ?, ?, ?, ?)",
                    ((audit_ids[id(line.source)], a.voucher, a.voucher_line, a.receipt, a.eaches, a.cents,
                      line.split_confirmed) for line in lines if line.cumulative_billed_eaches is not None
                     for a in line.allocations),
                )
            cursor = conn.execute(sql)
            headers = [column[0] for column in cursor.description]
            differences = cursor.fetchall()

        write_csv(staging / "row_audit.csv", AUDIT_COLUMNS, audit)
        write_csv(staging / "invoice_differences.csv", headers, differences)
        write_csv(staging / "device_prices.csv", PRICE_COLUMNS, prices)
        write_csv(staging / "device_price_evidence.csv", PRICE_EVIDENCE_COLUMNS, price_evidence)
        for name in OUTPUT_NAMES:
            (staging / name).replace(output / name)
    return [dict(zip(headers, row)) for row in differences]

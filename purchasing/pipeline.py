import csv
import json
import sqlite3
from contextlib import closing
from dataclasses import asdict, dataclass
from pathlib import Path

from .applications import load_applications
from .inputs import load
from .reconcile import price_book, reconcile
from .snapshots import stage_snapshot


AUDIT_COLUMNS = (
    "audit_id", "batch", "source_file", "source_line", "kind", "event_id", "vendor",
    "operation", "target_event_id", "decision", "reason", "po_event_id",
    "receipt_event_ids", "uncertain_receipt_event_ids", "source_fields", "normalized_event",
)
EXPORTS = {
    "row_audit": "audit_id",
    "ap_invoice_rows": "audit_id",
    "ap_summary": None,
    "invoice_differences": "vendor, invoice",
    "device_prices": "device_id, effective_from",
    "device_price_evidence": "device_id, effective_from, rowid",
}
OUTPUT_NAMES = (*(name + ".csv" for name in EXPORTS), "reconciliation.sqlite")
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
    billed_eaches INTEGER,
    PRIMARY KEY (invoice_audit_id, receipt_event_id)
);
CREATE TABLE device_prices (
    device_id TEXT, effective_from TEXT, effective_to TEXT, unit_price_cents INTEGER,
    currency TEXT, price_source TEXT, PRIMARY KEY (device_id, effective_from)
);
CREATE TABLE device_price_evidence (
    device_id TEXT, effective_from TEXT, unit_price_cents INTEGER, vendor TEXT,
    invoice TEXT, invoice_event_id TEXT, po_event_id TEXT, source_file TEXT, source_line INTEGER,
    FOREIGN KEY (device_id, effective_from) REFERENCES device_prices
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


@dataclass
class Snapshot:
    directory: Path
    differences: list[dict]


def build_snapshot(batches, mappings, output, *, receipt_applications=None):
    rows = load(batches, mappings)
    applications = load_applications(receipt_applications) if receipt_applications is not None else None
    lines = reconcile(rows, applications)
    prices, price_evidence = price_book(lines)
    audit = [audit_record(audit_id, row) for audit_id, row in enumerate(rows, start=1)]
    audit_ids = {id(row): audit_id for audit_id, row in enumerate(rows, start=1)}
    invoice_records = [invoice_record(audit_ids[id(line.source)], line) for line in lines]
    with stage_snapshot(output, OUTPUT_NAMES) as (staging, published):
        with closing(sqlite3.connect(staging / "reconciliation.sqlite")) as conn:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.executescript(SCHEMA)
            for view in ("ap_invoice_rows", "invoice_differences"):
                query = Path(__file__).with_name(f"{view}.sql").read_text(encoding="utf-8")
                conn.execute(f"CREATE VIEW {view} AS {query}")
            conn.execute("""
                CREATE VIEW ap_summary AS
                SELECT COUNT(*) AS received,
                       COUNT(*) FILTER (WHERE disposition = 'matched') AS matched,
                       COUNT(*) FILTER (WHERE disposition = 'exception') AS exception,
                       COUNT(*) FILTER (WHERE disposition = 'review') AS review,
                       COUNT(*) FILTER (WHERE disposition = 'rejected') AS rejected,
                       COUNT(*) FILTER (WHERE disposition = 'history') AS history
                FROM ap_invoice_rows
            """)
            with conn:
                conn.executemany("INSERT INTO row_audit VALUES (" + ",".join("?" for _ in AUDIT_COLUMNS) + ")", audit)
                conn.executemany(
                    "INSERT INTO invoice_lines VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    invoice_records,
                )
                conn.executemany(
                    "INSERT INTO verified_receipt_applications VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    ((audit_ids[id(line.source)], a.voucher, a.voucher_line, a.receipt, a.eaches, a.cents,
                      bool(line.split_basis), a.billed_eaches) for line in lines if line.cumulative_billed_eaches is not None
                     for a in line.allocations),
                )
                conn.executemany("INSERT INTO device_prices VALUES (?, ?, ?, ?, ?, ?)", prices)
                conn.executemany("INSERT INTO device_price_evidence VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", price_evidence)
            for table, order in EXPORTS.items():
                cursor = conn.execute(f"SELECT * FROM {table}" + (f" ORDER BY {order}" if order else ""))
                headers = [column[0] for column in cursor.description]
                write_csv(staging / f"{table}.csv", headers, cursor)
            conn.row_factory = sqlite3.Row
            differences = [dict(row) for row in conn.execute("SELECT * FROM invoice_differences ORDER BY vendor, invoice")]
    return Snapshot(published, differences)


def run(batches, mappings, output, *, receipt_applications=None):
    """Return active invoice differences for existing programmatic callers."""
    return build_snapshot(batches, mappings, output, receipt_applications=receipt_applications).differences

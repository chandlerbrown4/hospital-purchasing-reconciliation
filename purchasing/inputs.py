import csv
from dataclasses import dataclass
from datetime import date
from pathlib import Path


SOURCES = {"po": "po_lines.csv", "receipt": "receipts.csv", "invoice": "invoices.csv"}
ALIASES = {
    "record_id": "event_id",
    "supplier": "vendor",
    "order_no": "po",
    "line_no": "po_line",
    "item_code": "sku",
    "event_date": "date",
    "quantity": "qty",
    "uom": "unit",
    "price": "unit_price",
    "invoice_no": "invoice",
    "action": "operation",
    "prior_event": "target",
}
DOCUMENT_COLUMNS = {"event_id", "vendor", "po", "sku", "date", "qty", "unit"}
REVISION_COLUMNS = {"po_line", "operation", "target"}
FEED_COLUMNS = {
    "po": (DOCUMENT_COLUMNS | {"po_line", "unit_price"}, {"currency"}),
    "receipt": (DOCUMENT_COLUMNS, REVISION_COLUMNS),
    "invoice": (DOCUMENT_COLUMNS | {"invoice", "unit_price"}, REVISION_COLUMNS | {"currency"}),
}
MAX_QUANTITY = 1_000_000
MAX_CENTS = 100_000_000


@dataclass(frozen=True)
class Event:
    kind: str
    vendor: str
    event_id: str
    operation: str
    target: str
    date: str
    po: str = ""
    po_line: str = ""
    sku: str = ""
    device: str = ""
    invoice: str = ""
    eaches: int = 0
    amount_cents: int | None = None

    @property
    def key(self):
        return self.kind, self.vendor, self.event_id


@dataclass
class SourceRow:
    batch_number: int
    path: Path
    line: int
    kind: str
    raw: dict
    event: Event | None = None
    decision: str = ""
    reason: str = ""
    po_event: str = ""
    receipts: tuple[str, ...] = ()
    uncertain_receipts: tuple[str, ...] = ()


def read_csv(path, required_columns, optional_columns=()):
    with path.open(encoding="utf-8-sig", newline="") as source:
        reader = csv.reader(source, strict=True)
        try:
            headers = [field.strip().lower() for field in next(reader)]
        except StopIteration:
            raise ValueError(f"{path}: missing header") from None
        headers = [ALIASES.get(field, field) for field in headers]
        if "" in headers or len(headers) != len(set(headers)):
            raise ValueError(f"{path}: blank or duplicate canonical column")
        missing = required_columns.difference(headers)
        unexpected = set(headers).difference(required_columns, optional_columns)
        if missing or unexpected:
            raise ValueError(f"{path}: schema mismatch, missing {sorted(missing)}, unexpected {sorted(unexpected)}")
        while True:
            line = reader.line_num + 1
            try:
                values = next(reader)
            except StopIteration:
                break
            if values:
                if len(values) != len(headers):
                    raise ValueError(f"{path}:{line}: expected {len(headers)} fields, got {len(values)}")
                yield line, dict(zip(headers, values))


def add_mapping(mapping, key, value):
    if key in mapping and mapping[key] != value:
        raise ValueError(f"conflicting mapping for {key!r}")
    mapping[key] = value


def load_mappings(directory):
    vendors, units, devices = {}, {}, {}
    for _, row in read_csv(directory / "vendors.csv", {"alias", "vendor_id"}):
        alias = required(row, "alias").casefold()
        vendor = required(row, "vendor_id").upper()
        add_mapping(vendors, alias, vendor)
    for _, row in read_csv(directory / "units.csv", {"vendor_id", "vendor_sku", "device_id", "unit", "eaches"}):
        vendor = required(row, "vendor_id").upper()
        sku = required(row, "vendor_sku").upper()
        device = required(row, "device_id").upper()
        unit = required(row, "unit").upper()
        if vendor not in vendors.values():
            raise ValueError(f"unit mapping uses unknown vendor {vendor!r}")
        add_mapping(devices, (vendor, sku), device)
        add_mapping(units, (vendor, sku, unit), (device, quantity(required(row, "eaches"))))
    return vendors, units


def required(row, field):
    value = row.get(field, "").strip()
    if not value:
        raise ValueError(f"missing {field}")
    return value


def quantity(value):
    if not value.isascii() or not value.isdecimal() or len(value) > 7:
        raise ValueError(f"invalid positive integer quantity: {value!r}")
    count = int(value)
    if not 0 < count <= MAX_QUANTITY:
        raise ValueError(f"quantity out of range: {value!r}")
    return count


def unit_cents(value):
    # Parse plain dollar amounts exactly, without rounding or decimal context limits.
    whole, dot, fraction = value.partition(".")
    if (
        not whole.isascii() or not whole.isdecimal() or len(whole) > 7
        or (dot and (not fraction.isascii() or not fraction.isdecimal() or len(fraction) > 2))
    ):
        raise ValueError(f"invalid USD unit price: {value!r}")
    return int(whole) * 100 + int(fraction.ljust(2, "0"))


def normalize(kind, row, vendors, units):
    alias = required(row, "vendor")
    vendor = vendors.get(alias.casefold())
    if vendor is None:
        raise ValueError(f"unknown vendor alias: {alias!r}")
    event_id = required(row, "event_id").upper()
    operation = row.get("operation", "").strip().lower() or "record"
    target = row.get("target", "").strip().upper()
    when = required(row, "date")
    if date.fromisoformat(when).isoformat() != when:
        raise ValueError(f"date must use YYYY-MM-DD: {when!r}")
    if operation not in {"record", "replace", "reverse"}:
        raise ValueError(f"unknown operation: {operation!r}")
    if operation == "record" and target:
        raise ValueError("record forbids a target")
    if operation != "record" and not target:
        raise ValueError("replace and reverse require a target")
    if kind == "po" and operation != "record":
        raise ValueError("PO revisions are outside this feed contract")
    if operation == "reverse":
        payload_fields = ("po", "po_line", "sku", "qty", "unit", "invoice", "unit_price")
        if any(row.get(field, "").strip() for field in payload_fields):
            raise ValueError("reverse must only identify the prior event")
        return Event(kind, vendor, event_id, operation, target, when)

    po = required(row, "po").upper()
    po_line = required(row, "po_line").upper() if kind == "po" else row.get("po_line", "").strip().upper()
    sku = required(row, "sku").upper()
    unit = required(row, "unit").upper()
    device_and_factor = units.get((vendor, sku, unit))
    if device_and_factor is None:
        raise ValueError(f"no unit mapping for {vendor}/{sku}/{unit}")
    device, factor = device_and_factor
    count = quantity(required(row, "qty"))
    eaches = count * factor
    if eaches > MAX_QUANTITY:
        raise ValueError("normalized quantity out of range")
    amount = None
    if kind in {"po", "invoice"}:
        if "currency" in row:
            required(row, "currency")
        amount = count * unit_cents(required(row, "unit_price"))
        if amount > MAX_CENTS:
            raise ValueError("normalized amount out of range")
    invoice = required(row, "invoice").upper() if kind == "invoice" else ""
    return Event(kind, vendor, event_id, operation, target, when, po, po_line, sku, device, invoice, eaches, amount)


def load(batches, mappings):
    vendors, units = load_mappings(mappings)
    rows = []
    for batch_number, batch in enumerate(batches, start=1):
        for kind, filename in SOURCES.items():
            path = batch / filename
            for line, raw in read_csv(path, *FEED_COLUMNS[kind]):
                currency = raw.get("currency", "").strip().upper()
                if currency and currency != "USD":
                    raise ValueError(f"{path}:{line}: unsupported currency {currency!r}; expected USD")
                row = SourceRow(batch_number, path, line, kind, raw)
                try:
                    row.event = normalize(kind, raw, vendors, units)
                except ValueError as error:
                    row.decision = "invalid"
                    row.reason = str(error)
                rows.append(row)
    return rows

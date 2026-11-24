"""Resolve repeated invoice signatures using a complete snapshot of voucher applications."""
from collections import defaultdict
from dataclasses import dataclass

from .inputs import MAX_CENTS, MAX_QUANTITY, read_csv


@dataclass(frozen=True)
class Application:
    voucher: str
    voucher_line: str
    invoice_number: str
    receipt: str
    eaches: int | None
    cents: int | None
    billed_eaches: int | None = None


def unsigned(value, limit):
    if value.isascii() and value.isdecimal() and len(value) <= len(str(limit)):
        number = int(value)
        if number <= limit:
            return number
    return None


def load_applications(path):
    fields = {"vendor_id", "invoice_event_id", "voucher_id", "voucher_line", "invoice",
              "receipt_event_id", "applied_eaches", "applied_cents"}
    by_invoice = defaultdict(list)
    for number, raw in read_csv(path, fields, {"billed_eaches"}):
        r = {k: v.strip() for k, v in raw.items()}
        if not r["vendor_id"] or not r["invoice_event_id"]:
            raise ValueError(f"{path}:{number}: application lacks vendor or invoice event identity")
        key = (r["vendor_id"].upper(), r["invoice_event_id"].upper())
        # A missing legacy column is allowed. A supplied value must be a positive quantity.
        billed = (unsigned(r["billed_eaches"], MAX_QUANTITY) or 0) if "billed_eaches" in r else None
        by_invoice[key].append(Application(
            r["voucher_id"].upper(), r["voucher_line"].upper(), r["invoice"],
            r["receipt_event_id"].upper(), unsigned(r["applied_eaches"], MAX_QUANTITY),
            unsigned(r["applied_cents"], MAX_CENTS), billed,
        ))
    return by_invoice


def validate_order_line(lines, receipts, applications, claimants, owners):
    """Require every billed line and every claim on the selected shipments to reconcile."""
    po = lines[0].po
    if sum(line.source.event.eaches for line in lines) > po.eaches:
        return False
    active_ids = {line.source.event.event_id for line in lines}
    used = defaultdict(int)
    for line in lines:
        event = line.source.event
        records = applications.get((po.vendor, event.event_id), ())
        if not records or len({(a.voucher, a.voucher_line) for a in records}) != 1:
            return False
        seen = set()
        for a in records:
            if (not a.voucher or not a.voucher_line or a.invoice_number != line.source.raw["invoice"].strip()
                    or len(owners[po.vendor, a.voucher, a.voucher_line]) != 1
                    or a.receipt not in receipts or a.receipt in seen
                    or (a.billed_eaches is not None and a.billed_eaches != event.eaches)
                    or not a.eaches or a.cents is None
                    or a.cents * event.eaches != event.amount_cents * a.eaches):
                return False
            seen.add(a.receipt)
            used[a.receipt] += a.eaches
        if sum(a.eaches for a in records) != event.eaches:
            return False
    return all(qty <= receipts[rid].eaches and claimants[po.vendor, rid] <= active_ids
               for rid, qty in used.items())


def apply_split_evidence(invoices, receipts, uncertain, applications):
    if not applications:
        return
    claimants = defaultdict(set)
    owners = defaultdict(set)
    for (vendor, event_id), records in applications.items():
        for a in records:
            claimants[vendor, a.receipt].add(event_id)
            owners[vendor, a.voucher, a.voucher_line].add(event_id)

    by_order_line = defaultdict(list)
    unresolved_lines = set()
    unresolved_items = set()
    for line in invoices:
        if line.po:
            by_order_line[line.po.key].append(line)
        else:
            event = line.source.event
            if event.po_line:
                unresolved_lines.add((event.vendor, event.po, event.po_line))
            else:
                unresolved_items.add((event.vendor, event.po, event.sku))

    for key, lines in by_order_line.items():
        po = lines[0].po
        families = defaultdict(list)
        for line in lines:
            families[line.first_entry].append(line)
        repeats = [family for family in families.values() if len(family) > 1]
        if (not repeats or uncertain[key] or (po.vendor, po.po, po.po_line) in unresolved_lines
                or (po.vendor, po.po, po.sku) in unresolved_items):
            continue
        shipments = {r.event_id: r for r in receipts[key]}
        if not validate_order_line(lines, shipments, applications, claimants, owners):
            continue
        for family in repeats:
            groups = [applications[po.vendor, line.source.event.event_id] for line in family]
            if len({(g[0].voucher, g[0].invoice_number) for g in groups}) != 1:
                continue
            records = [a for group in groups for a in group]
            shipment_ids = {a.receipt for a in records}
            if len(shipment_ids) == len(records):
                basis = "distinct split deliveries confirmed"
            elif (len(shipment_ids) == 1
                  and all(a.billed_eaches is not None for a in records)
                  and sum(a.eaches for a in records) == shipments[records[0].receipt].eaches):
                # The order-line check already balances each line's billed units and value,
                # rejects reused voucher lines and overdraws, and checks all shipment claimants.
                basis = "shared shipment fully allocated across distinct voucher lines"
            else:
                continue
            for line in family:
                line.split_basis = basis
        if any(line.split_basis for line in lines):
            # Don't let another invoice borrow dated coverage earmarked for these lines.
            for line in lines:
                line.allocations = tuple(applications[po.vendor, line.source.event.event_id])

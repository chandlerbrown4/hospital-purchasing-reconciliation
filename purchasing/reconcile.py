from collections import Counter, defaultdict
from dataclasses import dataclass

from .inputs import Event, SourceRow


@dataclass
class InvoiceLine:
    source: SourceRow
    po: Event | None = None
    first_entry: str | None = None
    delivered_eaches: int | None = None
    cumulative_billed_eaches: int | None = None
    covered_eaches: int | None = None
    unbacked_eaches: int | None = None
    unbacked_po_cents: int | None = None
    expected_cents: int | None = None
    delta_cents: int | None = None


def revision_quarantine(payloads):
    neighbors = defaultdict(set)
    successors = defaultdict(set)
    for key, events in payloads.items():
        for event in events:
            if event.target:
                target = event.kind, event.vendor, event.target
                neighbors[key].add(target)
                neighbors[target].add(key)
                successors[target].add(key)

    seeds = {key for key, events in payloads.items() if len(events) > 1}
    seeds.update(key for key, children in successors.items() if len(children) > 1)
    quarantine = {}
    for seed in sorted(seeds):
        if seed in quarantine:
            continue
        component = set()
        stack = [seed]
        while stack:
            key = stack.pop()
            if key not in component:
                component.add(key)
                stack.extend(neighbors[key] - component)
        reason = "conflicting event payloads or competing revisions in chain: "
        reason += ", ".join(key[2] for key in sorted(component))
        quarantine.update(dict.fromkeys(component, reason))
    return quarantine


def active_ledger(rows):
    payloads = defaultdict(set)
    for row in rows:
        if row.event:
            payloads[row.event.key].add(row.event)
    quarantine = revision_quarantine(payloads)
    seen = set()
    active = {}
    for row in rows:
        event = row.event
        if event is None:
            continue
        if event.key in quarantine:
            row.decision = "id_conflict" if len(payloads[event.key]) > 1 else "revision_conflict"
            row.reason = quarantine[event.key]
            continue
        if event.key in seen:
            row.decision = "replay"
            row.reason = "same event identity and normalized payload"
            continue
        seen.add(event.key)
        if event.operation != "record":
            target_key = event.kind, event.vendor, event.target
            prior = active.get(target_key)
            if prior is None:
                row.decision = "invalid_revision"
                row.reason = "target must already be active in this vendor and feed"
                continue
            del active[target_key]
            prior.decision = "reversed" if event.operation == "reverse" else "superseded"
            prior.reason = f"{event.operation} event {event.event_id} retired this record"
            if event.operation == "reverse":
                row.decision = "reversal_applied"
                row.reason = f"retired {event.target}"
                continue
        active[event.key] = row
    return active.values()


def find_po(event, orders):
    if event.po_line:
        candidates = [po for po in orders if po.po_line == event.po_line]
    else:
        candidates = [po for po in orders if po.sku == event.sku]
    if len(candidates) > 1:
        return None, "ambiguous_po_line", "candidate PO events: " + ", ".join(sorted(po.event_id for po in candidates))
    if not candidates:
        return None, "no_po_line", "no matching PO line for this vendor and order"
    po = candidates[0]
    if sum(candidate.po_line == po.po_line for candidate in orders) > 1:
        return None, "ambiguous_po_line", "multiple active PO events claim this line"
    if po.sku != event.sku:
        return None, "sku_mismatch", f"PO event {po.event_id} has SKU {po.sku}"
    if event.date < po.date:
        return None, "before_po_date", f"document date precedes PO event {po.event_id}"
    return po, "", ""


def uncertain_receipts(rows, orders):
    by_po = defaultdict(set)
    for row in rows:
        receipt = row.event
        if row.kind != "receipt" or row.decision not in {"id_conflict", "revision_conflict", "ambiguous_po_line"}:
            continue
        if receipt.operation == "reverse":
            continue
        for po in orders.get((receipt.vendor, receipt.po), ()):
            if po.sku != receipt.sku or (receipt.po_line and receipt.po_line != po.po_line):
                continue
            if receipt.date >= po.date:
                by_po[po.key].add(receipt)
    return by_po


def exact_cents(numerator, denominator):
    cents, remainder = divmod(numerator, denominator)
    return cents if not remainder else None


def reconcile_line(line, receipts, uncertain, prior_billed):
    row, po = line.source, line.po
    invoice = row.event
    confirmed = sorted(
        (receipt for receipt in receipts if receipt.date <= invoice.date),
        key=lambda receipt: (receipt.date, receipt.event_id),
    )
    row.receipts = tuple(receipt.event_id for receipt in confirmed)
    row.uncertain_receipts = tuple(sorted({
        receipt.event_id for receipt in uncertain if receipt.date <= invoice.date
    }))
    line.expected_cents = exact_cents(po.amount_cents * invoice.eaches, po.eaches)
    if line.expected_cents is not None:
        line.delta_cents = invoice.amount_cents - line.expected_cents

    if line.first_entry != invoice.event_id:
        row.decision = "possible_duplicate_invoice"
        row.reason = (f"same vendor invoice, PO line, date, quantity and amount as active event {line.first_entry}; "
                      "hold this entry pending document review")
        return

    line.cumulative_billed_eaches = prior_billed + invoice.eaches

    flags = []
    if row.uncertain_receipts:
        flags.append(("uncertain_receipts", "quantity coverage requires review of conflicting or ambiguous receipt evidence"))
    else:
        line.delivered_eaches = sum(receipt.eaches for receipt in confirmed)
        available = max(0, min(po.eaches, line.delivered_eaches) - prior_billed)
        line.covered_eaches = min(invoice.eaches, available)
        line.unbacked_eaches = invoice.eaches - line.covered_eaches
        line.unbacked_po_cents = exact_cents(po.amount_cents * line.unbacked_eaches, po.eaches)
        if line.cumulative_billed_eaches > line.delivered_eaches:
            flags.append(("insufficient_receipts", "cumulative billed eaches exceed receipts on the invoice date"))
        if line.unbacked_po_cents is None:
            flags.append(("fractional_exposure_cent", "PO rate produces fractional cents for unbacked units"))

    if line.cumulative_billed_eaches > po.eaches:
        flags.append(("over_ordered_quantity", "cumulative billed eaches exceed ordered eaches"))
    if line.expected_cents is None:
        flags.append(("fractional_expected_cent", "PO rate produces a fractional cent for this invoice quantity"))
    elif line.delta_cents:
        flags.append(("price_variance", "actual invoice charge differs from the PO rate"))

    if flags:
        row.decision = flags[0][0]
        row.reason = "; ".join(reason for _, reason in flags)
    else:
        row.decision = "matched"
        row.reason = "invoice quantity covered by PO and receipts and charge matches PO rate"


def reconcile(rows):
    active = active_ledger(rows)
    orders = defaultdict(list)
    line_counts = Counter()
    for row in active:
        if row.kind == "po":
            po = row.event
            orders[po.vendor, po.po].append(po)
            line_counts[po.vendor, po.po, po.po_line] += 1

    receipts = defaultdict(list)
    invoices = []
    for row in active:
        event = row.event
        if row.kind == "po":
            if line_counts[event.vendor, event.po, event.po_line] == 1:
                row.decision = "po_recorded"
                row.reason = "normalized PO line"
            else:
                row.decision = "ambiguous_po_line"
                row.reason = "multiple active PO events claim this line"
            continue
        po, row.decision, row.reason = find_po(event, orders.get((event.vendor, event.po), ()))
        if po:
            row.po_event = po.event_id
        if row.kind == "receipt":
            if po:
                receipts[po.key].append(event)
                row.decision = "receipt_linked"
                row.reason = "delivery counted on its receipt date"
        else:
            invoices.append(InvoiceLine(row, po))

    uncertain = uncertain_receipts(rows, orders)
    # Elect the first surviving import for each document-line signature. Allocation still
    # follows invoice date and event ID, even when the first import sorts after its copy.
    first_entries = {}
    for line in invoices:
        if line.po:
            event = line.source.event
            signature = (line.po.key, event.invoice, event.date, event.eaches, event.amount_cents)
            line.first_entry = first_entries.setdefault(signature, event.event_id)

    billed = defaultdict(int)
    for line in sorted(invoices, key=lambda line: (line.source.event.date, line.source.event.key)):
        if line.po:
            key = line.po.key
            reconcile_line(line, receipts[key], uncertain[key], billed[key])
            if line.cumulative_billed_eaches is not None:
                billed[key] = line.cumulative_billed_eaches
    return invoices


def price_book(lines):
    proposals = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    for line in lines:
        if line.source.decision != "matched":
            continue
        invoice = line.source.event
        cents = exact_cents(invoice.amount_cents, invoice.eaches)
        if cents is None:
            line.source.reason += "; export requires a whole-cent per-each rate"
            continue
        proposals[invoice.device][line.po.date][cents].append(line)

    prices = []
    evidence = []
    for device, history in sorted(proposals.items()):
        if any(len(rates) > 1 for rates in history.values()):
            for line in lines:
                if line.source.decision == "matched" and line.source.event.device == device:
                    line.source.reason += "; device suppressed because matched rates conflict on an effective date"
            continue
        starts = sorted(history)
        ends = starts[1:] + [""]
        for start, end in zip(starts, ends):
            rate, contributing_lines = next(iter(history[start].items()))
            prices.append((device, start, end, rate, "USD", "reconciled_invoice"))
            for line in contributing_lines:
                event = line.source.event
                evidence.append((device, start, rate, event.vendor, event.invoice, event.event_id,
                                 line.po.event_id, str(line.source.path), line.source.line))
    return prices, evidence

# Hospital Purchasing Reconciliation

A reproducible three-way match for hospital purchase orders, deliveries, and invoices. The two batches and vendor mappings contain entirely fictional records.

## Follow one box into a surgical case

PO-100 orders MESH-A by the box. Invoice INV-380 bills six individual implants. The reconciler checks the receipt, converts the box price to cents per implant and exports a dated catalog row. The companion `surgical-case-outcomes` project uses that row to price the quantity implanted on each surgery date.

| Synthetic record | Original | After the source price change |
| --- | ---: | ---: |
| PO-100, six implants per box | $720.00 | $726.00 |
| INV-380, invoice price per implant | $120.00 | $121.00 |
| Exported MESH-A rate in cents per implant | 12,000 | 12,100 |
| Surgical case S001, one MESH-A plus fixation and other case costs | $4,304.50 | $4,305.50 |

The original export traces MESH-A to PO event `P-A` and invoice event `I-A1`. The walkthrough changes the PO, its replay in the second batch, and the invoice in a temporary copy. It reruns both pipelines and verifies that **55 of 113 case totals increase by $1**. [Original purchase order](fixtures/01_initial/po_lines.csv) · [Invoice](fixtures/01_initial/invoices.csv) · [Exported price evidence](build/device_price_evidence.csv)

With both repositories checked out side by side, run this from the purchasing repository:

```sh
python3 ../surgical-case-outcomes/examples/check_purchasing_handoff.py --purchasing .
```

## What the demo finds

| Invoice | Finding |
| --- | --- |
| HORIZON INV-480 | The initial snapshot shows a $100 overcharge. The follow-up reverses this invoice. INV-481 passes at $175 per device. |
| STERILE INV-391 | A first delivery covered six units. After the second delivery is corrected to a box of six, receipts cover all twelve units across the two invoices. INV-391 charges $750 for the second box, $30 above the $720 PO rate. |
| HORIZON INV-610 and INV-611 | The first invoice bills 100 units against 80 received, leaving 20 unbacked at $10 each ($200). The next invoice adds 10 unbacked units ($100). Both use the correct PO rate. |
| HORIZON INV-620 | Two source rows give receipt R-Q the same identity and disagree on quantity. The invoice goes to review with quantity exposure unknown. |
| HORIZON INV-499 | The invoice omits a line number and two lines on the PO use the same SKU. The PO match and amounts remain unknown. |

## Run locally

From this directory, use Python 3.11 or newer. The demo uses Python's standard library and writes files locally.

```sh
python3 -m purchasing
python3 -m unittest discover -s tests -v
```

The demo reconciles the initial batch into `build/before/` and both batches into `build/`, with 11 and 31 source rows in the respective audits. To run a custom snapshot, pass its batch folders in ingestion order. Each folder contains `po_lines.csv`, `receipts.csv`, and `invoices.csv`.

```sh
python3 -m purchasing fixtures/01_initial fixtures/02_followup --output build
```

## Review the evidence

- [Invoice differences](build/invoice_differences.csv) groups active exceptions by vendor and invoice. Positive price variance means the vendor charged more than the PO rate for the billed quantity. Quantity exposure values incremental unbacked units at the PO rate. The unknown-line counts indicate where totals are partial or blank.
- [Row audit](build/row_audit.csv) records every ingested source row, including replays, retired events, and quarantined candidates. Each row includes the input file and line, source fields under canonical column names, a reason, and applicable PO and receipt identities. Use vendor, feed, and event ID together to locate receipt evidence.
- The local SQLite snapshot `build/reconciliation.sqlite` contains the audit and active invoice lines. The invoice lines join to the audit on `audit_id`. Run the [invoice differences query](purchasing/invoice_differences.sql) against the snapshot to regenerate the exception report.
- [Device prices](build/device_prices.csv) exports USD cents per each. The demo publishes MESH-A at 12,000 cents, MESH-B at 17,500 cents, and FIX-1 at 2,100 cents through June 30, 2025 and 2,400 cents starting July 1. The [price evidence](build/device_price_evidence.csv) traces each published rate back to its PO, invoice, and source row. See the [shared CSV contract](docs/device-prices.md).

```sh
sqlite3 -csv -header build/reconciliation.sqlite < purchasing/invoice_differences.sql
```

The loader resolves header aliases and validates each feed's schema, including empty feeds. Vendor aliases and pack sizes come from explicit mappings. An optional currency column on POs or invoices must use USD. Events with the same feed, canonical vendor, ID, and normalized business payload are replays. A replacement or reversal targets an earlier active receipt or invoice in ingestion order. Competing revisions and conflicting payloads quarantine the affected chain regardless of branch order.

The pipeline rebuilds a snapshot from the supplied batches. It applies revisions before matching, so corrected receipt evidence uses the replacement's document date. It assigns coverage by invoice date and then by event ID for lines on the same day. Earlier active billed quantities consume available coverage up to the lesser of ordered and received quantity. Conflicting or ambiguous receipt evidence leaves affected quantity coverage unknown. PO-based price variance can still be calculated when the PO line is known.

The export requires a matched invoice with a whole-cent per-each rate. The matched PO date starts the price period, and the next eligible price date ends it. The latest eligible period stays open. If eligible rates for one device disagree on the same start date, the device is withheld from the export.

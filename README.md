# Hospital Purchasing Reconciliation

A reproducible three-way match for hospital purchase orders, deliveries, and invoices. The demo and the held-out evaluation use fictional transactions and vendor mappings.

## Held-out purchasing scorecard

We froze the reconciler at `6f56105`, then committed the batch and its expected answers at `a2a89f7` before the first run. Nine invoice lines adapt examples written by Microsoft, ERPNext and Oracle. Eight more are locally authored controls. The CSVs and labels were prepared here from the source scenarios. This measures performance on these specific cases. [Case provenance and scoring rules](evaluation/held-out-v1/PROTOCOL.md)

<!-- held-out-v1:start -->
**16 of 17 invoice lines matched the answer key** on both routing and amounts.
Of 7 lines accepted as clean, **1 was a false accept**.
The pipeline sent 2 to review. The answer key called for review on 2 of those lines.

| Expected decision | Accepted as clean | Flagged a discrepancy | Sent to review | Missing |
| --- | ---: | ---: | ---: | ---: |
| Clean | 6 | 0 | 0 | 0 |
| Exception | 0 | 8 | 0 | 0 |
| Review | 1 | 0 | 2 | 0 |

Exact results by origin were 8/9 for externally sourced cases and 8/8 for local controls.
Unexpected output lines: 0. Correctly routed lines with wrong amounts: 0.
<!-- held-out-v1:end -->

- **Correct discrepancy.** On the final drive invoice (M06), the vendor billed 200 at $10.80 against a $10 order. Earlier invoices had consumed 900 of the 1,000 delivered drives. The engine reported a $160 price difference and separately identified 100 units without remaining coverage, worth $1,000 at the PO rate.
- **Correct review.** C05 could refer to either of two PO lines, priced at $44 and $46. C06 had a single receipt reference reporting both four and six lamps. The engine withheld the unresolved quantities for both, while retaining the known $0 price difference on C06.
- **Wrongly accepted as clean.** O01 and O02 represent one $138 supplier invoice entered as `bg402` and `BG402` under different import IDs. The 12 received lamps covered both three-lamp entries, so the engine accepted both. This exposes $138 to possible duplicate payment. In the control pair C01/C02, two distinct $184 invoices and deliveries were correctly accepted.

The line-level scorecard includes the expected and actual decision, price difference, unbacked quantity and dollar exposure. Each result names the PO and receipts used by the engine. [Score all 17 lines](evaluation/held-out-v1/results.csv) · [Inspect the source invoices](evaluation/held-out-v1/batch/invoices.csv) · [Read the locked answer key](evaluation/held-out-v1/expected.csv)

To reproduce the score from a full Git clone, run the command below. It runs the pinned engine and batch in a temporary directory, then checks the README totals and the published CSV against the run.

```sh
python3 evaluation/score_holdout.py
```

## Duplicate-invoice regression

The updated engine sends the second $138 entry (O02) to review and links it to the first import (O01). Both distinct $184 invoices (C01 and C02) still pass. The regression test checks all 17 original decisions and their amounts. The scorecard above records the first, frozen run. [Regression tests](tests/test_duplicate_invoices.py)

After reversals and replacements, the engine compares active entries using the normalized supplier invoice number, resolved vendor and PO line, invoice date, quantity in eaches, and total merchandise charge. The first surviving import retains its normal reconciliation decision. A later identical signature has its own audit row and a known price difference when calculable. Its quantity exposure stays unknown pending review, and it contributes neither a second price variance to the invoice total nor a catalog price. Distinct invoice numbers remain separate. A complete voucher-to-receipt application extract can resolve a repeat as a split delivery.

### Clearing a split delivery

Supply an optional application snapshot with the columns `vendor_id,invoice_event_id,voucher_id,voucher_line,invoice,receipt_event_id,applied_eaches,applied_cents`. Each row links one voucher line to an accepted shipment. Use stable receipt event IDs at shipment grain and voucher IDs scoped to the source business unit. Quantities are in eaches and amounts in integer USD cents. The application snapshot must cover all claims against the shipments in scope.

```sh
python3 -m purchasing path/to/batch --mappings path/to/mappings --receipt-applications path/to/applications.csv --output build/review
```

The repeated lines must have the same source invoice number and voucher, distinct voucher lines and disjoint shipments. The engine checks the quantities and amounts for every billed line on that PO line, verifies that the order covers the billed quantities and checks the total claimed against each shipment. A third line can share a shipment if every claim reconciles to the accepted quantity or less. An incomplete crosswalk, conflicting receipt, overlapping shipments within the repeat group, or an overdraw leaves the duplicate hold in place.

Identity is resolved using the supplied snapshot. Receipt coverage still uses the invoice date. If the assigned second shipment arrived later, the retroactive identity check clears the duplicate hold and reports the original shortfall. The SQLite table `verified_receipt_applications` traces the result to the voucher line, shipment, applied units and cents. The row audit records the decision. [Synthetic split-delivery and ambiguity tests](tests/test_split_deliveries.py)

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

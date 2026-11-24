# Hospital Purchasing Reconciliation

## Which invoices should accounts payable review first?

This Python and SQLite demo matches fictional purchase orders, receipts, and invoices. Three examples from the evaluation batch show what an accounts payable analyst can do with the current results.

| Finding | Evidence | AP decision |
| --- | --- | --- |
| Possible duplicate payment · $138 | O01 and O02 entered the same supplier document as `bg402` and `BG402`. The PO and receipt had enough units to cover both entries. | Hold the second entry and verify it against the supplier document. |
| Price and quantity discrepancy · M06 | The supplier billed 200 drives at $10.80 against a $10.00 PO price, a **$160 price difference**. Earlier invoices had used 900 of 1,000 delivered units, leaving **100 units without coverage**, worth $1,000 at the PO rate. | Dispute the price difference and check receiving records for the 100 units. |
| Two legitimate $184 invoices | C01 and C02 have different invoice numbers and separate deliveries of four lamps each. | Accept both through the three-way match. |

**The original held-out run scored 16/17.** It accepted the second $138 entry. After the fix, the current engine matches the decisions and amounts on **17/17 of those same lines** in regression testing. [Frozen scorecard](#held-out-purchasing-scorecard) · [Source invoices](evaluation/held-out-v1/batch/invoices.csv) · [Regression tests](tests/test_duplicate_invoices.py)

The current AP summary accounts for all 17 source invoice rows in this batch. Six matched, eight have a discrepancy, and three need review. The summary counts source rows, so two entries for the same supplier document remain visible.

<details>
<summary>Reproduce the AP table with SQL</summary>

Run the current engine on the evaluation batch from this repository, then query the five invoice lines behind the three decisions. Monetary columns are in USD cents. SQL <code>NULL</code> means the amount or quantity is unresolved.

```sh
python3 -m purchasing evaluation/held-out-v1/batch --mappings evaluation/held-out-v1 --output build/ap-review
sqlite3 -header -column build/ap-review/reconciliation.sqlite <<'SQL'
SELECT event_id, invoice, actual_cents, decision,
       delta_cents, unbacked_eaches, unbacked_po_cents
FROM invoice_lines
WHERE event_id IN ('O01', 'O02', 'M06', 'C01', 'C02')
ORDER BY event_id;
SQL
```

</details>

## Follow one box into a surgical case

PO-100 orders MESH-A by the box. Invoice INV-380 bills six individual implants. The reconciler checks the receipt, converts the box price to cents per implant and exports a dated catalog row. The surgical project uses that catalog row on the surgery date. [See the price change from the surgical side](https://github.com/chandlerbrown4/surgical-case-outcomes#follow-a-purchase-price-into-the-operating-room)

| Synthetic record | Original | After the source price change |
| --- | ---: | ---: |
| PO-100, six implants per box | $720.00 | $726.00 |
| INV-380, invoice price per implant | $120.00 | $121.00 |
| Exported MESH-A rate in cents per implant | 12,000 | 12,100 |
| Surgical case S001, one MESH-A plus fixation and other case costs | $4,304.50 | $4,305.50 |

The original export traces MESH-A to PO event `P-A` and invoice event `I-A1`. The walkthrough changes the PO, its replay in the second batch, and the invoice in a temporary copy. It reruns both pipelines and verifies that **55 of 113 case totals increase by $1**. [Original purchase order](fixtures/01_initial/po_lines.csv) · [Invoice](fixtures/01_initial/invoices.csv) · [Exported price evidence](examples/outputs/device_price_evidence.csv)

GitHub Actions ran the walkthrough from fresh checkouts of both repositories. [View the verified walkthrough](https://github.com/chandlerbrown4/surgical-case-outcomes/actions/runs/45445859485/job/138629333952)

With both repositories checked out side by side, run the walkthrough from the purchasing repository.

```sh
python3 ../surgical-case-outcomes/examples/check_purchasing_handoff.py --purchasing .
```

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

Two other lines show why amounts can remain unknown. C05 could refer to either of two PO lines priced at $44 and $46. C06 has a single receipt reference reporting both four and six lamps. The engine withheld the unresolved quantities for both and retained the known $0 price difference on C06.

The line-level scorecard includes the expected and actual decision, price difference, unbacked quantity and dollar exposure. Each result names the PO and receipts used by the engine. [Score all 17 lines](evaluation/held-out-v1/results.csv) · [Inspect the source invoices](evaluation/held-out-v1/batch/invoices.csv) · [Read the locked answer key](evaluation/held-out-v1/expected.csv)

To reproduce the score from a full Git clone, run the command below. It runs the pinned engine and batch in a temporary directory, then checks the README totals and the published CSV against the run.

```sh
python3 evaluation/score_holdout.py
```

## How the duplicate check works

After reversals and replacements, the engine compares entries with the same normalized supplier invoice number, resolved vendor and PO line, quantity in eaches, and total merchandise charge. It checks the invoice date against earlier surviving originals. A date on the same calendar day, one day earlier, or one day later triggers review. A held copy cannot extend that window. The AP register gives the dates and event IDs so an analyst can find both entries.

For example, moving the second $138 entry from March 3 to March 4 still holds O02 against O01. Moving the second $184 invoice to March 4 still clears both B-801 and B-802 because their supplier invoice numbers differ and both deliveries cover the bills. These date variations are regression cases. [Date-window tests](tests/test_duplicate_invoices.py)

The original retains its normal decision. The held row keeps its own audit record and any known price difference. Its quantity exposure remains unknown pending review. It consumes no receipt coverage, adds no second price variance to the invoice total, and supplies no catalog price. A complete voucher-to-receipt application extract can resolve the repeat as a split delivery.

### Clearing a split delivery

Supply an optional application snapshot with the columns `vendor_id,invoice_event_id,voucher_id,voucher_line,invoice,receipt_event_id,applied_eaches,applied_cents`. Each row links one voucher line to an accepted shipment. Use stable receipt event IDs at shipment grain and voucher IDs scoped to the source business unit. Quantities are in eaches and amounts in integer USD cents. The application snapshot must cover all claims against the shipments in scope.

```sh
python3 -m purchasing path/to/batch --mappings path/to/mappings --receipt-applications path/to/applications.csv --output build/review
```

The repeated lines must share a source invoice number and voucher and have distinct voucher line IDs. The engine checks billed quantities and amounts for every line on the PO line, the quantity ordered, and every claim against the shipments. Disjoint shipments can support a split delivery.

For lines dividing a single shipment, include the optional `billed_eaches` column from the voucher application source. Each line’s independently reported billed quantity must agree with its invoice line. Applied quantities and cents must reconcile line by line, and the repeated lines together must account for the entire accepted shipment. Missing or contradictory evidence keeps the duplicate hold. Partial overlap across multiple shipments also stays in review. A merchandise match can coexist with a separate ledger discrepancy.

Identity is resolved using the supplied snapshot. Receipt coverage still uses the invoice date. If the assigned second shipment arrived later, the retroactive identity check clears the duplicate hold and reports the original shortfall. The SQLite table `verified_receipt_applications` traces the result to the voucher line, shipment, applied units and cents. The row audit records the decision. [Synthetic split-delivery and ambiguity tests](tests/test_split_deliveries.py)

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

- The [AP summary](examples/outputs/ap_summary.csv) counts every invoice source row once. The [invoice register](examples/outputs/ap_invoice_rows.csv) gives each row a source location, decision, reason, and available amounts. **Matched** rows pass reconciliation. **Exception** rows have a confirmed discrepancy. **Review** rows need evidence. **Rejected** rows need a source correction. **History** records replays, retired entries, and applied reversals. History stays out of payable totals.
- [Invoice differences](examples/outputs/invoice_differences.csv) groups active exceptions by vendor and invoice. Positive price variance means the vendor charged more than the PO rate for the billed quantity. Quantity exposure values incremental unbacked units at the PO rate. The unknown-line counts indicate where totals are partial or blank.
- [Row audit](examples/outputs/row_audit.csv) records every ingested source row, including replays, retired events, and quarantined candidates. Each row includes the input file and line, source fields under canonical column names, a reason, and applicable PO and receipt identities. Use vendor, feed, and event ID together to locate receipt evidence.
- The local SQLite snapshot `build/reconciliation.sqlite` contains the two AP views, the audit, active invoice lines, price catalog, and price evidence. Each CSV is exported from the SQLite table or view of the same name. The invoice lines join to the audit on `audit_id`. Run the [invoice differences query](purchasing/invoice_differences.sql) against the snapshot to regenerate the exception report.
- [Device prices](examples/outputs/device_prices.csv) exports USD cents per each. The demo publishes MESH-A at 12,000 cents, MESH-B at 17,500 cents, and FIX-1 at 2,100 cents through June 30, 2025 and 2,400 cents starting July 1. The [price evidence](examples/outputs/device_price_evidence.csv) traces each published rate back to its PO, invoice, and source row. See the [shared CSV contract](docs/device-prices.md).

```sh
sqlite3 -csv -header build/reconciliation.sqlite < purchasing/invoice_differences.sql
```

Each successful build closes the SQLite database and finishes all six CSVs before switching one directory pointer. The familiar filenames under `build/` all resolve through that pointer. If the process stops before the switch, AP sees the previous complete snapshot. After the switch, AP sees the complete new one. This requires a filesystem that supports symbolic links and atomic rename. The before and after demo outputs are each a separate snapshot.

For a multi-file read during a rebuild, use the directory printed with the invoice register, or resolve `build/.purchasing/current` once and open every file there. Prior generations remain available for readers and accumulate under `.purchasing/`. A file-level error such as an unsupported currency or unreadable schema fails the batch before publication. A readable invoice with an invalid quantity appears as rejected with its original source value in the reason.

The loader resolves header aliases and validates each feed's schema, including empty feeds. Vendor aliases and pack sizes come from explicit mappings. An optional currency column on POs or invoices must use USD. Events with the same feed, canonical vendor, ID, and normalized business payload are replays. A replacement or reversal targets an earlier active receipt or invoice in ingestion order. Competing revisions and conflicting payloads quarantine the affected chain regardless of branch order.

The pipeline rebuilds a snapshot from the supplied batches. It applies revisions before matching, so corrected receipt evidence uses the replacement's document date. It assigns coverage by invoice date and then by event ID for lines on the same day. Earlier active billed quantities consume available coverage up to the lesser of ordered and received quantity. Conflicting or ambiguous receipt evidence leaves affected quantity coverage unknown. PO-based price variance can still be calculated when the PO line is known.

The export requires a matched invoice with a whole-cent per-each rate. The matched PO date starts the price period, and the next eligible price date ends it. The latest eligible period stays open. If eligible rates for one device disagree on the same start date, the device is withheld from the export.

# Held-out purchasing batch v1

The purchasing engine and its documented rules were frozen at `6f561052eb096886b6b24f22a9efb00de3869666` (local tag `held-out-v1-baseline`). This directory contains the inputs, mappings and answer key committed before the first run. The executable receives only the three transaction feeds and two mappings. It has no access through its input interface to the answer key. None of the rows or mappings come from a hospital archive or from the development fixtures.

## Where the cases came from

| Invoice events | Independent source | Adaptation to the CSV contract |
| --- | --- | --- |
| M01–M03 | Microsoft's two three-way purchasing examples | The ordered, received and billed quantities and unit prices come from the examples. The unposted receipts stay absent. The CNC example passes Microsoft's configured tolerance. Here its merchandise variance is $500 under an exact-price rule. The USB line from the mixed-policy example uses two-way matching and is omitted. [Three-way matching policies](https://learn.microsoft.com/en-us/dynamics365/finance/accounts-payable/three-way-matching-policies) |
| M04–M06 | Microsoft's sequence of three partial invoices for 1,000 USB drives | The order and the three invoice quantities and prices are from the two-way example. A single on-time delivery of the full order is a local assumption that makes receipt coverage testable. [Invoice matching overview](https://learn.microsoft.com/en-us/dynamics365/finance/accounts-payable/accounts-payable-invoice-matching#two-way-price-totals-matching-and-net-unit-price-matching) |
| F01 | ERPNext's independently written purchase order → invoice → receipt workflow test | Its default order is ten units at 500 apiece. The test asserts 100% received and billed before teardown. The CSV uses one calendar date for the invoice and receipt and scores that end-of-day state. [ERPNext test and order factory](https://github.com/frappe/erpnext/blob/version-15/erpnext/buying/doctype/purchase_order/test_purchase_order.py) |
| O01–O02 | Oracle's duplicate invoice example | One genuine supplier invoice for three lamps is entered twice under distinct import event IDs. Only the capitalization of the invoice number changes. Quantities, spare PO coverage, and document IDs are invented. The first entry is valid. The second requires review for duplicate payment risk even though quantities and price appear to reconcile. [Identifying duplicate invoices](https://docs.oracle.com/cd/A60725_05/html/comnls/us/ap/duplinv.htm) |
| C01–C08 | New, locally authored controls | Separate legitimate invoices of equal amounts, pack conversion, an undercharge, two plausible PO lines, a conflicting receipt and a receipt posted after billing. |

The external authors supplied the source scenarios. The CSV transcription, fictional identifiers, calendar dates, controls and answer key were written locally after the freeze, independently of engine output. There was no separate human adjudicator. This is a small purpose-selected challenge set. Its frequencies do not estimate error rates at a hospital.

## Scoring contract

Each input invoice event is scored once. Two lines on one supplier invoice remain two observations. A clean line has a unique supported match, an exact merchandise price, sufficient quantities as of its invoice date, and no documented duplicate concern. A known discrepancy is an exception. Evidence that cannot distinguish competing answers calls for review. For the duplicate case the later import is held for a person to resolve. A general review flag counts as review, but a dropped line does not. Unexpected outputs are reported separately.

The answer key specifies signed total merchandise price variance in USD cents, incremental unbacked eaches, and their exposure at the ordered rate. A blank means unknown, and zero is an observed zero. Separate price and quantity figures should not be added. A row is exactly right only when both its routing decision and all three values agree. A false clean is any invoice the engine accepts that the answer key marks exception or review. Shipping, tax, payment execution and price-catalog exports are outside this score.

Treat the first run as the v1 result. Fixes informed by it can be regression-tested here. A fresh, independently specified batch is needed for another held-out claim.

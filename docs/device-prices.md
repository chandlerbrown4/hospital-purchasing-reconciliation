# Device prices

The purchasing demo writes `build/device_prices.csv`. The surgical demo accepts this file with `--prices`. Each row gives the acquisition price of one device for a period. Each project runs offline with its own fictional inputs.

| Field | Meaning |
| --- | --- |
| `device_id` | Stable catalog identifier shared by purchasing and surgical records. |
| `effective_from` | First covered service date, formatted as YYYY-MM-DD. |
| `effective_to` | First date outside the period. An empty value leaves the period open. |
| `unit_price_cents` | Integer cents per individual device after converting purchase packs to units. |
| `currency` | USD in this release. |
| `price_source` | Origin of the accepted price. The demo exports `reconciled_invoice`. |

Match the device ID and the surgery date. Periods for the same device must never overlap. Where no price covers the service date, retain the implant line with an unknown cost. Multiply a matched price by the number of devices implanted. Keep the price source and effective dates available for review.

For example, a fictional box containing six devices costs $720. The normalized price is 12,000 cents per device. Using two of those devices adds $240 to case cost. The hospital charge for the procedure is a separate measure.

With the two repositories side by side, run these commands from their parent directory.

```sh
(cd hospital-purchasing-reconciliation && python3 -m purchasing)
(cd surgical-case-outcomes && python3 -m surgical --prices ../hospital-purchasing-reconciliation/build/device_prices.csv)
```

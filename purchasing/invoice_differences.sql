SELECT vendor,
       invoice,
       COUNT(*) AS active_lines,
       COUNT(delta_cents) AS priced_lines,
       COUNT(*) - COUNT(delta_cents) AS unpriced_lines,
       SUM(CASE WHEN decision <> 'matched' THEN 1 ELSE 0 END) AS exception_lines,
       SUM(CASE WHEN delta_cents <> 0 AND decision <> 'possible_duplicate_invoice' THEN 1 ELSE 0 END) AS price_variance_lines,
       SUM(delta_cents) FILTER (WHERE decision <> 'possible_duplicate_invoice') AS priced_delta_cents,
       SUM(covered_eaches) AS covered_eaches,
       SUM(unbacked_eaches) AS unbacked_eaches,
       SUM(unbacked_po_cents) AS unbacked_po_cents,
       COUNT(*) - COUNT(unbacked_po_cents) AS unknown_exposure_lines
FROM invoice_lines
GROUP BY vendor, invoice
HAVING SUM(CASE WHEN decision <> 'matched' THEN 1 ELSE 0 END) > 0
ORDER BY vendor, invoice;

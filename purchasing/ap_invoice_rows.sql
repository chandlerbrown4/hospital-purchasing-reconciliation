SELECT a.audit_id, a.batch, a.source_file, a.source_line, a.vendor,
       COALESCE(i.invoice, json_extract(a.normalized_event, '$.invoice'),
                json_extract(a.source_fields, '$.invoice'), '') AS invoice,
       a.event_id,
       CASE
           WHEN a.decision = 'matched' THEN 'matched'
           WHEN a.decision IN ('insufficient_receipts', 'over_ordered_quantity', 'price_variance') THEN 'exception'
           WHEN a.decision IN ('invalid', 'invalid_revision') THEN 'rejected'
           WHEN a.decision IN ('replay', 'reversed', 'superseded', 'reversal_applied') THEN 'history'
           ELSE 'review'
       END AS disposition,
       a.decision, a.reason, a.target_event_id, i.actual_cents,
       i.delta_cents, i.unbacked_eaches, i.unbacked_po_cents
FROM row_audit AS a
LEFT JOIN invoice_lines AS i USING (audit_id)
WHERE a.kind = 'invoice'

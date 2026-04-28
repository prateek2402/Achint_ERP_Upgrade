# Legacy SQLite Import Mapping

## Source databases
- Legacy source: `old_erp.sqlite`
- Current target: `erp_database.sqlite`

The legacy database contains:
- `users` table
- `erp_data` table containing the latest serialized application JSON (`json_data`)

## Deterministic import order
1. `system_settings`
2. `users`
3. `clients`
4. `purchase_orders`
5. `po_baseline_items`
6. `invoices`
7. `invoice_dispatch_items`
8. `payment_history`
9. `payment_allocations`
10. Client ledger recomputation (`recalculate_client_ledger` equivalent)

## Truncate order (FK safe)
1. `payment_allocations`
2. `payment_history`
3. `invoice_dispatch_items`
4. `invoices`
5. `po_baseline_items`
6. `purchase_orders`
7. `clients`
8. `users`
9. `system_settings`

## Legacy to current mapping

### users table
- `users.username` -> `User.username`
- `users.password` -> `User.hashed_password` (hashed during import)
- `users.role` -> `User.role`

### _settings blob
- `_settings.exchangeRate` -> `SystemSettings.exchange_rate`
- `_settings.customColumns` -> `SystemSettings.custom_columns` (JSON string)

### Client envelope
- `<client_key>` -> `Client.name`
- `client.active` -> `Client.active`
- `client.excess` / `client.unallocated` -> seed `Client.excess_funds` before recalculation

### PO terms
- `poTerms[poNo].advPct` or `adv` -> `PurchaseOrder.adv_pct`
- `poTerms[poNo].retPct` or `ret` -> `PurchaseOrder.ret_pct`
- `poTerms[poNo].retBase` or `base` -> `PurchaseOrder.ret_base`
- `poTerms[poNo].tdsEnabled` -> `PurchaseOrder.tds_enabled`
- `poTerms[poNo].tdsRate` -> `PurchaseOrder.tds_rate`
- `poTerms[poNo].tdsThreshold` -> `PurchaseOrder.tds_threshold`
- `client.poAdvances[poNo]` -> `PurchaseOrder.advance_pool`

### PO baseline items (if present)
- `baselineItems[].description` -> `PoBaselineItem.description`
- `baselineItems[].ordered_qty` / `orderedQty` / `qty` -> `PoBaselineItem.ordered_qty`
- `baselineItems[].inspected_qty` / `inspectedQty` -> `PoBaselineItem.inspected_qty`
- `baselineItems[].uom` -> `PoBaselineItem.uom`

### Invoices
- `invoice.id` -> `Invoice.invoice_no`
- `invoice.poNo` -> linked `PurchaseOrder.po_no` (`UNASSIGNED` becomes null po_id)
- `invoice.subEntity` -> `Invoice.sub_entity`
- `invoice.lrNo` -> `Invoice.lr_no`
- `invoice.invDate` -> `Invoice.inv_date`
- `invoice.dueDate` -> `Invoice.due_date`
- `invoice.basic` -> `Invoice.basic`
- `invoice.gst` -> `Invoice.gst`
- `invoice.total` -> `Invoice.total`
- `invoice.advance` -> `Invoice.advance_adj`
- `invoice.tds` -> `Invoice.tds_ded`
- `invoice.retention` -> `Invoice.retention_held`
- `invoice.netPayable` -> `Invoice.net_payable`
- `invoice.paid` -> `Invoice.paid`
- `invoice.balance` -> `Invoice.balance`
- `invoice.isNote` -> `Invoice.is_note`
- `invoice.noteType` -> `Invoice.note_type`
- `invoice.noteReason` -> `Invoice.note_reason`

### Dispatch items (if present)
- `dispatchItems[].description` -> `InvoiceDispatchItem.description`
- `dispatchItems[].qty` / `dispatched_qty` -> `InvoiceDispatchItem.dispatched_qty`
- `dispatchItems[].inspected_qty` / `inspectedQty` -> `InvoiceDispatchItem.inspected_qty`
- `dispatchItems[].uom` -> `InvoiceDispatchItem.uom`

### Payments
- `payment.id` -> `PaymentHistory.id`
- `payment.date` -> `PaymentHistory.date`
- `payment.type` -> `PaymentHistory.type`
- `payment.amount` -> `PaymentHistory.amount`
- `payment.details` -> `PaymentHistory.details`
- `payment.note` -> `PaymentHistory.note`

### Payment allocations
- `alloc.type` -> `PaymentAllocation.alloc_type`
- `alloc.id` / `alloc.invId` -> `PaymentAllocation.target_inv_id`
- `alloc.po` -> `PaymentAllocation.target_po_no`
- `alloc.noteId` -> `PaymentAllocation.note_id`
- `alloc.amount` -> `PaymentAllocation.amount`

## Key strategy for links
- PO link key: `po_no` string
- Invoice link key: `invoice_no` string
- Payment link key: legacy `payment.id` string (deduplicated if collisions)
- Allocation link key: attached by inserted `payment.id`

## Validation checks after import
- No allocations referencing missing payment IDs
- No allocations referencing missing invoice numbers for `alloc_type=invoice`
- No dispatch items referencing missing invoice IDs
- Recomputed client balances non-negative and internally consistent
- Count reconciliation between source and target entities

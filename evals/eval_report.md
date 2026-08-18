# Evaluation report - Freight Claim Copilot

Overall: **106/106 checks passed (100.0%)**
Run: provider=claude-cli model=claude-sonnet-5 retrieval=datum | LLM cost this eval: $0.00

## Fact extraction accuracy  (72/72)

- [PASS] snapshot.claim_id
- [PASS] snapshot.carrier
- [PASS] snapshot.pro_number
- [PASS] snapshot.status
- [PASS] snapshot.claim_amount_usd
- [PASS] snapshot.reserve_usd
- [PASS] snapshot.carrier_offer_usd
- [PASS] tms.pieces_tendered
- [PASS] tms.edi_delivered_pieces
- [PASS] tms.weight_lb
- [PASS] tms.freight_charge_usd
- [PASS] tms.service_guaranteed
- [PASS] tms.pickup_date
- [PASS] erp.qty_shipped
- [PASS] erp.unit_price_usd
- [PASS] erp.promotion_launch_date
- [PASS] invoice.invoice_no
- [PASS] invoice.qty_units
- [PASS] invoice.unit_price_usd
- [PASS] invoice.extended_value_usd
- [PASS] invoice.cartons
- [PASS] invoice.units_per_carton
- [PASS] bol.bol_number
- [PASS] bol.pallets
- [PASS] bol.cartons
- [PASS] bol.weight_lb
- [PASS] pod.tendered_cartons
- [PASS] pod.received_cartons
- [PASS] pod.short_cartons
- [PASS] pod.damaged_cartons
- [PASS] pod.consignee_signature
- [PASS] overview.total_demand_usd
- [PASS] overview.carrier_offer_usd
- [PASS] overview.invoice_value_usd
- [PASS] overview.contents_units
- [PASS] email.initial_demand_usd
- [PASS] email.offer_total_usd
- [PASS] email.offer_accepted_missing_units
- [PASS] email.offer_accepted_damaged_units
- [PASS] email.offer_disputed_damaged_units
- [PASS] inspection.report_no
- [PASS] inspection.total_examined
- [PASS] inspection.total_unsellable
- [PASS] inspection.total_repackable
- [PASS] inspection.inspection_fee_usd
- [PASS] inspection.repack_labor_usd
- [PASS] inspection.foam_present
- [PASS] contract.liability_cap_per_lb
- [PASS] contract.notice_cargo_months
- [PASS] contract.notice_delay_days
- [PASS] contract.delay_consequential_excluded
- [PASS] derived.shortage_cartons
- [PASS] derived.shortage_units
- [PASS] derived.damage_units_affected
- [PASS] derived.unsellable_units
- [PASS] derived.repackable_units
- [PASS] derived.unit_weight_lb
- [PASS] derived.guaranteed_service_purchased
- [PASS] entitlement.missing_cap_usd
- [PASS] entitlement.missing_basis_usd
- [PASS] entitlement.damaged_cap_usd
- [PASS] entitlement.damaged_basis_usd
- [PASS] entitlement.cargo_notice_ok
- [PASS] entitlement.delay_notice_ok
- [PASS] position.core_low
- [PASS] position.core_high
- [PASS] position.recommended_counter
- [PASS] position.offer_equals_floor
- [PASS] history.xlsx_consistent
- [PASS] damage_photo_1.cartons_shown_damaged
- [PASS] damage_photo_2.cartons_shown_damaged
- [PASS] derived.photo_cartons_covered

## Discrepancy detection (planted conflicts)  (6/6)

- [PASS] edi_vs_pod_count - D-04
- [PASS] photo_coverage_partial - D-06
- [PASS] delay_demand_vs_contract - D-08
- [PASS] overview_verified - D-09
- [PASS] inspection_reconciles - D-05
- [PASS] demand_decomposition_verified - D-07

## Evidence-gap detection  (4/4)

- [PASS] packaging specification - G-02
- [PASS] Photographs of cartons - G-01
- [PASS] Salvage - G-03
- [PASS] repack labor - G-04

## Entitlement classification & bounds  (7/7)

- [PASS] missing_product
- [PASS] damaged_accepted
- [PASS] damaged_disputed
- [PASS] inspection_fee
- [PASS] repack_labor
- [PASS] late_markdown
- [PASS] freight_refund

## Historical comparables  (2/2)

- [PASS] top-5 includes HC-2025-0067
- [PASS] top-5 includes HC-2025-0118

## Grounding & guardrails  (3/3)

- [PASS] citation validity >= 0.95 - 100.0% (207 exact, 0 fuzzy, 0 failed)
- [PASS] quarantined facts <= 2 - []
- [PASS] position composed & NumberGuard clean - attempts=1 violations=0

## Ablation: inspection report removed (graceful degradation)  (12/12)

- [PASS] pipeline completed
- [PASS] no inspection.* facts invented
- [PASS] damage line downgraded to NEEDS_INFO - NEEDS_INFO
- [PASS] gap raised for the missing report - G-01
- [PASS] report-only detail 'IR-260518-44' absent from brief
- [PASS] report-only detail 'C-017' absent from brief
- [PASS] report-only detail 'C-018' absent from brief
- [PASS] report-only detail 'L. Greene' absent from brief
- [PASS] report-only detail 'IR-260518-44' absent from fact ledger
- [PASS] report-only detail 'C-017' absent from fact ledger
- [PASS] report-only detail 'C-018' absent from fact ledger
- [PASS] report-only detail 'L. Greene' absent from fact ledger

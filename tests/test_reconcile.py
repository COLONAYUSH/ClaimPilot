import unittest

from claimpilot.models import FactLedger
from claimpilot.reconcile import Ctx, r02_piece_counts, r03_damage_units


def _base_ledger():
    led = FactLedger()
    led.add("tms.pieces_tendered", 60)
    led.add("pod.received_cartons", 58)
    led.add("pod.short_cartons", 2)
    led.add("pod.damaged_cartons", 5)
    led.add("invoice.units_per_carton", 4)
    return led


class TestPieceCounts(unittest.TestCase):
    def test_edi_pod_conflict_detected_with_pod_authority(self):
        led = _base_ledger()
        led.add("tms.edi_delivered_pieces", 59)
        ctx = Ctx(led, {})
        r02_piece_counts(ctx)
        conflicts = [d for d in ctx.result.discrepancies
                     if d.category == "COUNT_CONFLICT" and d.severity == "HIGH"]
        self.assertEqual(len(conflicts), 1)
        self.assertIn("signed POD", conflicts[0].authority_note)
        self.assertEqual(led.value("derived.shortage_cartons"), 2)
        self.assertEqual(led.value("derived.shortage_units"), 8)

    def test_agreeing_counts_raise_nothing(self):
        led = _base_ledger()
        led.add("tms.edi_delivered_pieces", 58)
        ctx = Ctx(led, {})
        r02_piece_counts(ctx)
        self.assertFalse([d for d in ctx.result.discrepancies if d.severity == "HIGH"])

    def test_missing_pod_degrades_to_note(self):
        led = FactLedger()
        led.add("tms.pieces_tendered", 60)
        ctx = Ctx(led, {})
        r02_piece_counts(ctx)
        self.assertTrue(any("skipped" in n for n in ctx.result.notes))
        self.assertFalse(ctx.result.discrepancies)


class TestDamageUnits(unittest.TestCase):
    def test_consistent_inspection_verifies(self):
        led = _base_ledger()
        led.add("inspection.total_examined", 20)
        led.add("inspection.total_unsellable", 14)
        led.add("inspection.total_repackable", 6)
        led.add("inspection.carton_rows", [
            {"carton_id": "C-{}".format(i), "units": 4, "unsellable": u, "repackable": 4 - u}
            for i, u in zip(range(1, 6), [3, 2, 4, 2, 3])])
        ctx = Ctx(led, {})
        r03_damage_units(ctx)
        verified = [d for d in ctx.result.discrepancies
                    if d.status == "VERIFIED_CONSISTENT"]
        self.assertEqual(len(verified), 1)
        self.assertEqual(led.value("derived.damage_units_affected"), 20)

    def test_inconsistent_totals_flagged(self):
        led = _base_ledger()
        led.add("inspection.total_examined", 20)
        led.add("inspection.total_unsellable", 15)   # 15 + 6 != 20
        led.add("inspection.total_repackable", 6)
        ctx = Ctx(led, {})
        r03_damage_units(ctx)
        self.assertTrue([d for d in ctx.result.discrepancies if d.severity == "HIGH"])

    def test_missing_inspection_raises_gap(self):
        led = _base_ledger()
        ctx = Ctx(led, {})
        r03_damage_units(ctx)
        self.assertTrue(any("inspection" in g.item.lower() for g in ctx.result.gaps))


if __name__ == "__main__":
    unittest.main()

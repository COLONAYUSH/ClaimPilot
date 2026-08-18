import unittest

from claimpilot.grounding import (build_allowed_tokens, check_fact_refs,
                                  collect_tokens, scan_generated_text)
from claimpilot.models import FactLedger
from claimpilot.util import find_quote


class TestQuoteVerification(unittest.TestCase):
    SOURCE = ('BlueLine liability is limited to the lesser of (a) the actual invoice '
              'value of the goods lost or damaged, or (b) $50.00 per pound multiplied '
              'by the weight of the goods lost or damaged')

    def test_exact_match(self):
        ok, ratio = find_quote("the actual invoice value of the goods", self.SOURCE)
        self.assertTrue(ok)
        self.assertEqual(ratio, 1.0)

    def test_normalization_absorbs_typography(self):
        ok, _ = find_quote("BlueLine   liability is\nlimited to the lesser", self.SOURCE)
        self.assertTrue(ok)

    def test_fuzzy_absorbs_small_ocr_noise(self):
        ok, ratio = find_quote("$50.00 per pound multiplied by the welght of the goods",
                               self.SOURCE)
        self.assertTrue(ok)
        self.assertLess(ratio, 1.0)

    def test_fabricated_quote_fails(self):
        ok, _ = find_quote("liability is capped at $25.00 per kilogram", self.SOURCE)
        self.assertFalse(ok)

    def test_empty_quote_fails(self):
        ok, _ = find_quote("", self.SOURCE)
        self.assertFalse(ok)


class TestNumberGuard(unittest.TestCase):
    def _ledger(self):
        led = FactLedger()
        led.add("a.demand_usd", "29920.00")
        led.add("a.offer_usd", "7225.00")
        led.add("a.delivered_at", "2026-05-12T10:42:00-05:00")
        led.add("a.units", 240)
        led.add("a.pct_note", "settled at 81.28 percent")
        return led

    def test_allowed_renderings_pass(self):
        allowed = build_allowed_tokens(self._ledger())
        text = ("The demand of $29,920.00 against the offer of $7,225 was made after "
                "delivery on May 12, 2026 at 10:42 for 240 units (81.3%). "
                "[F-001] Section 4 applies; 2 photos exist.")
        self.assertEqual(scan_generated_text(text, allowed), [])

    def test_fabricated_number_flagged(self):
        allowed = build_allowed_tokens(self._ledger())
        violations = scan_generated_text("We recommend countering at $12,500.00.", allowed)
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0].token, "12500")

    def test_fabricated_date_flagged(self):
        allowed = build_allowed_tokens(self._ledger())
        violations = scan_generated_text("Delivered on May 14, 2026.", allowed)
        self.assertTrue(any(v.kind == "date" for v in violations))

    def test_identifiers_are_not_numbers(self):
        allowed = build_allowed_tokens(self._ledger())
        text = "Claim FCL-2026-0147 under PRO BLF-77209115 and BOL-884219 [CT-4]."
        self.assertEqual(scan_generated_text(text, allowed), [])

    def test_small_integers_pass(self):
        allowed = build_allowed_tokens(self._ledger())
        self.assertEqual(scan_generated_text("All 5 cartons and 2 photos.", allowed), [])

    def test_large_uncited_integer_flagged(self):
        allowed = build_allowed_tokens(self._ledger())
        self.assertTrue(scan_generated_text("roughly 18500 dollars", allowed))


class TestFactRefs(unittest.TestCase):
    def test_invalid_refs_reported(self):
        used, invalid = check_fact_refs("Supported [F-001] and [E-9].", {"F-001"})
        self.assertEqual(used, ["F-001", "E-9"])
        self.assertEqual(invalid, ["E-9"])


if __name__ == "__main__":
    unittest.main()

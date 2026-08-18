import unittest
from datetime import date
from decimal import Decimal

from claimpilot.entitlement import _add_months, _cap_math
from claimpilot.models import FactLedger


class TestAddMonths(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(_add_months(date(2026, 5, 12), 9), date(2027, 2, 12))

    def test_month_end_clamps(self):
        self.assertEqual(_add_months(date(2026, 5, 31), 9), date(2027, 2, 28))

    def test_leap_year(self):
        self.assertEqual(_add_months(date(2027, 11, 30), 3), date(2028, 2, 29))


class TestCapMath(unittest.TestCase):
    def _ledger(self, units, price, unit_weight, cap):
        led = FactLedger()
        led.add("x.units", units)
        led.add("invoice.unit_price_usd", price)
        led.add("derived.unit_weight_lb", unit_weight)
        led.add("contract.liability_cap_per_lb", cap)
        return led

    def test_invoice_value_governs_for_light_valuable_goods(self):
        led = self._ledger(8, "425.00", "15", 50)
        basis = _cap_math(led, "x.units", "missing")
        self.assertEqual(basis, Decimal("3400.00"))
        self.assertEqual(led.dec("entitlement.missing_cap_usd"), Decimal("6000.00"))
        self.assertIn("invoice value governs",
                      led.fact("entitlement.missing_basis_usd").note)

    def test_cap_governs_for_heavy_cheap_goods(self):
        led = self._ledger(10, "900.00", "2", 50)   # invoice 9000 vs cap 10*2*50=1000
        basis = _cap_math(led, "x.units", "d")
        self.assertEqual(basis, Decimal("1000.00"))
        self.assertIn("cap governs", led.fact("entitlement.d_basis_usd").note)

    def test_missing_inputs_return_none(self):
        led = FactLedger()
        led.add("x.units", 8)
        self.assertIsNone(_cap_math(led, "x.units", "m"))


if __name__ == "__main__":
    unittest.main()

import unittest

from claimpilot.benchmark import _evidence_set, _issue_set, score_row
from claimpilot.llm import validate_schema
from claimpilot.util import extract_json_block


class TestSchemaValidator(unittest.TestCase):
    SCHEMA = {"type": "object", "required": ["a", "b"],
              "properties": {"a": {"type": "integer"},
                             "b": {"type": "array", "items": {"type": "string"}},
                             "c": {"type": ["number", "null"]},
                             "k": {"type": "string", "enum": ["x", "y"]}}}

    def test_valid(self):
        self.assertEqual(validate_schema(self.SCHEMA, {"a": 1, "b": ["s"], "c": None}), [])

    def test_missing_required_and_bad_types(self):
        errors = validate_schema(self.SCHEMA, {"a": "1", "k": "z"})
        joined = " ".join(errors)
        self.assertIn("missing required key 'b'", joined)
        self.assertIn("$.a", joined)
        self.assertIn("enum", joined)

    def test_bool_is_not_integer(self):
        self.assertTrue(validate_schema({"type": "integer"}, True))


class TestJsonExtraction(unittest.TestCase):
    def test_fenced(self):
        self.assertEqual(extract_json_block('x\n```json\n{"a": 1}\n```\ny'), '{"a": 1}')

    def test_prose_wrapped_nested(self):
        raw = 'Sure: {"a": {"b": "}"}, "c": [1]} trailing'
        self.assertEqual(extract_json_block(raw), '{"a": {"b": "}"}, "c": [1]}')

    def test_none_when_absent(self):
        self.assertIsNone(extract_json_block("no json here"))


class TestBenchmarkScoring(unittest.TestCase):
    ROW = {"carrier": "BlueLine Freight Systems", "issue_type": "DAMAGE+SHORTAGE",
           "service_level": "Standard LTL", "evidence": "Photos+POD+inspection"}

    def test_perfect_dimensions_score_high(self):
        s = score_row(self.ROW, "BlueLine Freight Systems",
                      {"DAMAGE", "SHORTAGE"}, "Standard LTL",
                      {"photos", "pod", "inspection"})
        self.assertGreater(s, 0.99)

    def test_wrong_carrier_drops_035(self):
        s = score_row(self.ROW, "ArrowPeak Logistics",
                      {"DAMAGE", "SHORTAGE"}, "Standard LTL",
                      {"photos", "pod", "inspection"})
        self.assertAlmostEqual(s, 0.65, places=2)

    def test_parsers(self):
        self.assertEqual(_issue_set("DAMAGE+DELAY"), {"DAMAGE", "DELAY"})
        self.assertEqual(_evidence_set("POD+2 photos+inspection"),
                         {"pod", "photos", "inspection"})


if __name__ == "__main__":
    unittest.main()

"""Local interval replacement, independent of cryptography and Presidio operators."""
import importlib.util
import unittest
from types import SimpleNamespace


class MaskingTests(unittest.TestCase):
    def test_overlapping_detections_mask_the_full_union(self):
        self.assertIsNotNone(importlib.util.find_spec("hermes_pii.masking"))
        from hermes_pii.masking import replace_detected_spans
        detections = [
            SimpleNamespace(start=2, end=6, entity_type="LOCATION", score=0.85),
            SimpleNamespace(start=4, end=8, entity_type="EMAIL_ADDRESS", score=0.5),
        ]
        self.assertEqual(replace_detected_spans("abcdefghij", detections), "ab<EMAIL_ADDRESS>ij")


    def test_invalid_offsets_raise_instead_of_slicing_the_wrong_text(self):
        from hermes_pii.masking import replace_detected_spans
        for start, end in [(-1, 2), (0, 9), (3, 2), (1, 1)]:
            with self.subTest(start=start, end=end):
                detection = SimpleNamespace(start=start, end=end, entity_type="PERSON", score=0.9)
                with self.assertRaises(ValueError):
                    replace_detected_spans("hello", [detection])

    def test_empty_disjoint_adjacent_duplicate_and_unicode_spans(self):
        from hermes_pii.masking import replace_detected_spans
        def detection(start, end, entity_type="PERSON"):
            return SimpleNamespace(start=start, end=end, entity_type=entity_type, score=0.9)
        cases = [
            ("hello", [], "hello"),
            ("", [], ""),
            ("ab cd", [detection(0, 2), detection(3, 5)], "<PERSON> <PERSON>"),
            ("abcd", [detection(0, 2), detection(2, 4)], "<PERSON><PERSON>"),
            ("abcd", [detection(0, 4), detection(0, 4)], "<PERSON>"),
            ("é名前!", [detection(1, 3)], "é<PERSON>!"),
        ]
        for text, spans, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(replace_detected_spans(text, spans), expected)


if __name__ == "__main__":
    unittest.main()

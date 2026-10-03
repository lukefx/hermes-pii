"""Exercise real Presidio and a preinstalled Italian model, offline."""
import importlib.util
import socket
import unittest
from unittest.mock import patch


class RedactorTests(unittest.TestCase):
    def test_redacts_italian_name_and_email_without_network(self):
        self.assertIsNotNone(
            importlib.util.find_spec("hermes_pii.redactor"),
            "Presidio redactor is not implemented",
        )
        from hermes_pii.redactor import PresidioRedactor
        with patch.object(socket.socket, "connect", side_effect=AssertionError("network forbidden")):
            redactor = PresidioRedactor()
            result = redactor.redact("Mi chiamo Mario Rossi, email mario.rossi@example.com.")
        self.assertNotIn("Mario Rossi", result)
        self.assertNotIn("mario.rossi@example.com", result)
        self.assertIn("<PERSON>", result)
        self.assertIn("<EMAIL_ADDRESS>", result)


if __name__ == "__main__":
    unittest.main()

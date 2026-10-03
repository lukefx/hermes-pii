"""Exercise real Presidio and a preinstalled English model, offline."""
import importlib.util
import socket
import unittest
from unittest.mock import patch


class RedactorTests(unittest.TestCase):
    def test_redacts_english_name_and_email_without_network(self):
        self.assertIsNotNone(
            importlib.util.find_spec("hermes_pii.redactor"),
            "Presidio redactor is not implemented",
        )
        from hermes_pii.redactor import PresidioRedactor
        with patch.object(socket.socket, "connect", side_effect=AssertionError("network forbidden")):
            redactor = PresidioRedactor()
            result = redactor.redact("My name is John Smith, email john.smith@example.com.")
        self.assertNotIn("John Smith", result)
        self.assertNotIn("john.smith@example.com", result)
        self.assertIn("<PERSON>", result)
        self.assertIn("<EMAIL_ADDRESS>", result)


if __name__ == "__main__":
    unittest.main()

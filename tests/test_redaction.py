import unittest

from backend.app.outbox_service import sanitize_outbox_payload
from backend.app.redaction import redact_secrets


class RedactionTests(unittest.TestCase):
    def test_preserves_canonical_uuid_when_group_starts_with_010(self):
        import_id = "cf9b2a82-010f-4b1c-8f64-4eaee0b5312d"

        self.assertEqual(redact_secrets(import_id), import_id)
        self.assertEqual(sanitize_outbox_payload({"import_id": import_id}), {"import_id": import_id})

    def test_preserves_canonical_uuid_when_uuid_starts_with_010(self):
        import_id = "01000000-0000-4000-8000-000000000001"

        self.assertEqual(redact_secrets(import_id), import_id)

    def test_still_redacts_uuid_when_it_is_a_secret_value(self):
        import_id = "cf9b2a82-010f-4b1c-8f64-4eaee0b5312d"

        self.assertEqual(redact_secrets(f"password={import_id}"), "password=***")

    def test_redacts_supported_secret_shapes(self):
        rendered = redact_secrets(
            "bot123:secret-token Bearer abc.def password=clear 01040063960540670001"
        )

        self.assertNotIn("secret-token", rendered)
        self.assertNotIn("abc.def", rendered)
        self.assertNotIn("clear", rendered)
        self.assertNotIn("01040063960540670001", rendered)


if __name__ == "__main__":
    unittest.main()

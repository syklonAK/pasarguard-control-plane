import os
import unittest
from control_plane.secrets import encrypt_secret, resolve_secret

class SecretTests(unittest.TestCase):
    def setUp(self):
        self.old = os.environ.get("APP_ENCRYPTION_KEY")
        os.environ["APP_ENCRYPTION_KEY"] = "test-master-key-0123456789-abcdefghijklmnopqrstuvwxyz"

    def tearDown(self):
        if self.old is None:
            os.environ.pop("APP_ENCRYPTION_KEY", None)
        else:
            os.environ["APP_ENCRYPTION_KEY"] = self.old

    def test_encrypted_round_trip(self):
        reference = encrypt_secret("pg_key_example")
        self.assertTrue(reference.startswith("enc://"))
        self.assertNotIn("pg_key_example", reference)
        self.assertEqual(resolve_secret(reference), "pg_key_example")

    def test_env_reference(self):
        os.environ["TEST_SECRET"] = "value"
        self.assertEqual(resolve_secret("env://TEST_SECRET"), "value")

if __name__ == "__main__":
    unittest.main()

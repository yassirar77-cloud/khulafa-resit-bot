"""Telegram WebApp initData HMAC verification (webapp_auth)."""
import hashlib
import hmac
import json
import os
import sys
import unittest
from urllib.parse import urlencode

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from webapp_auth import INITDATA_MAX_AGE_S, verify_init_data  # noqa: E402

BOT_TOKEN = "12345:TEST-TOKEN"
NOW = 1_800_000_000


def _sign(fields: dict, token: str = BOT_TOKEN) -> str:
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    return hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()


def _init_data(user_id=777, auth_date=NOW, token: str = BOT_TOKEN, **overrides) -> str:
    fields = {
        "auth_date": str(auth_date),
        "query_id": "AAtest",
        "user": json.dumps({"id": user_id, "first_name": "Yassir"}),
    }
    fields.update(overrides)
    fields["hash"] = _sign(fields, token)
    return urlencode(fields)


class VerifyInitData(unittest.TestCase):
    def test_valid_init_data_returns_user_id(self):
        self.assertEqual(verify_init_data(_init_data(user_id=777), BOT_TOKEN, now=NOW), 777)

    def test_tampered_payload_rejected(self):
        # Re-signing with the wrong token / editing a field must fail.
        data = _init_data(user_id=777)
        tampered = data.replace("777", "666")
        self.assertIsNone(verify_init_data(tampered, BOT_TOKEN, now=NOW))

    def test_wrong_bot_token_rejected(self):
        data = _init_data(token="99999:OTHER-TOKEN")
        self.assertIsNone(verify_init_data(data, BOT_TOKEN, now=NOW))

    def test_missing_hash_rejected(self):
        self.assertIsNone(verify_init_data("auth_date=1&user=%7B%7D", BOT_TOKEN, now=NOW))

    def test_empty_rejected(self):
        self.assertIsNone(verify_init_data("", BOT_TOKEN, now=NOW))
        self.assertIsNone(verify_init_data(_init_data(), "", now=NOW))

    def test_stale_auth_date_rejected(self):
        old = NOW - INITDATA_MAX_AGE_S - 60
        self.assertIsNone(verify_init_data(_init_data(auth_date=old), BOT_TOKEN, now=NOW))

    def test_fresh_auth_date_accepted(self):
        recent = NOW - 3600
        self.assertEqual(
            verify_init_data(_init_data(user_id=42, auth_date=recent), BOT_TOKEN, now=NOW), 42
        )

    def test_missing_user_rejected(self):
        fields = {"auth_date": str(NOW), "query_id": "AAtest"}
        fields["hash"] = _sign(fields)
        self.assertIsNone(verify_init_data(urlencode(fields), BOT_TOKEN, now=NOW))


class BotWiring(unittest.TestCase):
    """Source-level checks that bot.py actually uses the verification."""

    @classmethod
    def setUpClass(cls):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "bot.py")) as f:
            cls.src = f.read()
        with open(os.path.join(root, "templates", "dashboard.html")) as f:
            cls.tpl = f.read()

    def test_webapp_data_verifies_and_gates_on_reviewer(self):
        idx = self.src.index("def webapp_data(")
        block = self.src[idx:idx + 1200]
        self.assertIn("verify_init_data(init_data, TELEGRAM_BOT_TOKEN)", block)
        self.assertIn("is_reviewer(user_id)", block)
        self.assertIn("401", block)
        self.assertIn("403", block)

    def test_no_supabase_credentials_shipped_to_browser(self):
        self.assertNotIn("supabase_anon_key", self.src)
        self.assertNotIn("SUPABASE_ANON_KEY", self.tpl)
        self.assertNotIn("supabase_anon_key", self.tpl)
        # The template fetches the bot's own endpoint instead.
        self.assertIn('fetch("/webapp/data"', self.tpl)
        self.assertIn("X-Telegram-Init-Data", self.tpl)


if __name__ == "__main__":
    unittest.main()

import io
import logging
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import log_redact

TOKEN = "8654283315:AAGexampleexampleexampleexample12"  # fake, test-only


class RedactTests(unittest.TestCase):
    def test_telegram_url_token_masked(self):
        line = f'POST https://api.telegram.org/bot{TOKEN}/sendMessage "HTTP/1.1 200 OK"'
        out = log_redact.redact(line)
        self.assertNotIn(TOKEN, out)
        self.assertIn("https://api.telegram.org/bot***/sendMessage", out)

    def test_bare_token_keys_and_jwt_masked(self):
        text = (f"token={TOKEN} key=sk-abcdefghijklmnopqrstuvwx "
                "supa=eyJhbGciOiJIUzI1NiJ9.eyJyb2xlIjoic2VydmljZSJ9.abcdefghijklmnop")
        out = log_redact.redact(text)
        for secret in (TOKEN, "sk-abcdefghijklmnopqrstuvwx", "eyJhbGciOiJIUzI1NiJ9"):
            self.assertNotIn(secret, out)

    def test_exact_env_values_masked(self):
        self.assertEqual(log_redact.redact("pw=hunter2hunter2", ["hunter2hunter2"]), "pw=***")

    def test_normal_text_untouched(self):
        line = "Order drafts posted (11/11 messages sent, delivery_enabled=True) chat=-5043287182"
        self.assertEqual(log_redact.redact(line), line)


class InstallTests(unittest.TestCase):
    def test_installed_handler_masks_messages_args_and_tracebacks(self):
        root = logging.Logger("test-root")
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(logging.Formatter("%(message)s"))
        root.addHandler(handler)
        with mock.patch.dict("os.environ", {"DEEPSEEK_API_KEY": "deepseek-secret-value"}):
            log_redact.install(root)
            log_redact.install(root)  # idempotent
        root.warning("url https://api.telegram.org/bot%s/getMe", TOKEN)
        root.warning("key deepseek-secret-value")
        try:
            raise RuntimeError(f"failed https://api.telegram.org/bot{TOKEN}/x")
        except RuntimeError:
            root.exception("boom")
        out = stream.getvalue()
        self.assertNotIn(TOKEN, out)
        self.assertNotIn("deepseek-secret-value", out)
        self.assertEqual(out.count("bot***"), 2)
        self.assertIn("RuntimeError", out)

    def test_request_loggers_quieted(self):
        log_redact.install(logging.Logger("x"))
        for name in ("httpx", "httpx2", "httpcore"):
            self.assertEqual(logging.getLogger(name).level, logging.WARNING)


if __name__ == "__main__":
    unittest.main()

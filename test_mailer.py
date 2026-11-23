import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from components import mailer
from resend.exceptions import ResendError


class ResendMailerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config_patch = patch.object(mailer, "CONFIG_FILE", Path(self.temp_dir.name) / "smtp.json")
        self.config_patch.start()
        self.env_patch = patch.dict(os.environ, {}, clear=True)
        self.env_patch.start()

    def tearDown(self):
        self.env_patch.stop()
        self.config_patch.stop()
        self.temp_dir.cleanup()

    def test_sends_plain_text_with_resend(self):
        os.environ["RESEND_API_KEY"] = "re_test_key"
        os.environ["RESEND_FROM_EMAIL"] = "TGM Assets <mail@example.com>"
        with patch.object(mailer.resend.Emails, "send") as send:
            self.assertIsNone(mailer.send_email("user@example.com", "Subject", "Body"))
        send.assert_called_once_with({
            "from": "TGM Assets <mail@example.com>",
            "to": ["user@example.com"],
            "subject": "Subject",
            "html": "Body",
        })
        self.assertTrue(mailer.configured())

    def test_resend_api_key_requires_sender(self):
        os.environ["RESEND_API_KEY"] = "re_test_key"
        self.assertFalse(mailer.configured())
        self.assertEqual(
            mailer.send_email("user@example.com", "Subject", "Body"),
            "RESEND_FROM_EMAIL is required when RESEND_API_KEY is set.",
        )

    def test_resend_error_is_reported(self):
        os.environ["RESEND_API_KEY"] = "re_test_key"
        os.environ["RESEND_FROM_EMAIL"] = "mail@example.com"
        error = ResendError(400, "validation_error", "rejected", "Check the sender configuration.")
        with patch.object(mailer.resend.Emails, "send", side_effect=error):
            self.assertEqual(
                mailer.send_email("user@example.com", "Subject", "Body"),
                "Could not send email: rejected",
            )

    def test_smtp_remains_available_without_resend_key(self):
        os.environ.update({
            "SMTP_HOST": "smtp.example.com",
            "SMTP_PORT": "587",
            "SMTP_SENDER": "mail@example.com",
        })
        with patch.object(mailer.smtplib, "SMTP") as smtp:
            smtp.return_value.__enter__.return_value = smtp.return_value
            self.assertIsNone(mailer.send_email("user@example.com", "Subject", "Body"))
        smtp.assert_called_once_with("smtp.example.com", 587, timeout=15)
        smtp.return_value.send_message.assert_called_once()

    def test_sends_resend_template_with_string_variables(self):
        os.environ["RESEND_API_KEY"] = "re_test_key"
        os.environ["RESEND_FROM_EMAIL"] = "TGM Assets <mail@example.com>"
        with patch.object(mailer.resend.Emails, "send") as send:
            self.assertIsNone(mailer.send_template(
                "user@example.com", "procurement", {"id": "PR-2001", "amount": 1500.5}, "Subject"))
        send.assert_called_once_with({
            "from": "TGM Assets <mail@example.com>",
            "to": ["user@example.com"],
            "subject": "Subject",
            "template": {"id": "procurement", "variables": {"id": "PR-2001", "amount": "1500.5"}},
        })

    def test_template_id_can_be_overridden_by_environment(self):
        os.environ["RESEND_TEMPLATE_WORK_ORDER"] = "wo-template-id"
        self.assertEqual(mailer.template_id("work_order"), "wo-template-id")
        self.assertEqual(mailer.template_id("some-other-alias"), "some-other-alias")

    def test_template_needs_resend_configuration(self):
        self.assertEqual(
            mailer.send_template("user@example.com", "procurement", {}, "Subject"),
            "Resend templates need RESEND_API_KEY and RESEND_FROM_EMAIL.",
        )


if __name__ == "__main__":
    unittest.main()

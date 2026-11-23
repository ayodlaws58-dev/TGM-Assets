import os
from unittest.mock import patch

import pytest

from components import emailkit


@pytest.fixture(autouse=True)
def base_url():
    with patch.dict(os.environ, {"APP_BASE_URL": "https://assets.example.com/"}):
        yield


def test_button_links_to_the_right_page():
    html, text = emailkit.render("request", "Work order awaiting approval", "Fix the generator",
                                 button=("Review work order", "/approvals"))
    assert 'href="https://assets.example.com/approvals"' in html
    assert "Review work order" in html
    assert "Review work order: https://assets.example.com/approvals" in text


def test_no_button_without_a_site_address():
    with patch.dict(os.environ, {"APP_BASE_URL": ""}):
        html, text = emailkit.render("info", "Hello", "Body", button=("Open", "/x"))
    assert "href=" not in html.split("<!-- Footer -->")[0] and "Open:" not in text


def test_values_are_escaped():
    html, _ = emailkit.render("info", "<script>x</script>", "a & b", facts=[("Note", "<b>hi</b>")])
    assert "<script>" not in html and "<b>hi</b>" not in html
    assert "&lt;b&gt;hi&lt;/b&gt;" in html


def test_empty_facts_are_skipped_and_priority_gets_a_chip():
    html, text = emailkit.render("request", "T", facts=[("Reason", ""), ("Priority", "High"), ("Item", "Oil")])
    assert "Reason" not in html and "Reason" not in text
    assert "#c62828" in html and "Priority: High" in text


def test_reset_code_is_shown_large_and_in_text():
    html, text = emailkit.render("account", "Reset", code=("Your password reset code", "123456"))
    assert 'id="reset-code"' in html and ">123456<" in html
    assert "code: 123456" in text


def test_rejected_decision_is_red():
    html, _ = emailkit.render("rejected", "WO-1 rejected")
    assert emailkit.STYLES["rejected"][0] in html


def test_safe_next_blocks_open_redirects():
    import app
    assert app._safe_next("/approvals") == "/approvals"
    assert app._safe_next("/assets/A-1?tab=x") == "/assets/A-1?tab=x"
    for bad in ("https://evil.example", "//evil.example", "/\\evil.example", "javascript:alert(1)", "",
                None, "/ok\r\nSet-Cookie: x=1"):
        assert app._safe_next(bad) == "", bad

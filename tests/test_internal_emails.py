"""A colleague on a personal address (INTERNAL_EMAILS) is trusted like the company domain; strangers still aren't."""
import os
import sys
import tempfile
from pathlib import Path

os.environ["STAGE_DELAY"] = "0"
os.environ.setdefault("RUNTIME_DIR", tempfile.mkdtemp())
os.environ.setdefault("DEMO_TODAY", "2026-11-04")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import agent  # noqa: E402


def test_listed_address_is_internal(monkeypatch):
    monkeypatch.setenv("INTERNAL_EMAILS", "Stores.Demo@gmail.com, ")
    assert agent.internal_emails() == {"stores.demo@gmail.com"}


def test_unlisted_gmail_is_not_internal(monkeypatch):
    monkeypatch.setenv("INTERNAL_EMAILS", "stores.demo@gmail.com")
    assert "someone@gmail.com" not in agent.internal_emails()
    assert "gmail.com" not in agent.internal_domains()


import pytest  # noqa: E402


@pytest.mark.parametrize("text,intent", [
    ("Remaining 100 helmets are on hold we will receive on friday", "not_received"),
    ("The rest will be received next week", "not_received"),
    ("Not received yet, expected next week.", "not_received"),
    ("Received the remaining 100 helmets today.", "confirm_receipt"),
    ("received 100 more today", "confirm_receipt"),
    ("Checked with the warehouse - the balance was received today, all items in good condition.", "confirm_receipt"),
])
def test_stores_reply_wording(text, intent):
    assert agent.parse_reply(text, {}, "internal:stores@x")["intent"] == intent

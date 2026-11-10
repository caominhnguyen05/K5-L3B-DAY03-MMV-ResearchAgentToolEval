from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import providers
import env_loader
from providers.base import ModelResponse, ToolCall
from streamlit.testing.v1 import AppTest


class FakeProvider:
    default_model = "fake-model"

    def __init__(self, responses):
        self.responses = list(responses)

    def complete(self, messages, tools=None, *, model=None, temperature=0.0):
        return self.responses.pop(0)


def test_app_accepts_question_and_renders_fake_answer(monkeypatch, tmp_path):
    fake = FakeProvider([ModelResponse(text="A test research answer."),])
    monkeypatch.setattr(providers, "make_provider", lambda name: fake)
    monkeypatch.setattr(env_loader, "load_lab_env", lambda root: None)
    monkeypatch.setattr("ui_core.TRANSCRIPTS_DIR", tmp_path)
    app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=10)
    app.run()
    app.chat_input[0].set_value("Find a recent AI update")
    app.run()
    assert any("A test research answer." in element.value for element in app.markdown)
    assert app.session_state["transcript"]["turns"][0]["status"] == "answered"



def test_send_call_shows_confirmation_and_disables_chat_input(monkeypatch, tmp_path):
    fake = FakeProvider([ModelResponse(tool_calls=[ToolCall("send", {"text": "Proposed post", "confirmed": True})])])
    monkeypatch.setattr(providers, "make_provider", lambda name: fake)
    monkeypatch.setattr(env_loader, "load_lab_env", lambda root: None)
    monkeypatch.setattr("ui_core.TRANSCRIPTS_DIR", tmp_path)
    app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=10)
    app.run()
    app.chat_input[0].set_value("Please post this")
    app.run()
    assert any(button.label == "Xác nhận gửi" for button in app.button)
    assert app.chat_input[0].disabled is True
    assert app.session_state["pending_turn"]["status"] == "awaiting_confirmation"



def test_tool_error_is_visible_in_chat(monkeypatch, tmp_path):
    fake = FakeProvider([ModelResponse(tool_calls=[ToolCall("not_registered", {"value": "x"})]), ModelResponse(text="Tool unavailable.")])
    monkeypatch.setattr(providers, "make_provider", lambda name: fake)
    monkeypatch.setattr(env_loader, "load_lab_env", lambda root: None)
    monkeypatch.setattr("ui_core.TRANSCRIPTS_DIR", tmp_path)
    app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=10)
    app.run()
    app.chat_input[0].set_value("Try a missing tool")
    app.run()
    assert any("Lỗi tool `not_registered`" in element.value for element in app.error)


def test_new_conversation_abandons_pending_send_and_saves_transcript(monkeypatch, tmp_path):
    fake = FakeProvider([ModelResponse(tool_calls=[ToolCall("send", {"text": "Pending post", "confirmed": True})])])
    monkeypatch.setattr(providers, "make_provider", lambda name: fake)
    monkeypatch.setattr(env_loader, "load_lab_env", lambda root: None)
    monkeypatch.setattr("ui_core.TRANSCRIPTS_DIR", tmp_path)
    app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=10)
    app.run()
    app.chat_input[0].set_value("Please post this")
    app.run()
    new_conversation = next(button for button in app.button if button.label == "Cuộc hội thoại mới")
    assert not new_conversation.disabled
    new_conversation.click().run()
    assert app.session_state["transcript"] is None
    transcripts = list(tmp_path.glob("*.transcript.json"))
    assert len(transcripts) == 1
    saved = json.loads(transcripts[0].read_text(encoding="utf-8"))
    assert saved["turns"][0]["status"] == "abandoned"
    assert saved["turns"][0]["tool_events"][0]["result"]["status"] == "abandoned"

from __future__ import annotations

import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from providers.base import ModelResponse, ToolCall
from ui_core import (
    abandon_turn,
    confirmation_tools,
    load_artifacts,
    new_transcript,
    redact,
    resolve_confirmation,
    run_turn,
    write_transcript,
)


class FakeProvider:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.messages = []

    def complete(self, messages, tools=None, *, model=None, temperature=0.0):
        self.messages.append(messages)
        assert temperature == 0.0
        return self.responses.pop(0)


def artifact_info():
    return {
        "version": "test",
        "artifact_version": "test+ptest+ttest",
        "prompt_hash": "p" * 64,
        "tools_hash": "t" * 64,
        "system_prompt": "test prompt",
        "openai_tools": [],
        "tool_declarations": [],
    }


def test_research_turn_returns_answer_and_records_calls():
    provider = FakeProvider(ModelResponse(text="Here is the research."))
    turn = run_turn(provider=provider, model=None, artifact_info=artifact_info(), history=[], user_text="Find news")
    assert turn["status"] == "answered"
    assert turn["assistant_text"] == "Here is the research."
    assert turn["rounds"][0]["tool_calls"] == []
    assert provider.messages[0][-1] == {"role": "user", "content": "Find news"}


def test_clarify_pauses_and_next_turn_receives_history():
    provider = FakeProvider(
        ModelResponse(tool_calls=[ToolCall("clarify", {"question": "Which account?", "response_type": "text"})]),
        ModelResponse(text="Here are the posts."),
    )
    tools = {"clarify": lambda **kwargs: {**kwargs, "awaiting_user": True}}
    first = run_turn(provider=provider, model=None, artifact_info=artifact_info(), history=[], user_text="Get tweets", tool_functions=tools)
    assert first["status"] == "waiting_for_user"
    history = [{"role": "user", "content": "Get tweets"}, {"role": "assistant", "content": "Which account?"}]
    second = run_turn(provider=provider, model=None, artifact_info=artifact_info(), history=history, user_text="Elon Musk", tool_functions=tools)
    assert second["status"] == "answered"
    assert provider.messages[1][1:3] == history


def test_send_is_intercepted_even_when_model_sets_confirmed_true():
    sent = []
    provider = FakeProvider(ModelResponse(tool_calls=[ToolCall("send", {"text": "draft", "confirmed": True})]))
    tools = {"send": lambda **kwargs: sent.append(kwargs) or {"status": "sent"}}
    turn = run_turn(provider=provider, model=None, artifact_info=artifact_info(), history=[], user_text="Post it", confirmation_names={"send"}, tool_functions=tools)
    assert turn["status"] == "awaiting_confirmation"
    assert sent == []
    assert turn["tool_events"][0]["ui_gate"]["model_confirmed_flag"] is True


def test_confirmation_can_edit_text_and_forces_confirmed_true():
    sent = []
    provider = FakeProvider(ModelResponse(tool_calls=[ToolCall("send", {"text": "draft", "confirmed": False})]), ModelResponse(text="Sent."))
    def send(text, confirmed=False):
        sent.append({"text": text, "confirmed": confirmed})
        return {"status": "sent"}
    tools = {"send": send}
    turn = run_turn(provider=provider, model=None, artifact_info=artifact_info(), history=[], user_text="Post it", confirmation_names={"send"}, tool_functions=tools)
    result = resolve_confirmation(turn, decision="confirmed", edited_text="approved")
    turn.update(result)
    assert sent == [{"text": "approved", "confirmed": True}]
    assert turn["tool_events"][0]["ui_gate"]["text_edited_by_user"] is True
    assert turn["tool_events"][0]["ui_gate"]["user_decision"] == "confirmed"
    assert turn["status"] == "answered"


def test_confirmation_adds_confirmed_when_model_omits_it():
    sent = []
    def send(text, confirmed=False):
        sent.append({"text": text, "confirmed": confirmed})
        return {"status": "sent"}
    provider = FakeProvider(ModelResponse(tool_calls=[ToolCall("send", {"text": "draft"})]), ModelResponse(text="Sent."))
    turn = run_turn(provider=provider, model=None, artifact_info=artifact_info(), history=[], user_text="Post it", confirmation_names={"send"}, tool_functions={"send": send})
    assert turn["status"] == "awaiting_confirmation"
    result = resolve_confirmation(turn, decision="confirmed")
    assert sent == [{"text": "draft", "confirmed": True}]
    assert result["status"] == "answered"


def test_confirmation_does_not_add_text_to_tool_without_text_parameter():
    received = []
    def send(destination, confirmed=False):
        received.append({"destination": destination, "confirmed": confirmed})
        return {"status": "sent"}
    provider = FakeProvider(ModelResponse(tool_calls=[ToolCall("send", {"destination": "private"})]), ModelResponse(text="Sent."))
    turn = run_turn(provider=provider, model=None, artifact_info=artifact_info(), history=[], user_text="Send it", confirmation_names={"send"}, tool_functions={"send": send})
    result = resolve_confirmation(turn, decision="confirmed", edited_text="not a tool argument")
    assert received == [{"destination": "private", "confirmed": True}]
    assert result["status"] == "answered"


def test_confirmation_can_only_be_resolved_once():
    provider = FakeProvider(ModelResponse(tool_calls=[ToolCall("send", {"text": "draft"})]), ModelResponse(text="Sent."))
    turn = run_turn(provider=provider, model=None, artifact_info=artifact_info(), history=[], user_text="Post it", confirmation_names={"send"}, tool_functions={"send": lambda text, confirmed=False: {"status": "sent"}})
    first = resolve_confirmation(turn, decision="cancelled")
    turn.update(first)
    second = resolve_confirmation(turn, decision="confirmed")
    assert second["status"] == "answered"
    assert second["tool_events"][0]["result"]["status"] == "cancelled_by_user"


def test_cancel_returns_cancelled_by_user_without_executing_tool():
    sent = []
    provider = FakeProvider(ModelResponse(tool_calls=[ToolCall("send", {"text": "draft", "confirmed": True})]), ModelResponse(text="Cancelled."))
    tools = {"send": lambda **kwargs: sent.append(kwargs) or {"status": "sent"}}
    turn = run_turn(provider=provider, model=None, artifact_info=artifact_info(), history=[], user_text="Post it", confirmation_names={"send"}, tool_functions=tools)
    result = resolve_confirmation(turn, decision="cancelled", edited_text="draft")
    turn.update(result)
    assert sent == []
    assert turn["tool_events"][0]["result"]["status"] == "cancelled_by_user"
    assert turn["tool_events"][0]["ui_gate"]["user_decision"] == "cancelled"


def test_tools_after_send_wait_until_confirmation_and_keep_order():
    order = []
    provider = FakeProvider(
        ModelResponse(tool_calls=[ToolCall("read", {"id": 1}), ToolCall("send", {"text": "draft", "confirmed": True}), ToolCall("tail", {"id": 3})]),
        ModelResponse(text="Done."),
    )
    tools = {
        "read": lambda **kwargs: order.append("read") or {"ok": True},
        "send": lambda **kwargs: order.append("send") or {"status": "sent"},
        "tail": lambda **kwargs: order.append("tail") or {"ok": True},
    }
    turn = run_turn(provider=provider, model=None, artifact_info=artifact_info(), history=[], user_text="Run", confirmation_names={"send"}, tool_functions=tools)
    assert order == ["read"]
    assert turn["status"] == "awaiting_confirmation"
    result = resolve_confirmation(turn, decision="confirmed")
    turn.update(result)
    assert order == ["read", "send", "tail"]
    assert turn["status"] == "answered"


def test_artifacts_are_re_read_and_hashed_from_current_bytes(tmp_path):
    (tmp_path / "system_prompt.md").write_text("first", encoding="utf-8")
    (tmp_path / "tools.yaml").write_text("tools: []\n", encoding="utf-8")
    first = load_artifacts(tmp_path, "v1")
    (tmp_path / "system_prompt.md").write_text("second", encoding="utf-8")
    second = load_artifacts(tmp_path, "v1")
    assert first["system_prompt"] == "first"
    assert second["system_prompt"] == "second"
    assert first["artifact_version"] != second["artifact_version"]


def test_manifest_confirmation_detection_only_blocks_external_side_effects(tmp_path):
    for name, side_effect in [("send", "true"), ("paper_text", "local_file_write"), ("safe", "false")]:
        folder = tmp_path / name
        folder.mkdir()
        (folder / "TOOL.md").write_text(f"---\nname: {name}\nside_effect: {side_effect}\n---\n", encoding="utf-8")
    assert confirmation_tools(tmp_path) == {"send"}


def test_missing_or_malformed_manifest_fails_closed_for_declared_tools(tmp_path):
    (tmp_path / "broken").mkdir()
    (tmp_path / "broken" / "TOOL.md").write_text("not valid frontmatter", encoding="utf-8")
    assert confirmation_tools(tmp_path, {"broken", "missing"}) == {"broken", "missing"}


def test_redact_preserves_internal_flags_and_redacts_telegram_url_token(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "custom_token_value_not_regex_match")
    value = {"_invalid_json_arguments": True, "url": "https://api.telegram.org/bot123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZ_1234567890/sendMessage", "env_echo": "custom_token_value_not_regex_match"}
    cleaned = redact(value)
    assert cleaned["_invalid_json_arguments"] is True
    assert "ABCDEFGHIJKLMNOPQRSTUVWXYZ" not in cleaned["url"]
    assert "custom_token_value_not_regex_match" not in cleaned["env_echo"]


def test_transcript_matches_chat_schema_and_adds_ui_source(monkeypatch, tmp_path):
    monkeypatch.setattr("ui_core.TRANSCRIPTS_DIR", tmp_path)
    transcript = new_transcript("openrouter", "model-x", artifact_info())
    assert transcript["source"] == "streamlit_ui"
    for field in ("transcript_id", "version", "artifact_version", "prompt_hash", "tools_hash", "provider", "model", "system_prompt", "tools", "history_window", "max_tool_rounds", "created_at", "updated_at", "turns"):
        assert field in transcript
    path = write_transcript(transcript)
    assert path.parent == tmp_path
    assert '"source": "streamlit_ui"' in path.read_text(encoding="utf-8")


def test_transcript_paths_are_relative():
    transcript = new_transcript("openrouter", "model-x", artifact_info())
    assert transcript["system_prompt"] == "artifacts/system_prompt.md"
    assert transcript["tools"] == "artifacts/tools.yaml"
    assert not Path(transcript["system_prompt"]).is_absolute()
    assert not Path(transcript["tools"]).is_absolute()


def test_abandon_turn_marks_status_without_running_tool():
    called = []
    provider = FakeProvider(ModelResponse(tool_calls=[ToolCall("send", {"text": "pending", "confirmed": True})]))
    turn = run_turn(
        provider=provider,
        model=None,
        artifact_info=artifact_info(),
        history=[],
        user_text="Send this",
        confirmation_names={"send"},
        tool_functions={"send": lambda **kwargs: called.append(kwargs) or {"status": "sent"}},
    )
    abandoned = abandon_turn(turn)
    assert abandoned["status"] == "abandoned"
    assert "_pending" not in abandoned
    assert abandoned["tool_events"][0]["result"]["status"] == "abandoned"
    assert abandoned["tool_events"][0]["ui_gate"]["user_decision"] == "abandoned"
    assert called == []

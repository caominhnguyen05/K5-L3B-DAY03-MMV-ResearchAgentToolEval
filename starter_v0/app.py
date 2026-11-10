from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import streamlit as st

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import providers
from env_loader import load_lab_env
from ui_core import (
    ROOT as UI_ROOT,
    abandon_turn,
    confirmation_tools,
    load_artifacts,
    new_transcript,
    redact,
    resolve_confirmation,
    run_turn,
    write_transcript,
)


PROVIDERS = ["openrouter", "openai", "anthropic", "gemini", "ollama"]
SAMPLE_QUESTIONS = {
    "Research thường": "Tìm tin AI hôm nay và tổng hợp thành một bản tin ngắn giúp mình.",
    "Thiếu thông tin": "Tóm tắt 5 tweet mới nhất giúp mình",
    "Yêu cầu Telegram": "Đăng bản tin AI ở lượt đầu lên Telegram giúp mình.",
    "Đọc URL": "Đọc giúp mình bài này: https://arxiv.org/abs/1706.03762",
}


def _init_state() -> None:
    defaults = {
        "history": [],
        "transcript": None,
        "pending_turn": None,
        "provider": None,
        "provider_name": None,
        "model": None,
        "requested_model": None,
        "transcript_path": None,
        "last_error": None,
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


def _persist() -> None:
    transcript = st.session_state.transcript
    if transcript:
        path = write_transcript(transcript)
        st.session_state.transcript_path = str(path)


def _append_history(turn: dict[str, Any]) -> None:
    if turn.get("status") != "provider_error":
        st.session_state.history.extend([
            {"role": "user", "content": turn.get("user", "")},
            {"role": "assistant", "content": turn.get("assistant_text") or ""},
        ])


def _start_conversation(provider_name: str, model: str | None, artifact_info: dict[str, Any]) -> None:
    st.session_state.transcript = new_transcript(provider_name, model, artifact_info)
    st.session_state.provider_name = provider_name
    st.session_state.model = model
    st.session_state.requested_model = model
    st.session_state.history = []
    st.session_state.pending_turn = None
    try:
        provider = providers.make_provider(provider_name)
        selected_model = model or getattr(provider, "default_model", None)
        st.session_state.provider = provider
        st.session_state.model = selected_model
        st.session_state.last_error = None
        st.session_state.transcript["model"] = selected_model
        _persist()
    except Exception as exc:
        st.session_state.last_error = redact(f"{type(exc).__name__}: {exc}")


def _submit(user_text: str, provider_name: str, model: str | None, artifact_info: dict[str, Any], gates: set[str]) -> None:
    if not st.session_state.transcript or st.session_state.provider_name != provider_name or st.session_state.requested_model != model:
        _start_conversation(provider_name, model, artifact_info)
    if st.session_state.last_error:
        from ui_core import now_iso
        turn = {
            "turn_index": len(st.session_state.transcript["turns"]) + 1,
            "started_at": now_iso(),
            "user": user_text,
            "status": "provider_error",
            "error": st.session_state.last_error,
            "assistant_text": "",
            "rounds": [],
            "tool_events": [],
            "ended_at": now_iso(),
        }
        st.session_state.transcript["turns"].append(turn)
        _persist()
        return
    turn = run_turn(
        provider=st.session_state.provider,
        model=st.session_state.model,
        artifact_info=artifact_info,
        history=st.session_state.history,
        user_text=user_text,
        confirmation_names=gates,
    )
    st.session_state.transcript["turns"].append(turn)
    if turn.get("status") == "awaiting_confirmation":
        st.session_state.pending_turn = turn
    else:
        _append_history(turn)
    _persist()


def _resolve(decision: str, edited_text: str) -> None:
    turn = st.session_state.pending_turn
    if not turn:
        return
    try:
        result = resolve_confirmation(turn, decision=decision, edited_text=edited_text)
        turn.update(result)
        turn.pop("_pending", None)
        turn["ended_at"] = __import__("datetime").datetime.now().isoformat(timespec="seconds")
        st.session_state.pending_turn = turn if turn.get("status") == "awaiting_confirmation" else None
        if st.session_state.pending_turn is None:
            _append_history(turn)
        _persist()
    except Exception as exc:
        turn.update({"status": "provider_error", "error": redact(f"{type(exc).__name__}: {exc}")})
        turn.pop("_pending", None)
        turn["ended_at"] = __import__("datetime").datetime.now().isoformat(timespec="seconds")
        st.session_state.pending_turn = None
        _persist()


def _render_event(event: dict[str, Any], event_number: int) -> None:
    name = event.get("tool", "unknown")
    args = event.get("args", {})
    result = event.get("result", {})
    status = result.get("status") if isinstance(result, dict) else None
    with st.container(border=True):
        st.markdown(f"**Tool {event_number}: `{name}`**")
        st.json(args, expanded=True)
        if isinstance(result, dict) and ("error" in result or str(status).lower() in {"error", "failed", "failure"}):
            detail = result.get("message") or result.get("error") or status
            st.error(f"Lỗi tool `{name}`: {detail}")
        elif status == "awaiting_confirmation":
            st.warning("Đã chặn tool; đang chờ bạn xem lại và quyết định.")
        elif status == "cancelled_by_user":
            st.info("Đã hủy theo lựa chọn của bạn.")
        with st.expander(f"Kết quả tool: {name}", expanded=False):
            st.json(result, expanded=True)
        gate = event.get("ui_gate")
        if gate:
            decision = gate.get("user_decision") or "đang chờ"
            st.caption(
                f"Chốt UI: đã chặn · model gửi confirmed={str(gate.get('model_confirmed_flag')).lower()} "
                f"· quyết định: {decision} · đã sửa nội dung: "
                f"{str(gate.get('text_edited_by_user')).lower()}"
            )


def _render_turn(turn: dict[str, Any]) -> None:
    with st.chat_message("user"):
        st.markdown(turn.get("user", ""))
    with st.chat_message("assistant"):
        st.markdown(turn.get("assistant_text") or "")
        status_messages = {
            "waiting_for_user": "Đang chờ bạn bổ sung thông tin.",
            "awaiting_confirmation": "Đang chờ bạn xem lại và xác nhận tác vụ.",
        }
        if turn.get("status") in status_messages:
            st.info(status_messages[turn["status"]])
        if turn.get("status") == "provider_error":
            st.error(f"Lỗi provider: {turn.get('error', 'Không rõ lỗi')}")
        if turn.get("status") == "max_tool_rounds":
            st.error(turn.get("assistant_text", "Đã đạt giới hạn vòng tool."))
        event_num = 0
        round_records = list(turn.get("rounds", []))
        pending = turn.get("_pending")
        if pending and (not round_records or round_records[-1] is not pending["round_record"]):
            round_records.append(pending["round_record"])
        for round_record in round_records:
            calls = round_record.get("tool_calls", [])
            events = round_record.get("tool_results", [])
            for call_index, call in enumerate(calls):
                if call_index < len(events) and events[call_index].get("tool") == call.get("name"):
                    event_num += 1
                    _render_event(events[call_index], event_num)
                else:
                    with st.container(border=True):
                        st.markdown(f"**Tool chưa chạy: `{call.get('name', 'unknown')}`**")
                        st.caption("Lượt đã tạm dừng trước khi tới tool này.")
                        st.json(call.get("args", {}), expanded=True)


def main() -> None:
    _init_state()
    # The app loads keys for provider/tool use but never renders environment values.
    load_lab_env(ROOT)
    st.set_page_config(page_title="Research Agent", page_icon="🔎", layout="wide")
    st.title("Research Agent")
    st.caption("Tra cứu tin, bài đăng, URL và tài liệu; xem rõ từng tool trước khi quyết định tác vụ có tác dụng phụ.")

    default_provider = os.getenv("UI_PROVIDER", "openrouter").lower()
    if default_provider not in PROVIDERS:
        default_provider = "openrouter"
    with st.sidebar:
        st.header("Thiết lập")
        busy = bool(st.session_state.pending_turn)
        provider_name = st.selectbox("Provider", PROVIDERS, index=PROVIDERS.index(default_provider), key="provider_selector", disabled=busy)
        model_value = st.text_input("Model (để trống dùng mặc định provider)", value=os.getenv("UI_MODEL", ""), key="model_input", disabled=busy)
        selected_model = model_value.strip() or None
        config_changed = bool(st.session_state.transcript) and (
            st.session_state.provider_name != provider_name
            or st.session_state.requested_model != selected_model
        )
        if config_changed:
            st.warning("Provider or model changed. Start a new conversation before sending to keep the current history.")
        version = st.text_input("Nhãn version", value="ui", key="version_input", disabled=busy)
        try:
            artifact_info = load_artifacts(version=version.strip() or "ui")
        except Exception as exc:
            st.error(f"Không đọc được artifacts/system_prompt.md hoặc artifacts/tools.yaml: {type(exc).__name__}: {exc}")
            st.stop()
        st.caption(f"artifact_version: `{artifact_info['artifact_version']}`")
        if st.button("Cuộc hội thoại mới"):
            if st.session_state.pending_turn:
                abandon_turn(st.session_state.pending_turn)
                st.session_state.pending_turn = None
            _persist()
            st.session_state.history = []
            st.session_state.transcript = None
            st.session_state.provider = None
            st.session_state.requested_model = None
            st.session_state.last_error = None
            st.session_state.transcript_path = None
            st.rerun()
        declared_names = {str(item.get("name", "unknown")) for item in artifact_info["tool_declarations"]}
        gates = confirmation_tools(declared_tools=declared_names)
        st.subheader("Tool hiện có")
        for declaration in artifact_info["tool_declarations"]:
            name = declaration.get("name", "unknown")
            badge = " · cần xác nhận" if name in gates else ""
            st.markdown(f"- `{name}`{badge}")
        st.subheader("Câu hỏi mẫu")
        sample_clicked = None
        for label, sample in SAMPLE_QUESTIONS.items():
            if st.button(label, disabled=bool(st.session_state.pending_turn), key=f"sample_{label}"):
                sample_clicked = sample

    if st.session_state.transcript:
        for turn in st.session_state.transcript["turns"]:
            _render_turn(turn)

    pending = st.session_state.pending_turn
    if pending:
        gate_event = next((event for event in reversed(pending.get("tool_events", [])) if event.get("ui_gate", {}).get("intercepted")), {})
        original_text = str(gate_event.get("args", {}).get("text", ""))
        with st.container(border=True):
            st.subheader("Cần bạn xác nhận")
            st.warning("Tool có tác dụng phụ đang bị giữ lại. Kiểm tra nội dung; chỉ xác nhận nếu muốn thực hiện.")
            edited_text = st.text_area("Nội dung sẽ gửi (có thể sửa)", value=original_text, key="confirmation_text")
            confirm_col, cancel_col = st.columns(2)
            with confirm_col:
                if st.button("Xác nhận gửi", type="primary", key="confirm_send"):
                    _resolve("confirmed", edited_text)
                    st.rerun()
            with cancel_col:
                if st.button("Hủy", key="cancel_send"):
                    _resolve("cancelled", edited_text)
                    st.rerun()

    last_turn = st.session_state.transcript["turns"][-1] if st.session_state.transcript and st.session_state.transcript["turns"] else None
    waiting_turn = last_turn if last_turn and last_turn.get("status") == "waiting_for_user" else None
    clarify_event = None
    if waiting_turn:
        clarify_event = next((e for e in reversed(waiting_turn.get("tool_events", [])) if isinstance(e.get("result"), dict) and e["result"].get("awaiting_user")), None)
    clarify_result = clarify_event.get("result", {}) if clarify_event else {}
    response_type = clarify_result.get("response_type")
    options = clarify_result.get("options", []) if response_type in {"choice", "yes_no"} else []
    if response_type == "yes_no" and not options:
        options = ["Có", "Không"]
    if options and not pending:
        selected_option = st.selectbox("Chọn câu trả lời", options, key="clarify_choice")
        if st.button("Gửi lựa chọn", key="send_clarify_choice"):
            sample_clicked = selected_option

    entered = st.chat_input("Nhập câu hỏi…", disabled=bool(pending))
    request = sample_clicked or entered
    if request and not pending and not config_changed:
        with st.spinner("Waiting for provider and tools..."):
            _submit(request, provider_name, selected_model, artifact_info, gates)
        st.rerun()

    if st.session_state.transcript_path:
        st.caption("Transcript UI đã lưu trong `starter_v0/transcripts_ui/`.")


if __name__ == "__main__":
    main()

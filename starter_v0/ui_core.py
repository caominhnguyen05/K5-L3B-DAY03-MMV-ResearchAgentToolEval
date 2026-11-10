"""Streamlit-independent research-agent loop with a confirmation pause."""
from __future__ import annotations

import inspect
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import yaml

from providers.base import ToolCall
from tools import TOOL_FUNCTIONS, load_tool_declarations, to_openai_tools
from versioning import artifact_version_dict, build_artifact_version


ROOT = Path(__file__).resolve().parent
ARTIFACTS_DIR = ROOT / "artifacts"
TRANSCRIPTS_DIR = ROOT / "transcripts_ui"
HISTORY_WINDOW = 5
MAX_TOOL_ROUNDS = 4
_SECRET_PATTERNS = re.compile(
    r"(?:sk-[A-Za-z0-9_-]{16,}|tvly-[A-Za-z0-9]+|AIza[0-9A-Za-z_-]{20,}|"
    r"(?<![0-9])[0-9]{8,}:[A-Za-z0-9_-]{30,}(?![A-Za-z0-9_-]))"
)


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def safe_slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())
    return slug.strip("_") or "run"


def json_text(value: Any, *, max_chars: int | None = None) -> str:
    result = json.dumps(value, ensure_ascii=False, indent=2, default=str)
    if max_chars is not None and len(result) > max_chars:
        return result[:max_chars] + "\n...<truncated>"
    return result


def redact(value: Any) -> Any:
    """Redact recognizable credentials before displaying or persisting tool output."""
    if isinstance(value, str):
        redacted = _SECRET_PATTERNS.sub("[REDACTED]", value)
        for name, secret in os.environ.items():
            if ("KEY" in name or "TOKEN" in name or "CHAT_ID" in name) and len(secret) >= 8:
                redacted = redacted.replace(secret, "[REDACTED]")
        return redacted
    if isinstance(value, dict):
        return {key: redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact(item) for item in value)
    return value


def trim_history(history: list[dict[str, str]], window: int = HISTORY_WINDOW) -> list[dict[str, str]]:
    if window <= 0:
        return []
    return history[-window * 2:]


def load_artifacts(
    artifacts_dir: Path = ARTIFACTS_DIR,
    version: str = "ui",
) -> dict[str, Any]:
    """Read current prompt/tools from disk on every call; no artifact cache."""
    prompt_path = artifacts_dir / "system_prompt.md"
    tools_path = artifacts_dir / "tools.yaml"
    prompt = prompt_path.read_text(encoding="utf-8")
    declarations = load_tool_declarations(tools_path)
    if not isinstance(declarations, list):
        raise ValueError("artifacts/tools.yaml must contain a 'tools' list")
    version_info = build_artifact_version(version, prompt_path, tools_path)
    return {
        "system_prompt": prompt,
        "tool_declarations": declarations,
        "openai_tools": to_openai_tools(declarations),
        **artifact_version_dict(version_info),
    }


def confirmation_tools(
    tools_dir: Path = ROOT / "tools", declared_tools: set[str] | None = None,
) -> set[str]:
    """Require confirmation for side effects and fail closed on missing manifests."""
    result: set[str] = set()
    valid_manifests: set[str] = set()
    for manifest in tools_dir.glob("*/TOOL.md"):
        try:
            text = manifest.read_text(encoding="utf-8")
            frontmatter = text.split("---", 2)[1]
            metadata = yaml.safe_load(frontmatter) or {}
            if not isinstance(metadata, dict) or not metadata.get("name"):
                continue
        except (OSError, IndexError, yaml.YAMLError):
            continue
        name = str(metadata.get("name", manifest.parent.name))
        valid_manifests.add(name)
        side_effect = str(metadata.get("side_effect", "false")).strip().lower()
        requires_confirmation = str(metadata.get("requires_confirmation", "false")).strip().lower()
        if side_effect == "true" or requires_confirmation == "true":
            result.add(name)
    if declared_tools is not None:
        result.update(declared_tools - valid_manifests)
    return result


def messages_for_turn(system_prompt: str, history: list[dict[str, str]], user_text: str) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": system_prompt},
        *trim_history(history, HISTORY_WINDOW),
        {"role": "user", "content": user_text},
    ]


def assistant_tool_message(response_text: str | None, calls: list[ToolCall]) -> dict[str, str]:
    summary = [{"name": call.name, "args": redact(call.args)} for call in calls]
    content = response_text or "I will call the selected tool(s)."
    return {"role": "assistant", "content": f"{redact(content)}\n\nTOOL_CALLS_JSON:\n{json_text(summary)}"}


def tool_results_message(events: list[dict[str, Any]]) -> dict[str, str]:
    return {
        "role": "user",
        "content": (
            "TOOL_RESULTS_JSON:\n"
            f"{json_text(redact(events), max_chars=24000)}\n\n"
            "Use only these tool results. If the user asked for a digest and the items are ready, "
            "call the formatting tool. Otherwise answer the user directly with cited sources when available."
        ),
    }


def execute_tool_call(call: ToolCall, tool_functions: dict[str, Callable[..., Any]] | None = None) -> dict[str, Any]:
    functions = tool_functions or TOOL_FUNCTIONS
    func = functions.get(call.name)
    if not func:
        return {"tool": call.name, "args": redact(call.args), "result": {"error": "unknown_tool", "message": f"No local implementation for {call.name}"}}
    try:
        result = func(**call.args)
    except Exception as exc:  # A tool failure is transcript evidence, not an app crash.
        result = {"error": type(exc).__name__, "message": str(exc)}
    return redact({"tool": call.name, "args": call.args, "result": result})


def new_transcript(provider: str, model: str | None, artifact_info: dict[str, Any]) -> dict[str, Any]:
    transcript_id = "_".join([safe_slug(artifact_info["version"]), safe_slug(provider), datetime.now().strftime("%Y%m%dT%H%M%S%f")])
    timestamp = now_iso()
    return {
        "transcript_id": transcript_id,
        "source": "streamlit_ui",
        "version": artifact_info["version"],
        "artifact_version": artifact_info["artifact_version"],
        "prompt_hash": artifact_info["prompt_hash"],
        "tools_hash": artifact_info["tools_hash"],
        "provider": provider,
        "model": model,
        "system_prompt": "artifacts/system_prompt.md",
        "tools": "artifacts/tools.yaml",
        "history_window": HISTORY_WINDOW,
        "max_tool_rounds": MAX_TOOL_ROUNDS,
        "created_at": timestamp,
        "updated_at": timestamp,
        "turns": [],
    }


def write_transcript(transcript: dict[str, Any], transcripts_dir: Path | None = None) -> Path:
    transcripts_dir = transcripts_dir or TRANSCRIPTS_DIR
    transcript["updated_at"] = now_iso()
    path = transcripts_dir / f"{safe_slug(transcript['transcript_id'])}.transcript.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    snapshot = dict(transcript)
    snapshot["turns"] = []
    for original_turn in transcript.get("turns", []):
        turn_copy = dict(original_turn)
        pending = turn_copy.pop("_pending", None)
        if pending:
            pending_round = pending["round_record"]
            if not turn_copy.get("rounds") or turn_copy["rounds"][-1] is not pending_round:
                turn_copy["rounds"] = [*turn_copy.get("rounds", []), pending_round]
        snapshot["turns"].append(turn_copy)
    path.write_text(json.dumps(redact(snapshot), ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return path


def _tool_call(item: dict[str, Any]) -> ToolCall:
    return ToolCall(name=item["name"], args=dict(item.get("args", {})))


def _event_error(event: dict[str, Any]) -> bool:
    result = event.get("result")
    if not isinstance(result, dict):
        return False
    status = str(result.get("status", "")).lower()
    return "error" in result or status in {"error", "failed", "failure"}


def _run_calls(
    *, provider: Any, model: str | None, messages: list[dict[str, str]],
    tools: list[dict[str, Any]], confirmation_names: set[str],
    turn: dict[str, Any], tool_functions: dict[str, Callable[..., Any]] | None,
    calls: list[ToolCall] | None = None, round_record: dict[str, Any] | None = None,
    event_index: int = 0, carried_events: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    rounds = turn["rounds"]
    all_events = turn["tool_events"]
    working = messages
    for round_index in range(len(rounds) + 1, MAX_TOOL_ROUNDS + 1):
        if calls is None:
            response = provider.complete(working, tools, model=model, temperature=0.0)
            calls = response.tool_calls
            round_record = {
                "round": round_index,
                "assistant_text": redact(response.text),
                "tool_calls": [{"name": c.name, "args": redact(c.args)} for c in calls],
                "tool_results": [],
            }
            if not calls:
                rounds.append(round_record)
                return {"status": "answered", "assistant_text": redact(response.text or ""), "rounds": rounds, "tool_events": all_events}
            working.append(assistant_tool_message(response.text, calls))
            event_index = 0
            carried_events = []
        assert round_record is not None
        assert carried_events is not None
        for index in range(event_index, len(calls)):
            call = calls[index]
            if call.name in confirmation_names:
                gate_event = {
                    "tool": call.name,
                    "args": redact(call.args),
                    "result": {"status": "awaiting_confirmation"},
                    "ui_gate": {
                        "intercepted": True,
                        "model_confirmed_flag": bool(call.args.get("confirmed", False)),
                        "user_decision": None,
                        "text_edited_by_user": False,
                    },
                }
                round_record["tool_results"].append(gate_event)
                all_events.append(gate_event)
                turn.update({"status": "awaiting_confirmation", "assistant_text": redact(call.args.get("text", "")), "rounds": rounds, "tool_events": all_events})
                turn["_pending"] = {
                    "messages": working,
                    "calls": [{"name": c.name, "args": c.args} for c in calls],
                    "call_index": index,
                    "round_record": round_record,
                    "carried_events": carried_events,
                    "provider": provider,
                    "model": model,
                    "tools": tools,
                    "confirmation_names": confirmation_names,
                    "tool_functions": tool_functions,
                }
                return {"status": "awaiting_confirmation", "assistant_text": turn["assistant_text"], "rounds": rounds, "tool_events": all_events}

            event = execute_tool_call(call, tool_functions)
            round_record["tool_results"].append(event)
            all_events.append(event)
            result = event.get("result", {})
            if isinstance(result, dict) and result.get("awaiting_user"):
                question = result.get("question") or call.args.get("question") or "Bạn bổ sung thêm thông tin nhé."
                rounds.append(round_record)
                turn.update({"status": "waiting_for_user", "assistant_text": redact(question), "rounds": rounds, "tool_events": all_events})
                return {"status": "waiting_for_user", "assistant_text": redact(question), "rounds": rounds, "tool_events": all_events}
            if call.name not in confirmation_names:
                carried_events.append(event)
        rounds.append(round_record)
        working.append(tool_results_message(carried_events))
        calls = None
        round_record = None
        carried_events = None
    return {
        "status": "max_tool_rounds",
        "assistant_text": f"Stopped after {MAX_TOOL_ROUNDS} tool rounds. Inspect the transcript for details.",
        "rounds": rounds,
        "tool_events": all_events,
    }


def run_turn(
    *, provider: Any, model: str | None, artifact_info: dict[str, Any],
    history: list[dict[str, str]], user_text: str,
    confirmation_names: set[str] | None = None,
    tool_functions: dict[str, Callable[..., Any]] | None = None,
) -> dict[str, Any]:
    messages = messages_for_turn(artifact_info["system_prompt"], history, user_text)
    turn = {
        "turn_index": len(history) // 2 + 1,
        "started_at": now_iso(),
        "user": redact(user_text),
        "status": "started",
        "assistant_text": None,
        "rounds": [],
        "tool_events": [],
    }
    try:
        result = _run_calls(
            provider=provider, model=model, messages=messages,
            tools=artifact_info["openai_tools"],
            confirmation_names=confirmation_names or set(), turn=turn,
            tool_functions=tool_functions,
        )
        turn.update(result)
    except Exception as exc:
        turn.update({"status": "provider_error", "error": redact(f"{type(exc).__name__}: {exc}")})
    turn["ended_at"] = now_iso()
    return turn


def resolve_confirmation(
    turn: dict[str, Any], *, decision: str, edited_text: str | None = None,
) -> dict[str, Any]:
    pending = turn.pop("_pending", None)
    if not pending:
        return {"status": turn.get("status", "error"), "assistant_text": turn.get("assistant_text", ""), "rounds": turn.get("rounds", []), "tool_events": turn.get("tool_events", [])}
    calls = [_tool_call(item) for item in pending["calls"]]
    call = calls[pending["call_index"]]
    original_text = str(call.args.get("text", ""))
    updated_text = original_text if edited_text is None else edited_text
    edited = updated_text != original_text
    gate_event = turn["tool_events"][-1]
    gate_event["ui_gate"].update({"user_decision": decision, "text_edited_by_user": edited})
    if decision == "confirmed":
        func = (pending["tool_functions"] or TOOL_FUNCTIONS).get(call.name)
        parameters = inspect.signature(func).parameters if func else {}
        if "text" in call.args or "text" in parameters:
            call.args["text"] = updated_text
        if "confirmed" in parameters:
            call.args["confirmed"] = True
        event = execute_tool_call(call, pending["tool_functions"])
    else:
        event = {"tool": call.name, "args": redact(call.args), "result": {"status": "cancelled_by_user"}}
    event["ui_gate"] = gate_event["ui_gate"]
    round_record = pending["round_record"]
    round_record["tool_results"][-1] = event
    turn["tool_events"][-1] = event
    remaining = calls[pending["call_index"] + 1:]
    return _run_calls(
        provider=pending["provider"], model=pending["model"],
        messages=pending["messages"], tools=pending["tools"],
        confirmation_names=pending["confirmation_names"], turn=turn,
        tool_functions=pending["tool_functions"], calls=remaining,
        round_record=round_record,
        carried_events=pending["carried_events"] + [event],
    ) if remaining else _run_calls(
        provider=pending["provider"], model=pending["model"],
        messages=pending["messages"], tools=pending["tools"],
        confirmation_names=pending["confirmation_names"], turn=turn,
        tool_functions=pending["tool_functions"], calls=[],
        round_record=round_record,
        carried_events=pending["carried_events"] + [event],
    )


def abandon_turn(turn: dict[str, Any]) -> dict[str, Any]:
    """Abandon a paused confirmation turn without executing its pending tool."""
    turn.pop("_pending", None)
    turn["status"] = "abandoned"
    turn["ended_at"] = now_iso()
    for event in reversed(turn.get("tool_events", [])):
        gate = event.get("ui_gate")
        if gate and gate.get("intercepted") and gate.get("user_decision") is None:
            gate["user_decision"] = "abandoned"
            event["result"] = {"status": "abandoned"}
            break
    return turn

"""UJN_LLM_API 端到端冒烟测试（需要真实上游与运行中的 LiteLLM）。

用法：
    uv run python tests/smoke_test.py
    uv run python tests/smoke_test.py --base-url http://127.0.0.1:4000
"""

import argparse
import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ujn_console import enable_utf8_stdout

TOOLS = [
    {"name": "get_weather", "description": "Get weather for a city.",
     "input_schema": {"type": "object",
                      "properties": {"city": {"type": "string"}},
                      "required": ["city"]}},
    {"name": "get_time", "description": "Get time for a city.",
     "input_schema": {"type": "object",
                      "properties": {"city": {"type": "string"}},
                      "required": ["city"]}},
]


def check(label: str, ok: bool, detail: str = "") -> bool:
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    return ok


def main() -> None:
    enable_utf8_stdout()

    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:4000")
    parser.add_argument("--chat-model", default="deepseek-v41-flash")
    parser.add_argument("--anthropic-model", default="deepseek-v41-flash")
    parser.add_argument("--responses-model", default="GLM-5.3")
    parser.add_argument("--timeout", type=float, default=300.0)
    args = parser.parse_args()

    base = args.base_url.rstrip("/")
    client = httpx.Client(timeout=args.timeout, trust_env=False)
    results: list[bool] = []

    print("1. 健康检查")
    try:
        r = client.get(f"{base}/health/liveliness")
        results.append(check("health", r.status_code == 200))
    except Exception as exc:
        results.append(check("health", False, f"{type(exc).__name__}: {exc}"))

    print("2. OpenAI /v1/chat/completions")
    r = client.post(f"{base}/v1/chat/completions",
                    json={"model": args.chat_model, "max_tokens": 64,
                          "messages": [{"role": "user", "content": "hi"}]})
    results.append(check("chat completions", r.status_code == 200, f"HTTP {r.status_code}"))

    print("3. Anthropic /v1/messages + 并行工具调用")
    r = client.post(f"{base}/v1/messages",
                    json={"model": args.anthropic_model, "max_tokens": 600,
                          "tools": TOOLS, "tool_choice": {"type": "auto"},
                          "messages": [{"role": "user",
                                        "content": "weather and time in Jinan? use both tools"}]})
    ok = False
    detail = f"HTTP {r.status_code}"
    if r.status_code == 200:
        payload = r.json()
        uses = [b for b in payload.get("content") or [] if b.get("type") == "tool_use"]
        ok = payload.get("stop_reason") == "tool_use" and len(uses) >= 1
        detail = f"stop={payload.get('stop_reason')} tools={[b['name'] for b in uses]}"
    results.append(check("anthropic tools", ok, detail))

    print("4. Anthropic /v1/messages 流式 + 工具")
    saw_start = saw_json = False
    stop_reason = None
    with client.stream("POST", f"{base}/v1/messages",
                       json={"model": args.anthropic_model, "max_tokens": 600,
                             "tools": TOOLS, "tool_choice": {"type": "auto"},
                             "messages": [{"role": "user",
                                           "content": "weather in Jinan? use the tool"}],
                             "stream": True}) as r:
        for raw in r.iter_lines():
            if not raw:
                continue
            line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
            if not line.startswith("data:"):
                continue
            try:
                event = json.loads(line[5:].strip())
            except json.JSONDecodeError:
                continue
            etype = event.get("type")
            if etype == "content_block_start" and (event.get("content_block") or {}).get("type") == "tool_use":
                saw_start = True
            if (event.get("delta") or {}).get("type") == "input_json_delta":
                saw_json = True
            if etype == "message_delta":
                stop_reason = (event.get("delta") or {}).get("stop_reason")
    results.append(check("anthropic stream tools", saw_start and saw_json and stop_reason == "tool_use",
                         f"start={saw_start} json_delta={saw_json} stop={stop_reason}"))

    print("5. Codex /v1/responses 流式 + 工具")
    names: list[str] = []
    arg_deltas = 0
    status = None
    body = {
        "model": args.responses_model, "stream": True, "store": False,
        "tool_choice": "auto", "include": ["reasoning.encrypted_content"],
        "input": [{"type": "message", "role": "user",
                   "content": [{"type": "input_text",
                                "text": "weather and time in Jinan? use both tools"}]}],
        "tools": [{"type": "function", "name": t["name"], "description": t["description"],
                   "parameters": t["input_schema"], "strict": False} for t in TOOLS],
    }
    with client.stream("POST", f"{base}/v1/responses", json=body) as r:
        for raw in r.iter_lines():
            if not raw:
                continue
            line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                event = json.loads(data)
            except json.JSONDecodeError:
                continue
            etype = event.get("type")
            if etype == "response.completed":
                status = (event.get("response") or {}).get("status")
            if etype == "response.output_item.added":
                item = event.get("item") or {}
                if item.get("type") == "function_call":
                    names.append(item.get("name"))
            if etype == "response.function_call_arguments.delta":
                arg_deltas += 1
    results.append(check("codex responses tools", status == "completed" and bool(names),
                         f"status={status} calls={names} deltas={arg_deltas}"))

    print("6. Anthropic tool_result 回传")
    r = client.post(f"{base}/v1/messages",
                    json={"model": args.anthropic_model, "max_tokens": 400,
                          "tools": TOOLS[:1], "tool_choice": {"type": "auto"},
                          "messages": [
                              {"role": "user", "content": "weather in Jinan?"},
                              {"role": "assistant", "content": [
                                  {"type": "tool_use", "id": "toolu_1",
                                   "name": "get_weather", "input": {"city": "Jinan"}}]},
                              {"role": "user", "content": [
                                  {"type": "tool_result", "tool_use_id": "toolu_1",
                                   "content": "Sunny, 26C."}]},
                          ]})
    ok = False
    if r.status_code == 200:
        text = "".join(b.get("text") or "" for b in r.json().get("content") or []
                       if b.get("type") == "text")
        ok = bool(text.strip())
    results.append(check("tool_result roundtrip", ok, f"HTTP {r.status_code}"))

    passed = sum(1 for x in results if x)
    print(f"\n{passed}/{len(results)} passed")
    if passed < len(results):
        print("\n提示：若 anthropic/codex 的 tools 失败但 chat 通过，"
              "检查 litellm_config.yaml 里两个 bridge 开关是否都在。")
        sys.exit(1)


if __name__ == "__main__":
    main()

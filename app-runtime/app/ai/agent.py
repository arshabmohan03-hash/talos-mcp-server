"""The agent loop: drives the tool-calling conversation and yields live events.

Events (dicts): {"type": ...}
  status         -> {"data": "thinking"}
  tool_call      -> {"data": {"name", "arguments"}}
  tool_result    -> {"data": {"name", "summary", "result"}}
  reasoning_delta-> {"data": "<chunk of the model's chain-of-thought>"}
  answer_delta   -> {"data": "<chunk of the final markdown answer>"}
  message_done   -> {"data": "<full final answer>", "model": "..."}
  error          -> {"data": "..."}
  done           -> {}

The final answer streams token-by-token via `answer_delta`; `message_done`
carries the assembled text (for history / a final clean re-render).
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator

from .client import stream_completion
from .prompts import NEMESIS_PROMPT, STORY_PROMPT, STUDY_PROMPT, SYSTEM_PROMPT
from .tools import TOOLS_SCHEMA, dispatch, summarize_result

MAX_ITERATIONS = 6
MAX_TOOL_RESULT_CHARS = 12000
# gpt-oss spends reasoning tokens against this same budget, and a rich, polished
# interactive `kapp` widget (controls, legend, captions) can be large — give headroom.
MAX_ANSWER_TOKENS = 24000

_PROMPTS = {
    "assistant": SYSTEM_PROMPT,
    "study": STUDY_PROMPT,
    "story": STORY_PROMPT,      # Talos: Origins handler (CIPHER)
    "nemesis": NEMESIS_PROMPT,  # the campaign's final-boss AI villain
}


async def run_agent(user_message: str,
                    history: list[dict] | None = None,
                    mode: str = "assistant") -> AsyncIterator[dict]:
    prompt = _PROMPTS.get(mode, SYSTEM_PROMPT)
    messages: list[dict] = [{"role": "system", "content": prompt}]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": user_message})

    for _ in range(MAX_ITERATIONS):
        yield {"type": "status", "data": "thinking"}

        model_used: str | None = None
        content_parts: list[str] = []
        tool_acc: dict[int, dict] = {}  # tool-call index -> {id, name, arguments}

        try:
            async for kind, payload in stream_completion(
                    messages, tools=TOOLS_SCHEMA, max_tokens=MAX_ANSWER_TOKENS):
                if kind == "model":
                    model_used = payload
                    continue

                choices = getattr(payload, "choices", None)
                if not choices:
                    continue
                delta = choices[0].delta

                reasoning = getattr(delta, "reasoning", None)
                if reasoning:
                    yield {"type": "reasoning_delta", "data": reasoning}

                content = getattr(delta, "content", None)
                if content:
                    content_parts.append(content)
                    yield {"type": "answer_delta", "data": content}

                for tcd in getattr(delta, "tool_calls", None) or []:
                    slot = tool_acc.setdefault(
                        tcd.index, {"id": "", "name": "", "arguments": ""}
                    )
                    if tcd.id:
                        slot["id"] = tcd.id
                    fn = getattr(tcd, "function", None)
                    if fn:
                        if fn.name:
                            slot["name"] = fn.name
                        if fn.arguments:
                            slot["arguments"] += fn.arguments
        except Exception as e:  # noqa: BLE001
            yield {"type": "error", "data": str(e)}
            return

        if tool_acc:
            ordered = [tool_acc[i] for i in sorted(tool_acc)]
            messages.append({
                "role": "assistant",
                "content": "".join(content_parts),
                "tool_calls": [
                    {"id": t["id"], "type": "function",
                     "function": {"name": t["name"], "arguments": t["arguments"]}}
                    for t in ordered
                ],
            })
            for t in ordered:
                name = t["name"]
                try:
                    args = json.loads(t["arguments"] or "{}")
                except json.JSONDecodeError:
                    args = {}
                yield {"type": "tool_call", "data": {"name": name, "arguments": args}}
                result = await dispatch(name, args)
                yield {"type": "tool_result", "data": {
                    "name": name,
                    "summary": summarize_result(name, result),
                    "result": result,
                }}
                messages.append({
                    "role": "tool",
                    "tool_call_id": t["id"],
                    "content": json.dumps(result)[:MAX_TOOL_RESULT_CHARS],
                })
            continue

        # No tool calls -> the final answer has finished streaming.
        yield {"type": "message_done", "data": "".join(content_parts), "model": model_used}
        yield {"type": "done"}
        return

    yield {"type": "message_done",
           "data": "I couldn't finish that within my step limit. Try narrowing the request."}
    yield {"type": "done"}

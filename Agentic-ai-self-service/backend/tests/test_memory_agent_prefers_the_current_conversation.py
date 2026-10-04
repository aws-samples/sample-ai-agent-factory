"""A remembered fact never outranks what the user says in the current conversation.

Measured live 2026-09-30: one memory, adopted by three deployments of the same owner
(memory_step same-caller adoption), held an older "durable customer reference code".
Told a new code, the generated agent answered that according to the long-term memory
on file the code was the old one, not the new one, and a turn later repeated the
stale value. Long-term records were handed to the model as plain "Relevant long-term
memory" with nothing saying they can be out of date.

The generated memory agent now frames them as possibly out of date and says the
conversation wins. These tests lift the real ``invoke`` out of the generated source
and inspect the prompt it hands the model, so they measure the shipped text.
"""

from __future__ import annotations

import ast

from app.services import codegen_templates
from app.services.code_generator import _generate_memory_agent

SESSION = "0123456789abcdef0123456789abcdef-0123456789abcdef0123456789abcdef"
ACTOR = "0123456789abcdef0123456789abcdef"


class _App:
    @staticmethod
    def entrypoint(fn):
        return fn


def _prompt_handed_to_the_model(long_term: str, recent: str) -> str:
    prompts: list[str] = []
    namespace = {
        "app": _App,
        "MEMORY_ID": "memory-AbCdEf1234",
        "_get_recent_context": lambda *_args: recent,
        "_get_long_term_context": lambda *_args: long_term,
        "_save_to_memory": lambda *_args: None,
        "_get_agent": lambda: lambda prompt: prompts.append(prompt) or "ok",
    }
    source = _generate_memory_agent("You are helpful.", "us.anthropic.claude-sonnet-5", "us-east-1")
    tree = ast.parse(source)  # the whole module must compile, not just the function
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "invoke")
    # invoke reports its tool calls through the receipt helpers spliced in beside it.
    exec(compile(codegen_templates.load_impl("tool_receipts"), "<tool_receipts>", "exec"), namespace)
    exec(compile(ast.Module(body=[node], type_ignores=[]), "<generated>", "exec"), namespace)
    namespace["invoke"]({"prompt": "My code is NEW-2.", "session_id": SESSION, "actor_id": ACTOR})
    assert len(prompts) == 1
    return prompts[0]


def test_long_term_memory_is_framed_as_possibly_out_of_date():
    prompt = _prompt_handed_to_the_model("The user's code is OLD-1.", "")

    header, _, rest = prompt.partition("The user's code is OLD-1.")
    assert rest, "the long-term record must still reach the model"
    assert header.startswith("Relevant long-term memory (")
    assert "possibly out of date" in header
    assert "the previous conversation context or the current message says otherwise, those are correct" in header
    assert prompt.endswith("Current message: My code is NEW-2.")


def test_the_conversation_follows_the_long_term_block():
    prompt = _prompt_handed_to_the_model("The user's code is OLD-1.", "user: My code is NEW-2.")

    assert prompt.index("Relevant long-term memory") < prompt.index("Previous conversation context:")
    assert prompt.index("Previous conversation context:") < prompt.index("Current message:")


def test_no_precedence_text_without_long_term_memory():
    prompt = _prompt_handed_to_the_model("", "user: hi")

    assert "out of date" not in prompt
    assert prompt.startswith("Previous conversation context:")

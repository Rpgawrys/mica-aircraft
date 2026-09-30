#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""One call site for every model call in the app.

Before this, each agent built its own Anthropic client and hunted for the JSON
object inside the reply with a regex. That is the part LangChain actually earns
its place on:

  * ChatAnthropic + JsonOutputParser composed with `|`, so the parser handles
    fenced blocks and preamble text instead of `text.find("{")`.
  * one place to change the model, add retries, or turn on LangSmith tracing.
  * swapping providers is a one-line change here, not a rewrite in three agents.

If langchain is not importable the module falls back to the raw Anthropic SDK
with the old brace-hunting parser, so a framework problem cannot take the
demo down. `backend()` reports which path is live.
"""

import json
import os
import re

PROVIDER = os.environ.get("LLM_PROVIDER", "anthropic")

_LC = None
try:
    from langchain_core.messages import HumanMessage, SystemMessage
    from langchain_core.output_parsers import JsonOutputParser
    _LC = True
except Exception:                                    # noqa: BLE001
    _LC = False

# Each vendor's chat class is imported only if it is installed, so the app runs
# with either one present and reports honestly which are actually available.
try:
    from langchain_anthropic import ChatAnthropic
except Exception:                                    # noqa: BLE001
    ChatAnthropic = None
try:
    from langchain_openai import ChatOpenAI
except Exception:                                    # noqa: BLE001
    ChatOpenAI = None


def backend():
    if not _LC:
        return "anthropic-sdk"
    return "langchain/%s" % PROVIDER


def _chat(model, max_tokens):
    """The one place a provider swap happens. The agents never name a vendor.

    Set LLM_PROVIDER plus the three model variables (AQL_AGENT_MODEL,
    RECONCILER_MODEL, UPDATE_AGENT_MODEL) and every agent moves together.
    """
    if PROVIDER == "anthropic":
        if ChatAnthropic is None:
            raise RuntimeError("LLM_PROVIDER=anthropic but langchain-anthropic "
                               "is not installed")
        return ChatAnthropic(model=model, max_tokens=max_tokens, timeout=180)
    if PROVIDER == "openai":
        if ChatOpenAI is None:
            raise RuntimeError("LLM_PROVIDER=openai but langchain-openai "
                               "is not installed")
        # max_tokens is mapped to max_completion_tokens by langchain-openai for
        # the models that renamed it, so one argument covers both generations.
        # JSON mode makes the vendor guarantee parseable output. Every prompt in
        # this app asks for a JSON object, and OpenAI requires the word "json" to
        # appear in the messages, which they all satisfy.
        return ChatOpenAI(model=model, max_tokens=max_tokens, timeout=180,
                          model_kwargs={"response_format": {"type": "json_object"}})
    raise RuntimeError("provider %r is not wired up in llm.py" % PROVIDER)


def _fallback_json(text):
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    s, e = text.find("{"), text.rfind("}")
    if s >= 0 and e > s:
        text = text[s:e + 1]
    return json.loads(text)


def _is_length_error(e):
    """Reasoning models spend reasoning tokens out of the same max_tokens budget,
    so a hard question can exhaust the allowance before a single character of JSON
    is emitted. OpenAI surfaces that as LengthFinishReasonError rather than a
    truncated body, and it is intermittent because reasoning length varies run to
    run on identical input."""
    return (type(e).__name__ == "LengthFinishReasonError"
            or "length limit was reached" in str(e)
            or "finish_reason" in str(e) and "length" in str(e))


def _invoke(system, user, model, max_tokens):
    chain = _chat(model, max_tokens) | JsonOutputParser()
    return chain.invoke([SystemMessage(content=system),
                         HumanMessage(content=user)])


def complete_json(system, user, model, max_tokens=3000):
    """Run one prompt and return the parsed JSON object it asked for.

    max_tokens is a ceiling, not a reservation, so raising it on a retry costs
    nothing unless the model actually needs the room.
    """
    if _LC:
        try:
            return _invoke(system, user, model, max_tokens)
        except Exception as e:                         # noqa: BLE001
            if not _is_length_error(e):
                raise
            return _invoke(system, user, model, max_tokens * 3)

    # LangChain missing: fall back to the vendor SDK directly. Only Anthropic has
    # a fallback path, so an OpenAI run says so rather than silently calling the
    # wrong vendor with the wrong key.
    if PROVIDER != "anthropic":
        raise RuntimeError("LLM_PROVIDER=%s needs langchain-openai; there is no "
                           "raw-SDK fallback for it" % PROVIDER)
    from anthropic import Anthropic
    resp = Anthropic().messages.create(
        model=model, max_tokens=max_tokens, system=system,
        messages=[{"role": "user", "content": user}])
    return _fallback_json("".join(b.text for b in resp.content if b.type == "text"))

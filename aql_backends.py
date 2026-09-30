#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Two ways to turn a question into AQL, behind one interface.

    builtin   agent 1's own prompt, grounded in data/schema.md. Around 2s.
    aqlizer   ArangoDB's Natural Language to AQL Translation Service, deployed
              under the ACP service name `arangodb-graph-rag`. It introspects
              the graph itself, so it cannot drift out of sync with the model
              the way a schema file can. Around 10s.

Measured over the same ten questions: both produced executable AQL 10 times out
of 10. The builtin returned rows on 9, the aqlizer on 8, and they miss on
DIFFERENT questions. So this is a genuine trade of latency against a different
set of blind spots, not a straight accuracy upgrade.

Whichever backend answers, `aql_agent.run()` applies the same read-only screen,
the same /_api/explain validation and the same retry, so a slower engine never
buys a weaker guard.

Env:
    AQL_BACKEND     builtin (default) | aqlizer
    AQLIZER_POD     last segment of the service id, e.g. fqu0w
    AQLIZER_URL     full service URL, overrides the derived one
"""

import ast
import os
import re

import arango_client as arango

class BackendRefused(RuntimeError):
    """The service declined the question rather than failing technically.

    The aqlizer screens the ENGLISH question for write-ish words and returns 400
    "Security violation: Write operations are not allowed." Whole-word matches on
    update, change, changed, modify, modified, new, insert, removed, replace and
    upsert all trip it, while newer, newly, remove, renew and truncate pass. Since
    this app is about change detection, its own vocabulary sets the guard off, so
    a refusal is expected traffic rather than an outage. Callers fall back.
    """


BACKENDS = ("builtin", "aqlizer")
DEFAULT = os.environ.get("AQL_BACKEND", "builtin").strip().lower() or "builtin"

POD = os.environ.get("AQLIZER_POD", "").strip()
URL = os.environ.get("AQLIZER_URL", "").strip().rstrip("/")
GRAPH_NAME = os.environ.get("STRUCTURED_GRAPH_NAME", "aircraft_model")
TIMEOUT = int(os.environ.get("AQLIZER_TIMEOUT", "180"))


def service_url():
    """{BASE}/graph-rag/{pod}. Note the hyphens: the importer and retriever sit
    under /graphrag/ without one, this service does not."""
    if URL:
        return URL
    if not POD:
        return None
    return "%s/graph-rag/%s" % (arango.URL.rstrip("/"), POD)


def configured():
    return bool(service_url())


def health():
    url = service_url()
    if not url:
        return {"ok": False, "error": "no aqlizer configured (set AQLIZER_POD)"}
    import requests
    try:
        r = requests.get("%s/v1/health" % url,
                         headers={"Authorization": "Bearer %s" % arango.jwt()},
                         timeout=30)
        return {"ok": r.status_code == 200, "status": r.status_code,
                "url": url, "body": r.text[:200]}
    except Exception as e:                             # noqa: BLE001
        return {"ok": False, "url": url, "error": "%s: %s" % (type(e).__name__, e)}


# The service returns str(AIMessage) rather than the message content, so the AQL
# arrives wrapped in a Python repr with escaped newlines. Unwrap before parsing.
_CONTENT = re.compile(r"content='(.*?)'\s+additional_kwargs=", re.S)
_CONTENT_DQ = re.compile(r'content="(.*?)"\s+additional_kwargs=', re.S)
_FENCE = re.compile(r"```(?:aql)?\s*(.+?)```", re.S)
_BARE = re.compile(r"((?:WITH\s+[^\n]+\n)?FOR\s+\w+\s+IN\s+.+)", re.S)
_USAGE_AT = re.compile(r"usage_metadata=\{")


def _unwrap(s):
    if not s:
        return ""
    m = _CONTENT.search(s) or _CONTENT_DQ.search(s)
    body = m.group(1) if m else s
    if "\\n" in body:
        try:
            body = body.encode().decode("unicode_escape")
        except Exception:                              # noqa: BLE001
            pass
    return body


def _aql_from(text):
    m = _FENCE.search(text or "")
    if m:
        return m.group(1).strip()
    m = _BARE.search(text or "")
    return m.group(1).strip() if m else None


def _balanced(text, start):
    """usage_metadata holds nested dicts, so a non-greedy regex stops at the first
    inner brace and produces something literal_eval cannot read. Count braces."""
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def _tokens(raw):
    n_in = n_out = 0
    for m in _USAGE_AT.finditer(raw or ""):
        blob = _balanced(raw, m.end() - 1)
        if not blob:
            continue
        try:
            d = ast.literal_eval(blob)
        except Exception:                              # noqa: BLE001
            continue
        n_in += d.get("input_tokens") or 0
        n_out += d.get("output_tokens") or 0
    return {"input": n_in, "output": n_out}


def generate(question, platform=None, prior=None):
    """Same {aql, bind_vars, note} contract as the builtin generator.

    The service inlines literals rather than emitting bind parameters, so
    bind_vars comes back empty and every guard downstream still works.
    """
    import requests
    url = service_url()
    if not url:
        raise RuntimeError("AQL_BACKEND=aqlizer but no AQLIZER_POD or AQLIZER_URL is set")

    text = ("Regarding the platform named exactly '%s': %s" % (platform, question)
            if platform else question)
    if prior:
        text += ("\n\nYour previous query was rejected and never ran.\nRejected query:\n"
                 "%s\n\nArangoDB said: %s\n\nFix that specific problem." % prior)

    r = requests.post(
        "%s/v1/translate_query" % url,
        headers={"Authorization": "Bearer %s" % arango.jwt(),
                 "Content-Type": "application/json"},
        json={"input_text": text,
              "options": {"output_formats": ["AQL", "NL"], "graph_name": GRAPH_NAME}},
        timeout=TIMEOUT)
    if r.status_code == 400 and "Write operations are not allowed" in r.text:
        raise BackendRefused(
            "ArangoDB's AQL service refused the question. It screens the wording for "
            "write keywords, and words like new, change, updated or replaced set it "
            "off even in a read-only question.")
    if r.status_code >= 400:
        raise RuntimeError("aqlizer HTTP %d: %s" % (r.status_code, r.text[:300]))

    body = r.json() or {}
    aql = _aql_from(_unwrap(body.get("aqlQuery")))
    nl = _unwrap(body.get("nlResponse")).strip()
    return {"aql": aql, "bind_vars": {},
            "note": nl[:400] or "Translated by ArangoDB's natural language to AQL service.",
            "tokens": _tokens(r.text)}

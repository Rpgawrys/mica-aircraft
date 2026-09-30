#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AGENT 2 - the unstructured-corpus agent.

Asks the SAME question of the ArangoDB GraphRAG Retriever service, which is built
over the unstructured source documents. It never touches the structured model -
the contrast with agent 1 is the point.

API contract taken from the AutoGraph e2e demo notebook (AutoGraph v0.0.12 /
GraphRAG Retriever v0.0.17):

    service URL = {BASE}/graphrag/retriever/{last 5 chars of service id}
    endpoints   = GET  /v1/health
                  POST /v1/graphrag-query
                  POST /v1/graphrag-query-stream
    auth        = Authorization: Bearer <ArangoDB JWT>
    body        = {"query": str, "query_type": int, ...}
                  query_type: GLOBAL=1 LOCAL=2 UNIFIED=3 CUSTOM=4

Env:
    AUTOGRAPH_RETRIEVER_ID    e.g. arangodb-graphrag-retriever-gh6vy
    AUTOGRAPH_RETRIEVER_URL   full service URL, overrides the derived one
    AUTOGRAPH_DB              informational only (the service is bound to its db)
"""

import json
import os
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent.parent))
import arango_client as arango  # noqa: E402

RETRIEVER_ID = os.environ.get("AUTOGRAPH_RETRIEVER_ID", "").strip()
RETRIEVER_URL = os.environ.get("AUTOGRAPH_RETRIEVER_URL", "").strip().rstrip("/")
AUTOGRAPH_DB = os.environ.get("AUTOGRAPH_DB", "Aircraft KG Strong")
ROUTE_PREFIX = os.environ.get("AUTOGRAPH_ROUTE_PREFIX", "graphrag/retriever")
TIMEOUT = int(os.environ.get("AUTOGRAPH_TIMEOUT", "300"))

QUERY_TYPES = {"GLOBAL": 1, "LOCAL": 2, "UNIFIED": 3, "CUSTOM": 4}

# The retriever answers in its own voice by default: "The Context says ...", bold
# on every noun. `response_instructions` is honoured per query (verified against
# the success retriever: the same question went from "The Context says" to naming
# "Electronic Warfare Systems Bulletin 2026-16"). These are MICA's writing rules,
# the same ones agents 3 and 4 already follow, so agent 2 stops being the one
# voice on the page that reads differently. The service still imposes its own
# section headings; the UI renders those fine, so they are left alone.
RESPONSE_INSTRUCTIONS = os.environ.get("AUTOGRAPH_RESPONSE_INSTRUCTIONS", "").strip() or (
    "Write for a working intelligence analyst. Answer directly in plain prose. Never "
    "refer to 'the context', 'the provided context', 'the documents provided' or the "
    "retrieval process; state findings as findings and name the source document when "
    "you can. Active voice. No em dashes, no adverbs, no bold. Keep every hedge the "
    "source itself uses: if a document says alleged or unconfirmed, say so.")

# What the analyst picks in "Corpus strategy". The platform's own web UI maps its
# "deep-search" onto query_type 2, the same type as local, so the two differ only
# in whether citations come back. Keeping both is deliberate: deep search trades
# the citation list for a less constrained answer.
STRATEGIES = {
    "deep":    {"query_type": 2, "show_citations": False,
                "label": "Deep search", "note": "no citations"},
    "local":   {"query_type": 2, "show_citations": True,
                "label": "Local", "note": "entity neighbourhood"},
    "unified": {"query_type": 3, "show_citations": True,
                "label": "Unified", "note": "local plus global"},
    "global":  {"query_type": 1, "show_citations": True,
                "label": "Global", "note": "community summaries"},
}
DEFAULT_STRATEGY = os.environ.get("CORPUS_STRATEGY", "unified").strip().lower()
if DEFAULT_STRATEGY not in STRATEGIES:
    DEFAULT_STRATEGY = "unified"


def service_url():
    """{BASE}/graphrag/retriever/{last5} - the notebook's derivation."""
    if RETRIEVER_URL:
        return RETRIEVER_URL
    if not RETRIEVER_ID or len(RETRIEVER_ID) < 5:
        return None
    return "%s/%s/%s" % (arango.URL.rstrip("/"), ROUTE_PREFIX, RETRIEVER_ID[-5:])


def _headers():
    return {"Authorization": "Bearer %s" % arango.jwt(),
            "Content-Type": "application/json"}


def health():
    url = service_url()
    if not url:
        return {"ok": False, "error": "no retriever configured"}
    try:
        r = requests.get("%s/v1/health" % url, headers=_headers(), timeout=30)
        return {"ok": r.status_code == 200, "status": r.status_code,
                "url": url, "body": r.text[:200]}
    except Exception as e:
        return {"ok": False, "url": url, "error": "%s: %s" % (type(e).__name__, e)}


def run(question, platform=None, strategy=None):
    """`strategy` is a key of STRATEGIES. Anything unrecognised falls back to the
    configured default rather than erroring, so a stale UI cannot break a demo."""
    strategy = (strategy or DEFAULT_STRATEGY).strip().lower()
    if strategy not in STRATEGIES:
        strategy = DEFAULT_STRATEGY
    cfg = STRATEGIES[strategy]
    url = service_url()
    out = {"agent": "unstructured", "question": question, "platform": platform,
           "strategy": strategy, "strategy_label": cfg["label"],
           "query_type": cfg["query_type"], "citations": cfg["show_citations"],
           "answer": None, "sources": [], "raw": None, "error": None,
           "configured": bool(url), "service_url": url}
    if not url:
        out["error"] = ("Retriever not configured. Set AUTOGRAPH_RETRIEVER_ID "
                        "(e.g. arangodb-graphrag-retriever-xxxxx).")
        return out

    scoped = "Regarding the %s: %s" % (platform, question) if platform else question
    payload = {"query": scoped,
               "query_type": cfg["query_type"],
               "include_metadata": True,
               "show_citations": cfg["show_citations"]}
    if RESPONSE_INSTRUCTIONS:
        payload["response_instructions"] = RESPONSE_INSTRUCTIONS
    try:
        r = requests.post("%s/v1/graphrag-query" % url, json=payload,
                          headers=_headers(), timeout=TIMEOUT)
        if r.status_code >= 400:
            out["error"] = "retriever HTTP %d: %s" % (r.status_code, r.text[:400])
            return out
        body = r.json()
        out["raw"] = body
        out["answer"] = (body.get("response") or body.get("global_context")
                         or body.get("answer") or body.get("result"))
        # the service returns `metadata` as a JSON-encoded STRING, not an object
        meta = body.get("metadata") or {}
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except ValueError:
                meta = {}
        out["metadata"] = meta
        cm = meta.get("citation_mapping") or {}
        for k, v in list(cm.items())[:12]:
            if isinstance(v, dict):
                out["sources"].append({
                    "id": k,
                    "excerpt": (v.get("content") or "")[:240],
                    "source": v.get("source") or v.get("file_name") or ""})
            elif isinstance(v, str):
                out["sources"].append({"id": k, "excerpt": v[:240], "source": ""})
        if not out["answer"] and isinstance(body, dict):
            out["error"] = "unexpected response shape: %s" % list(body)[:8]
    except Exception as e:
        out["error"] = "%s: %s" % (type(e).__name__, e)
    return out


if __name__ == "__main__":
    if "--health" in sys.argv:
        print(json.dumps(health(), indent=2)); sys.exit()
    q = sys.argv[1] if len(sys.argv) > 1 else \
        "Is there a scheduled replacement programme for any component inside the Khibiny-M jamming pod?"
    p = sys.argv[2] if len(sys.argv) > 2 else "Su-35S Flanker-E"
    print(json.dumps(run(q, p), indent=2)[:2500])

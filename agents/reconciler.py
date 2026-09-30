#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AGENT 3 - the reconciler.

Takes the outputs of agent 1 (official structured model) and agent 2 (unstructured
corpus) and tells the analyst three things:

  1. OFFICIAL      - what the data model formally records
  2. CORROBORATED  - where unstructured reporting agrees with the model
  3. NOT YET IN MODEL - what the documents assert that the model does not contain

It then proposes concrete, machine-applicable edits to the structured graph. Each
proposal is a typed operation the app can execute against gharial, carrying the
evidence that justified it so the change is auditable after the fact.

Proposals are SUGGESTIONS. Nothing is written until an analyst accepts it.
"""

import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import arango_client as arango  # noqa: E402

import llm  # noqa: E402

MODEL = os.environ.get("RECONCILER_MODEL", "claude-opus-5")
SCHEMA = (Path(__file__).parent.parent / "data" / "schema.md").read_text(encoding="utf-8")

SYSTEM = """You are an intelligence data-model steward. You are given:
  (A) the result of querying the OFFICIAL STRUCTURED DATA MODEL, and
  (B) the result of asking the SAME question of a knowledge graph built from
      unstructured source documents (technical bulletins, depot notices, OSINT
      posts, intercepts).

Your job is to reconcile them for the analyst and propose model updates.

Be rigorous about the distinction between:
  - what the model officially records,
  - what the documents CORROBORATE (agrees with the model),
  - what the documents assert that is NOT in the model yet,
  - and what the documents CONTRADICT.

Critical rules:
- Never present an unconfirmed claim as fact. If a source hedges ("alleged",
  "unconfirmed", "not corroborated", "reliability not established"), carry that
  hedge into your summary and into the proposal's confidence.
- If (B) is unavailable or errored, say so plainly and propose nothing. Do not
  speculate about what the documents might contain.
- Only propose an edit when the unstructured side genuinely supports it. Zero
  proposals is a perfectly good answer.
- Do not propose deleting or overwriting baseline values on the strength of a
  single hedged source. Prefer adding a new node/edge, or flagging a discrepancy
  for review, over silently changing an established figure.

Available edit operations, matched to the schema below:
  add_vertex  - a new entity (e.g. a DefensiveSystem the model lacks)
                {"op":"add_vertex","collection":"...","doc":{...}}
  add_edge    - connect an existing/new entity to a category node
                {"op":"add_edge","collection":"...","from":"Coll/key","to":"Coll/key","attrs":{...}}
  update_vertex - amend attributes on an existing node
                {"op":"update_vertex","collection":"...","key":"...","patch":{...}}
  flag_discrepancy - record a conflict without changing data
                {"op":"flag_discrepancy","target":"Coll/key","field":"...","model_value":...,"claimed_value":...}

Respond with ONLY a JSON object, no prose, no code fences:
{
 "summary": "2-4 sentences for the analyst",
 "official": ["what the structured model records"],
 "corroborated": ["points where documents agree with the model"],
 "not_in_model": ["assertions in documents absent from the model"],
 "contradictions": ["direct conflicts, with the hedge level of the source"],
 "proposals": [
   {"op":"...", "...":"...",
    "title":"short label for the analyst",
    "rationale":"why this edit follows from the evidence",
    "evidence":"source name / document referred to",
    "confidence":"high|medium|low"}
 ]
}

Write the prose fields (summary, official, corroborated, not_in_model,
contradictions, rationale) for a working analyst:
- Active voice. Name who or what does the thing.
- No em dashes. No adverbs. No "not X but Y" constructions.
- State findings directly. Skip throat-clearing and pull-quote endings.
- Vary sentence length. Be specific about which document said what."""


def _extract_json(text):
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    s, e = text.find("{"), text.rfind("}")
    if s >= 0 and e > s:
        text = text[s:e + 1]
    return json.loads(text)


def _trim(obj, cap=9000):
    s = json.dumps(obj, indent=1, default=str)
    return s if len(s) <= cap else s[:cap] + "\n...(truncated)"


def run(question, structured, unstructured, platform=None, client=None):
    out = {"agent": "reconciler", "summary": None, "official": [],
           "corroborated": [], "not_in_model": [], "contradictions": [],
           "proposals": [], "error": None}
    try:
        parts = ["Schema of the structured model:\n\n%s" % SCHEMA]
        if platform:
            parts.append("Platform in focus: %s" % platform)
        else:
            parts.append("Scope: every platform in the model and the whole "
                         "document corpus. No single platform is in focus, so "
                         "name the platform each finding belongs to.")
        parts.append("Analyst question: %s" % question)
        parts.append("(A) STRUCTURED MODEL RESULT\n%s" % _trim({
            "generated_aql": structured.get("aql"),
            "note": structured.get("note"),
            "row_count": structured.get("row_count"),
            "rows": structured.get("rows"),
            "error": structured.get("error"),
        }))
        parts.append("(B) UNSTRUCTURED CORPUS RESULT\n%s" % _trim({
            "configured": unstructured.get("configured"),
            "answer": unstructured.get("answer"),
            "sources": unstructured.get("sources"),
            "error": unstructured.get("error"),
        }))
        data = llm.complete_json(SYSTEM, "\n\n".join(parts),
                                 model=MODEL, max_tokens=16000)
        out.update({k: data.get(k, out[k]) for k in
                    ("summary", "official", "corroborated", "not_in_model",
                     "contradictions", "proposals")})
        for i, p in enumerate(out["proposals"]):
            p["id"] = "prop-%d" % i
            p["status"] = "pending"
    except Exception as e:
        out["error"] = "%s: %s" % (type(e).__name__, e)
    return out


# ------------------------------------------------------------------ applying
def apply_proposal(p, actor="analyst"):
    """Execute an accepted proposal against the structured graph, stamped with
    provenance so it is distinguishable from baseline content forever after."""
    import datetime
    stamp = {"provenance": "evidence_accepted",
             "accepted_by": actor,
             "accepted_at": datetime.datetime.utcnow().isoformat() + "Z",
             "evidence": p.get("evidence"),
             "confidence": p.get("confidence"),
             "proposal_title": p.get("title")}
    op = p.get("op")
    if op == "add_vertex":
        doc = dict(p.get("doc") or {}); doc.update(stamp)
        new = arango.create_vertex(p["collection"], doc)
        return {"undo": {"op": "delete_vertex", "collection": p["collection"],
                         "key": new["_key"]}, "result": new}
    if op == "add_edge":
        attrs = dict(p.get("attrs") or {}); attrs.update(stamp)
        new = arango.create_edge(p["collection"], p["from"], p["to"], attrs)
        return {"undo": {"op": "delete_edge", "collection": p["collection"],
                         "key": new["_key"]}, "result": new}
    if op == "update_vertex":
        patch = dict(p.get("patch") or {}); patch.update(stamp)
        res = arango.update_vertex(p["collection"], p["key"], patch)
        prev = res.get("old") or {}
        # revert EVERY key we touched - the proposal's own fields and the
        # provenance stamp - restoring prior values, or null to delete keys
        # that did not exist before (undo uses keep_null=False).
        revert = {k: prev.get(k) for k in patch}
        return {"undo": {"op": "update_vertex", "collection": p["collection"],
                         "key": p["key"], "patch": revert}, "result": res.get("new")}
    if op == "flag_discrepancy":
        doc = {"node_type": "Discrepancy", "target": p.get("target"),
               "field": p.get("field"), "model_value": p.get("model_value"),
               "claimed_value": p.get("claimed_value"),
               "rationale": p.get("rationale")}
        doc.update(stamp)
        rows, _ = arango.aql(
            "INSERT @d INTO Discrepancy OPTIONS {ignoreErrors:false} RETURN NEW",
            {"d": doc})
        new = rows[0] if rows else {}
        return {"undo": {"op": "delete_doc", "collection": "Discrepancy",
                         "key": new.get("_key")}, "result": new}
    raise ValueError("unknown op: %r" % op)


def undo(u):
    op = u.get("op")
    if op == "delete_vertex":
        return arango.delete_vertex(u["collection"], u["key"])
    if op == "delete_edge":
        return arango.delete_edge(u["collection"], u["key"])
    if op == "update_vertex":
        arango.update_vertex(u["collection"], u["key"], u["patch"], keep_null=False)
        return True
    if op == "delete_doc":
        arango.aql("REMOVE @k IN @@c", {"k": u["key"], "@c": u["collection"]})
        return True
    raise ValueError("unknown undo op: %r" % op)


GRAPH_NAME = os.environ.get("STRUCTURED_GRAPH_NAME", "aircraft_model")


def _anchor(p, undo=None):
    """The node to centre the graph view on, as a full _id.

    update_vertex and flag_discrepancy name their target outright. add_vertex and
    add_edge only learn their key at write time, so the key is recovered from the
    undo token stored when the change was applied.
    """
    op = p.get("op")
    if op == "update_vertex" and p.get("collection") and p.get("key"):
        return "%s/%s" % (p["collection"], p["key"])
    if op == "flag_discrepancy" and p.get("target"):
        return p["target"]
    if op == "add_vertex" and undo and undo.get("key"):
        return "%s/%s" % (undo.get("collection") or p.get("collection"), undo["key"])
    if op == "add_edge":
        # an edge is not a viewable centre; anchor on the node it starts from
        return p.get("from")
    return None


def change_context(p, undo=None):
    """Everything the UI needs to show an accepted change inside ArangoDB:
    which collections it touched, a deep link to the graph viewer, and an AQL
    query that works whether or not the deep link opens."""
    op = p.get("op")
    written = []
    if op == "add_vertex":
        written.append({"name": p.get("collection"), "role": "node added"})
    elif op == "update_vertex":
        written.append({"name": p.get("collection"), "role": "node updated"})
    elif op == "add_edge":
        written.append({"name": p.get("collection"), "role": "edge added"})
        for side, lbl in (("from", "edge start"), ("to", "edge end")):
            v = p.get(side) or ""
            if "/" in v:
                written.append({"name": v.split("/", 1)[0], "role": lbl})
    elif op == "flag_discrepancy":
        t = p.get("target") or ""
        if "/" in t:
            written.append({"name": t.split("/", 1)[0], "role": "flagged, not modified"})

    anchor = _anchor(p, undo)
    nearby = []
    if anchor:
        try:
            rows, _ = arango.aql(
                "FOR v IN 1..2 ANY @s GRAPH @g "
                "  COLLECT c = PARSE_IDENTIFIER(v._id).collection WITH COUNT INTO n "
                "  SORT n DESC RETURN {name: c, count: n}",
                {"s": anchor, "g": GRAPH_NAME})
            nearby = rows
        except Exception:                              # noqa: BLE001
            nearby = []

    src = p.get("evidence") or "the source document"
    aql = None
    if anchor:
        aql = (
            "/* %s\n"
            "   source document: %s\n"
            "   the change is on %s, tagged provenance='evidence_accepted' */\n"
            "FOR v, e, path IN 0..2 ANY '%s' GRAPH '%s'\n"
            "  RETURN path"
            % (p.get("title") or op, src, anchor, anchor, GRAPH_NAME))

    # ArangoDB's graph viewer takes its graph name from the raw URL string
    # (href.substring(href.lastIndexOf("/")+1)), so appending ?nodeStart=... makes
    # the name include the query string and the view errors with "graph not found".
    # There is no supported way to deep-link a start node. So: link to routes that
    # do work, and let the pasted query carry the neighbourhood.
    node_url = graph_url = None
    if anchor and "/" in anchor:
        from urllib.parse import quote
        coll, key = anchor.split("/", 1)
        stem = "%s/_db/%s/_admin/aardvark/index.html" % (
            arango.URL, quote(arango.STRUCTURED_DB, safe=""))
        node_url = "%s#collection/%s/%s" % (stem, quote(coll, safe=""),
                                            quote(key, safe=""))
        graph_url = "%s#graphs-v2/%s" % (stem, quote(GRAPH_NAME, safe=""))

    return {"anchor": anchor, "graph": GRAPH_NAME, "written": written,
            "nearby": nearby, "aql": aql, "node_url": node_url,
            "graph_url": graph_url, "evidence": p.get("evidence")}


def focus_graph_view(anchor, depth=2, limit=250):
    """Point ArangoDB's graph viewer at one node before opening it.

    The viewer takes its graph name from the raw URL string, so query params
    cannot carry a start node. It does persist its settings per user, under
    /_api/user/{user}/config/graphs-v2 keyed by "{db}_{graph}", and reloads them
    on mount. Writing nodeStart there is the supported-in-practice way to open
    the viewer on a chosen node.

    This applies to the ArangoDB user MICA connects as. An analyst signed into
    the web UI as a different user keeps their own settings and will not see it.
    """
    import requests
    key = "%s_%s" % (arango.STRUCTURED_DB, GRAPH_NAME)
    base = "%s/_db/%s/_api/user/%s/config" % (
        arango.URL, arango.STRUCTURED_DB, arango.USER)

    cur = {}
    try:
        r = requests.get(base, auth=(arango.USER, arango.PW), timeout=20)
        cur = ((r.json() or {}).get("result") or {}).get("graphs-v2") or {}
    except Exception:                                  # noqa: BLE001
        cur = {}

    # keep whatever the analyst already chose for colours, layout and labels
    settings = dict(cur.get(key) or {})
    settings.update({"nodeStart": anchor, "depth": depth, "limit": limit,
                     "mode": ""})
    settings.setdefault("layout", "forceAtlas2")
    settings.setdefault("nodeLabelByCollection", True)
    settings.setdefault("edgeLabelByCollection", True)

    merged = {k: dict(v) if isinstance(v, dict) else v for k, v in cur.items()}
    merged[key] = settings

    # the config lives on the _users document, so two writes in quick succession
    # collide on its revision. Retry rather than fail the click.
    import time
    last = None
    for attempt in range(5):
        r = requests.put(base + "/graphs-v2", json={"value": merged},
                         auth=(arango.USER, arango.PW), timeout=20)
        last = r
        if r.status_code < 400:
            # the _users collection replicates asynchronously, so the value is
            # not readable for ~0.5s. Wait for it: the browser tab opens straight
            # after this returns, and must not load the previous start node.
            confirmed = False
            for _ in range(8):
                time.sleep(0.25)
                try:
                    g = requests.get(base, auth=(arango.USER, arango.PW), timeout=20)
                    got = (((g.json() or {}).get("result") or {})
                           .get("graphs-v2") or {}).get(key, {}).get("nodeStart")
                    if got == anchor:
                        confirmed = True
                        break
                except Exception:                      # noqa: BLE001
                    pass
            return {"focused": anchor, "graph": GRAPH_NAME, "user": arango.USER,
                    "depth": depth, "limit": limit, "confirmed": confirmed}
        if r.status_code != 409:
            break
        time.sleep(0.4)
    last.raise_for_status()


def _doc_url(coll, key):
    from urllib.parse import quote
    return ("%s/_db/%s/_admin/aardvark/index.html#collection/%s/%s"
            % (arango.URL, quote(arango.STRUCTURED_DB, safe=""),
               quote(coll, safe=""), quote(key, safe="")))


def target_link(p):
    """Which nodes a proposal will touch, resolvable BEFORE it is applied.

    Pure string work, no database round trip, so the pending list can render a
    link on every card without a fetch per row.
    """
    from urllib.parse import quote
    op = p.get("op")
    out = []
    if op == "update_vertex" and p.get("collection") and p.get("key"):
        out.append({"id": "%s/%s" % (p["collection"], p["key"]),
                    "url": _doc_url(p["collection"], p["key"]),
                    "role": "this node gets the change"})
    elif op == "flag_discrepancy" and p.get("target") and "/" in str(p["target"]):
        coll, key = str(p["target"]).split("/", 1)
        out.append({"id": p["target"], "url": _doc_url(coll, key),
                    "role": "flagged for review, not modified"})
    elif op == "add_edge":
        for side, role in (("from", "new edge starts here"),
                           ("to", "new edge ends here")):
            v = str(p.get(side) or "")
            if "/" in v:
                coll, key = v.split("/", 1)
                out.append({"id": v, "url": _doc_url(coll, key), "role": role})
    elif op == "add_vertex" and p.get("collection"):
        # nothing to point at yet, so point at where it will appear
        out.append({"id": p["collection"],
                    "url": ("%s/_db/%s/_admin/aardvark/index.html"
                            "#collection/%s/documents/1"
                            % (arango.URL, quote(arango.STRUCTURED_DB, safe=""),
                               quote(p["collection"], safe=""))),
                    "role": "a new node lands in this collection"})
    return out


AUTOGRAPH_DB = os.environ.get("AUTOGRAPH_DB", "Aircraft KG Strong")
CORPUS_PREFIX = os.environ.get("CORPUS_PREFIX", "Aircraft-corpus")   # set by profiles.apply()


_CHUNK_CACHE = {"rows": None}


def _norm(t):
    import re
    return re.sub(r"\s+", " ", t or "").strip().lower()


def _corpus_chunks(refresh=False):
    """Every chunk, held in memory. The corpus is 184 chunks, so matching in
    Python beats trying to express whitespace-insensitive matching in AQL."""
    if _CHUNK_CACHE["rows"] is None or refresh:
        rows, _ = arango.aql(
            "FOR c IN @@c RETURN {key: c._key, order: c.chunk_order_index, "
            "tokens: c.tokens, content: c.content}",
            {"@c": "%s_Chunks" % CORPUS_PREFIX}, db=AUTOGRAPH_DB)
        for r in rows:
            r["_norm"] = _norm(r.get("content"))
        _CHUNK_CACHE["rows"] = rows
    return _CHUNK_CACHE["rows"]


def source_document(filename):
    """The source document behind a proposal, plus the corpus chunks it produced.

    `_sources` holds the whole document keyed by filename, so the real document
    is available rather than only a chunk. Chunks are tied back by comparing
    whitespace-normalised text: `_Documents.file_name` disagrees with its own
    content on most rows, so it cannot be trusted to make the link.
    """
    from urllib.parse import quote
    if not filename:
        return {"error": "this proposal names no source document"}

    pfx = CORPUS_PREFIX
    try:
        rows, _ = arango.aql(
            "FOR d IN @@c FILTER d.filename == @f LIMIT 1 "
            "RETURN {key: d._key, content: d.content, url: d.citable_url}",
            {"@c": "%s_sources" % pfx, "f": filename}, db=AUTOGRAPH_DB)
    except Exception as e:                             # noqa: BLE001
        return {"error": "%s: %s" % (type(e).__name__, e)}
    if not rows:
        return {"error": "no document named %s in the corpus" % filename}

    src = rows[0]
    body = src.get("content") or ""
    nd = _norm(body)

    matched = []
    try:
        for c in _corpus_chunks():
            nc = c.get("_norm") or ""
            if len(nc) < 40:
                continue
            probe = nc[len(nc) // 4: len(nc) // 4 + 80]
            if (probe and probe in nd) or nc[:80] in nd:
                matched.append({k: c[k] for k in
                                ("key", "order", "tokens", "content")})
    except Exception:                                  # noqa: BLE001
        matched = []
    matched.sort(key=lambda c: c.get("order") or 0)

    stem = ("%s/_db/%s/_admin/aardvark/index.html#collection/"
            % (arango.URL, quote(AUTOGRAPH_DB, safe="")))
    for c in matched:
        c["url"] = stem + "%s_Chunks/%s" % (quote(pfx, safe=""),
                                            quote(c["key"], safe=""))
    return {"filename": filename, "content": body, "chars": len(body),
            "source_key": src["key"], "citable_url": src.get("url") or None,
            "source_url": stem + "%s_sources/%s" % (quote(pfx, safe=""),
                                                    quote(src["key"], safe="")),
            "corpus_db": AUTOGRAPH_DB, "chunks": matched}

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""THE CORPUS-UPDATE AGENT — a triggered, stateful agent that keeps the model
from going stale without anyone having to think to ask.

Unlike agents 1-3 (request/response), this one runs on a trigger, holds state
between runs, and decides for itself what matters. That is what actually
justifies an orchestration framework here.

    detect ──→ read ──→ assess ──→ queue
      │                              │
      └── nothing new ───────────────┘

  detect  which source documents the analyst has not reviewed yet
  read    pull their text  (from `_sources`, NOT `_Documents` — see note below)
  assess  compare their claims against the model's CURRENT state for the platform
  queue   persist proposals for analyst review; mark the documents reviewed

State lives in two collections in the structured database, so a run is durable
and repeatable rather than a one-shot script:
    ReviewedDocument  - watermark: which corpus docs have been processed
    UpdateProposal    - the review queue: proposals awaiting an analyst

NOTE on the corpus: `Aircraft-corpus_Documents` was found to have filename/content
misalignment on ~157 of 180 rows. `Aircraft-corpus_sources` is correct, so all
reads here go through that collection.
"""

import json
import os
import re
from typing import Annotated, Any, Optional, TypedDict
import operator

from langgraph.graph import END, START, StateGraph
import llm

import arango_client as arango
import overview as ov
from pathlib import Path

SCHEMA = (Path(__file__).parent / "data" / "schema.md").read_text(encoding="utf-8")

# the ONLY collections a proposal may target - anything else is rejected
VALID_VERTEX = {"Platform", "Performance", "Signature", "Airframe", "Program",
                "Propulsion", "Sensors", "ElectronicWarfare", "Armament",
                "Operators", "Engine", "RadarSystem", "Weapon",
                "DefensiveSystem", "Country"}
VALID_EDGE = {"has_performance", "has_signature", "has_airframe", "has_program",
              "has_propulsion", "has_sensors", "has_electronic_warfare",
              "has_armament", "has_operators", "equipped_engine",
              "equipped_radar", "equipped_defensive_system", "carries_weapon",
              "operated_by"}

AUTOGRAPH_DB = os.environ.get("AUTOGRAPH_DB", "Aircraft KG Strong")
MODEL = os.environ.get("UPDATE_AGENT_MODEL", "claude-opus-5")
BATCH = int(os.environ.get("UPDATE_AGENT_BATCH", "6"))

REVIEWED = "ReviewedDocument"
QUEUE = "UpdateProposal"


def ensure_collections():
    import requests
    for name in (REVIEWED, QUEUE):
        requests.post("%s/_db/%s/_api/collection" % (arango.URL, arango.STRUCTURED_DB),
                      json={"name": name}, auth=(arango.USER, arango.PW), timeout=30)


def _prefix():
    return ov._corpus_prefix() or ov.CORPUS_PREFIX or "Aircraft-corpus"


# ------------------------------------------------------------------ state
class UpdateState(TypedDict, total=False):
    platform: Optional[str]
    limit: int
    candidates: list      # [{filename, content}]
    model_context: str
    proposals: list
    reviewed: list
    log: Annotated[list, operator.add]


# ------------------------------------------------------------------ nodes
def node_detect(state: UpdateState) -> dict:
    """Source documents the analyst has not reviewed yet."""
    p = _prefix()
    seen, _ = arango.aql("FOR d IN @@c RETURN d.filename", {"@c": REVIEWED})
    seen = set(seen)
    rows, _ = arango.aql(
        "FOR d IN @@c FILTER d.filename != null "
        "RETURN {filename: d.filename, content: d.content}",
        {"@c": "%s_sources" % p}, db=AUTOGRAPH_DB)

    plat = (state.get("platform") or "").strip()
    cands = []
    for r in rows:
        fn = r["filename"]
        if fn in seen or fn.endswith(".xlsx"):
            continue
        if plat and plat.lower() not in (r.get("content") or "").lower():
            continue
        cands.append({"filename": fn, "content": (r.get("content") or "")[:4000]})
    cands = cands[: state.get("limit") or BATCH]
    return {"candidates": cands,
            "log": ["detect: %d unreviewed source document(s) selected%s"
                    % (len(cands), " for %s" % plat if plat else "")]}


def node_read_model(state: UpdateState) -> dict:
    """What the model currently holds for this platform - the comparison baseline."""
    plat = state.get("platform")
    if not plat:
        return {"model_context": "(no platform selected)", "log": ["model: skipped"]}
    rows, _ = arango.aql(
        "FOR p IN Platform FILTER p.name == @n LIMIT 1 RETURN p._key", {"n": plat})
    if not rows:
        return {"model_context": "(platform not in model)", "log": ["model: not found"]}
    pkey = rows[0]
    summary = ov.platform_summary(pkey)

    # the agent cannot guess _key values - hand it the real ones, or every
    # update_vertex proposal targets a document that does not exist
    keys, _ = arango.aql("""
      LET cats = (FOR c IN ["Platform","Performance","Signature","Airframe",
                            "Program","Propulsion","Sensors","ElectronicWarfare",
                            "Armament","Operators"] RETURN {collection: c, key: @k})
      LET wpn = (FOR a IN Armament FILTER a._key == @k
                   FOR w IN 1..1 OUTBOUND a carries_weapon
                     RETURN {collection: "Weapon", key: w._key, name: w.name})
      LET ds  = (FOR e IN ElectronicWarfare FILTER e._key == @k
                   FOR d IN 1..1 OUTBOUND e equipped_defensive_system
                     RETURN {collection: "DefensiveSystem", key: d._key, name: d.name})
      LET eng = (FOR p IN Propulsion FILTER p._key == @k
                   FOR e IN 1..1 OUTBOUND p equipped_engine
                     RETURN {collection: "Engine", key: e._key, name: e.name})
      LET rad = (FOR s IN Sensors FILTER s._key == @k
                   FOR r IN 1..1 OUTBOUND s equipped_radar
                     RETURN {collection: "RadarSystem", key: r._key, name: r.name})
      RETURN APPEND(APPEND(APPEND(APPEND(cats, wpn), ds), eng), rad)
    """, {"k": pkey})
    keymap = keys[0] if keys else []

    ctx = ("EXACT NODE KEYS for this platform - use these verbatim in any "
           "update_vertex proposal, never invent a key:\n%s\n\n"
           "CURRENT STATE:\n%s"
           % (json.dumps(keymap, indent=1), json.dumps(summary, indent=1, default=str)[:6000]))
    return {"model_context": ctx,
            "log": ["model: loaded state + %d node keys for %s" % (len(keymap), plat)]}


SYSTEM = """You maintain a structured capability model against incoming source documents.

You are given (A) the model's CURRENT state for one platform and (B) source documents
the analyst has not reviewed yet. Decide what, if anything, the model is missing.

Rules:
- Propose an edit ONLY where a document states something the model does not hold.
  If a document merely restates what the model already has, that is corroboration,
  not an edit. Zero proposals is a good answer.
- Carry the source's own hedging. "alleged", "unconfirmed", "not corroborated",
  "reliability not established" => confidence low, and say so in the rationale.
- Prefer ADDING a node/edge or an annotation over overwriting a baseline value.
  Never propose overwriting an established figure on a single hedged source.
- Entity nodes are SHARED across platforms. A platform-specific finding must not
  rewrite a shared node's description; annotate instead.
- Group related findings: one proposal per real-world change, not one per sentence.

The model's schema is given below. You MUST use only the collections it defines.
If a finding has no home in the schema (for example a sub-component of an engine,
which the model has no collection for), do NOT invent a collection. Either attach
it as an annotation attribute on the nearest existing node via update_vertex, or
use flag_discrepancy. Inventing a collection produces an unusable proposal.

Operations available:
  {"op":"add_vertex","collection":"...","doc":{...}}
  {"op":"add_edge","collection":"...","from":"Coll/key","to":"Coll/key","attrs":{...}}
  {"op":"update_vertex","collection":"...","key":"...","patch":{...}}
  {"op":"flag_discrepancy","target":"Coll/key","field":"...","model_value":...,"claimed_value":...}

Respond with ONLY JSON:
{"assessment":"2-3 sentences on what these documents change, if anything",
 "proposals":[{"op":"...", "...":"...", "title":"short label",
               "rationale":"why this follows from the document",
               "evidence":"the source filename",
               "confidence":"high|medium|low"}]}

Write the prose fields (summary, assessment, rationale) for a working analyst:
- Active voice. Name who or what does the thing.
- No em dashes. No adverbs. No 'not X but Y' constructions.
- State findings directly. Skip throat-clearing and pull-quote endings.
- Vary sentence length. Be specific about which document said what."""


def _extract_json(text):
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    s, e = text.find("{"), text.rfind("}")
    return json.loads(text[s:e + 1] if s >= 0 and e > s else text)


def node_assess(state: UpdateState) -> dict:
    cands = state.get("candidates") or []
    if not cands:
        return {"proposals": [], "log": ["assess: nothing to do"]}
    docs = "\n\n".join("--- %s ---\n%s" % (c["filename"], c["content"])
                       for c in cands)
    msg = ("SCHEMA (the only collections that exist)\n%s\n\n"
           "(A) CURRENT MODEL STATE\n%s\n\n(B) UNRECONCILED SOURCE DOCUMENTS\n%s"
           % (SCHEMA, state.get("model_context", "(none)"), docs))
    try:
        data = llm.complete_json(SYSTEM, msg, model=MODEL, max_tokens=16000)
        props, rejected = [], []
        for p in data.get("proposals", []) or []:
            coll, op = p.get("collection"), p.get("op")
            ok = (op == "flag_discrepancy"
                  or (op == "add_edge" and coll in VALID_EDGE)
                  or (op in ("add_vertex", "update_vertex") and coll in VALID_VERTEX))
            (props if ok else rejected).append(p)
        for i, p in enumerate(props):
            p["id"] = "upd-%d" % i
            p["status"] = "pending"
        if rejected:
            state.setdefault("log", [])
        return {"proposals": props,
                "log": ["assess: %s" % data.get("assessment", "")[:180],
                        "assess: %d proposal(s) valid%s" % (
                            len(props),
                            ", %d rejected (targeted a collection outside the schema: %s)"
                            % (len(rejected), ", ".join(sorted({str(r.get("collection"))
                                                               for r in rejected})))
                            if rejected else "")]}
    except Exception as e:
        return {"proposals": [], "log": ["assess FAILED: %s: %s" % (type(e).__name__, e)]}


def node_queue(state: UpdateState) -> dict:
    """Persist proposals for review and watermark the documents as reviewed."""
    import datetime
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    batch = [c["filename"] for c in state.get("candidates") or []]
    stored = []
    for p in state.get("proposals") or []:
        doc = dict(p)
        # the agent names its source in `evidence` as free text. Resolve that
        # back to real filenames so a document knows which proposals it produced.
        ev = str(p.get("evidence") or "").lower()
        hits = [f for f in batch
                if f.lower() in ev or f.rsplit(".", 1)[0].lower() in ev]
        doc.update({"queued_at": now, "status": "pending",
                    "platform": state.get("platform"),
                    "source_files": hits or batch})
        rows, _ = arango.aql("INSERT @d INTO @@c RETURN NEW",
                             {"d": doc, "@c": QUEUE})
        if rows:
            stored.append(rows[0])
    reviewed = []
    for c in state.get("candidates") or []:
        arango.aql(
            "UPSERT {filename: @f} INSERT {filename: @f, reviewed_at: @t} "
            "UPDATE {reviewed_at: @t} IN @@c",
            {"f": c["filename"], "t": now, "@c": REVIEWED})
        reviewed.append(c["filename"])
    return {"proposals": stored, "reviewed": reviewed,
            "log": ["queue: %d proposal(s) queued, %d document(s) marked reviewed"
                    % (len(stored), len(reviewed))]}


def route_after_detect(state: UpdateState) -> str:
    """Real conditional branching: skip the LLM entirely when nothing is new."""
    return "read_model" if state.get("candidates") else "done"


def build_graph():
    g = StateGraph(UpdateState)
    g.add_node("detect", node_detect)
    g.add_node("read_model", node_read_model)
    g.add_node("assess", node_assess)
    g.add_node("queue", node_queue)
    g.add_edge(START, "detect")
    g.add_conditional_edges("detect", route_after_detect,
                            {"read_model": "read_model", "done": END})
    g.add_edge("read_model", "assess")
    g.add_edge("assess", "queue")
    g.add_edge("queue", END)
    return g.compile()


GRAPH = build_graph()


def run(platform: Optional[str] = None, limit: int = BATCH) -> dict:
    ensure_collections()
    final = GRAPH.invoke({"platform": platform, "limit": limit, "log": []})
    return {"platform": platform,
            "checked": [c["filename"] for c in final.get("candidates") or []],
            "proposals": final.get("proposals") or [],
            "reviewed": final.get("reviewed") or [],
            "log": final.get("log") or []}


def pending():
    rows, _ = arango.aql(
        "FOR d IN @@c FILTER d.status == 'pending' SORT d.queued_at DESC RETURN d",
        {"@c": QUEUE})
    return rows


def set_status(key, status):
    arango.aql("UPDATE @k WITH {status: @s} IN @@c",
               {"k": key, "s": status, "@c": QUEUE})


def backlog(platform: Optional[str] = None):
    """Where every source document stands in the review cycle.

    A document is `not_reviewed` until the agent has read it. Once read, its
    outcome follows what the analyst did with the proposals it produced:
    accepted, rejected, still awaiting review, or no change proposed at all
    (the document corroborated what the model already held).
    """
    p = _prefix()
    seen, _ = arango.aql("FOR d IN @@c RETURN d.filename", {"@c": REVIEWED})
    seen = set(seen)

    # every queued proposal, with the documents it came from and its disposition
    props, _ = arango.aql(
        "FOR d IN @@c RETURN {files: d.source_files, evidence: d.evidence, "
        "status: d.status}", {"@c": QUEUE})
    by_file = {}
    for pr in props:
        # `evidence` is usually the bare filename; `source_files` is the resolved
        # list added at queue time. Take both so older rows still attribute.
        files = set(pr.get("files") or [])
        ev = str(pr.get("evidence") or "").strip()
        if ev.endswith((".pdf", ".txt")):
            files.add(ev)
        for f in files:
            by_file.setdefault(f, []).append(pr.get("status") or "pending")

    rows, _ = arango.aql(
        "FOR d IN @@c FILTER d.filename != null RETURN {f: d.filename, c: d.content}",
        {"@c": "%s_sources" % p}, db=AUTOGRAPH_DB)

    plat = (platform or "").strip().lower()
    total = 0
    b = {"not_reviewed": 0, "accepted": 0, "rejected": 0,
         "awaiting_review": 0, "no_change": 0}
    for r in rows:
        if (r["f"] or "").endswith(".xlsx"):
            continue
        if plat and plat not in (r.get("c") or "").lower():
            continue
        total += 1
        if r["f"] not in seen:
            b["not_reviewed"] += 1
            continue
        st = by_file.get(r["f"])
        if not st:
            b["no_change"] += 1
        elif "accepted" in st:
            b["accepted"] += 1
        elif "pending" in st:
            b["awaiting_review"] += 1
        else:
            b["rejected"] += 1

    b["total"] = total
    b["reviewed"] = total - b["not_reviewed"]

    # Proposals are a SEPARATE population from documents: one document can
    # produce several changes, or none. The tab badge counts these, so the UI
    # has to show them under their own heading or the two sets of numbers
    # look like they should reconcile and do not.
    q = ("FOR d IN @@c %s COLLECT st = d.status WITH COUNT INTO n "
         "RETURN {st, n}") % ("FILTER d.platform == @p" if platform else "")
    binds = {"@c": QUEUE}
    if platform:
        binds["p"] = platform
    rows, _ = arango.aql(q, binds)
    counts = {r["st"] or "pending": r["n"] for r in rows}
    b["proposals"] = {"pending": counts.get("pending", 0),
                      "accepted": counts.get("accepted", 0),
                      "rejected": counts.get("denied", 0) + counts.get("rejected", 0)}
    return b


def document_states():
    """Every source document, with the platform it names and its review state.

    Built from the documents themselves. Nothing here counts extracted entities.
    """
    pfx = _prefix()
    seen, _ = arango.aql("FOR d IN @@c RETURN d.filename", {"@c": REVIEWED})
    seen = set(seen)

    props, _ = arango.aql(
        "FOR d IN @@c RETURN {files: d.source_files, evidence: d.evidence, "
        "status: d.status}", {"@c": QUEUE})
    by_file = {}
    for pr in props:
        files = set(pr.get("files") or [])
        ev = str(pr.get("evidence") or "").strip()
        if ev.endswith((".pdf", ".txt")):
            files.add(ev)
        for f in files:
            by_file.setdefault(f, []).append(pr.get("status") or "pending")

    names = [r["name"] for r in arango.platforms()]
    rows, _ = arango.aql(
        "FOR d IN @@c FILTER d.filename != null RETURN {f: d.filename, c: d.content}",
        {"@c": "%s_sources" % pfx}, db=AUTOGRAPH_DB)

    docs = []
    for r in rows:
        fn = r["f"] or ""
        if fn.endswith(".xlsx"):
            continue
        body = (r.get("c") or "").lower()
        plat = next((n for n in names if n.lower() in body), None)
        if fn not in seen:
            state = "not_reviewed"
        else:
            st = by_file.get(fn)
            if not st:
                state = "no_change"
            elif "accepted" in st:
                state = "accepted"
            elif "pending" in st:
                state = "awaiting_review"
            else:
                state = "rejected"
        docs.append({"filename": fn, "platform": plat,
                     "format": fn.rsplit(".", 1)[-1].lower() if "." in fn else "",
                     "state": state})
    docs.sort(key=lambda d: (d["platform"] or "zzz", d["filename"]))
    return {"documents": docs, "total": len(docs)}


def mermaid():
    return GRAPH.get_graph().draw_mermaid()


if __name__ == "__main__":
    import sys
    if "--diagram" in sys.argv:
        print(mermaid()); sys.exit()
    plat = sys.argv[1] if len(sys.argv) > 1 else "Su-35S Flanker-E"
    ensure_collections()
    print("backlog:", backlog(plat))
    r = run(plat, limit=3)
    for line in r["log"]:
        print("  ", line)
    print("proposals:", len(r["proposals"]))
    for p in r["proposals"]:
        print("   *", p.get("title"), "|", p.get("op"), "|", p.get("confidence"))

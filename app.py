#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Analyst UI + three-agent orchestration.

    agent 1  structured   NL -> AQL -> aircraft_model graph        (official model)
    agent 2  unstructured same question -> GraphRAG retriever      (source documents)
    agent 3  reconciler   both -> official / corroborated / new + proposed edits

Run:
    uvicorn app:app --reload --port 8000
"""

import sys
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from typing import Optional
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).parent))
import os

import aql_backends
import arango_client as arango
import profiles
import llm
import overview as ov
import scheduler
import schema_live
import update_agent
from agents import aql_agent, autograph_agent, reconciler

# LangGraph orchestration is on by default; USE_LANGGRAPH=false falls back to the
# direct sequential path below, so a framework problem can never block the demo.
USE_LANGGRAPH = os.environ.get("USE_LANGGRAPH", "true").lower() != "false"
graph_flow = None
if USE_LANGGRAPH:
    try:
        import graph_flow
    except Exception as _e:          # noqa: F841 - fall back, don't crash
        USE_LANGGRAPH = False

HERE = Path(__file__).parent
app = FastAPI(title="MICA - Model Interrogation & Change Analysis")

# undo tokens for accepted proposals, keyed by proposal id (in-memory: demo scope)
UNDO = {}


class Ask(BaseModel):
    platform: Optional[str] = None
    question: str
    # "builtin" (fast, our own prompt) or "aqlizer" (slower, ArangoDB's service).
    # Omitted means whatever AQL_BACKEND says, so the UI can offer the choice
    # per question without changing the server's default.
    aql_backend: Optional[str] = None
    # which GraphRAG retrieval strategy agent 2 uses: deep | local | unified | global
    corpus_strategy: Optional[str] = None


class Accept(BaseModel):
    proposal: dict


class Undo(BaseModel):
    proposal_id: str


class ChangeCtx(BaseModel):
    proposal: Optional[dict] = None
    key: Optional[str] = None


class Focus(BaseModel):
    anchor: str
    depth: int = 2


class ProfileIn(BaseModel):
    key: str


class AutoRun(BaseModel):
    enabled: Optional[bool] = None
    interval_hours: Optional[int] = None
    auto_accept: Optional[dict] = None


@app.get("/api/health")
def health():
    return {"arango": arango.ping(),
            "host": arango.host(),
            "profile": profiles.describe(profiles.active()),
            "orchestrator": "langgraph" if (USE_LANGGRAPH and graph_flow) else "direct",
            "llm_backend": llm.backend(),
            "aql_backend": aql_backends.DEFAULT,
            "corpus_strategy": autograph_agent.DEFAULT_STRATEGY,
            "corpus_strategies": [{"key": k, "label": v["label"], "note": v["note"]}
                                  for k, v in autograph_agent.STRATEGIES.items()],
            "aql_backends_available": [b for b in aql_backends.BACKENDS
                                       if b == "builtin" or aql_backends.configured()],
            "models": {"structured": aql_agent.MODEL,
                       "reconciler": reconciler.MODEL,
                       "update_agent": update_agent.MODEL},
            "structured_db": arango.STRUCTURED_DB,
            "autograph_configured": bool(autograph_agent.service_url()),
            "autograph_db": autograph_agent.AUTOGRAPH_DB}


@app.get("/api/profile")
def profile_get():
    """Which deployment MICA is on, and what the other one looks like."""
    return {"active": profiles.active(),
            "profiles": [profiles.describe(k) for k in profiles.PROFILES]}


@app.get("/api/profile/test")
def profile_test():
    """Reachability of every agent's target on every profile. Read-only; does
    not switch, so the modal can show both deployments before you choose."""
    return {k: profiles.test(k) for k in profiles.PROFILES}


@app.put("/api/profile")
def profile_put(body: ProfileIn):
    """Move every agent to the other deployment, then re-read the schema."""
    try:
        return profiles.apply(body.key)
    except Exception as e:                             # noqa: BLE001
        raise HTTPException(400, "%s: %s" % (type(e).__name__, e))


@app.get("/api/platforms")
def get_platforms():
    return arango.platforms()


@app.post("/api/ask")
def ask(body: Ask):
    if not body.question.strip():
        raise HTTPException(400, "question is required")
    if USE_LANGGRAPH and graph_flow is not None:
        out = graph_flow.run(body.question, body.platform, body.aql_backend,
                             body.corpus_strategy)
        out["orchestrator"] = "langgraph"
        return out
    # direct fallback: same three agents, run sequentially
    structured = aql_agent.run(body.question, body.platform, backend=body.aql_backend)
    unstructured = autograph_agent.run(body.question, body.platform,
                                       strategy=body.corpus_strategy)
    recon = reconciler.run(body.question, structured, unstructured, body.platform)
    return {"structured": structured, "unstructured": unstructured,
            "reconciliation": recon, "orchestrator": "direct"}


@app.post("/api/proposal/accept")
def accept(body: Accept):
    p = body.proposal
    try:
        res = reconciler.apply_proposal(p)
    except Exception as e:
        raise HTTPException(400, "%s: %s" % (type(e).__name__, e))
    pid = p.get("id") or "prop"
    UNDO[pid] = res["undo"]
    schema_live.refresh_async()
    return {"status": "accepted", "proposal_id": pid,
            "result": res["result"], "undoable": True}


@app.post("/api/proposal/undo")
def undo_proposal(body: Undo):
    u = UNDO.get(body.proposal_id)
    if not u:
        raise HTTPException(404, "nothing to undo for %s" % body.proposal_id)
    try:
        reconciler.undo(u)
    except Exception as e:
        raise HTTPException(400, "%s: %s" % (type(e).__name__, e))
    UNDO.pop(body.proposal_id, None)
    schema_live.refresh_async()
    return {"status": "reverted", "proposal_id": body.proposal_id}


class UpdateCheck(BaseModel):
    platform: Optional[str] = None
    limit: int = 6


class QueueAction(BaseModel):
    key: str


@app.get("/api/updates/backlog")
def updates_backlog(platform: Optional[str] = None):
    return update_agent.backlog(platform)


@app.post("/api/updates/check")
def updates_check(body: UpdateCheck):
    """Run the change-detection agent over source documents nobody has reviewed."""
    return update_agent.run(body.platform, body.limit)


@app.get("/api/updates/pending")
def updates_pending():
    """Each row carries where it will land, so the card can link the node
    before anyone accepts it."""
    rows = update_agent.pending()
    for r in rows:
        r["targets"] = reconciler.target_link(r)
    return rows


@app.get("/api/source")
def source(filename: str):
    """The corpus document behind a proposal, with the chunks it produced."""
    return reconciler.source_document(filename)


@app.post("/api/updates/accept")
def updates_accept(body: QueueAction):
    rows, _ = arango.aql("FOR d IN UpdateProposal FILTER d._key == @k RETURN d",
                         {"k": body.key})
    if not rows:
        raise HTTPException(404, "no queued proposal %s" % body.key)
    # Applying an already-accepted proposal a second time captures the stamped
    # state as "previous", so a single revert would only roll back to the stamp
    # and leave the change behind. Refuse instead.
    if rows[0].get("status") == "accepted":
        raise HTTPException(409, "proposal %s is already accepted" % body.key)
    try:
        res = reconciler.apply_proposal(rows[0])
    except Exception as e:
        raise HTTPException(400, "%s: %s" % (type(e).__name__, e))
    arango.aql("UPDATE @k WITH {status: 'accepted', undo_token: @u, "
               "accepted_by: 'analyst'} IN UpdateProposal",
               {"k": body.key, "u": res.get("undo")})
    UNDO["queue:" + body.key] = res["undo"]
    schema_live.refresh_async()
    return {"status": "accepted", "key": body.key, "result": res["result"]}


@app.post("/api/updates/deny")
def updates_deny(body: QueueAction):
    update_agent.set_status(body.key, "denied")
    return {"status": "denied", "key": body.key}


@app.get("/api/autorun")
def autorun_get():
    return scheduler.get_config()


@app.post("/api/autorun")
def autorun_set(body: AutoRun):
    """Turn the unattended sweep on or off, and set how often it runs."""
    return scheduler.set_config(body.enabled, body.interval_hours,
                                body.auto_accept)


@app.post("/api/autorun/run-now")
def autorun_now():
    """Run the sweep immediately instead of waiting for the timer."""
    return scheduler.run_sweep("manual")


@app.get("/api/updates/accepted")
def updates_accepted():
    """Accepted changes that can still be rolled back, newest first."""
    rows, _ = arango.aql(
        "FOR d IN UpdateProposal FILTER d.status == 'accepted' "
        "SORT d.queued_at DESC LIMIT 60 "
        "RETURN MERGE(UNSET(d, 'undo_token'), "
        "  {revertable: d.undo_token != null})")
    for r in rows:
        r["targets"] = reconciler.target_link(r)
    return rows


@app.post("/api/change/focus")
def change_focus(body: Focus):
    """Point ArangoDB's graph viewer at one node, then hand back the URL."""
    try:
        return reconciler.focus_graph_view(body.anchor, depth=body.depth)
    except Exception as e:                             # noqa: BLE001
        raise HTTPException(400, "%s: %s" % (type(e).__name__, e))


@app.post("/api/change/revert")
def change_revert(body: QueueAction):
    """Roll back an accepted change, whether a person or the agent accepted it."""
    try:
        return scheduler.revert(body.key)
    except Exception as e:                             # noqa: BLE001
        raise HTTPException(400, "%s: %s" % (type(e).__name__, e))


@app.post("/api/autorun/revert")
def autorun_revert(body: QueueAction):
    """Kept so older links keep working; same behaviour as /api/change/revert."""
    try:
        return scheduler.revert(body.key)
    except Exception as e:                             # noqa: BLE001
        raise HTTPException(400, "%s: %s" % (type(e).__name__, e))


@app.post("/api/change/context")
def change_context(body: ChangeCtx):
    """Where an accepted change landed in the graph: which collections it wrote,
    a deep link into ArangoDB's graph viewer, and an AQL query to paste."""
    p, undo = body.proposal, None
    if body.key:
        rows, _ = arango.aql("FOR d IN UpdateProposal FILTER d._key == @k RETURN d",
                             {"k": body.key})
        if not rows:
            raise HTTPException(404, "no proposal %s" % body.key)
        p = rows[0]
        undo = p.get("undo_token")
    if not p:
        raise HTTPException(400, "proposal or key is required")
    if undo is None:
        undo = UNDO.get("queue:" + str(p.get("_key"))) or UNDO.get(str(p.get("id")))
    return reconciler.change_context(p, undo)


@app.get("/api/reports")
def reports_list():
    return scheduler.reports()


@app.get("/api/reports/{key}")
def reports_get(key: str):
    r = scheduler.report(key)
    if not r:
        raise HTTPException(404, "no report %s" % key)
    return r


@app.get("/api/corpus/documents")
def corpus_documents():
    """One row per source document: the platform it names and its review state."""
    return update_agent.document_states()


@app.get("/api/updates/diagram")
def updates_diagram():
    return {"mermaid": update_agent.mermaid()}


@app.get("/api/flow-diagram")
def flow_diagram():
    """The orchestration graph, rendered by LangGraph itself so the diagram in
    the UI is generated from the executing code rather than hand-drawn."""
    if not (USE_LANGGRAPH and graph_flow is not None):
        return {"available": False, "orchestrator": "direct"}
    return {"available": True, "orchestrator": "langgraph",
            "mermaid": graph_flow.mermaid()}


@app.post("/api/schema/refresh")
def schema_refresh():
    """Re-read the live graph into the schema the agents are grounded in.
    Runs on startup and after every accepted change; this is the manual
    lever for when the graph was edited outside MICA."""
    return schema_live.refresh()


@app.get("/api/overview")
def data_overview():
    return ov.overview()


@app.get("/api/platform/{platform_key}/summary")
def platform_summary(platform_key: str):
    data = ov.platform_summary(platform_key)
    if not data:
        raise HTTPException(404, "unknown platform %s" % platform_key)
    return data


@app.get("/api/graph/{platform_key}")
def platform_graph(platform_key: str):
    """Neighbourhood of one platform, for the 'what changed' view."""
    rows, _ = arango.aql("""
        FOR p IN Platform FILTER p._key == @k
          LET cats = (
            FOR c, e IN 1..1 OUTBOUND p GRAPH "aircraft_model"
              LET kids = (
                FOR k, ke IN 1..1 OUTBOUND c GRAPH "aircraft_model"
                  RETURN {name: k.name, type: k.node_type,
                          provenance: k.provenance,
                          edge_provenance: ke.provenance}
              )
              RETURN {category: c.node_type, provenance: c.provenance,
                      children: kids}
          )
          RETURN {platform: p.name, categories: cats}
    """, {"k": platform_key})
    return rows[0] if rows else {}


app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")


@app.on_event("startup")
def _apply_profile():
    """Point every module at the persisted deployment before the first request.
    Runs before the schema refresh below, which reads from whatever this set."""
    try:
        r = profiles.apply(profiles.active(), refresh_schema=False)
        print("profile: %s (%s)" % (r["active"], r["profile"].get("host")))
    except Exception as e:                             # noqa: BLE001
        print("profile not applied: %s: %s" % (type(e).__name__, e))


@app.on_event("startup")
def _refresh_schema():
    """Agents 1, 3 and 4 read data/schema.md at import, which describes the
    model as it was built. Accepted proposals add attributes to it, so
    re-read the live graph before the first question arrives."""
    r = schema_live.refresh()
    print("schema: %s" % r)


@app.on_event("startup")
def _start_scheduler():
    """The unattended sweep runs on a daemon thread, so it never blocks a request."""
    try:
        scheduler.start()
    except Exception as e:                             # noqa: BLE001
        print("scheduler did not start: %s: %s" % (type(e).__name__, e))


@app.get("/")
def index():
    return FileResponse(str(HERE / "static" / "index.html"))

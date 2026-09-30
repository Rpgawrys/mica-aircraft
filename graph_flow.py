#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LangGraph orchestration of the three agents.

Thin wrap: the agent logic is unchanged and still lives in agents/. This module
only expresses the flow as a StateGraph, which buys three things over the
straight-line version in app.py:

  1. agents 1 and 2 run in PARALLEL (fan-out from the entry node, fan-in at the
     reconciler) instead of sequentially - agent 1's latency stops being additive
  2. the graph renders ITSELF, so the architecture diagram in the UI is generated
     from the executing code rather than hand-drawn and liable to drift
  3. per-node streaming, so the UI can report progress instead of a blank spinner

The flow is deliberately still deterministic. Nothing here pretends the agents
choose their own path; that would be inventing autonomy the demo does not have.

Set USE_LANGGRAPH=false to fall back to the direct sequential path in app.py.
"""

import operator
from typing import Annotated, Any, Optional, TypedDict

from langgraph.graph import END, START, StateGraph

from agents import aql_agent, autograph_agent, reconciler


class FlowState(TypedDict, total=False):
    question: str
    platform: Optional[str]
    aql_backend: Optional[str]
    corpus_strategy: Optional[str]
    structured: dict
    unstructured: dict
    reconciliation: dict
    # nodes append their name as they finish; Annotated+operator.add lets the two
    # parallel branches write to the same key without clobbering each other
    completed: Annotated[list, operator.add]


def node_structured(state: FlowState) -> dict:
    """Agent 1 - query the official model of record."""
    out = aql_agent.run(state["question"], state.get("platform"),
                        backend=state.get("aql_backend"))
    return {"structured": out, "completed": ["structured"]}


def node_unstructured(state: FlowState) -> dict:
    """Agent 2 - ask the same question of the document corpus."""
    out = autograph_agent.run(state["question"], state.get("platform"),
                              strategy=state.get("corpus_strategy"))
    return {"unstructured": out, "completed": ["unstructured"]}


def node_reconcile(state: FlowState) -> dict:
    """Agent 3 - reconcile both answers and propose model edits."""
    out = reconciler.run(state["question"], state.get("structured", {}),
                         state.get("unstructured", {}), state.get("platform"))
    return {"reconciliation": out, "completed": ["reconciliation"]}


def build_graph():
    g = StateGraph(FlowState)
    g.add_node("structured", node_structured)
    g.add_node("unstructured", node_unstructured)
    g.add_node("reconcile", node_reconcile)

    # fan out: both agents start from the entry point and run concurrently
    g.add_edge(START, "structured")
    g.add_edge(START, "unstructured")
    # fan in: the reconciler waits for BOTH before running
    g.add_edge("structured", "reconcile")
    g.add_edge("unstructured", "reconcile")
    g.add_edge("reconcile", END)
    return g.compile()


GRAPH = build_graph()


def run(question: str, platform: Optional[str] = None,
        aql_backend: Optional[str] = None,
        corpus_strategy: Optional[str] = None) -> dict:
    final = GRAPH.invoke({"question": question, "platform": platform,
                          "aql_backend": aql_backend,
                          "corpus_strategy": corpus_strategy, "completed": []})
    return {"structured": final.get("structured", {}),
            "unstructured": final.get("unstructured", {}),
            "reconciliation": final.get("reconciliation", {}),
            "completed": final.get("completed", [])}


def stream(question: str, platform: Optional[str] = None):
    """Yield (node_name, partial_state) as each node finishes."""
    for chunk in GRAPH.stream({"question": question, "platform": platform,
                               "completed": []}):
        for node, payload in chunk.items():
            yield node, payload


def mermaid() -> str:
    """The graph's own rendering - used by the UI so the diagram cannot drift."""
    return GRAPH.get_graph().draw_mermaid()


if __name__ == "__main__":
    print(mermaid())

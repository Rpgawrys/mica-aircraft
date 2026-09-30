#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Keep data/schema.md honest as analysts accept proposals.

Agents 1, 3 and 4 are grounded in data/schema.md. That file is a build artifact:
gen_schema_doc.py writes it from data/graph_model.json, which describes the model
as it was LOADED, not as it stands after someone accepts a change.

The graph's SHAPE cannot drift. Agent 4's allowlist and the graph's own edge
definitions confine every proposal to the 15 vertex and 14 edge collections the
model already has, so no accepted change adds a collection. What drifts is counts
and attribute names. An accepted update_vertex writes its patch fields plus the
provenance stamp, and the schema document never hears about it. Agent 1 then
cannot write AQL against a field an analyst approved ten minutes earlier.

So: take the shape from data/schema.json, which is stable, and re-read counts and
attribute names from the live graph. The aircraft narrative stays hand-authored.
This is deliberately less machinery than deriving the whole graph from gharial.
We know what this graph is.

data/schema.json is never rewritten here. It stays the record of the shape as
built, so this module always has a trustworthy skeleton to hang live data on.
"""

import json
import os
import threading
from pathlib import Path

import arango_client as arango

HERE = Path(__file__).parent
SHAPE_FILE = HERE / "data" / "schema.json"
DOC_FILE = HERE / "data" / "schema.md"

SAMPLE = 40          # documents read per collection to collect attribute names

# the stamp apply_proposal() writes onto everything it touches. Listed apart in
# the document so agent 1 can see they are bookkeeping, not capability data.
STAMP = ("provenance", "accepted_by", "accepted_at", "evidence", "confidence",
         "proposal_title")

_lock = threading.Lock()
_running = False
_again = False


def _shape():
    return json.loads(SHAPE_FILE.read_text(encoding="utf-8"))


def _live(collections):
    """Counts and attribute names for every collection, in ONE query.

    Bind parameters rather than interpolated names: the handoff's own rule, and
    it keeps this correct if a collection is ever renamed to something AQL would
    otherwise read as an expression.
    """
    lets, binds = [], {}
    for i, c in enumerate(collections):
        binds["@c%d" % i] = c
        lets.append("LET n%d = LENGTH(@@c%d)" % (i, i))
        lets.append("LET a%d = UNIQUE(FLATTEN("
                    "FOR d IN @@c%d LIMIT %d RETURN ATTRIBUTES(d, true)))"
                    % (i, i, SAMPLE))
    ret = ", ".join('"%s": {"n": n%d, "a": a%d}' % (c, i, i)
                    for i, c in enumerate(collections))
    rows, _ = arango.aql("\n".join(lets) + "\nRETURN {" + ret + "}", binds)
    return rows[0] if rows else {}


def _order(baseline, live):
    """Baseline attributes keep their original order, anything an accepted
    proposal added lands after them. Stable document, additions easy to spot."""
    base = [a for a in baseline if a in live]
    return base + sorted(a for a in live if a not in baseline)


def render(shape, live):
    """Rebuild the document. Prose matches gen_schema_doc.py so the three agent
    prompts see the format they were written against."""
    graph = shape.get("graph", "aircraft_model")
    verts = shape["vertex_collections"]
    edges = shape["edge_collections"]

    L = ["# Structured graph schema — `%s`\n" % graph]
    L.append("The analyst's official model of record. Contains ONLY baseline data; "
             "nothing inferred from unstructured documents.\n")
    L.append("## Traversal shape\n")
    L.append("```")
    L.append("Platform")
    for n, e in edges.items():
        if e["from"] == ["Platform"]:
            L.append("  |-- %-24s -> %s" % (n, e["to"][0]))
    L.append("")
    for n, e in edges.items():
        if e["from"] != ["Platform"]:
            L.append("  %-20s --%s--> %s" % (e["from"][0], n, e["to"][0]))
    L.append("```\n")
    L.append("Category nodes (Performance, Signature, Airframe, Program, Propulsion, "
             "Sensors, ElectronicWarfare, Armament, Operators) are one-per-platform and "
             "carry a `platform` attribute with the platform name. Entity nodes (Engine, "
             "RadarSystem, Weapon, DefensiveSystem, Country) are deduplicated and shared "
             "across platforms.\n")
    L.append("Every element carries `provenance`: `baseline_csv` or `baseline_reference` "
             "for original model content, or `evidence_accepted` for anything an analyst "
             "later promoted from unstructured evidence.\n")

    L.append("## Vertex collections\n")
    for c, info in verts.items():
        lv = live.get(c) or {}
        attrs = _order(list(info.get("attributes") or {}), lv.get("a") or [])
        L.append("**%s** (%d docs) — %s" % (c, lv.get("n", info.get("count", 0)),
                                            ", ".join("`%s`" % a for a in attrs)))
        L.append("")

    L.append("## Edge collections\n")
    for n, e in edges.items():
        lv = live.get(n) or {}
        attrs = _order(list(e.get("attributes") or {}), lv.get("a") or [])
        extra = [a for a in attrs if a != "provenance"]
        L.append("**%s** (%d) `%s` → `%s`%s"
                 % (n, lv.get("n", e.get("count", 0)), e["from"][0], e["to"][0],
                    (" — edge attributes: " + ", ".join("`%s`" % a for a in extra))
                    if extra else ""))
        L.append("")

    L.append("## Notes for query generation\n")
    L.append("- Numeric tolerance bands are flat: `max_speed_kmh_nominal` / `_min` / `_max`, "
             "same for `combat_radius_km` and `rcs_m2`.")
    L.append("- To reach a platform's weapons: "
             "`Platform --has_armament--> Armament --carries_weapon--> Weapon`.")
    L.append("- To reach its EW systems: "
             "`Platform --has_electronic_warfare--> ElectronicWarfare "
             "--equipped_defensive_system--> DefensiveSystem`.")
    L.append("- Aggregate scores live on the category node, not the platform: "
             "`radar_score` on Sensors, `ew_score` on ElectronicWarfare, "
             "`missile_score` on Armament.")
    L.append("- Every collection listed under \"Edge collections\" holds EDGES. "
             "Traverse them, never iterate them. Iterating one returns edge "
             "documents, whose only attributes are the few listed above, so every "
             "field you read off the result is null and the query still succeeds.")
    L.append("")
    L.append("```aql")
    L.append("// correct: OUTBOUND reaches the connected document")
    L.append("FOR p IN Platform FILTER p.name == @name")
    L.append("  LET perf = FIRST(FOR v IN 1..1 OUTBOUND p has_performance RETURN v)")
    L.append("  LET arm  = FIRST(FOR v IN 1..1 OUTBOUND p has_armament RETURN v)")
    L.append("  LET wpns = (FOR w IN 1..1 OUTBOUND arm carries_weapon RETURN w.name)")
    L.append("  RETURN {platform: p.name,")
    L.append("          combat_radius_km: perf.combat_radius_km_nominal,")
    L.append("          weapons: wpns}")
    L.append("")
    L.append("// WRONG: this returns the edge, so combat_radius_km is always null")
    L.append("LET perf = FIRST(FOR e IN has_performance FILTER e._from == p._id RETURN e)")
    L.append("```")
    L.append("- Entity names are full designators carrying descriptive suffixes: "
             "`AIM-120C-7 AMRAAM`, `R-73 (AA-11 Archer)`, `PL-5E II`, "
             "`Khibiny-M (L175M)`. An analyst naming a family or a short "
             "designator will match nothing with `==`. Match partially and "
             "case-insensitively instead:")
    L.append("")
    L.append("```aql")
    L.append("FILTER LIKE(w.name, CONCAT(\"%\", @weapon, \"%\"), true)   "
             "// true = case-insensitive")
    L.append("```")
    L.append("- Attribute lists above are read from the live graph, so they include "
             "fields an analyst promoted from a source document. Those carry "
             "`provenance: 'evidence_accepted'` alongside the bookkeeping fields "
             + ", ".join("`%s`" % a for a in STAMP[1:]) +
             ". Filter on `provenance` to separate promoted values from baseline.")
    return "\n".join(L) + "\n"


def refresh(push=True):
    """Re-read the live graph and rewrite data/schema.md.

    Never raises and never leaves a partial file: on any failure the existing
    document stays exactly as it was, and the caller gets the reason back.
    """
    try:
        shape = _shape()
        names = list(shape["vertex_collections"]) + list(shape["edge_collections"])
        live = _live(names)
        text = render(shape, live)
    except Exception as e:                             # noqa: BLE001
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}

    try:
        tmp = DOC_FILE.with_suffix(".md.tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, DOC_FILE)                      # atomic, no torn read
    except Exception as e:                             # noqa: BLE001
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}

    if push:
        # late imports: these modules import this one's dependencies, not this one
        from agents import aql_agent, reconciler
        import update_agent
        aql_agent.SCHEMA = text
        reconciler.SCHEMA = text
        update_agent.SCHEMA = text

    promoted = sum(1 for c in shape["vertex_collections"]
                   if set((live.get(c) or {}).get("a") or [])
                   - set(shape["vertex_collections"][c].get("attributes") or {}))
    return {"ok": True, "collections": len(names), "chars": len(text),
            "collections_with_new_attributes": promoted}


def _worker():
    global _running, _again
    while True:
        refresh()
        with _lock:
            if not _again:
                _running = False
                return
            _again = False


def refresh_async():
    """Fire and forget after a write. A sweep can accept many proposals, so runs
    coalesce: one in flight, at most one more queued behind it."""
    global _running, _again
    with _lock:
        if _running:
            _again = True
            return {"queued": True}
        _running = True
    threading.Thread(target=_worker, name="schema-refresh", daemon=True).start()
    return {"started": True}


if __name__ == "__main__":
    print(json.dumps(refresh(push=False), indent=1))

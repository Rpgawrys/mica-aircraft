#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Emit a schema description of the structured graph, for grounding text-to-AQL.

Any text-to-AQL service (AQLizer or otherwise) needs to be told the collections,
their attributes, and how they connect. This derives that description from the
generated model rather than hand-maintaining it, so it can never drift.

Writes data/schema.json and data/schema.md
"""
import json
from pathlib import Path

HERE = Path(__file__).parent
g = json.loads((HERE / "data" / "graph_model.json").read_text(encoding="utf-8"))
V, E = g["vertices"], g["edges"]

import build_graph_model as B

def attrs_of(docs):
    seen = {}
    for d in docs:
        for k, v in d.items():
            if k.startswith("_"):
                continue
            seen.setdefault(k, type(v).__name__)
    return seen

schema = {"graph": B.GRAPH_NAME, "vertex_collections": {}, "edge_collections": {}}
for c in B.VERTEX_COLLECTIONS:
    schema["vertex_collections"][c] = {
        "count": len(V[c]),
        "attributes": attrs_of(V[c]),
        "example_key": V[c][0]["_key"] if V[c] else None,
    }
for name, frm, to in B.EDGE_DEFINITIONS:
    schema["edge_collections"][name] = {
        "count": len(E[name]), "from": frm, "to": to,
        "attributes": attrs_of(E[name]),
    }
(HERE / "data" / "schema.json").write_text(json.dumps(schema, indent=1), encoding="utf-8")

L = []
L.append("# Structured graph schema — `%s`\n" % B.GRAPH_NAME)
L.append("The analyst's official model of record. Contains ONLY baseline data; "
         "nothing inferred from unstructured documents.\n")
L.append("## Traversal shape\n")
L.append("```")
L.append("Platform")
for n, f, t in B.EDGE_DEFINITIONS:
    if f == ["Platform"]:
        L.append("  |-- %-24s -> %s" % (n, t[0]))
L.append("")
for n, f, t in B.EDGE_DEFINITIONS:
    if f != ["Platform"]:
        L.append("  %-20s --%s--> %s" % (f[0], n, t[0]))
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
for c in B.VERTEX_COLLECTIONS:
    info = schema["vertex_collections"][c]
    L.append("**%s** (%d docs) — %s" % (c, info["count"],
             ", ".join("`%s`" % a for a in info["attributes"])))
    L.append("")
L.append("## Edge collections\n")
for n, f, t in B.EDGE_DEFINITIONS:
    info = schema["edge_collections"][n]
    extra = [a for a in info["attributes"] if a != "provenance"]
    L.append("**%s** (%d) `%s` → `%s`%s" % (n, info["count"], f[0], t[0],
             (" — edge attributes: " + ", ".join("`%s`" % a for a in extra)) if extra else ""))
    L.append("")
L.append("## Notes for query generation\n")
L.append("- Numeric tolerance bands are flat: `max_speed_kmh_nominal` / `_min` / `_max`, "
         "same for `combat_radius_km` and `rcs_m2`.")
L.append("- To reach a platform's weapons: "
         "`Platform --has_armament--> Armament --carries_weapon--> Weapon`.")
L.append("- To reach its EW systems: "
         "`Platform --has_electronic_warfare--> ElectronicWarfare --equipped_defensive_system--> DefensiveSystem`.")
L.append("- Aggregate scores live on the category node, not the platform: "
         "`radar_score` on Sensors, `ew_score` on ElectronicWarfare, "
         "`missile_score` on Armament.")
(HERE / "data" / "schema.md").write_text("\n".join(L) + "\n", encoding="utf-8")
print("wrote data/schema.json and data/schema.md")

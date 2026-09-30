#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Build the STRUCTURED graph model (the "official data model") from the CSV +
synthetic baseline layer, and load it into ArangoDB.

This is deliberately NOT the autograph corpus. It is the analyst's model of record:
everything currently, officially understood about each platform, with nothing
inferred from unstructured documents. Agent 1 queries only this.

Shape (hybrid, per design decision):

    Platform
      |-- has_performance        -> Performance   (attributes only)
      |-- has_signature          -> Signature     (attributes only)
      |-- has_airframe           -> Airframe      (attributes only)
      |-- has_program            -> Program       (attributes only)
      |-- has_propulsion         -> Propulsion    --equipped_engine-->            Engine
      |-- has_sensors            -> Sensors       --equipped_radar-->             RadarSystem
      |-- has_electronic_warfare -> ElectronicWarfare --equipped_defensive_system-> DefensiveSystem
      |-- has_armament           -> Armament      --carries_weapon-->             Weapon
      '-- has_operators          -> Operators     --operated_by-->                Country

Category nodes carry the aggregate values that have no other home (radar_score,
ew_score, missile_score, engine_count, number_of_operators). Entity nodes are
deduplicated and SHARED across platforms - one AIM-9X node reached by three
platforms - which is what makes the graph worth traversing rather than a table.

Attributes are flat (max_speed_kmh_nominal, not nested) so generated AQL stays
simple and predictable.

Every element carries `provenance`, so elements later added from evidence are
distinguishable from the baseline at a glance.

Usage:
    python3 build_graph_model.py                  # emit + validate only
    python3 build_graph_model.py --load           # also load into ArangoDB
Env for --load (falls back to local docker defaults):
    ARANGO_CLOUD_URL / ARANGO_CLOUD_USER / ARANGO_CLOUD_PASSWORD
    or ARANGO_URL / ARANGO_ROOT_USER / ARANGO_ROOT_PASSWORD
    STRUCTURED_DB_NAME  (default: aircraft_model)
"""

import json
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).parent
# The source folder moved under "Aircraft Strong Corpus/" after the first build.
# Accept either location so this script (and gen_schema_doc.py, which imports it)
# keep working.
_DOD_CANDIDATES = [HERE.parent / "Dod_KG_Aircraft",
                   HERE.parent / "Aircraft Strong Corpus" / "Dod_KG_Aircraft"]
DOD = next((p for p in _DOD_CANDIDATES if (p / "load_csv.py").exists()),
           _DOD_CANDIDATES[0])
sys.path.insert(0, str(DOD))

from load_csv import load_aircraft  # noqa: E402

SYNTHETIC = json.loads((DOD / "synthetic_data.json").read_text(encoding="utf-8"))

CURATED = [
    "f_22a_raptor", "f_35a_lightning_ii", "f_16c_fighting_falcon",
    "su_57_felon", "su_35s_flanker_e", "j_20_mighty_dragon",
    "jf_17_block_3_thunder", "eurofighter_typhoon", "dassault_rafale",
]

GRAPH_NAME = "aircraft_model"

VERTEX_COLLECTIONS = [
    "Platform", "Performance", "Signature", "Airframe", "Program",
    "Propulsion", "Sensors", "ElectronicWarfare", "Armament", "Operators",
    "Engine", "RadarSystem", "Weapon", "DefensiveSystem", "Country",
]

EDGE_DEFINITIONS = [
    ("has_performance", ["Platform"], ["Performance"]),
    ("has_signature", ["Platform"], ["Signature"]),
    ("has_airframe", ["Platform"], ["Airframe"]),
    ("has_program", ["Platform"], ["Program"]),
    ("has_propulsion", ["Platform"], ["Propulsion"]),
    ("has_sensors", ["Platform"], ["Sensors"]),
    ("has_electronic_warfare", ["Platform"], ["ElectronicWarfare"]),
    ("has_armament", ["Platform"], ["Armament"]),
    ("has_operators", ["Platform"], ["Operators"]),
    ("equipped_engine", ["Propulsion"], ["Engine"]),
    ("equipped_radar", ["Sensors"], ["RadarSystem"]),
    ("equipped_defensive_system", ["ElectronicWarfare"], ["DefensiveSystem"]),
    ("carries_weapon", ["Armament"], ["Weapon"]),
    ("operated_by", ["Operators"], ["Country"]),
]
EDGE_COLLECTIONS = [e[0] for e in EDGE_DEFINITIONS]

BASE = "baseline_csv"
BASE_SYN = "baseline_reference"


def slug(s):
    return re.sub(r"[^a-z0-9]+", "_", str(s).lower()).strip("_")


def band(v, field):
    """Flatten a {nominal,min,max} band into three flat fields."""
    if not v:
        return {}
    return {"%s_nominal" % field: v.get("nominal"),
            "%s_min" % field: v.get("min"),
            "%s_max" % field: v.get("max")}


def build():
    ac = {d["_key"]: d for d in load_aircraft()}
    V = {c: {} for c in VERTEX_COLLECTIONS}
    E = {c: [] for c in EDGE_COLLECTIONS}

    def add_v(coll, key, doc):
        doc = {k: v for k, v in doc.items() if v is not None}
        doc["_key"] = key
        doc.setdefault("provenance", BASE)
        V[coll][key] = doc
        return "%s/%s" % (coll, key)

    def add_e(coll, frm, to, **attrs):
        attrs.setdefault("provenance", BASE)
        E[coll].append(dict(_from=frm, _to=to, **attrs))

    # entity lookup tables from the synthetic reference layer
    weapon_by_key = {w["_key"]: w for w in SYNTHETIC["Weapon"]}
    ds_by_key = {d["_key"]: d for d in SYNTHETIC["DefensiveSystem"]}
    country_by_key = {c["_key"]: c for c in SYNTHETIC["Country"]}

    for pkey in CURATED:
        a = ac[pkey]
        pname = a["name"]
        p_id = add_v("Platform", pkey, {
            "node_type": "Platform", "name": pname,
            "manufacturer": a["manufacturer"],
            "origin_country": a["origin_country"],
            "generation": a["generation"], "role": a["role"],
        })

        perf = {"node_type": "Performance", "platform": pname,
                "cruise_speed_kmh": a["cruise_speed_kmh"],
                "supercruise_kmh": a["supercruise_kmh"],
                "supercruise_capable": a["supercruise_capable"],
                "service_ceiling_m": a["service_ceiling_m"],
                "ferry_range_km": a["ferry_range_km"]}
        perf.update(band(a["max_speed_kmh"], "max_speed_kmh"))
        perf.update(band(a["combat_radius_km"], "combat_radius_km"))
        add_e("has_performance", p_id, add_v("Performance", pkey, perf))

        sig = {"node_type": "Signature", "platform": pname}
        sig.update(band(a["rcs_m2"], "rcs_m2"))
        add_e("has_signature", p_id, add_v("Signature", pkey, sig))

        add_e("has_airframe", p_id, add_v("Airframe", pkey, {
            "node_type": "Airframe", "platform": pname,
            "empty_weight_kg": a["empty_weight_kg"], "mtow_kg": a["mtow_kg"],
            "hardpoints": a["hardpoints"],
            "max_weapon_load_kg": a["max_weapon_load_kg"],
            "internal_weapons_bay": a["internal_weapons_bay"],
            "internal_fuel_kg": a["internal_fuel_kg"],
            "fuel_consumption_cruise_kg_h": a["fuel_consumption_cruise_kg_h"],
        }))

        add_e("has_program", p_id, add_v("Program", pkey, {
            "node_type": "Program", "platform": pname,
            "first_flight_year": a["first_flight_year"],
            "in_service_year": a["in_service_year"], "status": a["status"],
            "units_produced": a["units_produced"],
            "unit_cost_m_usd": a["unit_cost_m_usd"],
            "cost_per_flight_hour_usd": a["cost_per_flight_hour_usd"],
        }))

        # propulsion -> engine (deduped by model)
        prop_id = add_v("Propulsion", pkey, {
            "node_type": "Propulsion", "platform": pname,
            "engine_count": a["engine_count"],
            "thrust_per_engine_kn": a["thrust_per_engine_kn"],
            "thrust_vectoring": a["thrust_vectoring"],
        })
        add_e("has_propulsion", p_id, prop_id)
        ekey = slug(a["engine_model"])
        eid = add_v("Engine", ekey, {
            "node_type": "Engine", "name": a["engine_model"],
            "thrust_per_engine_kn": a["thrust_per_engine_kn"],
        })
        add_e("equipped_engine", prop_id, eid, count=a["engine_count"])

        # sensors -> radar (deduped by model)
        sens_id = add_v("Sensors", pkey, {
            "node_type": "Sensors", "platform": pname,
            "radar_score": a["radar_score"],
        })
        add_e("has_sensors", p_id, sens_id)
        rkey = slug(a["radar_model"])
        rid = add_v("RadarSystem", rkey, {
            "node_type": "RadarSystem", "name": a["radar_model"],
            "radar_type": a["radar_type"],
        })
        add_e("equipped_radar", sens_id, rid)

        # electronic warfare -> defensive systems
        ew_id = add_v("ElectronicWarfare", pkey, {
            "node_type": "ElectronicWarfare", "platform": pname,
            "ew_score": a["ew_score"],
        })
        add_e("has_electronic_warfare", p_id, ew_id)

        # armament -> weapons
        arm_id = add_v("Armament", pkey, {
            "node_type": "Armament", "platform": pname,
            "missile_score": a["missile_score"],
        })
        add_e("has_armament", p_id, arm_id)

        # operators -> countries
        ops_id = add_v("Operators", pkey, {
            "node_type": "Operators", "platform": pname,
            "number_of_operators": a["number_of_operators"],
        })
        add_e("has_operators", p_id, ops_id)

    # fan-outs that come from the synthetic reference layer
    for edge in SYNTHETIC["equipped_with"]:
        pk = edge["_from"].split("/", 1)[1]
        dk = edge["_to"].split("/", 1)[1]
        d = ds_by_key[dk]
        did = add_v("DefensiveSystem", dk, {
            "node_type": "DefensiveSystem", "name": d["name"],
            "system_type": d["system_type"], "description": d["description"],
            "provenance": BASE_SYN,
        })
        add_e("equipped_defensive_system", "ElectronicWarfare/%s" % pk, did,
              provenance=BASE_SYN)

    for edge in SYNTHETIC["carries"]:
        pk = edge["_from"].split("/", 1)[1]
        wk = edge["_to"].split("/", 1)[1]
        w = weapon_by_key[wk]
        wid = add_v("Weapon", wk, {
            "node_type": "Weapon", "name": w["name"], "weapon_type": w["type"],
            "guidance": w["guidance"], "range_km": w["range_km"],
            "warhead_kg": w["warhead_kg"], "origin_country": w["origin_country"],
            "provenance": BASE_SYN,
        })
        add_e("carries_weapon", "Armament/%s" % pk, wid,
              integration_status=edge["integration_status"],
              qty_typical=edge["qty_typical"], provenance=BASE_SYN)

    for edge in SYNTHETIC["operates"]:
        ck = edge["_from"].split("/", 1)[1]
        pk = edge["_to"].split("/", 1)[1]
        c = country_by_key[ck]
        cid = add_v("Country", ck, {
            "node_type": "Country", "name": c["name"], "region": c["region"],
            "provenance": BASE_SYN,
        })
        add_e("operated_by", "Operators/%s" % pk, cid, variant=edge["variant"],
              quantity_est=edge["quantity_est"], since_year=edge["since_year"],
              provenance=BASE_SYN)

    return {c: list(v.values()) for c, v in V.items()}, E


def validate(V, E):
    ids = set()
    for coll, docs in V.items():
        for d in docs:
            ids.add("%s/%s" % (coll, d["_key"]))
    errs = []
    for coll, edges in E.items():
        for e in edges:
            for side in ("_from", "_to"):
                if e[side] not in ids:
                    errs.append("%s: dangling %s=%s" % (coll, side, e[side]))
    return errs


def main():
    V, E = build()
    errs = validate(V, E)

    print("VERTICES")
    for c in VERTEX_COLLECTIONS:
        print("   %-20s %4d" % (c, len(V[c])))
    print("EDGES")
    for c in EDGE_COLLECTIONS:
        print("   %-28s %4d" % (c, len(E[c])))
    print("\ntotal vertices: %d   total edges: %d"
          % (sum(len(v) for v in V.values()), sum(len(e) for e in E.values())))
    print("referential errors:", errs or "NONE")
    if errs:
        raise SystemExit(1)

    out = HERE / "data" / "graph_model.json"
    out.write_text(json.dumps({"vertices": V, "edges": E}, indent=1), encoding="utf-8")
    print("wrote", out.relative_to(HERE.parent))

    if "--load" in sys.argv:
        load(V, E)


def load(V, E):
    from arango import ArangoClient
    url = os.environ.get("ARANGO_CLOUD_URL") or os.environ.get("ARANGO_URL", "http://localhost:8529")
    user = os.environ.get("ARANGO_CLOUD_USER") or os.environ.get("ARANGO_ROOT_USER", "root")
    pw = os.environ.get("ARANGO_CLOUD_PASSWORD") or os.environ.get("ARANGO_ROOT_PASSWORD", "")
    dbname = os.environ.get("STRUCTURED_DB_NAME", "aircraft_model")

    client = ArangoClient(hosts=url)
    sys_db = client.db("_system", username=user, password=pw)
    if not sys_db.has_database(dbname):
        sys_db.create_database(dbname)
        print("created database", dbname)
    db = client.db(dbname, username=user, password=pw)

    for c in VERTEX_COLLECTIONS:
        if not db.has_collection(c):
            db.create_collection(c)
    for c in EDGE_COLLECTIONS:
        if not db.has_collection(c):
            db.create_collection(c, edge=True)
    if not db.has_graph(GRAPH_NAME):
        db.create_graph(GRAPH_NAME, edge_definitions=[
            {"edge_collection": n, "from_vertex_collections": f,
             "to_vertex_collections": t} for n, f, t in EDGE_DEFINITIONS])
        print("created graph", GRAPH_NAME)

    for c in VERTEX_COLLECTIONS:
        col = db.collection(c); col.truncate()
        if V[c]:
            col.import_bulk(V[c], on_duplicate="replace")
        print("  loaded %-20s %4d" % (c, len(V[c])))
    for c in EDGE_COLLECTIONS:
        col = db.collection(c); col.truncate()
        if E[c]:
            col.import_bulk(E[c], on_duplicate="replace")
        print("  loaded %-28s %4d" % (c, len(E[c])))
    print("\nGraph viewer: %s/_db/%s/_admin/aardvark/index.html#graph/%s"
          % (url, dbname, GRAPH_NAME))


if __name__ == "__main__":
    main()

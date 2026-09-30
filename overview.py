#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Data-overview queries: the structured model, and the cloud of unstructured
material that surrounds it but is not part of it yet.

Two sides:
  * the MODEL  - aircraft_model graph: counted by collection, plus how much of it
                 was promoted from evidence (provenance == evidence_accepted)
  * the CORPUS - the autograph database: documents, chunks, entities, relations,
                 and an estimate of how many corpus entities have no counterpart
                 in the model. That last number is the "not yet integrated" gap
                 the whole demo is about.
"""

import os
import re

import requests

import arango_client as arango

AUTOGRAPH_DB = os.environ.get("AUTOGRAPH_DB", "Aircraft KG Strong")
# collection prefix differs between corpora (Aircraft-corpus on prod, Aircraft_KG
# on success). profiles.apply() sets CORPUS_PREFIX; otherwise detect it from
# whichever collection ends in _sources, preferring the names we know.
CORPUS_PREFIX = ""
CORPUS_PREFIXES = ["Aircraft-corpus", "aircraft-corpus", "Aircraft_KG"]

# entity types that could plausibly become nodes in the capability model.
# Extraction also yields dates, metrics and document labels - real, but not
# things the structured model is meant to hold.
MODELLABLE_TYPES = {
    "aircraft", "aircraft_platform", "component", "aircraft_component",
    "munition", "munition_component", "electronic_warfare_system",
    "ew_subsystem_component", "engine", "modification",
}


def _corpus_prefix():
    if CORPUS_PREFIX:
        return CORPUS_PREFIX
    url = "%s/_db/%s/_api/collection?excludeSystem=true" % (
        arango.URL, requests.utils.quote(AUTOGRAPH_DB, safe=""))
    try:
        r = requests.get(url, auth=(arango.USER, arango.PW), timeout=30)
        names = [c["name"] for c in r.json().get("result", [])]
    except Exception:                                  # noqa: BLE001
        return None
    for p in CORPUS_PREFIXES:
        if any(n.startswith(p + "_") for n in names):
            return p
    found = sorted(n[: -len("_sources")] for n in names if n.endswith("_sources"))
    return found[0] if found else None


def _count(db, coll):
    try:
        rows, _ = arango.aql("RETURN LENGTH(@@c)", {"@c": coll}, db=db)
        return rows[0] if rows else 0
    except Exception:
        return 0


def model_side():
    counts, promoted = {}, 0
    for c in ["Platform", "Performance", "Signature", "Airframe", "Program",
              "Propulsion", "Sensors", "ElectronicWarfare", "Armament",
              "Operators", "Engine", "RadarSystem", "Weapon",
              "DefensiveSystem", "Country"]:
        counts[c] = _count(arango.STRUCTURED_DB, c)
    edges = {}
    for c in ["has_performance", "has_signature", "has_airframe", "has_program",
              "has_propulsion", "has_sensors", "has_electronic_warfare",
              "has_armament", "has_operators", "equipped_engine",
              "equipped_radar", "equipped_defensive_system", "carries_weapon",
              "operated_by"]:
        edges[c] = _count(arango.STRUCTURED_DB, c)
    # NOTE: AQL has no COLLECTION() function - dynamic collection names are not
    # available here, so each collection is unioned explicitly.
    rows, _ = arango.aql("""
        LET w = (FOR d IN Weapon FILTER d.provenance=="evidence_accepted" RETURN 1)
        LET s = (FOR d IN DefensiveSystem FILTER d.provenance=="evidence_accepted" RETURN 1)
        LET r = (FOR d IN RadarSystem FILTER d.provenance=="evidence_accepted" RETURN 1)
        LET e = (FOR d IN Engine FILTER d.provenance=="evidence_accepted" RETURN 1)
        LET c = (FOR d IN Country FILTER d.provenance=="evidence_accepted" RETURN 1)
        LET p = (FOR d IN Platform FILTER d.provenance=="evidence_accepted" RETURN 1)
        RETURN LENGTH(w)+LENGTH(s)+LENGTH(r)+LENGTH(e)+LENGTH(c)+LENGTH(p)
    """)
    promoted = rows[0] if rows else 0
    return {"vertex_counts": counts, "edge_counts": edges,
            "total_vertices": sum(counts.values()),
            "total_edges": sum(edges.values()),
            "promoted_from_evidence": promoted}


def _norm(s):
    return re.sub(r"[^a-z0-9]+", "", str(s or "").lower())


def corpus_side(sample_unmatched=14):
    p = _corpus_prefix()
    if not p:
        return {"available": False,
                "error": "no corpus collections found in %r" % AUTOGRAPH_DB}
    out = {"available": True, "db": AUTOGRAPH_DB, "prefix": p}
    for label, coll in [("documents", "Documents"), ("chunks", "Chunks"),
                        ("entities", "Entities"), ("relations", "Relations"),
                        ("communities", "Communities")]:
        out[label] = _count(AUTOGRAPH_DB, "%s_%s" % (p, coll))

    # names already represented in the structured model
    rows, _ = arango.aql("""
        LET a = (FOR d IN Platform RETURN d.name)
        LET b = (FOR d IN Weapon RETURN d.name)
        LET c = (FOR d IN DefensiveSystem RETURN d.name)
        LET e = (FOR d IN RadarSystem RETURN d.name)
        LET f = (FOR d IN Engine RETURN d.name)
        LET g = (FOR d IN Country RETURN d.name)
        FOR n IN APPEND(APPEND(APPEND(APPEND(APPEND(a,b),c),e),f),g)
          FILTER n != null RETURN DISTINCT n
    """)
    known = {_norm(n) for n in rows if n}
    out["model_entity_names"] = len(known)

    unmatched, matched, sample = 0, 0, []
    by_type = {}
    try:
        rows, _ = arango.aql(
            "FOR e IN @@c FILTER e.entity_name != null "
            "COLLECT n = e.entity_name, t = e.entity_type "
            "RETURN {name: n, type: t}",
            {"@c": "%s_Entities" % p}, db=AUTOGRAPH_DB)
        for row in rows:
            name, etype = row["name"], row.get("type") or "unknown"
            by_type[etype] = by_type.get(etype, 0) + 1
            n = _norm(name)
            if not n:
                continue
            hit = n in known or any(n in k or k in n for k in known if len(k) > 4)
            if hit:
                matched += 1
            else:
                unmatched += 1
                if (len(sample) < sample_unmatched and 3 < len(name) < 60
                        and etype in MODELLABLE_TYPES and not name.isdigit()):
                    sample.append({"name": name, "type": etype})
    except Exception as e:
        out["entity_match_error"] = str(e)[:160]

    out["distinct_entity_names"] = matched + unmatched
    out["entities_matching_model"] = matched
    out["entities_not_in_model"] = unmatched
    out["unmatched_sample"] = sample
    out["entity_types"] = dict(sorted(by_type.items(), key=lambda kv: -kv[1])[:12])
    return out


def overview():
    return {"model": model_side(), "corpus": corpus_side()}


def platform_summary(platform_key):
    """Compact profile of one platform for the overview table."""
    rows, _ = arango.aql("""
    FOR p IN Platform FILTER p._key == @k LIMIT 1
      LET perf = FIRST(FOR x IN 1..1 OUTBOUND p has_performance RETURN x)
      LET sig  = FIRST(FOR x IN 1..1 OUTBOUND p has_signature RETURN x)
      LET air  = FIRST(FOR x IN 1..1 OUTBOUND p has_airframe RETURN x)
      LET prog = FIRST(FOR x IN 1..1 OUTBOUND p has_program RETURN x)
      LET prop = FIRST(FOR x IN 1..1 OUTBOUND p has_propulsion RETURN x)
      LET sens = FIRST(FOR x IN 1..1 OUTBOUND p has_sensors RETURN x)
      LET ew   = FIRST(FOR x IN 1..1 OUTBOUND p has_electronic_warfare RETURN x)
      LET arm  = FIRST(FOR x IN 1..1 OUTBOUND p has_armament RETURN x)
      LET ops  = FIRST(FOR x IN 1..1 OUTBOUND p has_operators RETURN x)
      LET engine = FIRST(FOR e IN 1..1 OUTBOUND prop equipped_engine RETURN e)
      LET radar  = FIRST(FOR r IN 1..1 OUTBOUND sens equipped_radar RETURN r)
      LET ewsys = (FOR d IN 1..1 OUTBOUND ew equipped_defensive_system
                   SORT d.name RETURN {name: d.name, type: d.system_type,
                                       provenance: d.provenance})
      LET wpns  = (FOR w, e IN 1..1 OUTBOUND arm carries_weapon
                   SORT w.name RETURN {name: w.name, type: w.weapon_type,
                                       range_km: w.range_km,
                                       status: e.integration_status,
                                       qty: e.qty_typical,
                                       provenance: w.provenance})
      LET countries = (FOR c, e IN 1..1 OUTBOUND ops operated_by
                       SORT e.quantity_est DESC
                       RETURN {name: c.name, qty: e.quantity_est,
                               since: e.since_year, variant: e.variant})
      RETURN {
        identity: {name: p.name, manufacturer: p.manufacturer,
                   origin: p.origin_country, generation: p.generation,
                   role: p.role, status: prog.status,
                   first_flight: prog.first_flight_year,
                   in_service: prog.in_service_year},
        performance: {max_speed_kmh: perf.max_speed_kmh_nominal,
                      cruise_speed_kmh: perf.cruise_speed_kmh,
                      supercruise: perf.supercruise_capable,
                      ceiling_m: perf.service_ceiling_m,
                      combat_radius_km: perf.combat_radius_km_nominal,
                      combat_radius_band: [perf.combat_radius_km_min,
                                           perf.combat_radius_km_max],
                      ferry_range_km: perf.ferry_range_km},
        signature: {rcs_m2: sig.rcs_m2_nominal,
                    rcs_band: [sig.rcs_m2_min, sig.rcs_m2_max]},
        airframe: {mtow_kg: air.mtow_kg, empty_weight_kg: air.empty_weight_kg,
                   hardpoints: air.hardpoints,
                   max_weapon_load_kg: air.max_weapon_load_kg,
                   internal_bay: air.internal_weapons_bay,
                   internal_fuel_kg: air.internal_fuel_kg},
        propulsion: {engine: engine.name, count: prop.engine_count,
                     thrust_kn: prop.thrust_per_engine_kn,
                     thrust_vectoring: prop.thrust_vectoring},
        sensors: {radar: radar.name, radar_type: radar.radar_type,
                  radar_score: sens.radar_score},
        ew: {score: ew.ew_score, systems: ewsys},
        armament: {missile_score: arm.missile_score, weapons: wpns},
        operators: {count: ops.number_of_operators, top: countries},
        program: {units_produced: prog.units_produced,
                  unit_cost_m_usd: prog.unit_cost_m_usd,
                  cost_per_flight_hour_usd: prog.cost_per_flight_hour_usd}
      }
    """, {"k": platform_key})
    return rows[0] if rows else {}

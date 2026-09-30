# Structured graph schema — `aircraft_model`

The analyst's official model of record. Contains ONLY baseline data; nothing inferred from unstructured documents.

## Traversal shape

```
Platform
  |-- has_performance          -> Performance
  |-- has_signature            -> Signature
  |-- has_airframe             -> Airframe
  |-- has_program              -> Program
  |-- has_propulsion           -> Propulsion
  |-- has_sensors              -> Sensors
  |-- has_electronic_warfare   -> ElectronicWarfare
  |-- has_armament             -> Armament
  |-- has_operators            -> Operators

  Propulsion           --equipped_engine--> Engine
  Sensors              --equipped_radar--> RadarSystem
  ElectronicWarfare    --equipped_defensive_system--> DefensiveSystem
  Armament             --carries_weapon--> Weapon
  Operators            --operated_by--> Country
```

Category nodes (Performance, Signature, Airframe, Program, Propulsion, Sensors, ElectronicWarfare, Armament, Operators) are one-per-platform and carry a `platform` attribute with the platform name. Entity nodes (Engine, RadarSystem, Weapon, DefensiveSystem, Country) are deduplicated and shared across platforms.

Every element carries `provenance`: `baseline_csv` or `baseline_reference` for original model content, or `evidence_accepted` for anything an analyst later promoted from unstructured evidence.

## Vertex collections

**Platform** (9 docs) — `node_type`, `name`, `manufacturer`, `origin_country`, `generation`, `role`, `provenance`

**Performance** (9 docs) — `node_type`, `platform`, `cruise_speed_kmh`, `supercruise_kmh`, `supercruise_capable`, `service_ceiling_m`, `ferry_range_km`, `max_speed_kmh_nominal`, `max_speed_kmh_min`, `max_speed_kmh_max`, `combat_radius_km_nominal`, `combat_radius_km_min`, `combat_radius_km_max`, `provenance`

**Signature** (9 docs) — `node_type`, `platform`, `rcs_m2_nominal`, `rcs_m2_min`, `rcs_m2_max`, `provenance`

**Airframe** (9 docs) — `node_type`, `platform`, `empty_weight_kg`, `mtow_kg`, `hardpoints`, `max_weapon_load_kg`, `internal_weapons_bay`, `internal_fuel_kg`, `fuel_consumption_cruise_kg_h`, `provenance`, `accepted_at`, `accepted_by`, `airframe_sustainment_note`, `confidence`, `evidence`, `proposal_title`

**Program** (9 docs) — `node_type`, `platform`, `first_flight_year`, `in_service_year`, `status`, `units_produced`, `unit_cost_m_usd`, `cost_per_flight_hour_usd`, `provenance`

**Propulsion** (9 docs) — `node_type`, `platform`, `engine_count`, `thrust_per_engine_kn`, `thrust_vectoring`, `provenance`, `accepted_at`, `accepted_by`, `al41f1_turbine_blade_wear_notice`, `confidence`, `evidence`, `proposal_title`

**Sensors** (9 docs) — `node_type`, `platform`, `radar_score`, `provenance`

**ElectronicWarfare** (9 docs) — `node_type`, `platform`, `ew_score`, `provenance`

**Armament** (9 docs) — `node_type`, `platform`, `missile_score`, `provenance`, `accepted_at`, `accepted_by`, `confidence`, `evidence`, `kh38m_datalink_alignment_notice`, `proposal_title`

**Operators** (9 docs) — `node_type`, `platform`, `number_of_operators`, `provenance`

**Engine** (9 docs) — `node_type`, `name`, `thrust_per_engine_kn`, `provenance`

**RadarSystem** (9 docs) — `node_type`, `name`, `radar_type`, `provenance`

**Weapon** (15 docs) — `node_type`, `name`, `weapon_type`, `guidance`, `range_km`, `warhead_kg`, `origin_country`, `provenance`

**DefensiveSystem** (12 docs) — `node_type`, `name`, `system_type`, `description`, `provenance`, `accepted_at`, `accepted_by`, `confidence`, `evidence`, `maintenance_replacement_note`, `proposal_title`

**Country** (34 docs) — `node_type`, `name`, `region`, `provenance`

## Edge collections

**has_performance** (9) `Platform` → `Performance`

**has_signature** (9) `Platform` → `Signature`

**has_airframe** (9) `Platform` → `Airframe`

**has_program** (9) `Platform` → `Program`

**has_propulsion** (9) `Platform` → `Propulsion`

**has_sensors** (9) `Platform` → `Sensors`

**has_electronic_warfare** (9) `Platform` → `ElectronicWarfare`

**has_armament** (9) `Platform` → `Armament`

**has_operators** (9) `Platform` → `Operators`

**equipped_engine** (9) `Propulsion` → `Engine` — edge attributes: `count`

**equipped_radar** (9) `Sensors` → `RadarSystem`

**equipped_defensive_system** (16) `ElectronicWarfare` → `DefensiveSystem`

**carries_weapon** (25) `Armament` → `Weapon` — edge attributes: `integration_status`, `qty_typical`

**operated_by** (47) `Operators` → `Country` — edge attributes: `variant`, `quantity_est`, `since_year`

## Notes for query generation

- Numeric tolerance bands are flat: `max_speed_kmh_nominal` / `_min` / `_max`, same for `combat_radius_km` and `rcs_m2`.
- To reach a platform's weapons: `Platform --has_armament--> Armament --carries_weapon--> Weapon`.
- To reach its EW systems: `Platform --has_electronic_warfare--> ElectronicWarfare --equipped_defensive_system--> DefensiveSystem`.
- Aggregate scores live on the category node, not the platform: `radar_score` on Sensors, `ew_score` on ElectronicWarfare, `missile_score` on Armament.
- Every collection listed under "Edge collections" holds EDGES. Traverse them, never iterate them. Iterating one returns edge documents, whose only attributes are the few listed above, so every field you read off the result is null and the query still succeeds.

```aql
// correct: OUTBOUND reaches the connected document
FOR p IN Platform FILTER p.name == @name
  LET perf = FIRST(FOR v IN 1..1 OUTBOUND p has_performance RETURN v)
  LET arm  = FIRST(FOR v IN 1..1 OUTBOUND p has_armament RETURN v)
  LET wpns = (FOR w IN 1..1 OUTBOUND arm carries_weapon RETURN w.name)
  RETURN {platform: p.name,
          combat_radius_km: perf.combat_radius_km_nominal,
          weapons: wpns}

// WRONG: this returns the edge, so combat_radius_km is always null
LET perf = FIRST(FOR e IN has_performance FILTER e._from == p._id RETURN e)
```
- Entity names are full designators carrying descriptive suffixes: `AIM-120C-7 AMRAAM`, `R-73 (AA-11 Archer)`, `PL-5E II`, `Khibiny-M (L175M)`. An analyst naming a family or a short designator will match nothing with `==`. Match partially and case-insensitively instead:

```aql
FILTER LIKE(w.name, CONCAT("%", @weapon, "%"), true)   // true = case-insensitive
```
- Attribute lists above are read from the live graph, so they include fields an analyst promoted from a source document. Those carry `provenance: 'evidence_accepted'` alongside the bookkeeping fields `accepted_by`, `accepted_at`, `evidence`, `confidence`, `proposal_title`. Filter on `provenance` to separate promoted values from baseline.

# MICA — Model Interrogation & Change Analysis

A multi-agent analyst demo on ArangoDB. An analyst asks one question about a fighter aircraft. Three agents answer it at once: one queries the official structured model, one asks a GraphRAG knowledge graph built from evidence documents, and one reconciles the two and proposes concrete, auditable edits to the model. A fourth agent runs on a schedule, reads documents nobody has reviewed yet, and queues proposals so the model does not go stale.

Everything in the corpus is fictional. The aircraft, weapons and countries are real names taken from a public dataset; every "bulletin", "depot notice" and "OSINT post" was written for this demo and describes events that did not happen.

## Goals

- Show a structured graph and an unstructured knowledge graph answering the **same question side by side**, and make the disagreement between them the product.
- Keep the structured model the **model of record**: nothing from a document enters it until an analyst accepts a typed proposal, and every accepted element carries `provenance: evidence_accepted` plus the evidence that justified it. Every acceptance is undoable.
- Demonstrate an agent that is **triggered and stateful** (watermark of reviewed documents, a review queue, dated change reports) rather than request/response.
- Stay honest about what the agents do. The flow is deterministic; nothing pretends the agents choose their own path.

## How it works

```
question ──┬── Agent 1  NL → AQL against the structured graph (read-only, validated with /_api/explain)
           └── Agent 2  GraphRAG retriever over the evidence knowledge graph
                   └── Agent 3  reconciler: official / corroborated / not in model / contradictions → proposals
Agent 4  scheduled change detection: unreviewed documents → assess against the live model → review queue
```

- Python, FastAPI, one static page. LangGraph runs agents 1 and 2 in parallel and fans in at agent 3.
- ArangoDB 3.12 enterprise: the structured graph `aircraft_model` and a GraphRAG corpus database built with the platform's AutoGraph importer. Agent 2 calls the platform's retriever service; its model runs server side.
- LLMs via LangChain. Agent 1 uses a small model grounded in a schema document that is regenerated from the live graph after every accepted change. Agents 3 and 4 use a larger model.
- Two natural-language-to-AQL backends behind one guard (the app's own prompt, or ArangoDB's translation service), four retriever strategies (local, unified, global, deep search), and a switch between two ArangoDB deployments.

## The structured model

Graph `aircraft_model`: nine platforms, 169 vertices, 187 edges. Each platform hangs nine category nodes (performance, signature, airframe, program, propulsion, sensors, electronic warfare, armament, operators); entity nodes (engines, radars, weapons, defensive systems, countries) are deduplicated and shared, so the graph is worth traversing rather than joining.

```
Platform
  |-- has_performance / has_signature / has_airframe / has_program   (attributes only)
  |-- has_propulsion         -> Propulsion        --equipped_engine-->            Engine
  |-- has_sensors            -> Sensors           --equipped_radar-->             RadarSystem
  |-- has_electronic_warfare -> ElectronicWarfare --equipped_defensive_system-->  DefensiveSystem
  |-- has_armament           -> Armament          --carries_weapon-->             Weapon
  '-- has_operators          -> Operators         --operated_by-->                Country
```

### Source dataset

Baseline aircraft data comes from the Kaggle **[Military Aircraft Dataset](https://www.kaggle.com/datasets/oxcartcorporation/military-aircraft-dataset)** by OXCART (Victor Samuel), CC0 public domain: 48 fighter aircraft with performance, airframe, program and cost figures compiled from Wikipedia, manufacturer specifications and defence reporting. The dataset card states that `Radar_Score`, `Missile_Score` and `EW_Score` are qualitative expert ratings, not measurements; they are carried into the model unchanged and marked as baseline data.

### What we changed

- **Nine aircraft** selected from the 48: F-22A Raptor, F-35A Lightning II, F-16C Fighting Falcon, Su-57 Felon, Su-35S Flanker-E, J-20 Mighty Dragon, JF-17 Block 3 Thunder, Eurofighter Typhoon, Dassault Rafale.
- **Tolerance bands** synthesised around three point values so evidence can be classified against a range: radar cross-section ±15%, combat radius ±10%, maximum speed ±5% (`*_nominal`, `*_min`, `*_max`).
- **A hand-authored synthetic layer** the dataset does not have (`synthetic_data.json`): 16 weapons, 13 defensive systems, 35 operator countries, and the `carries`, `equipped_with` and `operates` edges with integration status, typical loadout, variant, fleet estimate and year. Radar and engine models were promoted from CSV attributes into shared `RadarSystem` and `Engine` nodes.
- **Combat-record fields dropped** (kills, losses, sorties, kill ratio). The demo is about characteristics and subsystems, not combat history.
- **Provenance on every element**: `baseline_csv`, `baseline_reference`, or `evidence_accepted`.
- A few entities in the synthetic layer are deliberately absent from the loaded graph (one weapon, one operator country) so that evidence documents can "introduce" something the model does not yet hold.

`build_graph_model.py` builds and validates the model from the CSV and the synthetic layer, and loads it with `--load`. `data/schema.json` records the shape as built; `data/schema.md` is regenerated from the live graph.

## The evidence corpus

180 fictional documents, 20 per aircraft, authored as structured Python data and rendered to 108 PDFs and 72 text files, plus the baseline workbook rendered as Markdown tables. They were built to test entity resolution at three tiers:

| Tier | Docs | What the document names |
|---|---|---|
| aircraft | 55 | the aircraft itself |
| subcomponent | 64 | a weapon, defensive system, radar or engine the aircraft carries |
| subsubcomponent | 61 | a part of one of those (a seeker, a guidance computer, an amplifier module) |

Twenty-nine document types (manufacturer technical bulletins, depot inspection notices, independent modelling briefs, aviation OSINT tracker notes, industry newsletter notes, flight test summaries, and so on), each with a fictional source organisation, a date, and a body that keeps a deliberate hedging level: some claims are stated plainly, some are "alleged" or "single source", and the demo questions depend on those hedges being preserved. Shared weapons name every aircraft that carries them, so the fan-in case is real. Two documents are in Russian with the aircraft glossed in English and designators kept in Latin script.

How it was generated:

1. An original set was written with the aircraft **hidden** in the lower tiers, so a knowledge-graph builder had to infer the link through the weapon or system name. An answer key recorded the intended linkage for every file.
2. `build_augmented_evidence.py` rewrote that set from the answer key so every document **states** its aircraft and system chain explicitly, and translated nine of eleven Russian documents to English. Same topics, same count.
3. The twenty Su-35S documents were expanded from one paragraph to full-length documents with reference blocks, sample populations and distribution lists, under a hard rule that every load-bearing claim and its exact hedge stays verbatim in substance.
4. `render_evidence.py` renders the set to PDF and text (with a Unicode font for Cyrillic) and packages it for the AutoGraph importer.

A parallel "weak" variant that never names the aircraft exists for controlled entity-resolution experiments and is not used by this app.

## Running it

```bash
cp .env.example .env            # fill in your retriever pod id and corpus names
export ARANGO_CLOUD_URL=... ARANGO_CLOUD_USER=... ARANGO_CLOUD_PASSWORD=...
export OPENAI_API_KEY=...
./run.sh                        # http://127.0.0.1:8000
```

Credentials come from the shell; `.env` holds only topology (database names, service pod ids, model names). `GET /api/health` reports the resolved host and runs a real query against the structured database, so a wrong deployment shows up immediately.

## Repository layout

| Path | Purpose |
|---|---|
| `app.py` | FastAPI routes, startup hooks |
| `graph_flow.py` | LangGraph flow: agents 1 + 2 in parallel, then 3 |
| `agents/aql_agent.py` | agent 1, NL → AQL with read-only screen, explain validation, retries |
| `agents/autograph_agent.py` | agent 2, GraphRAG retriever client and strategies |
| `agents/reconciler.py` | agent 3, reconciliation and typed proposals |
| `update_agent.py`, `scheduler.py` | agent 4 and its timer, watermark, queue, reports |
| `aql_backends.py` | second NL → AQL backend (ArangoDB's translation service) |
| `schema_live.py` | regenerates the schema document from the live graph |
| `profiles.py` | switch between two ArangoDB deployments |
| `arango_client.py`, `llm.py`, `overview.py` | HTTP client, model provider, data-overview queries |
| `build_graph_model.py`, `gen_schema_doc.py` | build and load the structured model |
| `data/` | `schema.json` (shape as built), `graph_model.json`, `schema.md` (live) |
| `static/index.html` | the analyst console |

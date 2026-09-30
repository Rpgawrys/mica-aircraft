#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AGENT 1 - the structured-model agent.

Translates the analyst's natural-language question into AQL, runs it against the
STRUCTURED graph only (aircraft_model), and returns both the generated query and
its results. It never sees the unstructured corpus - that separation is the whole
point of the comparison the analyst is shown.

Text-to-AQL is done with Claude grounded in data/schema.md (derived from the live
graph, so it cannot drift). This sits behind `generate_aql()` so an ArangoDB
AQLizer service can be dropped in later without touching anything else.

Safety: generated AQL is hard-blocked from mutating. The model is told to produce
read-only queries, and independently the query is regex-screened for write
operations and then validated with /_api/explain before it is allowed to run.
"""

import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import arango_client as arango  # noqa: E402

import aql_backends  # noqa: E402
import llm  # noqa: E402

MODEL = os.environ.get("AQL_AGENT_MODEL", "claude-sonnet-5")

# Row cap for a question with no platform selected. The model holds nine
# platforms, so a per-platform comparison still fits under it; anything broader
# gets trimmed, and the agent has to say so in `note` rather than let a partial
# result read as the whole answer.
FLEET_ROW_LIMIT = 10
SCHEMA = (Path(__file__).parent.parent / "data" / "schema.md").read_text(encoding="utf-8")

# An exact match on a name that returns nothing is the one empty result worth
# doubting: entity names are full designators, so `w.name == "AIM-120"` finds
# nothing while the model does hold "AIM-120C-7 AMRAAM". Everything else that
# comes back empty is left alone; empty is usually the honest answer.
NAME_EQUALITY = re.compile(r"\bname\s*==", re.I)

# A valid AQL query ends with RETURN, never with LIMIT. Told to "use LIMIT 10",
# a model sometimes appends it after the RETURN, where it parses as a second
# statement and the whole query is rejected. Feeding the parser error back does
# not reliably fix it, and the mistake is unambiguous, so repair it in place
# rather than spending another model call on it.
TRAILING_LIMIT = re.compile(r"\s*\bLIMIT\s+\d+\s*(?:,\s*\d+\s*)?;?\s*$", re.I)


def strip_trailing_limit(aql):
    """Returns (cleaned_aql, was_repaired)."""
    if not aql:
        return aql, False
    body = aql.rstrip()
    cleaned = TRAILING_LIMIT.sub("", body)
    return (cleaned, True) if cleaned != body else (aql, False)

# any of these appearing as a bare keyword means the query is not read-only
WRITE_OPS = re.compile(
    r"\b(INSERT|UPDATE|REPLACE|REMOVE|UPSERT|TRUNCATE|CREATE|DROP)\b", re.I)

SYSTEM = """You translate an intelligence analyst's question into a single ArangoDB AQL query.

You are querying the OFFICIAL STRUCTURED DATA MODEL only. This graph contains solely
what is formally recorded about each platform. It contains nothing derived from
unstructured reporting.

Rules:
- Return ONE read-only AQL query. Never INSERT, UPDATE, REPLACE, REMOVE, UPSERT or TRUNCATE.
- Use bind parameters (@name) for literal values, especially the platform name.
- Prefer returning whole documents or named projections over scalars, so the analyst
  can see what the model actually holds.
- If the question asks about something the schema has no field for, still return a
  valid query retrieving the closest relevant nodes, and say so in `note`. Do not
  invent collections or attributes that are not in the schema.
- Keep results small (LIMIT where sensible); this is for on-screen review.
- Names in this model are full designators ("AIM-120C-7 AMRAAM", "R-73 (AA-11
  Archer)"). When the analyst names a weapon, radar, engine or system, assume they
  gave a partial designator and match with LIKE(x.name, CONCAT("%", @v, "%"), true),
  never with ==. An equality filter on a partial name returns zero rows, which
  reads to the analyst as "the model does not hold this".
- Edge collections (has_*, equipped_*, carries_weapon, operated_by) contain ONLY
  edges. Reach a connected document by traversing:
      FOR v IN 1..1 OUTBOUND doc <edge_collection> RETURN v
  Never write `FOR e IN <edge_collection> FILTER e._from == ...` and then read
  attributes off e. That returns the edge, every attribute reads as null, and the
  query still succeeds, so the mistake is invisible until the answer is wrong.

Respond with ONLY a JSON object, no prose and no code fences:
{"aql": "...", "bind_vars": {...}, "note": "one sentence on what the query retrieves and any limitation"}"""


def _extract_json(text):
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        text = text[start:end + 1]
    return json.loads(text)


def generate_aql(question, platform=None, client=None, prior=None):
    """NL -> {aql, bind_vars, note}. Swap this body for an AQLizer call if desired.

    `prior` is (rejected_aql, error) from a failed validation. ArangoDB's parser
    message names the offending token and position, which is better feedback than
    anything this prompt could anticipate, so it goes straight back.
    """
    ctx = "Schema:\n\n%s\n\n" % SCHEMA
    if platform:
        ctx += ("The analyst is currently looking at the platform named exactly "
                "'%s'. Scope the query to it unless the question is explicitly "
                "cross-platform.\n\n" % platform)
    else:
        ctx += ("The analyst is asking across EVERY platform in the model. Do not "
                "scope the query to one platform and do not pick one yourself. "
                "Name the platform in every row you return, so a row can be read "
                "without a second lookup. Where the question compares something, "
                "return one row per platform and sort so the comparison is "
                "obvious.\n"
                "Cap the result at %d rows. Put the LIMIT inside the FOR body, "
                "immediately before the RETURN. A LIMIT written after the RETURN "
                "is a syntax error. The model holds nine platforms, so one row "
                "each still fits. If the question asks for something broader, sort "
                "so the %d most relevant rows come first, and state in `note` that "
                "the result is capped at %d and is not the full set.\n\n"
                % (FLEET_ROW_LIMIT, FLEET_ROW_LIMIT, FLEET_ROW_LIMIT))
    if prior:
        ctx += ("Your previous query was rejected by ArangoDB and never ran.\n"
                "Rejected query:\n%s\n\nArangoDB said: %s\n\n"
                "Fix that specific problem and return the corrected query.\n\n"
                % (prior[0], prior[1]))
    return llm.complete_json(SYSTEM, ctx + "Question: " + question,
                             model=MODEL, max_tokens=6000)


def is_read_only(aql_text):
    # strip string literals so a write keyword inside a value can't trip the check
    stripped = re.sub(r'"[^"]*"|\'[^\']*\'', '""', aql_text)
    hit = WRITE_OPS.search(stripped)
    return (hit is None), (hit.group(0) if hit else None)


def _generate(question, platform, client, prior, backend):
    """Route to whichever engine is selected. Both return the same shape, so the
    read-only screen, the explain validation and the retries below apply equally."""
    if backend == "aqlizer":
        return aql_backends.generate(question, platform, prior=prior)
    return generate_aql(question, platform, client, prior=prior)


def run(question, platform=None, client=None, backend=None):
    backend = (backend or aql_backends.DEFAULT).strip().lower()
    if backend not in aql_backends.BACKENDS:
        backend = "builtin"
    out = {"agent": "structured", "question": question, "platform": platform,
           "backend": backend,
           "aql": None, "bind_vars": None, "note": None,
           "rows": [], "row_count": 0, "error": None}
    try:
        try:
            gen = _generate(question, platform, client, None, backend)
        except aql_backends.BackendRefused as refused:
            # Answer the question rather than hand the analyst a service error.
            # The swap is recorded so the UI can say which engine actually ran.
            out["fell_back"] = str(refused)
            backend = out["backend"] = "builtin"
            gen = _generate(question, platform, client, None, backend)
        out["aql"], repaired = strip_trailing_limit(gen.get("aql"))
        if repaired:
            out["repaired"] = "removed a LIMIT written after the RETURN"
        out["bind_vars"] = gen.get("bind_vars") or {}
        out["note"] = gen.get("note")

        ok, offending = is_read_only(out["aql"] or "")
        if not ok:
            out["error"] = ("Generated query was rejected as non-read-only "
                            "(contains %s). Nothing was executed." % offending)
            return out

        # A generated query can look fine and still not parse. Hand the parser's
        # complaint back once rather than reporting zero rows to an analyst who
        # cannot tell a real empty result from a broken query.
        try:
            arango.explain(out["aql"], out["bind_vars"])       # validate, don't run
        except Exception as first:
            out["retried"] = "%s" % first
            gen = _generate(question, platform, client,
                            (out["aql"], str(first)), backend)
            out["aql"], repaired = strip_trailing_limit(gen.get("aql"))
            if repaired:
                out["repaired"] = "removed a LIMIT written after the RETURN"
            out["bind_vars"] = gen.get("bind_vars") or {}
            out["note"] = gen.get("note")
            ok, offending = is_read_only(out["aql"] or "")
            if not ok:
                out["error"] = ("Corrected query was rejected as non-read-only "
                                "(contains %s). Nothing was executed." % offending)
                return out
            arango.explain(out["aql"], out["bind_vars"])
        rows, _stats = arango.aql(out["aql"], out["bind_vars"])

        if not rows and NAME_EQUALITY.search(out["aql"] or ""):
            hint = ("That query ran and returned zero rows. It filters a name with "
                    "==, and names in this model are full designators, so an exact "
                    "match on a partial name finds nothing. Rewrite it using "
                    "LIKE(x.name, CONCAT(\"%\", @value, \"%\"), true) and keep "
                    "everything else the same.")
            try:
                gen = _generate(question, platform, client,
                                (out["aql"], hint), backend)
                cand, _ = strip_trailing_limit(gen.get("aql"))
                binds = gen.get("bind_vars") or {}
                ok, _off = is_read_only(cand or "")
                if ok and cand:
                    arango.explain(cand, binds)
                    retry_rows, _ = arango.aql(cand, binds)
                    # only keep the rewrite if it actually found something
                    if retry_rows:
                        out["aql"], out["bind_vars"] = cand, binds
                        out["note"] = gen.get("note")
                        out["widened"] = "exact name match found nothing; retried with partial matching"
                        rows = retry_rows
            except Exception:                          # noqa: BLE001
                pass                                   # keep the honest empty result

        if not platform and len(rows) > FLEET_ROW_LIMIT:
            out["capped"] = ("showing %d of %d rows; the question was asked across "
                             "every platform" % (FLEET_ROW_LIMIT, len(rows)))
            rows = rows[:FLEET_ROW_LIMIT]
        out["rows"] = rows
        out["row_count"] = len(rows)
    except Exception as e:
        out["error"] = "%s: %s" % (type(e).__name__, e)
    return out


if __name__ == "__main__":
    q = sys.argv[1] if len(sys.argv) > 1 else \
        "What electronic warfare systems does this platform carry?"
    p = sys.argv[2] if len(sys.argv) > 2 else "Su-35S Flanker-E"
    r = run(q, p)
    print(json.dumps(r, indent=2)[:2500])

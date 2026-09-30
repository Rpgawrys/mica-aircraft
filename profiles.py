#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Two named deployments, one switch.

MICA runs against the same aircraft model on two ArangoDB deployments. A profile
is the WHOLE topology for one of them: structured database, corpus database and
prefix, GraphRAG retriever, AQLizer. Switching moves every agent together, so
agent 1 can never end up reading one cluster while agent 2 answers from another,
which is the failure the handoff calls the retriever-binding trap.

Credentials never live here. Each profile names an env prefix and reads
ARANGO_<PREFIX>_URL / _USER / _PASSWORD from the environment, which ~/.zshenv
already exports for both deployments. Only topology (database names, service
pod ids) is in this file, and each of those can be overridden from .env.

The active choice persists in data/runtime_profile.json. With no file, startup
picks the profile whose URL matches ARANGO_CLOUD_URL, i.e. whatever ARANGO_PROFILE
resolved to, so nothing changes until someone flips the switch.
"""

import json
import os
import threading
from pathlib import Path
from urllib.parse import urlparse

HERE = Path(__file__).parent
STATE_FILE = HERE / "data" / "runtime_profile.json"
_lock = threading.Lock()


def _env(*names, default=""):
    for n in names:
        v = os.environ.get(n, "").strip()
        if v:
            return v
    return default


# Topology per deployment. "prod" honours the existing .env variables so the
# behaviour before this module existed is exactly the behaviour with it active.
PROFILES = {
    "prod": {
        "label": "roman-poc",
        "env": "ARANGO_PROD",
        "structured_db": _env("STRUCTURED_DB_NAME", default="aircraft_model"),
        "corpus_db": _env("AUTOGRAPH_DB", default="Aircraft KG Strong"),
        "corpus_prefix": _env("CORPUS_PREFIX", default="Aircraft-corpus"),
        "retriever_url": _env("AUTOGRAPH_RETRIEVER_URL"),
        "retriever_pod": "47kzd",
        "aqlizer_pod": _env("AQLIZER_POD", default="fqu0w"),
    },
    "success": {
        "label": "success",
        "env": "ARANGO_SUCCESS",
        "structured_db": _env("STRUCTURED_DB_NAME_SUCCESS", default="aircraft_model"),
        "corpus_db": _env("AUTOGRAPH_DB_SUCCESS", default="Aircraft_KG"),
        "corpus_prefix": _env("CORPUS_PREFIX_SUCCESS", default="Aircraft_KG"),
        "retriever_url": _env("AUTOGRAPH_RETRIEVER_URL_SUCCESS"),
        "retriever_pod": _env("AUTOGRAPH_RETRIEVER_POD_SUCCESS"),   # none deployed yet
        "aqlizer_pod": _env("AQLIZER_POD_SUCCESS"),                  # none deployed yet
    },
}


def _creds(p):
    e = p["env"]
    return (_env(e + "_URL").rstrip("/"), _env(e + "_USER", default="root"), _env(e + "_PASSWORD"))


def _retriever_url(p, url):
    if p.get("retriever_url"):
        return p["retriever_url"].rstrip("/")
    if p.get("retriever_pod") and url:
        return "%s/graphrag/retriever/%s" % (url, p["retriever_pod"])
    return ""


def describe(key):
    """Everything the UI shows about a profile. No secrets."""
    p = PROFILES[key]
    url, user, pw = _creds(p)
    return {"key": key, "label": p["label"],
            "host": urlparse(url).hostname if url else None,
            "credentials": bool(url and pw),
            "structured_db": p["structured_db"],
            "corpus_db": p["corpus_db"], "corpus_prefix": p["corpus_prefix"],
            "retriever": bool(_retriever_url(p, url)),
            "aqlizer": bool(p.get("aqlizer_pod"))}


def _default_key():
    cur = _env("ARANGO_CLOUD_URL").rstrip("/")
    for k, p in PROFILES.items():
        if cur and _creds(p)[0] == cur:
            return k
    return "prod"


def active():
    try:
        k = json.loads(STATE_FILE.read_text(encoding="utf-8")).get("active")
        if k in PROFILES:
            return k
    except Exception:                                  # noqa: BLE001
        pass
    return _default_key()


def apply(key, refresh_schema=True):
    """Point every module at one deployment. Returns a summary for the caller."""
    if key not in PROFILES:
        raise ValueError("unknown profile %r" % key)
    p = PROFILES[key]
    url, user, pw = _creds(p)
    if not url or not pw:
        raise RuntimeError("profile %r has no credentials: set %s_URL/_USER/_PASSWORD"
                           % (key, p["env"]))

    import arango_client as arango
    import aql_backends
    import overview as ov
    import update_agent
    from agents import autograph_agent, reconciler

    with _lock:
        # structured side: agents 1, 3, 4, scheduler, schema, overview
        arango.URL, arango.USER, arango.PW = url, user, pw
        arango.STRUCTURED_DB = p["structured_db"]
        arango._jwt["token"], arango._jwt["at"] = None, 0     # a JWT is per deployment

        # corpus side: agent 2, source lookup, change detection, overview
        autograph_agent.RETRIEVER_URL = _retriever_url(p, url)
        autograph_agent.RETRIEVER_ID = ""
        autograph_agent.AUTOGRAPH_DB = p["corpus_db"]
        for m in (ov, update_agent, reconciler):
            m.AUTOGRAPH_DB = p["corpus_db"]
        ov.CORPUS_PREFIX = p["corpus_prefix"]
        reconciler.CORPUS_PREFIX = p["corpus_prefix"]
        reconciler._CHUNK_CACHE["rows"] = None                # cache belongs to the old corpus

        # AQLizer is a per-deployment service too
        aql_backends.POD = p.get("aqlizer_pod") or ""
        aql_backends.URL = ""

        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps({"active": key}, indent=1), encoding="utf-8")

    notes = []
    # a fresh deployment may lack the review-state collections; creating them is
    # idempotent and stops the first read from returning 500
    try:
        update_agent.ensure_collections()
        import scheduler
        scheduler.ensure_collections()
    except Exception as e:                             # noqa: BLE001
        notes.append("state collections: %s" % e)
    if refresh_schema:
        import schema_live
        r = schema_live.refresh()
        if not r.get("ok"):
            notes.append("schema not refreshed: %s" % r.get("error"))
    return {"active": key, "profile": describe(key), "notes": notes}


def test(key):
    """Can each agent reach this profile's targets? Read-only, and it does NOT
    switch, so the modal can show both deployments' status side by side."""
    import requests
    p = PROFILES[key]
    url, user, pw = _creds(p)
    out = {"key": key}
    if not url or not pw:
        out["structured"] = out["corpus"] = {"ok": False, "detail": "no credentials in environment"}
        out["retriever"] = {"ok": False, "detail": "no credentials"}
        return out
    auth = (user, pw)

    def q(db, aql, binds=None):
        r = requests.post("%s/_db/%s/_api/cursor" % (url, requests.utils.quote(db, safe="")),
                          json={"query": aql, "bindVars": binds or {}}, auth=auth, timeout=15).json()
        if r.get("error"):
            raise RuntimeError(r.get("errorMessage"))
        return r["result"]

    try:
        n = q(p["structured_db"], "RETURN LENGTH(Platform)")[0]
        g = requests.get("%s/_db/%s/_api/gharial" % (url, p["structured_db"]), auth=auth, timeout=15).json()
        graphs = [x["_key"] for x in g.get("graphs", [])]
        ok = "aircraft_model" in graphs
        out["structured"] = {"ok": ok, "detail": "%d platforms, graph %s" % (n, "present" if ok else "MISSING")}
    except Exception as e:                             # noqa: BLE001
        out["structured"] = {"ok": False, "detail": "%s: %s" % (type(e).__name__, str(e)[:80])}

    try:
        n = q(p["corpus_db"], "RETURN LENGTH(@@c)", {"@c": p["corpus_prefix"] + "_sources"})[0]
        out["corpus"] = {"ok": n > 0, "detail": "%d source documents" % n}
    except Exception as e:                             # noqa: BLE001
        out["corpus"] = {"ok": False, "detail": "%s: %s" % (type(e).__name__, str(e)[:80])}

    ru = _retriever_url(p, url)
    if not ru:
        out["retriever"] = {"ok": False, "detail": "no retriever deployed for this corpus"}
    else:
        try:
            jwt = requests.post(url + "/_open/auth", json={"username": user, "password": pw}, timeout=15).json()["jwt"]
            r = requests.get(ru + "/v1/health", headers={"Authorization": "Bearer " + jwt}, timeout=20)
            out["retriever"] = {"ok": r.status_code == 200, "detail": "HTTP %d" % r.status_code}
        except Exception as e:                         # noqa: BLE001
            out["retriever"] = {"ok": False, "detail": "%s: %s" % (type(e).__name__, str(e)[:80])}
    out["aqlizer"] = {"ok": bool(p.get("aqlizer_pod")),
                      "detail": ("pod %s" % p["aqlizer_pod"]) if p.get("aqlizer_pod") else "not deployed"}
    return out

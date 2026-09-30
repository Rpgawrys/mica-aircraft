#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Thin ArangoDB client for the analyst app.

Two auth modes, because the deployment needs both:
  * Core API (/_api/cursor, /_api/gharial)  -> HTTP basic works
  * GenAI API (/gen-ai/v1/*)                -> requires a JWT bearer token

Contracts follow the ArangoDB Core API OpenAPI spec (3.12.9).
"""

import os
import time

import requests

URL = os.environ.get("ARANGO_CLOUD_URL", "http://localhost:8529").rstrip("/")
USER = os.environ.get("ARANGO_CLOUD_USER", "root")
PW = os.environ.get("ARANGO_CLOUD_PASSWORD", "")
STRUCTURED_DB = os.environ.get("STRUCTURED_DB_NAME", "aircraft_model")
GRAPH = "aircraft_model"

TIMEOUT = 60
_jwt = {"token": None, "at": 0}


class ArangoError(RuntimeError):
    pass


def _url(db, path):
    return "%s/_db/%s%s" % (URL, requests.utils.quote(db, safe=""), path)


def jwt():
    """Cached JWT for the GenAI API (basic auth is rejected there)."""
    if _jwt["token"] and time.time() - _jwt["at"] < 1800:
        return _jwt["token"]
    r = requests.post("%s/_open/auth" % URL,
                      json={"username": USER, "password": PW}, timeout=TIMEOUT)
    r.raise_for_status()
    _jwt["token"] = r.json()["jwt"]
    _jwt["at"] = time.time()
    return _jwt["token"]


def aql(query, bind_vars=None, db=None, limit_batch=200):
    """Execute AQL via POST /_api/cursor. Returns (rows, stats)."""
    db = db or STRUCTURED_DB
    payload = {"query": query, "bindVars": bind_vars or {},
               "batchSize": limit_batch, "count": True}
    r = requests.post(_url(db, "/_api/cursor"), json=payload,
                      auth=(USER, PW), timeout=TIMEOUT)
    body = r.json()
    if body.get("error"):
        raise ArangoError(body.get("errorMessage", "AQL error"))
    rows = body.get("result", [])
    # drain additional batches if the result set is larger than one batch
    cursor_id = body.get("id")
    while body.get("hasMore") and cursor_id:
        r2 = requests.post(_url(db, "/_api/cursor/%s" % cursor_id),
                           auth=(USER, PW), timeout=TIMEOUT)
        body = r2.json()
        if body.get("error"):
            break
        rows.extend(body.get("result", []))
    return rows, body.get("extra", {}).get("stats", {})


def explain(query, bind_vars=None, db=None):
    """Validate a query without running it - used to reject unsafe/invalid AQL."""
    db = db or STRUCTURED_DB
    r = requests.post(_url(db, "/_api/explain"),
                      json={"query": query, "bindVars": bind_vars or {}},
                      auth=(USER, PW), timeout=TIMEOUT)
    body = r.json()
    if body.get("error"):
        raise ArangoError(body.get("errorMessage", "explain failed"))
    return body


# ----------------------------------------------------------- graph mutation
def create_vertex(collection, doc, db=None):
    db = db or STRUCTURED_DB
    r = requests.post(
        _url(db, "/_api/gharial/%s/vertex/%s" % (GRAPH, collection)),
        json=doc, auth=(USER, PW), params={"returnNew": "true"}, timeout=TIMEOUT)
    body = r.json()
    if body.get("error"):
        raise ArangoError(body.get("errorMessage", "vertex create failed"))
    return body.get("new") or body.get("vertex")


def create_edge(collection, frm, to, attrs=None, db=None):
    db = db or STRUCTURED_DB
    doc = dict(attrs or {})
    doc["_from"], doc["_to"] = frm, to
    r = requests.post(
        _url(db, "/_api/gharial/%s/edge/%s" % (GRAPH, collection)),
        json=doc, auth=(USER, PW), params={"returnNew": "true"}, timeout=TIMEOUT)
    body = r.json()
    if body.get("error"):
        raise ArangoError(body.get("errorMessage", "edge create failed"))
    return body.get("new") or body.get("edge")


def update_vertex(collection, key, patch, db=None, keep_null=True):
    """keep_null=False makes a null value REMOVE the attribute (needed for undo)."""
    db = db or STRUCTURED_DB
    params = {"returnNew": "true", "returnOld": "true",
              "keepNull": "true" if keep_null else "false"}
    r = requests.patch(
        _url(db, "/_api/gharial/%s/vertex/%s/%s" % (GRAPH, collection, key)),
        json=patch, auth=(USER, PW),
        params=params, timeout=TIMEOUT)
    body = r.json()
    if body.get("error"):
        raise ArangoError(body.get("errorMessage", "vertex update failed"))
    return body


def delete_vertex(collection, key, db=None):
    db = db or STRUCTURED_DB
    r = requests.delete(
        _url(db, "/_api/gharial/%s/vertex/%s/%s" % (GRAPH, collection, key)),
        auth=(USER, PW), timeout=TIMEOUT)
    return not r.json().get("error", False)


def delete_edge(collection, key, db=None):
    db = db or STRUCTURED_DB
    r = requests.delete(
        _url(db, "/_api/gharial/%s/edge/%s/%s" % (GRAPH, collection, key)),
        auth=(USER, PW), timeout=TIMEOUT)
    return not r.json().get("error", False)


def platforms():
    rows, _ = aql("FOR p IN Platform SORT p.name RETURN "
                  "{key: p._key, name: p.name, manufacturer: p.manufacturer, "
                  " origin_country: p.origin_country, role: p.role, "
                  " generation: p.generation}")
    return rows


def ping():
    """Server reachable AND the structured database answers.

    /_api/version succeeds against any ArangoDB these credentials can reach,
    including a deployment that does not hold this model. A shell carrying a
    stale ARANGO_PROFILE resolution once made the app report healthy while
    every query returned 500. Check the database, not just the server.
    """
    try:
        r = requests.get("%s/_api/version" % URL, auth=(USER, PW), timeout=15)
        if r.status_code != 200:
            return False
        aql("RETURN 1")
        return True
    except Exception:                                  # noqa: BLE001
        return False


def host():
    """Hostname only, so /api/health can name the deployment without leaking
    credentials into a response the browser renders."""
    from urllib.parse import urlparse
    return urlparse(URL).hostname or URL

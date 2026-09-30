#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Autonomous change detection: run the update agent on a timer, unattended.

The manual button on the change-detection tab runs one platform on demand. This
runs every platform on a schedule and writes a dated report you can read later.

Two things keep it cheap and honest:

  * it reads the SAME ReviewedDocument watermark the manual run uses, so a
    scheduled sweep only ever looks at documents nobody has reviewed. A day with
    no new documents costs nothing, because the agent never reaches a model call.
  * it skips any platform whose backlog is empty before spending a token on it.

Proposals still land in the normal review queue. The report is a separate
record of what the sweep found, so you can read yesterday's findings without
reconstructing them from the queue.

State lives in two collections so a restart does not lose the schedule:
    AgentConfig   - one doc, key 'autorun': enabled, interval, last/next run
    ChangeReport  - one doc per sweep
"""

import datetime
import threading
import traceback

import arango_client as arango
import schema_live
import update_agent
from agents import reconciler

CONFIG = "AgentConfig"
REPORT = "ChangeReport"
KEY = "autorun"

# how many unreviewed documents one platform contributes to a single sweep
PER_PLATFORM = 6
TICK_SECONDS = 30

_lock = threading.Lock()
_running = False


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _iso(dt):
    return dt.isoformat()


def ensure_collections():
    import requests
    for name in (CONFIG, REPORT):
        requests.post("%s/_db/%s/_api/collection" % (arango.URL, arango.STRUCTURED_DB),
                      json={"name": name}, auth=(arango.USER, arango.PW), timeout=30)


LEVELS = ("high", "medium", "low")

DEFAULT = {"_key": KEY, "enabled": False, "interval_hours": 24,
           "last_run": None, "next_run": None, "last_error": None,
           # confidence levels the analyst has pre-approved. A proposal at an
           # enabled level is written to the graph by the agent, with no human
           # in the loop, stamped accepted_by=auto_agent so it stays auditable.
           "auto_accept": {"high": False, "medium": False, "low": False}}


def get_config():
    rows, _ = arango.aql("FOR d IN @@c FILTER d._key == @k RETURN d",
                         {"@c": CONFIG, "k": KEY})
    cfg = rows[0] if rows else dict(DEFAULT)
    aa = cfg.get("auto_accept") or {}
    cfg["auto_accept"] = {lv: bool(aa.get(lv)) for lv in LEVELS}
    cfg["running"] = _running
    return cfg


def set_config(enabled=None, interval_hours=None, auto_accept=None):
    cfg = get_config()
    if enabled is not None:
        cfg["enabled"] = bool(enabled)
    if interval_hours is not None:
        cfg["interval_hours"] = max(1, int(interval_hours))
    if auto_accept is not None:
        cur = cfg.get("auto_accept") or {}
        cfg["auto_accept"] = {lv: bool(auto_accept.get(lv, cur.get(lv)))
                              for lv in LEVELS}
    # turning it on schedules the first sweep a full interval out, so flipping
    # the switch during a demo does not immediately burn a sweep
    if cfg["enabled"]:
        if not cfg.get("next_run"):
            cfg["next_run"] = _iso(_now() + datetime.timedelta(
                hours=cfg["interval_hours"]))
    else:
        cfg["next_run"] = None
    doc = {k: cfg.get(k) for k in
           ("enabled", "interval_hours", "last_run", "next_run", "last_error",
            "auto_accept")}
    doc["_key"] = KEY
    arango.aql("UPSERT {_key: @k} INSERT @d UPDATE @d IN @@c",
               {"k": KEY, "d": doc, "@c": CONFIG})
    return get_config()


def _platforms_with_backlog():
    """Only platforms holding documents nobody has reviewed. Everything else is
    skipped before it can cost a model call."""
    out = []
    for p in arango.platforms():
        try:
            b = update_agent.backlog(p["name"])
        except Exception:                              # noqa: BLE001
            continue
        if b.get("not_reviewed", 0) > 0:
            out.append((p["name"], b["not_reviewed"]))
    return out


def _auto_accept(props, levels):
    """Apply the proposals whose confidence level the analyst pre-approved.

    Writes go through the same path a human Accept uses, so an auto-accepted
    change carries the same provenance stamp plus accepted_by=auto_agent.
    Anything at a level that is switched off is left in the queue untouched.
    """
    applied, failed = [], []
    for p in props:
        conf = str(p.get("confidence") or "medium").lower()
        if not levels.get(conf):
            continue
        if p.get("status") == "accepted":
            continue          # applying twice would break the undo chain
        try:
            res = reconciler.apply_proposal(p, actor="auto_agent")
            # nobody watched this write happen, so the undo token is persisted
            # rather than held in memory. A restart cannot strand the change.
            arango.aql("UPDATE @k WITH {status: 'accepted', undo_token: @u, "
                       "accepted_by: 'auto_agent'} IN UpdateProposal",
                       {"k": p["_key"], "u": res.get("undo")})
            applied.append({"title": p.get("title"), "confidence": conf,
                            "evidence": p.get("evidence"), "_key": p.get("_key")})
        except Exception as e:                         # noqa: BLE001
            failed.append({"title": p.get("title"), "confidence": conf,
                           "error": "%s: %s" % (type(e).__name__, e)})
    return applied, failed


def run_sweep(trigger="scheduled"):
    """One pass over every platform that has unreviewed documents."""
    global _running
    with _lock:
        if _running:
            return {"skipped": "a sweep is already running"}
        _running = True
    started = _now()
    findings, docs_read, n_props, n_auto = [], 0, 0, 0
    error = None
    levels = (get_config().get("auto_accept") or {})
    try:
        targets = _platforms_with_backlog()
        for name, backlog_n in targets:
            try:
                r = update_agent.run(name, limit=PER_PLATFORM)
            except Exception as e:                     # noqa: BLE001
                findings.append({"platform": name, "error":
                                 "%s: %s" % (type(e).__name__, e),
                                 "documents": [], "proposals": [], "log": []})
                continue
            props = r.get("proposals") or []
            read = r.get("checked") or []
            docs_read += len(read)
            n_props += len(props)
            applied, failed = _auto_accept(props, levels)
            n_auto += len(applied)
            auto_keys = {a["_key"] for a in applied}
            findings.append({
                "platform": name,
                "backlog_before": backlog_n,
                "documents": read,
                "proposals": [dict({k: p.get(k) for k in
                               ("_key", "title", "op", "collection", "confidence",
                                "evidence", "rationale")},
                               auto_accepted=p.get("_key") in auto_keys)
                              for p in props],
                "auto_accepted": applied,
                "auto_failed": failed,
                "log": r.get("log") or [],
            })
        report = {
            "generated_at": _iso(started),
            "finished_at": _iso(_now()),
            "trigger": trigger,
            "platforms_scanned": len(targets),
            "documents_read": docs_read,
            "proposal_count": n_props,
            "auto_accepted_count": n_auto,
            "auto_accept_levels": [lv for lv in LEVELS if levels.get(lv)],
            "findings": findings,
        }
        rows, _ = arango.aql("INSERT @d INTO @@c RETURN NEW",
                             {"d": report, "@c": REPORT})
        report = rows[0] if rows else report
    except Exception as e:                             # noqa: BLE001
        error = "%s: %s" % (type(e).__name__, e)
        traceback.print_exc()
        report = {"error": error}
    finally:
        with _lock:
            _running = False

    cfg = get_config()
    cfg["last_run"] = _iso(started)
    cfg["last_error"] = error
    if cfg.get("enabled"):
        cfg["next_run"] = _iso(_now() + datetime.timedelta(
            hours=cfg.get("interval_hours") or 24))
    doc = {k: cfg.get(k) for k in
           ("enabled", "interval_hours", "last_run", "next_run", "last_error",
            "auto_accept")}
    doc["_key"] = KEY
    arango.aql("UPSERT {_key: @k} INSERT @d UPDATE @d IN @@c",
               {"k": KEY, "d": doc, "@c": CONFIG})
    if n_auto:
        schema_live.refresh_async()
    return report


def revert(key):
    """Undo an auto-accepted change using the token stored when it was written."""
    rows, _ = arango.aql("FOR d IN UpdateProposal FILTER d._key == @k RETURN d",
                         {"k": key})
    if not rows:
        raise ValueError("no proposal %s" % key)
    token = rows[0].get("undo_token")
    if not token:
        raise ValueError("no undo token stored for %s" % key)
    reconciler.undo(token)
    arango.aql("UPDATE @k WITH {status: 'pending', undo_token: null} "
               "IN UpdateProposal", {"k": key})
    schema_live.refresh_async()
    return {"status": "reverted", "key": key}


def reports(limit=20):
    rows, _ = arango.aql(
        "FOR d IN @@c SORT d.generated_at DESC LIMIT @n "
        "RETURN {_key: d._key, generated_at: d.generated_at, trigger: d.trigger, "
        "platforms_scanned: d.platforms_scanned, documents_read: d.documents_read, "
        "proposal_count: d.proposal_count, auto_accepted_count: d.auto_accepted_count}",
        {"@c": REPORT, "n": int(limit)})
    return rows


def report(key):
    rows, _ = arango.aql("FOR d IN @@c FILTER d._key == @k RETURN d",
                         {"@c": REPORT, "k": key})
    return rows[0] if rows else None


def _loop():
    import time
    while True:
        time.sleep(TICK_SECONDS)
        try:
            cfg = get_config()
            if not cfg.get("enabled") or _running:
                continue
            nxt = cfg.get("next_run")
            if nxt and _now() >= datetime.datetime.fromisoformat(nxt):
                run_sweep("scheduled")
        except Exception:                              # noqa: BLE001
            traceback.print_exc()


def start():
    ensure_collections()
    t = threading.Thread(target=_loop, name="autorun", daemon=True)
    t.start()
    return t


if __name__ == "__main__":
    ensure_collections()
    print("config:", get_config())
    print("backlog platforms:", _platforms_with_backlog())

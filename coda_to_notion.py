#!/usr/bin/env python3
"""
coda_to_notion.py
=================

Migrate one Coda (Superhuman Docs) doc's tables into Notion databases via
API-to-API transfer, preserving column types and (crucially) inter-table
relations that a CSV export flattens into plain text.

Targets the Notion API version 2025-09-03, in which a "database" is a container
and the actual table of records is a "data source". Pages are created under a
`data_source_id` parent, and relation properties point at data source IDs.

Two-pass design (single run):
  Pass 1  Discover tables/columns/rows in Coda -> create a Notion database per
          table with scalar properties -> create one page per row, recording a
          coda_row_id -> notion_page_id map.
  Pass 2  For each Coda lookup column, add a relation property to the source
          data source pointing at the target data source, then patch each page
          to set the relation links.

Resumable: progress is saved to migration_state_<doc_id>.json as it goes. If a
run is interrupted (crash, timeout, Ctrl+C), just run it again with the same
CODA_DOC_ID and it picks up where it left off -- completed tables are skipped,
partially-inserted tables continue from the last saved row, and databases are
reused rather than recreated. Use --restart to discard saved progress and begin
the doc from scratch (note: this does NOT delete databases already created in
Notion, so remove those manually first to avoid duplicates).

Credentials and targets are read from environment variables (a .env file is
supported). Copy .env.example to .env and fill it in:

  CODA_API_TOKEN         Coda / Superhuman Docs API token
  NOTION_API_TOKEN       Notion internal integration secret
  CODA_DOC_ID            Doc id (the part after "_d" in the doc URL)
  NOTION_PARENT_PAGE_ID  Notion page the databases are created under; the
                         integration must be connected to this page

Run:
  python coda_to_notion.py
  python coda_to_notion.py --restart

Requirements:  Python 3.9+, `pip install -r requirements.txt`
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import logging
from dataclasses import dataclass, field
from typing import Any, Iterable

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("coda2notion")

CODA_BASE = "https://coda.io/apis/v1"
NOTION_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2025-09-03"

# Save progress to disk every N rows so a crash loses at most this many inserts.
SAVE_EVERY = 25

# Notion caps rich_text content at 2000 chars per text object.
TEXT_LIMIT = 2000
# Practical ceiling on select options we will pre-declare per column.
MAX_SELECT_OPTIONS = 100


# --------------------------------------------------------------------------- #
# HTTP plumbing: throttle + retry                                             #
# --------------------------------------------------------------------------- #

class Throttle:
    """Simple minimum-interval throttle to respect per-service rate limits."""

    def __init__(self, min_interval: float) -> None:
        self.min_interval = min_interval
        self._last = 0.0

    def wait(self) -> None:
        delta = time.monotonic() - self._last
        if delta < self.min_interval:
            time.sleep(self.min_interval - delta)
        self._last = time.monotonic()


def _request(
    session: requests.Session,
    method: str,
    url: str,
    throttle: Throttle,
    *,
    max_retries: int = 6,
    **kwargs: Any,
) -> requests.Response:
    """Issue a request with throttling and backoff on 429/5xx and network errors."""
    kwargs.setdefault("timeout", 90)
    for attempt in range(max_retries):
        throttle.wait()
        try:
            resp = session.request(method, url, **kwargs)
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
            # Network hiccups (read timeouts, dropped connections) are transient;
            # back off and retry rather than crashing a long migration.
            if attempt == max_retries - 1:
                raise
            delay = min(2 ** attempt, 30)
            log.warning(
                "%s %s -> network error (%s); retrying in %.1fs (attempt %d/%d)",
                method, url, type(exc).__name__, delay, attempt + 1, max_retries,
            )
            time.sleep(delay)
            continue
        if resp.status_code < 400:
            return resp
        if resp.status_code == 429 or resp.status_code >= 500:
            retry_after = resp.headers.get("Retry-After")
            delay = float(retry_after) if retry_after else min(2 ** attempt, 30)
            log.warning(
                "%s %s -> %s; retrying in %.1fs (attempt %d/%d)",
                method, url, resp.status_code, delay, attempt + 1, max_retries,
            )
            time.sleep(delay)
            continue
        # Non-retryable: surface the body to help debugging.
        raise RuntimeError(f"{method} {url} failed {resp.status_code}: {resp.text}")
    raise RuntimeError(f"{method} {url} exhausted retries")


# --------------------------------------------------------------------------- #
# Coda client                                                                 #
# --------------------------------------------------------------------------- #

class CodaClient:
    def __init__(self, token: str) -> None:
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {token}"})
        self.throttle = Throttle(0.12)  # generous; Coda allows more than this

    def _get(self, path: str, params: dict | None = None) -> dict:
        url = f"{CODA_BASE}{path}"
        return _request(self.session, "GET", url, self.throttle, params=params).json()

    def _paged(self, path: str, params: dict | None = None) -> Iterable[dict]:
        params = dict(params or {})
        while True:
            data = self._get(path, params)
            for item in data.get("items", []):
                yield item
            token = data.get("nextPageToken")
            if not token:
                break
            params = {"pageToken": token}

    def list_tables(self, doc_id: str) -> list[dict]:
        return list(self._paged(f"/docs/{doc_id}/tables", {"tableTypes": "table"}))

    def get_table(self, doc_id: str, table_id: str) -> dict:
        return self._get(f"/docs/{doc_id}/tables/{table_id}")

    def list_columns(self, doc_id: str, table_id: str) -> list[dict]:
        return list(self._paged(f"/docs/{doc_id}/tables/{table_id}/columns"))

    def list_rows(self, doc_id: str, table_id: str) -> list[dict]:
        # Rich values expose row references (rowId/tableId) needed for relations.
        params = {"valueFormat": "rich", "limit": 200}
        return list(self._paged(f"/docs/{doc_id}/tables/{table_id}/rows", params))


# --------------------------------------------------------------------------- #
# Notion client                                                               #
# --------------------------------------------------------------------------- #

class NotionClient:
    def __init__(self, token: str) -> None:
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        })
        self.throttle = Throttle(0.34)  # ~3 requests/second

    def _post(self, path: str, body: dict) -> dict:
        url = f"{NOTION_BASE}{path}"
        return _request(self.session, "POST", url, self.throttle, json=body).json()

    def _patch(self, path: str, body: dict) -> dict:
        url = f"{NOTION_BASE}{path}"
        return _request(self.session, "PATCH", url, self.throttle, json=body).json()

    def _get(self, path: str) -> dict:
        url = f"{NOTION_BASE}{path}"
        return _request(self.session, "GET", url, self.throttle).json()

    def create_database(
        self, parent_page_id: str, title: str, properties: dict
    ) -> tuple[str, str]:
        """Create a database + initial data source. Returns (db_id, data_source_id)."""
        body = {
            "parent": {"type": "page_id", "page_id": parent_page_id},
            "title": [{"type": "text", "text": {"content": title[:TEXT_LIMIT]}}],
            "initial_data_source": {"properties": properties},
        }
        resp = self._post("/databases", body)
        db_id = resp["id"]
        sources = resp.get("data_sources") or []
        if sources:
            return db_id, sources[0]["id"]
        # Fallback: fetch the database to read its data source id.
        got = self._get(f"/databases/{db_id}")
        return db_id, got["data_sources"][0]["id"]

    def create_page(self, data_source_id: str, properties: dict) -> str:
        body = {
            "parent": {"type": "data_source_id", "data_source_id": data_source_id},
            "properties": properties,
        }
        return self._post("/pages", body)["id"]

    def add_relation_property(
        self, data_source_id: str, prop_name: str, target_data_source_id: str
    ) -> None:
        body = {
            "properties": {
                prop_name: {
                    "type": "relation",
                    "relation": {
                        "data_source_id": target_data_source_id,
                        "single_property": {},
                    },
                }
            }
        }
        self._patch(f"/data_sources/{data_source_id}", body)

    def set_page_relation(
        self, page_id: str, prop_name: str, target_page_ids: list[str]
    ) -> None:
        body = {"properties": {prop_name: {"relation": [{"id": p} for p in target_page_ids]}}}
        self._patch(f"/pages/{page_id}", body)


# --------------------------------------------------------------------------- #
# Value flattening / reference extraction                                     #
# --------------------------------------------------------------------------- #

def flatten(value: Any) -> Any:
    """Reduce a Coda rich value to a scalar (or list of scalars) for display."""
    if isinstance(value, list):
        return [flatten(v) for v in value]
    if isinstance(value, dict):
        # Row reference or other structured value.
        if "name" in value:
            return value["name"]
        if "amount" in value:
            return value["amount"]
        if "url" in value:
            return value["url"]
        return json.dumps(value, ensure_ascii=False)
    return value


def extract_refs(value: Any) -> list[dict]:
    """Pull row-reference dicts (carrying rowId + tableId) out of a rich value."""
    refs: list[dict] = []

    def walk(v: Any) -> None:
        if isinstance(v, list):
            for item in v:
                walk(item)
        elif isinstance(v, dict):
            if v.get("rowId") and v.get("tableId"):
                refs.append({"rowId": v["rowId"], "tableId": v["tableId"]})

    walk(value)
    return refs


def to_text(value: Any) -> str:
    if isinstance(value, list):
        return ", ".join(str(flatten(v)) for v in value if v not in (None, ""))
    return "" if value is None else str(value)


def sanitize_option(name: str) -> str:
    # Notion select/multi_select option names may not contain commas.
    return name.replace(",", " ").strip()[:TEXT_LIMIT]


# --------------------------------------------------------------------------- #
# Coda column type -> Notion property mapping                                  #
# --------------------------------------------------------------------------- #

# Coda format.type -> a coarse category we know how to build.
SCALAR_MAP = {
    "text": "rich_text",
    "email": "email",
    "link": "url",
    "phoneNumber": "phone_number",
    "number": "number",
    "percent": "number",
    "currency": "number",
    "slider": "number",
    "scale": "number",
    "duration": "rich_text",
    "date": "date",
    "dateTime": "date",
    "time": "rich_text",
    "checkbox": "checkbox",
    "person": "rich_text",     # Notion people needs real user ids; store names
    "image": "url",
    "attachments": "rich_text",
    "select": "select",        # may be promoted to multi_select after scanning
}
LOOKUP_TYPES = {"lookup"}
SKIP_TYPES = {"canvas", "button", "reaction"}  # no clean Notion equivalent


@dataclass
class ColumnPlan:
    coda_id: str
    coda_type: str
    name: str
    notion_kind: str                       # rich_text/number/date/select/multi_select/relation/...
    options: set[str] = field(default_factory=set)
    is_title: bool = False


def notion_property_def(col: ColumnPlan) -> dict:
    kind = col.notion_kind
    if col.is_title:
        return {"title": {}}
    if kind == "number":
        fmt = {"currency": "dollar", "percent": "percent"}.get(col.coda_type, "number")
        return {"number": {"format": fmt}}
    if kind in ("select", "multi_select"):
        opts = [{"name": o} for o in sorted(col.options)][:MAX_SELECT_OPTIONS]
        return {kind: {"options": opts}}
    if kind in ("email", "url", "phone_number", "checkbox", "date", "rich_text"):
        return {kind: {}}
    return {"rich_text": {}}


def notion_property_value(col: ColumnPlan, raw: Any) -> dict | None:
    kind = "title" if col.is_title else col.notion_kind
    flat = flatten(raw)

    if kind == "title":
        return {"title": [{"text": {"content": to_text(flat)[:TEXT_LIMIT]}}]}
    if kind == "rich_text":
        text = to_text(flat)
        if not text:
            return None
        return {"rich_text": [{"text": {"content": text[:TEXT_LIMIT]}}]}
    if kind == "number":
        try:
            s = str(flat).replace("$", "").replace("%", "").replace(",", "").strip()
            return {"number": float(s)} if s not in ("", "None") else None
        except (TypeError, ValueError):
            return None
    if kind == "checkbox":
        return {"checkbox": bool(flat) and str(flat).lower() not in ("false", "0", "")}
    if kind == "date":
        s = to_text(flat).strip()
        if not s:
            return None
        start = s.split(" - ")[0].strip()  # take start of a range, if any
        return {"date": {"start": start}}
    if kind == "select":
        name = sanitize_option(to_text(flat))
        return {"select": {"name": name}} if name else None
    if kind == "multi_select":
        values = flat if isinstance(flat, list) else [flat]
        names = [sanitize_option(str(v)) for v in values if str(v).strip()]
        names = [n for n in names if n]
        return {"multi_select": [{"name": n} for n in names]} if names else None
    if kind in ("email", "url", "phone_number"):
        text = to_text(flat).strip()
        return {kind: text} if text else None
    return None


# --------------------------------------------------------------------------- #
# Planning                                                                     #
# --------------------------------------------------------------------------- #

def build_column_plans(
    columns: list[dict], rows: list[dict], display_column_id: str | None
) -> tuple[list[ColumnPlan], list[ColumnPlan]]:
    """Return (scalar_plans, relation_plans). Names are de-duplicated."""
    scalar_plans: list[ColumnPlan] = []
    relation_plans: list[ColumnPlan] = []
    used_names: set[str] = set()

    def unique(name: str) -> str:
        base = (name or "Untitled").strip()[:TEXT_LIMIT] or "Untitled"
        candidate, i = base, 2
        while candidate in used_names:
            candidate = f"{base} ({i})"
            i += 1
        used_names.add(candidate)
        return candidate

    # Ensure exactly one title. Prefer the display column; else the first column.
    title_id = display_column_id or (columns[0]["id"] if columns else None)

    for col in columns:
        cid, ctype = col["id"], (col.get("format", {}) or {}).get("type", "text")
        name = unique(col.get("name", "Untitled"))

        if ctype in SKIP_TYPES:
            log.info("  skipping column %r (type %s has no Notion equivalent)", name, ctype)
            continue

        if ctype in LOOKUP_TYPES:
            relation_plans.append(ColumnPlan(cid, ctype, name, "relation"))
            continue

        kind = SCALAR_MAP.get(ctype, "rich_text")
        plan = ColumnPlan(cid, ctype, name, kind, is_title=(cid == title_id))

        # Decide single vs multi select, and collect option names, by scanning data.
        if kind == "select":
            saw_list = False
            for row in rows:
                val = flatten(row.get("values", {}).get(cid))
                if isinstance(val, list):
                    saw_list = True
                    for v in val:
                        if str(v).strip():
                            plan.options.add(sanitize_option(str(v)))
                elif val not in (None, ""):
                    plan.options.add(sanitize_option(str(val)))
            plan.notion_kind = "multi_select" if saw_list else "select"

        scalar_plans.append(plan)

    # Guarantee a title exists even if the display column was a lookup/skipped one.
    if not any(p.is_title for p in scalar_plans) and scalar_plans:
        scalar_plans[0].is_title = True
        scalar_plans[0].notion_kind = "rich_text"  # title config ignores this anyway

    return scalar_plans, relation_plans


# --------------------------------------------------------------------------- #
# Config + resumable state                                                    #
# --------------------------------------------------------------------------- #

def _split_names(raw: str | None) -> list[str]:
    """Parse a comma-separated list of table names into a clean list."""
    if not raw:
        return []
    return [name.strip() for name in raw.split(",") if name.strip()]


def load_config() -> dict:
    """Read required settings from the environment (optionally via a .env file)."""
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    cfg = {
        "coda_token": os.environ.get("CODA_API_TOKEN"),
        "notion_token": os.environ.get("NOTION_API_TOKEN"),
        "doc_id": os.environ.get("CODA_DOC_ID"),
        "parent_page": os.environ.get("NOTION_PARENT_PAGE_ID"),
        # Optional table filters (comma-separated table names). Command-line
        # --skip / --only override these when provided.
        "skip_tables": _split_names(os.environ.get("CODA_SKIP_TABLES")),
        "only_tables": _split_names(os.environ.get("CODA_ONLY_TABLES")),
    }
    missing = [name for name, key in (
        ("CODA_API_TOKEN", "coda_token"),
        ("NOTION_API_TOKEN", "notion_token"),
        ("CODA_DOC_ID", "doc_id"),
        ("NOTION_PARENT_PAGE_ID", "parent_page"),
    ) if not cfg[key]]
    if missing:
        log.error("Missing required environment variable(s): %s", ", ".join(missing))
        log.error("Set them in your shell or a .env file (see .env.example).")
        sys.exit(1)
    return cfg


def state_path_for(doc_id: str) -> str:
    """One state file per doc, so migrating different docs never collide."""
    safe = "".join(c if (c.isalnum() or c in "-_") else "_" for c in doc_id)
    return f"migration_state_{safe}.json"


def load_state(path: str, doc_id: str, parent_page: str) -> dict:
    """Load saved progress for this doc, or start a fresh state object."""
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            state = json.load(fh)
        if state.get("doc_id") == doc_id:
            state.setdefault("tables", {})
            done = sum(1 for r in state["tables"].values() if r.get("complete"))
            log.info("Resuming from %s: %d table(s) already complete", path, done)
            return state
        log.warning("State file %s is for a different doc; ignoring it.", path)
    return {"doc_id": doc_id, "parent_page": parent_page, "tables": {}}


def save_state(path: str, state: dict) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# Migration driver                                                            #
# --------------------------------------------------------------------------- #

def migrate(
    restart: bool = False,
    skip_tables: list[str] | None = None,
    only_tables: list[str] | None = None,
) -> None:
    cfg = load_config()
    coda_token = cfg["coda_token"]
    notion_token = cfg["notion_token"]
    doc_id = cfg["doc_id"]
    parent_page = cfg["parent_page"]

    # Command-line filters win over env-var filters; fall back to env, then none.
    skip = set(skip_tables if skip_tables is not None else cfg["skip_tables"])
    only = set(only_tables if only_tables is not None else cfg["only_tables"])
    if skip:
        log.info("Skipping tables: %s", ", ".join(sorted(skip)))
    if only:
        log.info("Migrating only tables: %s", ", ".join(sorted(only)))

    coda = CodaClient(coda_token)
    notion = NotionClient(notion_token)

    state_path = state_path_for(doc_id)
    if restart and os.path.exists(state_path):
        log.warning(
            "--restart: discarding saved progress in %s. Databases already "
            "created in Notion are NOT removed; delete them manually to avoid "
            "duplicates.", state_path,
        )
        os.remove(state_path)

    state = load_state(state_path, doc_id, parent_page)
    tables_state: dict[str, dict] = state["tables"]

    tables = coda.list_tables(doc_id)
    log.info("Found %d table(s) in Coda doc %s", len(tables), doc_id)

    # -------- Pass 1: schema + rows -------- #
    for tbl in tables:
        tid, tname = tbl["id"], tbl.get("name", tbl["id"])

        # Apply table-name filters. `only` (if set) is an allow-list; `skip` is
        # a deny-list. A table already recorded in state is left as-is.
        if only and tname not in only and tid not in tables_state:
            log.info("Skipping table %r: not in --only list", tname)
            continue
        if tname in skip and tid not in tables_state:
            log.info("Skipping table %r: in skip list", tname)
            continue

        rec = tables_state.get(tid)

        if rec and rec.get("complete"):
            log.info("Skipping table %r (%s): already migrated, %d rows",
                     tname, tid, len(rec["row_map"]))
            continue

        log.info("Reading table %r (%s)", tname, tid)
        detail = coda.get_table(doc_id, tid)
        display_column_id = (detail.get("displayColumn") or {}).get("id")
        columns = coda.list_columns(doc_id, tid)
        rows = coda.list_rows(doc_id, tid)
        log.info("  %d columns, %d rows", len(columns), len(rows))

        scalar_plans, relation_plans = build_column_plans(columns, rows, display_column_id)

        if rec and rec.get("notion_data_source_id"):
            # Resume a partially-inserted table: reuse its database and row map.
            ds_id = rec["notion_data_source_id"]
            row_map = rec["row_map"]
            log.info("  resuming into existing database %s (%d/%d rows already done)",
                     rec["notion_database_id"], len(row_map), len(rows))
        else:
            properties = {p.name: notion_property_def(p) for p in scalar_plans}
            db_id, ds_id = notion.create_database(parent_page, tname, properties)
            row_map = {}
            log.info("  created Notion database %s (data source %s)", db_id, ds_id)
            rec = {
                "coda_table_id": tid,
                "coda_table_name": tname,
                "notion_database_id": db_id,
                "notion_data_source_id": ds_id,
                "row_map": row_map,
                "relations": [
                    {"name": rp.name, "coda_column_id": rp.coda_id} for rp in relation_plans
                ],
                "complete": False,
                "relations_wired": False,
            }
            tables_state[tid] = rec
            save_state(state_path, state)  # record the db before inserting rows

        inserted = 0
        for row in rows:
            if row["id"] in row_map:
                continue  # already inserted on a prior run
            props: dict[str, Any] = {}
            for p in scalar_plans:
                built = notion_property_value(p, row.get("values", {}).get(p.coda_id))
                if built is not None:
                    props[p.name] = built
            # Guarantee the title is populated (Notion requires a title value).
            title_plan = next((p for p in scalar_plans if p.is_title), None)
            if title_plan and title_plan.name not in props:
                props[title_plan.name] = {
                    "title": [{"text": {"content": to_text(row.get("name", ""))[:TEXT_LIMIT]}}]
                }
            page_id = notion.create_page(ds_id, props)
            row_map[row["id"]] = page_id
            inserted += 1
            if inserted % SAVE_EVERY == 0:
                save_state(state_path, state)

        rec["complete"] = True
        save_state(state_path, state)
        log.info("  inserted %d new page(s); %d rows total", inserted, len(row_map))

    # -------- Pass 2: relations -------- #
    # Build a Coda-table-id -> Notion-data-source-id index for resolving targets.
    ds_by_coda_table = {tid: rec["notion_data_source_id"] for tid, rec in tables_state.items()}

    for tid, rec in tables_state.items():
        if not rec["relations"]:
            rec["relations_wired"] = True
            continue
        if rec.get("relations_wired"):
            log.info("Skipping relations for %r: already wired", rec["coda_table_name"])
            continue

        log.info("Wiring relations for table %r", rec["coda_table_name"])
        rows = coda.list_rows(doc_id, tid)  # re-read to get rich reference values
        rows_by_id = {r["id"]: r for r in rows}

        for rel in rec["relations"]:
            col_id, prop_name = rel["coda_column_id"], rel["name"]

            # Determine the target table from the first resolvable reference.
            target_coda_table = None
            for row in rows:
                refs = extract_refs(row.get("values", {}).get(col_id))
                if refs:
                    target_coda_table = refs[0]["tableId"]
                    break
            if target_coda_table is None:
                log.info("  %r: no references found; leaving empty", prop_name)
                continue
            target_ds = ds_by_coda_table.get(target_coda_table)
            if target_ds is None:
                log.warning(
                    "  %r references table %s which was not migrated; skipping",
                    prop_name, target_coda_table,
                )
                continue

            notion.add_relation_property(rec["notion_data_source_id"], prop_name, target_ds)

            target_row_map = tables_state[target_coda_table]["row_map"]
            wired = 0
            for coda_row_id, page_id in rec["row_map"].items():
                row = rows_by_id.get(coda_row_id)
                if not row:
                    continue
                refs = extract_refs(row.get("values", {}).get(col_id))
                target_pages = [
                    target_row_map[r["rowId"]]
                    for r in refs
                    if r["rowId"] in target_row_map
                ]
                if target_pages:
                    notion.set_page_relation(page_id, prop_name, target_pages)
                    wired += 1
            log.info("  %r -> %s: linked %d rows", prop_name, target_coda_table, wired)

        rec["relations_wired"] = True
        save_state(state_path, state)

    save_state(state_path, state)
    log.info("Done. State saved to %s", state_path)
    print("\nSummary")
    for rec in tables_state.values():
        print(
            f"  {rec['coda_table_name']}: {len(rec['row_map'])} rows, "
            f"{len(rec['relations'])} relation column(s) -> database {rec['notion_database_id']}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate a Coda doc's tables into Notion.")
    parser.add_argument(
        "--restart",
        action="store_true",
        help="Discard saved progress for this doc and start over. Does not "
             "delete databases already created in Notion.",
    )
    parser.add_argument(
        "--skip",
        metavar="NAMES",
        help="Comma-separated table names to exclude (deny-list). Overrides "
             "CODA_SKIP_TABLES.",
    )
    parser.add_argument(
        "--only",
        metavar="NAMES",
        help="Comma-separated table names to migrate exclusively (allow-list). "
             "Overrides CODA_ONLY_TABLES.",
    )
    args = parser.parse_args()
    # None means "not provided on the command line" so env vars can apply;
    # an explicit flag (even empty) takes precedence.
    skip = _split_names(args.skip) if args.skip is not None else None
    only = _split_names(args.only) if args.only is not None else None
    try:
        migrate(restart=args.restart, skip_tables=skip, only_tables=only)
    except KeyboardInterrupt:
        log.warning("Interrupted. Progress was saved; re-run to resume.")


if __name__ == "__main__":
    main()

# coda-to-notion

Migrate a [Coda](https://coda.io) (now Superhuman Docs) doc's tables into
[Notion](https://notion.so) databases over the API, preserving column types and
rebuilding the inter-table relations that a CSV export flattens into plain text.

A plain CSV round-trip loses relations, formulas, and property types: every
column arrives in Notion as text and cross-table links become bare strings. This
tool reads the Coda schema and rows directly, maps each column to the closest
Notion property type, and runs a second pass that reconstructs relation columns
as live Notion relations.

## How it works

The migration runs in two passes in a single command:

1. **Schema + rows.** For each Coda table it reads the columns, the display
   column, and all rows (paginated), creates one Notion database with the scalar
   properties, and inserts a page per row — recording a
   `coda_row_id -> notion_page_id` map.
2. **Relations.** For each Coda lookup column it adds a relation property to the
   source data source pointing at the target data source, then patches each page
   to set the actual links, resolving Coda row references to the Notion pages
   created in pass 1.

Progress is checkpointed to `migration_state_<doc_id>.json` as it runs, so an
interrupted migration can be resumed rather than restarted (see below).

This targets **Notion API version `2025-09-03`**, in which a "database" is a
container and the table of records is a "data source". Pages are created under a
`data_source_id` parent and relations point at data source IDs.

## Requirements

- Python 3.9+
- `pip install -r requirements.txt` (`requests`; `python-dotenv` is optional)

## Setup

1. **Get a Coda API token** from your Coda / Superhuman Docs account settings
   (API section).
2. **Create a Notion internal integration** at
   [https://www.notion.so/my-integrations](https://www.notion.so/my-integrations) and copy its secret.
3. **Connect the integration to a parent page.** Open the Notion page you want
   the databases created under, then `•••` -\> **Connections** -\> select your
   integration. This step is required: without it, the API returns
   `object_not_found` even with a correct page ID. Tip: confirm the integration
   can see the page by searching first —

```bash
curl -s -X POST "https://api.notion.com/v1/search" \
  -H "Authorization: Bearer $NOTION_API_TOKEN" \
  -H "Notion-Version: 2025-09-03" \
  -H "Content-Type: application/json" -d '{}' | python3 -m json.tool
```

   If the page appears in the results, its ID will work as the parent.
4. **Find the two IDs.** The Coda doc ID is the part after `_d` in a doc URL
   (`.../d/MyDoc_dAbCdEf123` -\> `AbCdEf123`). The Notion parent page ID is the
   32-character string at the end of the page URL.

## Configuration

Copy `.env.example` to `.env` and fill in the four values:

```
CODA_API_TOKEN=...
NOTION_API_TOKEN=ntn_...
CODA_DOC_ID=...
NOTION_PARENT_PAGE_ID=...
```

With `python-dotenv` installed, the script loads `.env` automatically. Otherwise
export the same four variables in your shell before running. \*\*`.env` is
gitignored — never commit real tokens.\*\*

## Run

```bash
python coda_to_notion.py
```

The log reports each table created, rows inserted, and relations linked, ending
with a summary.

**Resuming.** If a run is interrupted — a crash, a network timeout, or `Ctrl+C` —
just run the same command again. Completed tables are skipped, a partially
inserted table continues from the last checkpoint, and databases already created
are reused rather than duplicated. Use `--restart` to discard saved progress and
begin the doc from scratch (this does not delete databases already created in
Notion; remove those manually first to avoid duplicates).

**Filtering tables.** By default every base table in the doc is migrated. To
exclude specific tables (for example, large synced reference tables you do not
need), use a deny-list; to migrate only a named few, use an allow-list:

```bash
python coda_to_notion.py --skip "Emojis Table, Colors"
python coda_to_notion.py --only "Projects, Workspaces"
```

The same lists can be set persistently via `CODA_SKIP_TABLES` / `CODA_ONLY_TABLES`
in `.env`; command-line flags override them.

## What is preserved, and what is not

Preserved: text, numbers (with currency/percent formatting), dates, checkboxes,
email/URL/phone, single- and multi-select (options detected from the data), and
lookup columns rebuilt as Notion relations.

Lossy or skipped, by design:

- **Person** columns become plain text of the name, because Notion's people
  property needs real workspace user IDs that will not match across tools.
- **Attachments / images** become a URL or text rather than uploaded files;
  Coda file URLs are often signed and expire.
- **Canvas** columns (a row's sub-page body) are skipped — no clean property
  equivalent.
- **Button / reaction** columns are skipped.
- Lookups pointing at tables outside the migrated doc (e.g. Coda sync
  connections) are logged and left unlinked rather than guessed at.

## Notes and limits

- Notion's API is rate-limited to roughly 3 requests/second, so large docs take
  a while: budget on the order of one request per row created plus one per
  relation set. The script throttles and backs off automatically.
- Resume assumes the databases recorded in the state file still exist in Notion.
  If you manually delete a database the state considers complete, use `--restart`
  (and clear the corresponding Notion databases) rather than resuming.

## Engineering notes

A few design decisions worth calling out, since they are the parts that separate
this from a throwaway export script.

**Relational integrity is the whole point.** A CSV round-trip is trivial but
lossy: it flattens every cross-table link into a bare string. Preserving those
links requires a two-pass approach, because a relation can only be created once
*both* sides exist as real records with stable IDs. Pass 1 creates every
database and row and records a `coda_row_id -> notion_page_id` map; pass 2 walks
the lookup columns and resolves each Coda row reference to the Notion page
created in pass 1. Relations that point outside the migrated set (Coda sync
connections, for instance) are detected and left unlinked rather than guessed at.

**Built against a live API-model change.** Notion's `2025-09-03` API version
restructured the data model: a "database" became a container holding one or more
"data sources," and record creation, schema edits, and relation targets all moved
to `data_source_id` rather than `database_id`. The tool targets that model
directly — creating databases with an `initial_data_source`, parenting pages
under a data source, and pointing relations at data source IDs — rather than the
older, now-superseded shape.

**Idempotent, resumable, and rate-aware.** Real migrations of thousands of rows
run long enough that failures are a certainty, not an edge case. Progress is
checkpointed to disk per doc, so any interruption resumes from the last saved
row instead of restarting; already-created databases are reused, and rows are
matched by source ID so nothing is duplicated. Every request is throttled to the
provider's limit and retried with exponential backoff — covering not just
`429`/`5xx` responses but also read timeouts, dropped connections, and a
transient `400` Notion returns while a freshly created database settles.

**Type mapping with explicit tradeoffs.** Each Coda column type is mapped to the
closest Notion property, and single- versus multi-select is inferred from the
data rather than assumed. Where no faithful mapping exists, the loss is
deliberate and documented rather than silent: person columns degrade to names
(Notion's people type needs matching workspace user IDs), signed attachment URLs
are not treated as durable files, and canvas/button columns are skipped. The
guiding rule is to never fabricate data to fill a type that cannot be honestly
populated.

**Configuration and secrets.** Credentials and targets are read from the
environment (via an optional `.env`), never hardcoded, and `.env` is gitignored.
Table selection is configurable so large or reference-heavy docs can be migrated
as a chosen subset rather than all-or-nothing.

## License

MIT — see [LICENSE](LICENSE).

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

State is written to `migration_state.json` for inspection and partial re-runs.

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
   <https://www.notion.so/my-integrations> and copy its secret.
3. **Connect the integration to a parent page.** Open the Notion page you want
   the databases created under, then `•••` -> **Connections** -> select your
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
   (`.../d/MyDoc_dAbCdEf123` -> `AbCdEf123`). The Notion parent page ID is the
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
export the same four variables in your shell before running. **`.env` is
gitignored — never commit real tokens.**

## Run

```bash
python coda_to_notion.py
```

The log reports each table created, rows inserted, and relations linked, ending
with a summary. Re-running as written creates fresh databases rather than
updating existing ones.

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
  relation set. The script throttles and backs off on 429/5xx automatically.
- The tool migrates every base table in the doc. For very large or very
  many-table docs you will likely want to migrate a subset instead.

## License

MIT — see [LICENSE](LICENSE).

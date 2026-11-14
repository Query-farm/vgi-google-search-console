<p align="center">
  <a href="https://query.farm/vgi/">
    <img src="https://raw.githubusercontent.com/Query-farm/vgi-google-search-console/main/docs/vgi-logo.png" alt="Vector Gateway Interface logo" width="320">
  </a>
</p>

<h1 align="center">vgi-google-search-console</h1>

<p align="center">
  <a href="https://developers.google.com/webmaster-tools">Google Search Console</a> search performance, sitemaps and
  URL index status as ordinary DuckDB tables.<br>
  A <strong>read-only</strong> <a href="https://query.farm/vgi/">VGI</a> worker, built by <a href="https://query.farm">🚜 Query.Farm</a>
</p>

<p align="center">
  <a href="https://github.com/Query-farm/vgi-google-search-console/actions/workflows/ci.yml"><img src="https://github.com/Query-farm/vgi-google-search-console/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="License: MIT"></a>
  <img src="https://img.shields.io/badge/python-3.13%2B-blue.svg" alt="Python 3.13+">
  <a href="https://query.farm/vgi/"><img src="https://img.shields.io/badge/VGI-Vector%20Gateway%20Interface-2f7d32.svg" alt="VGI"></a>
</p>

---

```sql
ATTACH 'gsc' (TYPE vgi, LOCATION 'uv run gsc_worker.py');
CREATE SECRET gsc (TYPE google_search_console, service_account_json '<key file contents>');

SELECT query, clicks, impressions, round(position, 1) AS position
FROM gsc.search_analytics('sc-domain:example.com',
    dimensions => ['query'],
    start_date => '28daysAgo', end_date => 'today')
ORDER BY clicks DESC
LIMIT 25;
```

> **Read-only, twice over.** The worker only ever requests the
> `webmasters.readonly` OAuth scope, and every HTTP call goes through one
> chokepoint (`gsc_api._call`) that allows `GET` on the site resources plus
> exactly two read RPCs (`searchAnalytics/query` and
> `urlInspection/index:inspect`). Adding a site, submitting or deleting a
> sitemap — anything else is refused before a socket opens.
> `tests/test_readonly_guard.py` fails the build if either guarantee weakens.

## Contents

- [Run](#run)
- [Credentials](#credentials)
- [Surface](#surface)
- [Search performance](#search-performance)
- [Sitemaps](#sitemaps)
- [URL inspection](#url-inspection)
- [Recipes](#recipes)
- [Design notes](#design-notes)
- [Not included, and why](#not-included-and-why)
- [ATTACH options](#attach-options)
- [Development](#development)
- [CI](#ci)
- [License](#license)

## Run

```bash
uv run gsc_worker.py            # stdio — what DuckDB launches
uv run serve.py --port 8000     # HTTP
```

Both scripts carry PEP-723 headers pinning their dependencies, so they run from a
fresh clone with nothing installed. A relative `LOCATION` resolves against the
working directory, so it only works from inside the clone. Anywhere else, point
`LOCATION` at the repository and let `uvx` fetch and cache it:

```sql
ATTACH 'gsc' (TYPE vgi,
  LOCATION 'uvx --from git+https://github.com/Query-farm/vgi-google-search-console vgi-google-search-console');
```

Pin a tag for a deployment, so the worker cannot change under you.

[Haybarn](https://query.farm) 2.0.0rc11 or newer ships the compatible `vgi.v3`
extension. The client and worker must both use VGI 3; older VGI 2 extensions
cannot attach this worker. The worker uses a separate DuckDB 1.5.5 environment
for filter evaluation, provisioned automatically by the script headers or `uvx`.

## Credentials

Every Search Console call needs an OAuth token; there is no anonymous access.
For local browser sign-in, use `ATTACH ... (credentials 'oauth')` to sign in to
Query Farm's Google OAuth app. The default `credentials 'auto'` uses a supplied
secret or permitted Application Default Credentials; it does not open a browser.
Each OAuth attachment carries its own authorization in the client's opaque
attach data. For HTTP deployments or supplied credentials, use a DuckDB
**secret** of type `google_search_console`. Credential fields are
redacted in `duckdb_secrets()`; the secret's *name* does not matter, because the
worker finds it by type.

### Sign in to Query Farm's Google app (local)

Query Farm distributes a Desktop OAuth client registration with each worker.
Analytics and Search Console use the same sign-in flow, with separate client
registrations and read-only scopes. Each attachment signs in independently;
users do not create a Google Cloud project or configure client credentials.

From the project directory:

```sql
ATTACH 'gsc' (TYPE vgi, LOCATION 'uv run gsc_worker.py', credentials 'oauth');
SELECT * FROM gsc.sites;
```

ATTACH opens Google's account chooser. Choose an account with access to your
Search Console properties and approve read-only access. ATTACH completes when
sign-in succeeds. A login attempt times out after three minutes; rerun ATTACH
to retry.

Each attachment has an independent login. ATTACH returns its refreshable OAuth
grant in VGI's `attach_opaque_data`, which the client retains and supplies to
subsequent worker calls. Each worker can obtain and refresh its own access token,
so queries continue after the worker that performed sign-in exits, including
with `POOL false`. The worker uses no Keychain or credential files for browser
sign-in and no background token server.

**The opaque attach data contains credentials, including the refresh token.**
It is not encrypted by this worker. Clients must treat it as a secret: keep it
out of logs, diagnostics and ordinary SQL options, and protect any storage if
they choose to persist it. The local SQL flow retains it for the attachment;
this does not automatically remember the login after the SQL application exits.
A new ATTACH starts a new sign-in. Google's browser session may make that quicker.

Workers keep a bounded in-memory credential cache to reuse access tokens. A
detach RPC evicts the receiving worker's cache entry. SQL DETACH removes the
catalog; it does not revoke credentials copied into other workers or clients.
Worker exit clears its cache without invalidating the client's grant. There
are no worker-imposed idle or total session limits: Google's token validity
and revocation determine whether a retained grant can still refresh.

For two Google accounts, attach twice and choose the appropriate account in
each browser flow:

```sql
ATTACH 'gsc' AS work_gsc (
    TYPE vgi, LOCATION 'uv run gsc_worker.py', credentials 'oauth'
);
ATTACH 'gsc' AS personal_gsc (
    TYPE vgi, LOCATION 'uv run gsc_worker.py', credentials 'oauth'
);

-- Remove only the work catalog from this SQL connection.
DETACH work_gsc;
```

Only the browser authorization callback uses a temporary loopback listener;
it closes when sign-in finishes. To revoke the app's authorization, use Google's
connected-app settings. If a refresh token expires or is revoked, detach and
attach again to sign in. Attachments created by older builds with a loopback
session reference must also be detached and attached again after upgrading.

Browser sign-in currently supports **local stdio** workers, with the browser on
the same computer. HTTP, TCP and Unix-socket workers require credentials supplied
by their clients unless the operator explicitly enables ADC. A future hosted
Query Farm sign-in service would need its own callback and storage isolated by
user; this local implementation does not provide that service.

**Query Farm setup (publisher only):** this checkout includes Query Farm's
Desktop OAuth client registration in `vgi_google_search_console/oauth_app.py`.
Its Google Cloud project must have the Search Console API enabled and a consent
screen configured for the intended audience. During testing, add participating
Google accounts as test users. If replacing the registration, create a client
of type **Desktop app** and update `CLIENT_ID` and `CLIENT_SECRET` in that module.
Complete Google's applicable verification before a public rollout. Desktop
client identifiers are public app configuration; never put user tokens in that module.
See Google's [desktop OAuth documentation](https://developers.google.com/identity/protocols/oauth2/native-app).

### Supplying credentials with a DuckDB secret

Set **exactly one** of the following shapes. A secret that sets two is
rejected rather than resolved by precedence, so it is never ambiguous which
identity is reading your data.

### Option 1 — a service account

Best for servers, schedules and CI: no user involved, nothing expires.

1. In the [Google Cloud console](https://console.cloud.google.com/), pick or
   create a project and **enable** the *Google Search Console API*.
2. *IAM & Admin → Service accounts → Create*. No Cloud roles are needed.
3. On the new account, *Keys → Add key → JSON*. Keep the downloaded file safe.
4. In **Search Console**, open the property, then *Settings → Users and
   permissions → Add user*, and add the service account's email (the
   `client_email` in the key). Repeat for each property it should read.

```sql
CREATE SECRET gsc (TYPE google_search_console,
    service_account_json '{"type": "service_account", "project_id": "...", ...}');
```

From a file, without pasting it into SQL history:

```sql
SET VARIABLE gsc_key = (SELECT content FROM read_text('/secure/path/gsc-reader.json'));
CREATE SECRET gsc (TYPE google_search_console, service_account_json getvariable('gsc_key'));
```

### Option 2 — an OAuth user grant

For reading the properties your own Google account can see, without adding a
service account to each. Create an *OAuth client ID* (type *Desktop app*) in the
Cloud console, run any OAuth flow once with scope
`https://www.googleapis.com/auth/webmasters.readonly` to obtain a refresh
token, then:

```sql
CREATE SECRET gsc (TYPE google_search_console,
    client_id '1234-abc.apps.googleusercontent.com',
    client_secret 'GOCSPX-...',
    refresh_token '1//0g...');
```

### Option 3 — a short-lived access token

Handy for a quick test. Expires in about an hour.

```sql
CREATE SECRET gsc (TYPE google_search_console, access_token 'ya29....');
```

### Fallback — Application Default Credentials

With no secret, and unless the catalog was attached with
`credentials 'secret'`, a **stdio** worker falls back to Google's
[Application Default Credentials](https://cloud.google.com/docs/authentication/application-default-credentials):
`GOOGLE_APPLICATION_CREDENTIALS`, or your own login via

```bash
gcloud auth application-default login \
  --client-id-file=/path/to/desktop-oauth-client.json \
  --scopes=https://www.googleapis.com/auth/webmasters.readonly
```

The **HTTP** server does *not* fall back: a shared server's ADC is the server's
identity, and lending it to every client that forgot a secret would be a
privilege leak. An operator who wants that starts the server with
`uv run serve.py --allow-adc`. This is deliberately a server flag rather than an
ATTACH option, because ATTACH options are chosen by clients.

The fallback is also disabled whenever `api_url` is overridden, so no ATTACH can
aim the worker at another host and receive its token. A secret that is present
but broken never falls back to ADC either.

### Quota project

Add `quota_project_id 'my-project'` to any shape to bill API usage to a specific
Cloud project (sent as `x-goog-user-project`).

## Surface

Everything lives in one schema, `gsc.main`. One table needs no arguments;
everything else is keyed by a property's **`site_url`**, written exactly as
Search Console lists it:

- `sc-domain:example.com` — a *domain property*, covering every protocol and
  subdomain;
- `https://www.example.com/` — a *URL-prefix property*, covering only URLs that
  begin with that exact prefix. `http://` and `https://`, and `www` and the bare
  host, are four different properties with four different sets of numbers.

| Object | Returns |
|---|---|
| `sites` (table) | every property the credential can see — **start here** |
| `search_analytics(site_url, dimensions =>, …)` | clicks, impressions, CTR and position — see [Search performance](#search-performance) |
| `sitemaps(site_url)` | submitted sitemaps with status, errors and contents — see [Sitemaps](#sitemaps) |
| `url_inspection(site_url, url)` | Google's index status for one URL — see [URL inspection](#url-inspection) |

The table is backed by a scan function, `all_sites()`, because a function and a
table cannot share a name in one schema.

`search_analytics` and `sitemaps` accept `''` in place of a site to walk
**every** readable property; their `site_url` column says where each row came
from, and a `WHERE site_url = …` visits only that property.

Every object carries catalog documentation — descriptions, column comments,
categorised examples — readable from `duckdb_functions()`, `duckdb_tables()` and
`DESCRIBE`, so an agent can discover the surface without this README.

## Search performance

`search_analytics()` is the API's `searchAnalytics.query`, the data behind the
Performance report. Columns are `site_url`, then one per requested dimension in
order, then the four metrics.

| Dimension | Column type | Notes |
|---|---|---|
| `date` | `DATE` | Pacific Time, however the site's audience is spread |
| `hour` | `TIMESTAMP WITH TIME ZONE` | needs `data_state => 'hourly_all'`; last several days only |
| `query` | `VARCHAR` | lowercased; rare queries are withheld |
| `page` | `VARCHAR` | final URL after redirects |
| `country` | `VARCHAR` | lowercase ISO 3166-1 alpha-3, e.g. `usa` |
| `device` | `VARCHAR` | `DESKTOP`, `MOBILE`, `TABLET` |
| `search_appearance` | `VARCHAR` | must be the only dimension (a Google restriction) |

| Metric | Type | Combine rows with |
|---|---|---|
| `clicks` | `BIGINT` | `sum()` |
| `impressions` | `BIGINT` | `sum()` |
| `ctr` | `DOUBLE` | `sum(clicks) / sum(impressions)` — never `avg()` |
| `position` | `DOUBLE` | `sum(position * impressions) / sum(impressions)` |

| Argument | Default | |
|---|---|---|
| `start_date`, `end_date` | `'28daysAgo'`, `'today'` | `YYYY-MM-DD`, `today`, `yesterday` or `NdaysAgo`; inclusive |
| `search_type` | `'web'` | `web`, `image`, `video`, `news`, `discover`, `google_news` |
| `aggregation_type` | `'auto'` | `auto`, `by_property`, `by_page`, `by_news_showcase_panel` |
| `data_state` | `'final'` | `final`, `all` (includes fresh, still-changing days), `hourly_all` |
| `filters` | `''` | JSON array of filters, ANDed, applied by Google before aggregation |
| `max_rows` | `0` | stop after this many rows per property |
| `cache_ttl` | `0` | seconds DuckDB may reuse an identical result |

### Totals are Google's, not SQL's

Google aggregates before SQL sees anything, and three things follow:

- Grouped by `query`, rare queries are withheld for privacy and appear in **no
  row**, so `sum(clicks)` over the rows is less than the real total.
- Grouped by `page`, a search that showed two of the site's pages is an
  impression for both.
- Google returns the top rows for each day, sorted by clicks — not every row.

Ask for the breakdown you mean to read. For a property total, pass no
dimensions; grouping by `date` alone also adds up.

### Filtering

A SQL `WHERE` is always honoured. Part of it is also sent to Google, so fewer
rows come back:

- `=` on a `page`, `query`, `country` or `device` column becomes an `equals`
  filter;
- range predicates on `date` narrow the date range;
- `=` / `IN` on `site_url` (with `''` as the site) picks which properties are
  visited.

To filter on a dimension that is *not* a column, use `filters`:

```sql
SELECT device, clicks
FROM gsc.search_analytics('sc-domain:example.com', dimensions => ['device'],
    filters => '[{"dimension": "country", "operator": "equals", "expression": "usa"},
                 {"dimension": "page", "operator": "contains", "expression": "/blog/"}]');
```

Operators: `equals`, `not_equals`, `contains`, `not_contains`,
`including_regex`, `excluding_regex` (RE2). Filterable dimensions: `country`,
`device`, `page`, `query`, `search_appearance`.

### Freshness and limits

Final data lags two or three days; `data_state => 'all'` includes the fresh
days while they are still changing. History goes back 16 months. Queries
grouped or filtered by `page` or `query` over long ranges are the ones that
spend Google's load quota fastest.

## Sitemaps

`sitemaps(site_url)` returns one row per submitted sitemap: when it was last
submitted and downloaded, whether it is still pending, its error and warning
counts, and `contents` — a list with one entry per content type and the number
of URLs submitted. Pass `sitemap_index => '<url>'` to list the sitemaps inside
a sitemap index.

```sql
-- Sitemaps with problems, across every property
SELECT site_url, path, errors, warnings, last_downloaded
FROM gsc.sitemaps('') WHERE errors > 0 OR warnings > 0;
```

Google has deprecated the per-sitemap `indexed` count and no longer fills it in.

## URL inspection

`url_inspection(site_url, url)` asks what Google's index knows about one URL:
the verdict (`PASS` means indexed), the coverage reason in words, the last
crawl, whether robots.txt or a `noindex` blocks it, the canonical Google chose
against the one the page declares, and AMP and structured-data problems.

Both arguments are per-row inputs, so it composes under `LATERAL` — and
`search_analytics` returns both columns it needs:

```sql
SELECT p.page, i.verdict, i.coverage_state, i.last_crawl_time
FROM (SELECT site_url, page
      FROM gsc.search_analytics('sc-domain:example.com', dimensions => ['page'], max_rows => 100)
      ORDER BY clicks DESC LIMIT 20) p,
     LATERAL gsc.url_inspection(p.site_url, p.page, cache_ttl => 3600) i;
```

Each row is one API call against a quota of **2,000 a day and 600 a minute per
property**, so inspect a bounded list and pass `cache_ttl` — repeated URLs and
re-runs are then served from DuckDB's cache. This reads the indexed version of
the page; it is not a live test and cannot request indexing.

## Recipes

```sql
-- Clicks and impressions per day, including days that are not final yet
SELECT date, clicks, impressions
FROM gsc.search_analytics('sc-domain:example.com', dimensions => ['date'], data_state => 'all')
ORDER BY date;

-- "Striking distance" queries: ranking 8–20 with real demand
SELECT query, impressions, round(position, 1) AS position
FROM gsc.search_analytics('sc-domain:example.com', dimensions => ['query'])
WHERE position BETWEEN 8 AND 20 AND impressions > 200
ORDER BY impressions DESC;

-- Week over week, per property, in one query
SELECT site_url,
       sum(clicks) FILTER (WHERE date >  current_date - 10) AS this_week,
       sum(clicks) FILTER (WHERE date <= current_date - 10) AS last_week
FROM gsc.search_analytics('', dimensions => ['date'], start_date => '16daysAgo', end_date => '3daysAgo')
GROUP BY site_url;

-- Which queries lead to one page
SELECT query, clicks, impressions
FROM gsc.search_analytics('sc-domain:example.com', dimensions => ['query'],
    filters => '[{"dimension": "page", "expression": "https://example.com/pricing"}]')
ORDER BY clicks DESC;

-- Properties with no sitemap at all
SELECT site_url FROM gsc.sites
WHERE permission_level <> 'siteUnverifiedUser'
  AND site_url NOT IN (SELECT site_url FROM gsc.sitemaps(''));
```

## Design notes

**REST over `httpx`, not Google's discovery client.** The API speaks plain JSON,
which maps directly onto Arrow, needs one small dependency, and gives the tests
an `httpx.MockTransport` seam that exercises the real request code.
`google-auth` is still used — only to mint and refresh tokens — and its own
HTTP calls are routed through the same `httpx` pool by a small transport
adapter (`gsc_api.AuthTransport`), so `requests` is never imported.

**If the endpoint pages, the function pages.** `search_analytics` is a scan that
fetches one page per `process()` tick, so a `LIMIT` stops early and a large pull
never sits in memory whole. The price is that a scan takes literal arguments
only — which is why `''` exists as "every property", rather than a `LATERAL`
over `sites`. `url_inspection` is one bounded call per URL, so it is a blended
row transform instead and *does* compose under `LATERAL`.

**A site's walk ends on an empty page.** Google documents that a start row past
the last result returns no rows. A page shorter than requested is not treated as
the end: if Google ever returned a short page mid-result, stopping there would
be a silently truncated answer. That costs one extra request per walk (none for
a total, which has at most one row).

**Pushdown is always re-applied.** Declaring `filter_pushdown` makes DuckDB drop
its own filter above the scan, so a predicate the worker receives and ignores
would silently stop applying. The worker therefore evaluates *every* pushed
predicate against the rows it emits (`pushdown.build_filtered`), and only
*additionally* translates the subset Google evaluates identically. `IN` and
`LIKE` are not translated: the API only ANDs its filters, and its regex
operators are not specified precisely enough to promise SQL's semantics.
`tests/test_end_to_end.py` holds this with impossible predicates that must
return nothing.

**Site URLs are escaped, not trusted.** A `site_url` is full of `/` and `:` and
comes straight from SQL. It is percent-encoded into exactly one path segment,
and the allow-list checks the *shape* of the final path, so no value can turn a
sitemap listing into a different call.

**Errors say what to do.** A 403 names the service account to add to the
property; a disabled API says which one to enable; a bare hostname passed as a
site suggests both property forms.

## Not included, and why

| Feature | Status |
|---|---|
| Adding or removing a property (`sites.add` / `sites.delete`) | A **write**; out of scope for a read-only worker. |
| Submitting or deleting a sitemap | A write, likewise. |
| Requesting indexing (the Indexing API) | A write, and a different API with its own scope. |
| Mobile-Friendly Test | Retired by Google in December 2023; the matching inspection fields are omitted too. |
| Page Experience, Core Web Vitals, Links, Manual actions reports | Not available through the Search Console API. |
| `first_incomplete_date` response metadata | Not surfaced yet; use `data_state => 'final'` when only settled days will do. |
| Bulk data export | That is a BigQuery export, not this API — query it with DuckDB's BigQuery support. |

## ATTACH options

Everything that changes how one attached catalog behaves is an ATTACH option,
visible in the statement that made the choice and scoped to that catalog. Each
is validated when you ATTACH, so a typo fails there rather than on a later query.

```sql
ATTACH 'gsc' (TYPE vgi, LOCATION 'uv run gsc_worker.py', credentials 'secret', page_size 5000);
```

| Option | Default | Purpose |
|---|---|---|
| `credentials` | `'auto'` | `'auto'`: the secret, else Application Default Credentials where the worker permits it. `'secret'`: require the secret. `'oauth'`: browser sign-in to Query Farm's app, with refreshable credentials in this attachment's opaque data. |
| `page_size` | `25000` | Rows per `searchAnalytics.query` request, 1–25,000. Smaller pages reach DuckDB sooner. |
| `api_url` | `https://searchconsole.googleapis.com` | API base URL, for a proxy or a test double. Overriding it disables the ADC fallback. |

The worker reads no environment variables of its own. The only ones that matter
are Google's: `GOOGLE_APPLICATION_CREDENTIALS`, used by the ADC fallback.

## Development

### Commands

```bash
uv sync                                   # dev environment
uv run ruff check . && uv run ruff format --check .
uv run mypy                               # strict
uv run pytest                             # offline: unit + SQL end-to-end against a fake API
uv run pytest -m live                     # against real Search Console (see below)
uv run --no-sources --with vgi-lint-check==2.0.0 vgi-lint lint "uv run --no-sources gsc_worker.py" \
    --no-execute --no-check-links --audit-waivers --fail-on warning
```

### Shared Google OAuth implementation

The Analytics repository is the canonical source for `oauth.py`,
`oauth_model.py`, `oauth_callback.py`, `oauth_page.py`, `oauth_assets/`, and the
shared credential-policy tests. Both workers bundle identical copies so each
can be installed independently. Connector adapters, client registrations,
read-only scopes, and API endpoint validation remain in their respective workers.

With both repositories checked out beside each other, update the Analytics
source and synchronize from the Analytics repository:

```bash
python3 scripts/sync_oauth.py --target ../vgi-google-search-console
python3 scripts/sync_oauth.py --check --target ../vgi-google-search-console
```

Run both workers' test suites before publishing a shared change. Push the
canonical Analytics change first, then Search Console; Search Console CI
compares its bundled files and policy tests against Analytics `main`.

### Layout

| Module | Role |
|---|---|
| `gsc_api.py` | Upstream HTTP requests: allow-list, auth headers, retries, errors |
| `auth.py` | The `google_search_console` secret type; credential parsing, caching, ADC fallback |
| `oauth.py` | Query Farm browser sign-in, portable OAuth grants and per-worker credential caching |
| `oauth_model.py` | Shared credential selection and portable grant validation |
| `oauth_callback.py`, `oauth_page.py`, `oauth_assets/` | Shared loopback listener and self-contained sign-in pages |
| `oauth_adapter.py` | Connector-specific API transport and error bindings |
| `oauth_app.py` | This worker's Query Farm Desktop OAuth client registration |
| `settings.py` | ATTACH options: validation and the ADC safety rules |
| `sites.py` | `sites` / `all_sites` and `sitemaps` |
| `search_analytics.py` | `search_analytics`: request building, paging, the bind-time schema |
| `inspection.py` | `url_inspection`, the blended (LATERAL-capable) function |
| `pushdown.py` | Applying pushed filters; translating the safe subset for Google |
| `schemas.py` | Fixed schemas (one documented comment per column) and total coercers |
| `meta.py` | Builders for the `vgi.*` documentation tags |
| `worker.py` | Catalog assembly, tables, catalog docs, entry points |

### Tests

Three tiers, all under `tests/`:

- **Unit** (`test_auth`, `test_gsc_api`, `test_shapes`, `test_settings`,
  `test_catalog`, `test_packaging`, `test_readonly_guard`) — pure functions, the
  HTTP chokepoint via `httpx.MockTransport`, metadata completeness, PEP-723
  header drift, and the read-only guarantees.
- **End-to-end SQL, offline** (`test_end_to_end`) — the real worker subprocess
  attached from DuckDB, talking over a socket to `tests/fake_gsc.py`, a
  deterministic stand-in for the API. Covers binding, typing, secrets, filter
  and date pushdown, paging, `LIMIT` early-exit, the every-property walk,
  `LATERAL` provenance and error messages — and runs every example and agent
  reference query the catalog publishes. Needs no credentials.
- **Live** (`test_live`, marker `live`) — the same through a real ATTACH against
  real Search Console. Configure with `VGI_GSC_TEST_SERVICE_ACCOUNT_JSON` (key
  JSON text) or `VGI_GSC_TEST_ACCESS_TOKEN`, or neither to use your ADC; and
  optionally `VGI_GSC_TEST_SITE`. It skips when no credential resolves. Every
  assertion holds for any property, quiet or busy.

## CI

`.github/workflows/ci.yml` runs on every push and pull request:

- **check** — ruff lint + format, mypy `--strict`, the full offline suite
  (including the end-to-end SQL tier), and both entry points starting from their
  PEP-723 headers alone.
- **catalog** — `vgi-lint` structural tier at a pinned version, with
  `--audit-waivers` (a waiver that stops suppressing anything fails the build)
  and `--fail-on warning`.

`.github/workflows/live.yml` runs nightly and on demand against real Search
Console: the live test tier, then `vgi-lint` executing every catalog example. It
needs the repository secret `GSC_SERVICE_ACCOUNT_JSON` (and optionally the
variable `GSC_TEST_SITE`); without them it skips cleanly.

`vgi-lint.toml` carries one waiver, with its reason; `vgi-agent-tests.yaml`
holds the private graders for the agent-suitability suite, whose public prompts
are in the catalog.

## License

MIT — see [LICENSE](LICENSE). Copyright © 2026 Query Farm LLC.

Search data belongs to the owner of each property and is accessed under the
[Google APIs Terms of Service](https://developers.google.com/terms). Google
Search Console is a trademark of Google LLC; this project is not affiliated with
or endorsed by Google. See [NOTICE](NOTICE).

## Executed notebook

[Compare weighted CTR and a bounded URL-inspection review](notebooks/README.md) using an explicit synthetic local API through the real worker.

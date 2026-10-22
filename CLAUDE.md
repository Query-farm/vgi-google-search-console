# CLAUDE.md — vgi-google-search-console

A **read-only VGI worker** (Python, `vgi-python`) exposing the Google Search
Console API to DuckDB: search performance (`searchAnalytics.query`), the site
list, sitemaps, and URL inspection. The catalog is `gsc`, one schema `main`.
README.md is the user documentation; this file is for working on the code.

## Gates — all must pass before a commit

```bash
uv run ruff check . && uv run ruff format --check .
uv run mypy                       # strict
uv run pytest                     # offline, ~15s, includes the SQL end-to-end tier
uv run --no-sources --with vgi-lint-check==0.82.0 vgi-lint lint "uv run --no-sources gsc_worker.py" \
    --no-execute --no-check-links --audit-waivers --fail-on warning   # run FROM the repo root
```

vgi-lint reads `vgi-lint.toml` (waivers) and `vgi-agent-tests.yaml` (graders)
from the current directory — run it from the repo root or the waivers vanish.

## Invariants (each has a test)

- **Only `gsc_api.py` touches the network or imports `httpx`.** Every request
  goes through `gsc_api._call`, whose allow-list permits GET on
  `/webmasters/v3/sites…` and POST only to `…/searchAnalytics/query` and
  `/v1/urlInspection/index:inspect`. Do not add a write (site add/delete,
  sitemap submit/delete); the worker is read-only by design. google-auth's token
  refreshes go through `gsc_api.AuthTransport`, so never import
  `google.auth.transport.requests`. Browser OAuth uses the same token transport
  and a temporary loopback callback listener in `gsc_api.OAuthCallback`.
  After sign-in there is no loopback token server; each worker refreshes the
  grant from the client's opaque attach data through `gsc_api.AuthTransport`.
- **Only the `webmasters.readonly` scope.** Never request `webmasters` or
  `indexing`.
- **Site URLs are escaped, and validated for shape.** `gsc_api.site_url()`
  accepts `sc-domain:<host>` or an `http(s)://` prefix and nothing else;
  `gsc_api.segment()` percent-encodes it into one path segment. Every
  user-supplied site must pass through both before it reaches a path.
- **Filter pushdown ⇒ apply every filter.** Any function with
  `filter_pushdown = True` must also set `auto_apply_filters` and
  `projection_pushdown`, and emit through `pushdown.build_filtered`. Translating
  a predicate for Google (`pushdown.dimension_filters`, `date_bounds`,
  `discrete_strings`) is an extra optimisation, and must only ever *widen* —
  never push a filter that could drop a row the SQL predicate keeps.
- **If the endpoint pages, the function pages.** `search_analytics` is a
  `TableFunctionGenerator` carrying `SearchAnalyticsState`
  (`ArrowSerializableDataclass`, string/int/bool fields only) under
  `@init_single_worker`, one API page per tick, with the request frozen on the
  first tick. A site's walk ends on an *empty* page, not a short one.
- **Scans take literals only.** DuckDB rejects a subquery or a LATERAL column as
  a table-function argument, so `''` means "every readable property" and the
  functions stamp `site_url`. Only `url_inspection` is blended
  (`RowTransformFunction`) and so accepts columns; it must emit `parent_rows`.
- **Examples name no site.** Every catalog example, executable example and agent
  reference query uses `''`, so it runs against any credential;
  `tests/test_end_to_end.py::TestCatalogExamples` runs them all against the fake.
- **Secrets are found by type, not name.** The framework keys resolved secrets
  by the DuckDB secret *name*; `auth.secret_values()` searches by type.
- **Browser OAuth is local only.** Only the stdio entry point enables browser
  sign-in. HTTP/TCP/Unix workers cannot use it, including with `--allow-adc`.
  Query Farm supplies the Desktop app registration; users have no configure
  command or OS credential store. Each ATTACH returns a versioned refreshable
  `OAuthGrant` in `attach_opaque_data`, including the client registration and
  refresh token. Treat these bytes as credentials, never log them, and omit
  secrets from reprs and error messages. They are not encrypted by the worker.
  Fresh workers must reconstruct credentials without the authorizing process
  or a token broker; test SQL with `POOL false`. Keep per-worker credential
  caching bounded and isolate it by the complete grant, including its random
  attachment ID. Detach only evicts this worker's cache; it cannot invalidate
  copies or revoke Google consent. Native extension 74015fc does not send a
  detach RPC on SQL DETACH. Clients own retention of attach data; no worker
  expiry or credential file is required. OAuth mode must reject `api_url`
  overrides before sign-in and when decoding opaque data. The refresh endpoint
  and scope must stay fixed, and malformed/old grants must require a new ATTACH
  rather than falling back to ADC.
- **Configuration is ATTACH options, not environment variables.** Per-catalog
  choices live in `settings.Settings`, validated in
  `SearchConsoleCatalog.catalog_attach` and delivered to every call as
  `attach_opaque_data`; `common.session(params)` decodes them. The one
  explicit process-level switch is the server's `--allow-adc` (`auth.permit_adc`),
  because ATTACH options are client-chosen. The entry point also restricts
  browser OAuth to stdio. Overriding `api_url` must keep disabling ADC
  (`Settings.may_use_adc`). Desktop app configuration is supplied by Query Farm in
  `oauth_app.py`; never add user tokens to source.
- **PEP-723 headers mirror pyproject.** `gsc_worker.py` and `serve.py` must list
  the same runtime deps at the same lower bounds (`tests/test_packaging.py`).
  Bump both together.

## Adding a function

1. Fixed schema → `schemas.py`, every field via `meta.field(name, type, comment)`.
2. Function class in the right module with a `Meta` carrying `name`,
   `description`, `categories`, `required_secrets = REQUIRED_SECRETS`, pushdown
   flags, `tags = docs(...)` (llm + md + `example_queries`, `result_schema` or
   `vgi.result_dynamic_columns_md`), and `examples` (each also present, with a
   description, in the tag).
3. Register in the module's `*_FUNCTIONS` list.
4. Add a public task (`name`, `prompt`) to `_AGENT_TEST_TASKS` in `worker.py`
   **and** its grader to `vgi-agent-tests.yaml` (same name, same order).
5. Teach `tests/fake_gsc.py` the endpoint, then add unit + end-to-end coverage.
6. Re-run all four gates; vgi-lint must stay at 0 findings.

## Testing notes

- `tests/fake_gsc.py` is a deterministic Search Console stand-in (a synthetic
  fact table over 2026-09-01..28, four sites of which two are readable).
  `handle()` plugs into `httpx.MockTransport`; `serve()` runs it on a socket for
  the end-to-end tier, which ATTACHes the worker subprocess with `api_url` aimed
  at it and `page_size 10`. Every request lands in `fake_gsc.REQUESTS`.
- `conftest.py` resets the connection pool and credential cache around every
  test, and disables ADC so a developer's own Google login is never used by
  accident.
- Live tests (`-m live`) need `VGI_GSC_TEST_SERVICE_ACCOUNT_JSON` or
  `VGI_GSC_TEST_ACCESS_TOKEN` (or ADC), optionally `VGI_GSC_TEST_SITE`; they skip
  when no credential resolves.
- When analysing data, go through SQL against an ATTACHed worker, and fix a
  missing capability in the worker rather than working around it.

## SDK quirks worth knowing (vgi-python 0.43.x)

- `params.secrets` at bind is a `SecretsAccessor` whose `.get(type)` looks up by
  secret name; use `.to_dict().of_type(...)` (wrapped by `auth.secret_values`).
- `OutputCollector.emit(..., cache_control=, parent_rows=)` is typed on
  vgi-rpc's base class without those kwargs; use `common.emit()` /
  `common.emit_fanout()`, which cast to `VgiOutputCollector`.
- An `Arg(pattern=...)` / `choices=` / `ge=` is enforced by the framework at bind
  and is what vgi-lint's VGI317 wants for constrained arguments. `choices`
  cannot be declared on a list-typed argument (the whole list is compared), so
  `dimensions` is validated in `normalise_dimensions` instead.
- A blended function's positional arguments are not on `params.args`; read them
  off `batch` by name and validate per row.

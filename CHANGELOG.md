# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/).

## Unreleased

- Standardized the Google OAuth runtime, portable grant validation, credential
  selection, and callback pages across Analytics and Search Console. Shared
  files and policy tests have a sync command and a Search Console CI parity
  check against the canonical Analytics source. Client registrations and
  read-only scopes remain connector-specific.
- Both workers reject obsolete `oauth_profile` / `oauth_reauth` options;
  browser sign-in requires explicit `credentials 'oauth'`.
- Upgraded to `vgi-python` 0.48.2 and `vgi-rpc` 0.52.4, with canonical VGI 3
  table constraints. Clients now need a VGI 3 extension (Haybarn 2.0.0rc11 or
  newer). Workers use DuckDB 1.5.5 for the SDK's pinned filter-evaluation
  profile; SQL tests launch isolated workers from their PEP-723 headers.
- Updated the SQL test client to Haybarn 2.0.0rc11 and `vgi-lint-check` to 2.0.0.
- Browser sign-in responses use a self-contained Query Farm page with light
  and dark themes, connector-specific documentation, and distinct received,
  declined and invalid response states. Authorization receipt is shown before
  the SQL session completes the credential exchange.
- Browser sign-in at ATTACH through Query Farm's shared Desktop OAuth app,
  with PKCE, a loopback callback and a portable refreshable grant for each
  attachment. Worker exit or a detach RPC clears only that worker's cache;
  there are no worker-imposed session limits, Keychain or credential files.
- Query Farm supplies the app registration in `oauth_app.py`; end users sign
  in with their own Google accounts without creating or configuring an app.
- Disable local OAuth on all shared transports and restrict ADC there to an
  explicit `--allow-adc` operator flag.

## 0.1.0

First release: read-only Google Search Console for DuckDB.

### Added

- **Search performance.** `search_analytics()` runs the API's
  `searchAnalytics.query` with typed columns decided at bind time (`DATE` /
  `TIMESTAMP WITH TIME ZONE` calendar dimensions, `BIGINT` clicks and
  impressions, `DOUBLE` CTR and position), every search type, aggregation type
  and data state, a JSON `filters` argument, `max_rows` and an opt-in
  `cache_ttl`. Rows stream one API page per tick.
- **Every property at once.** `search_analytics('')` and `sitemaps('')` walk
  every readable property, stamping `site_url` on each row; a predicate on
  `site_url` visits only the properties it names.
- **Sites.** The `sites` table (and `all_sites()` behind it), with the access
  level and whether each is a domain or URL-prefix property.
- **Sitemaps.** `sitemaps()` with status, error and warning counts, contents by
  type, and `sitemap_index` to look inside a sitemap index.
- **URL inspection.** `url_inspection()`, a blended function that works as a
  literal call and under a correlated `LATERAL`, with per-URL caching.
- **Credentials.** A `google_search_console` DuckDB secret type taking a
  service-account key, an OAuth refresh-token grant or an access token, with
  Application Default Credentials as a stdio-only fallback.
- **ATTACH options.** `credentials` (`auto` / `secret`), `page_size` and
  `api_url`, validated at ATTACH. The worker reads no environment variables of
  its own; the HTTP server's ADC fallback is an operator-only `--allow-adc`
  flag.
- **Pushdown.** `=` on `page`, `query`, `country` and `device` becomes an API
  filter and range predicates on `date` narrow the date range; every pushed
  predicate is also applied by the worker.
- **Read-only guarantees.** One HTTP chokepoint with a GET + two-read-RPC
  allow-list, the `webmasters.readonly` scope only, and site URLs confined to a
  single encoded path segment, all held by `tests/test_readonly_guard.py`.
- **Tests and CI.** Unit tests, an offline SQL end-to-end tier against a fake
  Search Console API, a live tier, ruff, mypy `--strict` and vgi-lint.

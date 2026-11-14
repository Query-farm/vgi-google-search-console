# Search Console: weighted rates and a bounded review

[Open the executed notebook](01_weighted_rates_and_bounded_inspection.ipynb).
It combines device rows using impression weights, reconciles them with a separate
page aggregation, then uses a bounded LATERAL URL-inspection query. The experiment
raises the minimum impression count from 1,000 to 5,000.

## Run from this folder

Install [uv](https://docs.astral.sh/uv/), then:

```bash
uv sync --frozen
uv run python execute.py
```

The locked notebook environment uses Python 3.13–3.14 and Haybarn 2.0.0rc11.
The visible SQL launches an isolated worker from this exact public revision:

```text
uvx --python 3.13 --with vgi-python==0.51.1 --from git+https://github.com/Query-farm/vgi-google-search-console@36c7449c1be7a6168b53610a4e06380079090cd5 vgi-google-search-console
```

No worker checkout, local worker build, Google account, API key or globally
registered kernel is required. First use downloads from GitHub, package indexes
and the VGI community extension repository. The companion fixture server binds
only to loopback and closes during cleanup. This whole folder can be copied out
of the repository and executed independently.

For an editor, install and select this kernel:

```bash
uv run python -m ipykernel install --user --name gsc-examples \
  --display-name "Python (Search Console notebooks)"
```

Parameters and SQL are visible; Python presentation and setup are folded. Restart
and run all after editing. To upgrade the worker, change the literal revision or
SDK pin, then re-execute default and experiment with the current source checks.

## Experiment

```bash
uv run python execute.py --parameters '{"MIN_IMPRESSIONS":5000}' \
  --output-dir validation/high-threshold
```

`MIN_IMPRESSIONS` accepts integers 0–20,000. It changes only eligibility for a
three-page review budget. Ordering is by descending impressions, then URL.
The threshold does not change the source traffic, formulas or first chart.
An empty shortlist is valid. Restore 1,000 for the default.

The runner uses a fresh temporary kernel, runs with this directory as its working
directory, and saves only after success. Parameter overrides require a separate
output folder; that folder receives all fixture, documentation and branding
companions. Generated logs, charts and experiment copies are ignored.

## Explicit synthetic provenance

[fixtures/search_console.json](fixtures/search_console.json) contains 12 invented
weekly page/device rows across six `example.com` pages and six invented index
statuses. These are neither recorded Google responses nor customer data. Counts
were chosen to expose the difference between combined CTR and equal weighting
of device rates. The fixed interval is 2026-09-01 through 2026-09-07 inclusive,
using Pacific Time calendar dates. Inspection crawl times are a separate invented
snapshot after that interval. The run clock is UTC and is not a capture time.

[synthetic_api.py](synthetic_api.py) serves this narrow API through the worker's
supported `api_url` ATTACH option, following its existing offline-test pattern.
It implements only the documented fixture requests, including pagination and
page aggregation. Unsupported requests fail explicitly. The unchanged real
worker performs all catalog, HTTP, Arrow and VGI work. The notebook reads worker
results, never the fixture's fact rows, for its analysis.

The dummy token `notebook-synthetic-token` authenticates only this local fixture.
The visible `credentials 'secret'` setting and overridden API URL disable ADC;
no browser OAuth, credentials discovery or Google requests are used. This
notebook deliberately has no live mode. Live validation requires an authorized
Google property and credentials and was not performed. For real-account setup,
see the [pinned worker documentation](https://github.com/Query-farm/vgi-google-search-console/blob/36c7449c1be7a6168b53610a4e06380079090cd5/README.md#credentials).

## Interpretation

CTR is clicks divided by impressions over the returned rows. Position is weighted
by impressions. Equal weighting of device CTRs is displayed only as a comparison.
The separate page request checks the internally complete synthetic fixture;
[Google's real API](https://developers.google.com/webmaster-tools/v1/searchanalytics/query)
does not guarantee all rows. Returned page-impression sums are not a unique-search
or property-total denominator. Reassess grouping, privacy and truncation before
using these formulas with a live property.

All six pages remain visible after inspection. Missing verdicts mean uninspected,
not passing. The [URL Inspection API](https://developers.google.com/webmaster-tools/v1/urlInspection.index/inspect)
describes the indexed version rather than running a live URL test. Neither this
snapshot nor the synthetic statuses explain the earlier traffic or forecast gains.
See [STANDARD.md](STANDARD.md) for notebook conventions.

The notebook creates its Haybarn connection with an empty temporary secret
directory and persistent secrets disabled. Its visible SQL creates only a
TEMPORARY dummy secret for the local fixture. Assertions check the initial empty
inventory and absence of persistent secrets before cleanup. Existing saved SQL
credentials are neither loaded nor modified.

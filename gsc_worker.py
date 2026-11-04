# /// script
# requires-python = ">=3.13"
# dependencies = [
#     "vgi-python[http,duckdb]>=0.48.2,<0.49",
#     "vgi-rpc>=0.52.4",
#     "httpx>=0.27",
#     "google-auth>=2.30",
#     "oauthlib>=3.3.1",
# ]
# ///
"""Stdio entry point for the Google Search Console VGI worker (``uv run``).

ATTACH 'gsc' (TYPE vgi, LOCATION 'uv run gsc_worker.py');
"""

from __future__ import annotations

from vgi_google_search_console.worker import main

if __name__ == "__main__":
    main()

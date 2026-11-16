#!/usr/bin/env python3
"""Serve the offline fake Search Console API for the SQL suite (test/sql/*.test).

The fake itself lives in ``tests/fake_gsc.py``, shared with the pytest suite, so
the SQL suite and the unit tests assert against one synthetic dataset. It
accepts only ``Authorization: Bearer test-token`` and never talks to Google.

Run with the project's interpreter (the fake imports httpx)::

    .venv/bin/python test/mock_api/server.py   # prints PORT:<n> once listening
"""

from __future__ import annotations

import argparse
import contextlib
import sys
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests import fake_gsc  # needs the repo root on sys.path (above)


def main() -> None:
    """Start the fake and print ``PORT:<n>`` once it is listening."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), fake_gsc._Handler)
    print(f"PORT:{server.server_address[1]}", flush=True)
    with contextlib.suppress(KeyboardInterrupt):
        server.serve_forever()


if __name__ == "__main__":
    main()

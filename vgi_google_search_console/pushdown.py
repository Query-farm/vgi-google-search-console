"""Applying a DuckDB ``WHERE`` to emitted rows, and translating part of it for Search Console.

Two separate jobs, with very different stakes:

* :func:`build_filtered` **applies** every pushed predicate to the rows a
  function emits. That is not optional. Once a function declares
  ``filter_pushdown`` the engine drops its own filter above the scan, so a
  predicate the worker receives and ignores is not applied by anyone — the
  query silently returns rows that fail it.

* :func:`dimension_filters` and :func:`date_bounds` **translate** the subset of
  predicates Search Console can evaluate itself into its own filters and date
  range, so the API returns fewer rows. That is a pure optimisation, and it
  declines whenever it is unsure: pushing too little costs bandwidth and quota,
  pushing too much drops rows nothing downstream can recover.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import date, datetime
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc
from vgi.table_function import ProcessParams

from vgi_google_search_console.schemas import batch_from_rows

#: Values the API is known to accept for the two enumerated dimensions. A
#: predicate naming anything else cannot match a row, and is left for the
#: worker to apply rather than sent to an API that may reject it outright.
_COUNTRY = re.compile(r"^[a-z]{3}$")
_DEVICES = frozenset({"DESKTOP", "MOBILE", "TABLET"})

#: Result columns whose ``=`` predicate means the same thing as the API's own
#: ``equals`` filter, mapped to the API's name for the dimension.
_PUSHABLE = {"page": "page", "query": "query", "country": "country", "device": "device"}


def _filters(params: ProcessParams[Any]) -> Any:
    return getattr(params, "current_pushdown_filters", None)


def equality(params: ProcessParams[Any], column: str) -> str | None:
    """The constant from ``WHERE <column> = '...'``, when there is exactly one string."""
    filters = _filters(params)
    if filters is None:
        return None
    scalar = filters.get_column_constant(column)
    value = scalar.as_py() if scalar is not None else None
    return value if isinstance(value, str) and value else None


def discrete_strings(params: ProcessParams[Any], column: str) -> list[str] | None:
    """The strings ``column`` is pinned to by ``=`` or ``IN``, or ``None`` when it is not pinned.

    Used to visit only the properties a ``WHERE site_url ...`` names instead of
    every readable one. Exact by construction: a row for any other value could
    not have survived the predicate.
    """
    filters = _filters(params)
    if filters is None:
        return None
    values = filters.get_column_values(column)
    if values is None:
        return None
    found = values.to_pylist()
    if not all(isinstance(value, str) for value in found):
        return None
    return list(dict.fromkeys(found))


def dimension_filters(params: ProcessParams[Any], columns: Sequence[str]) -> list[dict[str, str]]:
    """Search Console filters equivalent to pushed ``=`` predicates on dimension columns.

    Only string equality is translated, and only on ``page``, ``query``,
    ``country`` and ``device``. A predicate on a *requested* dimension means the
    same thing whether Google applies it or DuckDB does: each row is already
    grouped by that dimension's value, so the row either matches or it does not.

    ``IN``, ``LIKE`` and everything else are left for :func:`build_filtered`:
    the API only ANDs its filters together, and its regex operators are not
    documented precisely enough to promise they match what SQL would.
    """
    expressions: list[dict[str, str]] = []
    for column in columns:
        dimension = _PUSHABLE.get(column)
        value = equality(params, column) if dimension else None
        if dimension is None or value is None:
            continue
        if column == "country" and not _COUNTRY.match(value):
            continue
        if column == "device" and value not in _DEVICES:
            continue
        expressions.append({"dimension": dimension, "operator": "equals", "expression": value})
    return expressions


def _as_date(scalar: Any) -> date | None:
    value = scalar.as_py() if scalar is not None else None
    if isinstance(value, datetime):
        return value.date()
    return value if isinstance(value, date) else None


def date_bounds(params: ProcessParams[Any], column: str = "date") -> tuple[date | None, date | None]:
    """``(first, last)`` days implied by range predicates on a DATE column.

    Search Console's date range is inclusive at both ends, and so is what this
    returns: a strict ``>`` or ``<`` is *not* tightened by a day. Fetching the
    boundary day costs one day of rows that :func:`build_filtered` then drops;
    tightening it wrongly would lose a day nothing could bring back.

    Returns ``(None, None)`` when nothing usable was pushed.
    """
    filters = _filters(params)
    if filters is None:
        return None, None
    bounds = filters.get_column_bounds(column)
    if bounds is None:
        return None, None
    return _as_date(getattr(bounds, "min_value", None)), _as_date(getattr(bounds, "max_value", None))


def build_filtered(
    params: ProcessParams[Any],
    rows: Sequence[dict[str, Any]],
    parent_rows: Sequence[int] | None = None,
) -> tuple[pa.RecordBatch, list[int]]:
    """Build the projected output batch, with every pushed filter applied to it.

    Everything is evaluated against ``params.output_schema`` — the *projected*
    schema — and that is load-bearing: a pushed filter carries a column index
    relative to the projection DuckDB asked for, and evaluating it against any
    other column order compares the wrong column. DuckDB never projects away a
    column it filters on, so the projected batch always holds what the predicate
    references.

    Args:
        params: The tick's parameters, carrying the filters and the projection.
        rows: Decoded API rows, keyed by the schema's column names.
        parent_rows: For a function called under ``LATERAL``, the input-row
            index each output row came from; filtered in lockstep with the rows.

    Returns:
        The projected, filtered batch and its surviving ``parent_rows``.
    """
    batch = batch_from_rows(rows, params.output_schema)
    parents = list(parent_rows or [])
    filters = _filters(params)
    if filters is None or batch.num_rows == 0:
        return batch, parents
    mask = filters.evaluate(batch)
    filtered = pc.filter(batch, mask)
    if parent_rows is None:
        return filtered, []
    # `pc.filter` drops rows whose mask is null, which is the SQL meaning of a
    # predicate that did not evaluate to true; provenance follows the same rule.
    keep = mask.to_pylist()
    return filtered, [parent for parent, ok in zip(parents, keep, strict=True) if ok]

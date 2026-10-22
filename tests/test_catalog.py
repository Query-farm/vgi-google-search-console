"""Catalog metadata: every object documented, every example well-formed.

vgi-lint checks the same things against a running worker in CI; these run in
milliseconds and point at the exact object when something is missing.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest
import yaml

from vgi_google_search_console import worker
from vgi_google_search_console.meta import comment_of

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = worker._GSC_CATALOG.schemas[0]
FUNCTIONS = list(SCHEMA.functions)
TABLES = list(SCHEMA.tables)


class TestFunctions:
    @pytest.mark.parametrize("fn", FUNCTIONS, ids=lambda f: f.Meta.name)
    def test_documented(self, fn: Any) -> None:
        tags = fn.Meta.tags
        assert fn.Meta.description
        assert len(tags["vgi.doc_llm"]) > 80
        assert len(tags["vgi.doc_md"]) > 80
        assert tags["vgi.category"] in {c["name"] for c in json.loads(worker._CATEGORIES)}

    @pytest.mark.parametrize("fn", FUNCTIONS, ids=lambda f: f.Meta.name)
    def test_examples_are_described_and_qualified(self, fn: Any) -> None:
        tagged = json.loads(fn.Meta.tags["vgi.example_queries"])
        assert tagged
        for example in tagged:
            assert example["description"]
            assert "gsc.main." in example["sql"]
        assert any(f"gsc.main.{fn.Meta.name}(" in example["sql"] for example in tagged)
        native = {e.sql for e in fn.Meta.examples}
        assert native <= {e["sql"] for e in tagged}, "every native example must also carry a description"

    @pytest.mark.parametrize("fn", FUNCTIONS, ids=lambda f: f.Meta.name)
    def test_examples_run_against_any_account(self, fn: Any) -> None:
        """No example may name a site that exists in only one account."""
        for example in json.loads(fn.Meta.tags["vgi.example_queries"]):
            assert "example.com" not in example["sql"], example["sql"]

    @pytest.mark.parametrize("fn", FUNCTIONS, ids=lambda f: f.Meta.name)
    def test_requires_the_secret(self, fn: Any) -> None:
        assert [s.secret_type for s in fn.Meta.required_secrets] == ["google_search_console"]

    @pytest.mark.parametrize(
        "fn", [f for f in FUNCTIONS if "vgi.result_columns_schema" in f.Meta.tags], ids=lambda f: f.Meta.name
    )
    def test_fixed_result_columns_are_described(self, fn: Any) -> None:
        for column in json.loads(fn.Meta.tags["vgi.result_columns_schema"]):
            assert column["description"], f"{fn.Meta.name}.{column['name']}"

    def test_names_are_unique(self) -> None:
        names = [f.Meta.name for f in FUNCTIONS]
        assert len(names) == len(set(names))

    def test_filter_pushdown_always_comes_with_auto_apply(self) -> None:
        """Declaring pushdown without applying the filters returns wrong rows."""
        for fn in FUNCTIONS:
            if getattr(fn.Meta, "filter_pushdown", False):
                assert fn.Meta.auto_apply_filters, fn.Meta.name
                assert fn.Meta.projection_pushdown, fn.Meta.name


class TestTables:
    @pytest.mark.parametrize("table", TABLES, ids=lambda t: t.name)
    def test_every_column_has_a_comment(self, table: Any) -> None:
        schema: pa.Schema = table.function.FIXED_SCHEMA
        assert set(table.column_comments) == set(schema.names)
        assert all(comment_of(f) for f in schema)

    @pytest.mark.parametrize("table", TABLES, ids=lambda t: t.name)
    def test_documented(self, table: Any) -> None:
        assert table.comment
        assert json.loads(table.tags["vgi.example_queries"])


class TestCatalog:
    def test_executable_examples_parse(self) -> None:
        for example in json.loads(worker._EXECUTABLE_EXAMPLES):
            assert {"name", "description", "sql", "expected_result"} <= set(example)

    def test_agent_tasks_are_public_only(self) -> None:
        """The catalog carries name + prompt; graders would let an evaluated agent read the answers."""
        for task in json.loads(worker._AGENT_TEST_TASKS):
            assert set(task) == {"name", "prompt"}

    def test_every_agent_task_has_a_grader_and_vice_versa(self) -> None:
        graders = yaml.safe_load((ROOT / "vgi-agent-tests.yaml").read_text())["tasks"]
        public = [t["name"] for t in json.loads(worker._AGENT_TEST_TASKS)]
        assert [g["name"] for g in graders] == public
        for grader in graders:
            assert grader["reference_sql"]
            assert grader["success_criteria"]

    def test_secret_type_is_advertised(self) -> None:
        assert [s.name for s in worker.SearchConsoleCatalog.secret_types] == ["google_search_console"]

    def test_versions(self) -> None:
        assert f"=={worker.IMPLEMENTATION_VERSION}" == worker.DATA_VERSION_SPEC

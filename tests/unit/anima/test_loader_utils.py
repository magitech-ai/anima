"""Unit tests for loader-utils module-loading utilities."""

from types import SimpleNamespace

import pytest

from anima.loader_utils import (
    ModuleJob,
    ModuleLoader,
    ModuleStatus,
    ModuleWrap,
    get_module_jobs,
    unwrap_exports,
)


def test_unwrap_exports_nullable():
    """unwrap_exports passes through None."""
    assert unwrap_exports(None) is None


def test_unwrap_exports_plain_value():
    """A non-module value is returned as-is."""
    assert unwrap_exports("plugin") == "plugin"
    assert unwrap_exports(42) == 42


def test_unwrap_exports_no_es_module():
    """A namespace without __es_module returns its default or itself."""
    ns = SimpleNamespace(default="my-plugin", other="value")
    assert unwrap_exports(ns) == "my-plugin"

    ns_no_default = SimpleNamespace(other="value")
    assert unwrap_exports(ns_no_default) is ns_no_default


def test_unwrap_exports_with_es_module():
    """A namespace with __es_module returns its default export, or itself."""
    ns = SimpleNamespace(default="esm-plugin", __es_module=True)
    assert unwrap_exports(ns) == "esm-plugin"

    # No default export: anima returns the namespace itself (?? fallback)
    ns_no_default = SimpleNamespace(__es_module=True)
    assert unwrap_exports(ns_no_default) is ns_no_default


def test_module_status_enum():
    """ModuleStatus enum values match the reference."""
    assert ModuleStatus.UNINSTANTIATED.value == 0
    assert ModuleStatus.INSTANTIATING.value == 1
    assert ModuleStatus.INSTANTIATED.value == 2
    assert ModuleStatus.EVALUATING.value == 3
    assert ModuleStatus.EVALUATED.value == 4
    assert ModuleStatus.ERRORED.value == 5


def test_get_module_jobs_empty_cache():
    """An empty load cache returns no jobs."""
    cache: dict = {}
    assert get_module_jobs(cache, "file:///app/mod.js") == []


def test_get_module_jobs_single_job():
    """A load cache with one job returns it."""
    job = {"url": "file:///app/mod.js"}
    cache = {"file:///app/mod.js": {"javascript": job}}
    assert get_module_jobs(cache, "file:///app/mod.js") == [job]


def test_get_module_jobs_multiple_types():
    """A load cache with multiple import attribute types returns all jobs."""
    job_js = {"url": "file:///app/mod.js"}
    job_json = {"url": "file:///app/mod.json"}
    cache = {
        "file:///app/mod.js": {
            "javascript": job_js,
            "json": job_json,
        }
    }
    jobs = get_module_jobs(cache, "file:///app/mod.js")
    assert job_js in jobs
    assert job_json in jobs


def test_get_module_jobs_unknown_url():
    """An unknown URL returns no jobs."""
    cache = {"file:///app/other.js": {"javascript": {"url": "file:///app/other.js"}}}
    assert get_module_jobs(cache, "file:///app/missing.js") == []


def test_unwrap_exports_es_module_with_default():
    """__es_module + default returns the default (line 33 branch)."""
    ns = SimpleNamespace(default="my-plugin", __es_module=True)
    assert unwrap_exports(ns) == "my-plugin"


def test_unwrap_exports_nested_default():
    """Nested __es_module: inner namespace's default is returned (line 33)."""
    inner = SimpleNamespace(default="inner-plugin", __es_module=True)
    ns = SimpleNamespace(default=inner, __es_module=True)
    assert unwrap_exports(ns) == "inner-plugin"


def test_module_wrap():
    """ModuleWrap stores url and its methods raise NotImplementedError."""
    wrap = ModuleWrap("file:///app/mod.js")
    assert wrap.url == "file:///app/mod.js"
    with pytest.raises(NotImplementedError):
        wrap.get_namespace()
    with pytest.raises(NotImplementedError):
        wrap.get_status()


def test_module_job():
    """ModuleJob stores attributes and its methods raise NotImplementedError."""
    job = ModuleJob("file:///app/mod.js", loader=None)
    assert job.url == "file:///app/mod.js"
    assert job.loader is None
    assert job.module is None
    assert job.import_attributes == {}
    assert job.module_promise is None
    assert job.linked is None
    assert job.instantiated is None
    with pytest.raises(NotImplementedError):
        job.instantiate()
    with pytest.raises(NotImplementedError):
        job.run()


def test_module_loader_from_internal():
    """ModuleLoader.from_internal returns None in Python (no Node loader)."""
    assert ModuleLoader.from_internal() is None

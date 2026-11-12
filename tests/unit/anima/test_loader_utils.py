"""Unit tests for loader-utils module-loading utilities."""

import pytest

from anima.loader_utils import (
    ModuleJob,
    ModuleLoader,
    ModuleStatus,
    ModuleWrap,
    get_module_jobs,
    is_nullable,
)


def test_is_nullable():
    """is_nullable is True only for None."""
    assert is_nullable(None) is True
    assert is_nullable(0) is False
    assert is_nullable("") is False
    assert is_nullable(False) is False


def test_module_status_enum():
    """ModuleStatus enum values match anima."""
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

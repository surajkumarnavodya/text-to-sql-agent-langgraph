"""Unit tests for `db/result_cache.py` -- Prompt 22 (scale/performance
hardening): the opt-in, short-TTL, bounded result cache for `POST /execute`.

Prompt 20 added the tenant partition to the cache key; every case below
passes an explicit tenant (`"t1"` unless the case is specifically about
cross-tenant behavior), and `TestResultCache
::test_two_tenants_with_identical_sql_do_not_collide` is the negative test
for the leak that partition exists to prevent.
"""

from __future__ import annotations

from db.result_cache import ResultCache, get_result_cache, is_cacheable_sql


class TestIsCacheableSql:
    def test_cacheable_when_no_restricted_columns_classified(self, monkeypatch):
        monkeypatch.setattr("db.result_cache.load_sensitive_columns", lambda: {})
        assert is_cacheable_sql("SELECT id, name FROM customers", dialect=None) is True

    def test_not_cacheable_when_sql_references_a_restricted_column(self, monkeypatch):
        monkeypatch.setattr(
            "db.result_cache.load_sensitive_columns",
            lambda: {("customers", "ssn"): "restricted"},
        )
        assert is_cacheable_sql("SELECT ssn FROM customers", dialect=None) is False

    def test_cacheable_when_restricted_column_not_referenced(self, monkeypatch):
        monkeypatch.setattr(
            "db.result_cache.load_sensitive_columns",
            lambda: {("customers", "ssn"): "restricted"},
        )
        assert is_cacheable_sql("SELECT id, name FROM customers", dialect=None) is True

    def test_not_cacheable_on_wildcard_projection(self, monkeypatch):
        monkeypatch.setattr(
            "db.result_cache.load_sensitive_columns",
            lambda: {("customers", "ssn"): "restricted"},
        )
        assert is_cacheable_sql("SELECT * FROM customers", dialect=None) is False

    def test_internal_tier_alone_does_not_block_caching(self, monkeypatch):
        """Only "restricted" blocks caching -- "internal" is a lower tier
        this cache doesn't need to reason about (see module docstring)."""
        monkeypatch.setattr(
            "db.result_cache.load_sensitive_columns",
            lambda: {("customers", "last_login"): "internal"},
        )
        assert is_cacheable_sql("SELECT last_login FROM customers", dialect=None) is True


class TestResultCache:
    def test_miss_when_never_stored(self):
        cache = ResultCache(max_entries=10)
        assert cache.get("t1", "default", "SELECT 1", ttl_seconds=30) is None

    def test_hit_returns_the_stored_columns_and_rows(self):
        cache = ResultCache(max_entries=10)
        cache.store("t1", "default", "SELECT 1", ["col"], [(1,)], now=0.0)
        assert cache.get("t1", "default", "SELECT 1", ttl_seconds=30, now=5.0) == (["col"], [(1,)])

    def test_entry_expires_past_ttl(self):
        cache = ResultCache(max_entries=10)
        cache.store("t1", "default", "SELECT 1", ["col"], [(1,)], now=0.0)
        assert cache.get("t1", "default", "SELECT 1", ttl_seconds=30, now=31.0) is None

    def test_expired_entry_is_evicted_on_read(self):
        cache = ResultCache(max_entries=10)
        cache.store("t1", "default", "SELECT 1", ["col"], [(1,)], now=0.0)
        cache.get("t1", "default", "SELECT 1", ttl_seconds=30, now=31.0)
        assert len(cache) == 0

    def test_two_databases_with_identical_sql_do_not_collide(self):
        cache = ResultCache(max_entries=10)
        cache.store("t1", "db_a", "SELECT 1", ["col"], [("a",)], now=0.0)
        cache.store("t1", "db_b", "SELECT 1", ["col"], [("b",)], now=0.0)
        assert cache.get("t1", "db_a", "SELECT 1", ttl_seconds=30, now=1.0) == (["col"], [("a",)])
        assert cache.get("t1", "db_b", "SELECT 1", ttl_seconds=30, now=1.0) == (["col"], [("b",)])

    def test_two_tenants_with_identical_sql_do_not_collide(self):
        """The cross-tenant negative test (Prompt 20): two tenants running
        byte-identical SQL against the *same* database must each see only
        their own rows. Before the tenant partition existed, the second
        `store` would have overwritten the first and both tenants would have
        read the same cached result set."""
        cache = ResultCache(max_entries=10)
        cache.store("tenant_a", "shared_db", "SELECT 1", ["col"], [("a",)], now=0.0)
        cache.store("tenant_b", "shared_db", "SELECT 1", ["col"], [("b",)], now=0.0)

        assert cache.get("tenant_a", "shared_db", "SELECT 1", ttl_seconds=30, now=1.0) == (
            ["col"],
            [("a",)],
        )
        assert cache.get("tenant_b", "shared_db", "SELECT 1", ttl_seconds=30, now=1.0) == (
            ["col"],
            [("b",)],
        )

    def test_one_tenant_never_reads_anothers_entry(self):
        """A tenant with no entry of its own gets a clean miss, not another
        tenant's cached rows."""
        cache = ResultCache(max_entries=10)
        cache.store("tenant_a", "shared_db", "SELECT 1", ["col"], [("a",)], now=0.0)

        assert cache.get("tenant_b", "shared_db", "SELECT 1", ttl_seconds=30, now=1.0) is None

    def test_different_sql_text_does_not_collide(self):
        cache = ResultCache(max_entries=10)
        cache.store("t1", "default", "SELECT 1", ["col"], [(1,)], now=0.0)
        assert cache.get("t1", "default", "SELECT 2", ttl_seconds=30, now=1.0) is None

    def test_lru_eviction_past_max_entries(self):
        cache = ResultCache(max_entries=2)
        cache.store("t1", "default", "SELECT 1", ["c"], [(1,)], now=0.0)
        cache.store("t1", "default", "SELECT 2", ["c"], [(2,)], now=0.0)
        cache.store("t1", "default", "SELECT 3", ["c"], [(3,)], now=0.0)  # evicts SELECT 1 (LRU)

        assert len(cache) == 2
        assert cache.get("t1", "default", "SELECT 1", ttl_seconds=30, now=1.0) is None
        assert cache.get("t1", "default", "SELECT 2", ttl_seconds=30, now=1.0) is not None
        assert cache.get("t1", "default", "SELECT 3", ttl_seconds=30, now=1.0) is not None

    def test_get_refreshes_recency_for_lru_purposes(self):
        cache = ResultCache(max_entries=2)
        cache.store("t1", "default", "SELECT 1", ["c"], [(1,)], now=0.0)
        cache.store("t1", "default", "SELECT 2", ["c"], [(2,)], now=0.0)
        cache.get("t1", "default", "SELECT 1", ttl_seconds=30, now=1.0)  # touches SELECT 1
        cache.store(
            "t1", "default", "SELECT 3", ["c"], [(3,)], now=2.0
        )  # should evict SELECT 2, not 1

        assert cache.get("t1", "default", "SELECT 1", ttl_seconds=30, now=3.0) is not None
        assert cache.get("t1", "default", "SELECT 2", ttl_seconds=30, now=3.0) is None

    def test_clear_removes_every_entry(self):
        cache = ResultCache(max_entries=10)
        cache.store("t1", "default", "SELECT 1", ["c"], [(1,)], now=0.0)
        cache.clear()
        assert len(cache) == 0


class TestGetResultCache:
    def test_returns_the_same_instance_on_repeated_calls(self):
        first = get_result_cache(500)
        second = get_result_cache(500)
        assert first is second

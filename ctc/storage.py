from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .compressor import CompressedItemStat
from .permissions import enforce_private_file
from .profiles import PROFILE_OFF, PROFILE_SAFE, VALID_PROFILES, normalize_profile


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class RequestStat:
    request_id: str
    timestamp: str
    model: str
    path: str
    stream: bool
    original_chars: int
    compressed_chars: int
    estimated_original_tokens: int
    estimated_compressed_tokens: int
    estimated_saved_tokens: int
    saved_ratio: float
    compressed_items_count: int
    passthrough_items_count: int
    latency_ms: int
    status_code: int
    source: str = ""
    client_host: str = ""
    profile: str = ""
    provider_id: str = ""
    provider_name: str = ""
    provider_type: str = ""
    error: str | None = None
    actual_input_tokens: int | None = None
    actual_output_tokens: int | None = None
    actual_total_tokens: int | None = None
    cached_input_tokens: int | None = None
    actual_uncached_input_tokens: int | None = None
    baseline_input_tokens: int | None = None
    baseline_uncached_input_tokens: int | None = None
    cache_aligned_saved_tokens: int | None = None
    cache_aligned_saved_ratio: float | None = None
    metering_source: str = ""


class CtcStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=30)
        enforce_private_file(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA temp_store=MEMORY")
        return conn

    @contextmanager
    def _connection(self):
        """Yield a connection that commits/rolls back and is always closed."""
        conn = self._connect()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def init_db(self) -> None:
        with self._lock, self._connection() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA wal_autocheckpoint=1000")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS request_stats (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id TEXT NOT NULL UNIQUE,
                    timestamp TEXT NOT NULL,
                    model TEXT NOT NULL DEFAULT '',
                    path TEXT NOT NULL,
                    stream INTEGER NOT NULL DEFAULT 0,
                    original_chars INTEGER NOT NULL DEFAULT 0,
                    compressed_chars INTEGER NOT NULL DEFAULT 0,
                    estimated_original_tokens INTEGER NOT NULL DEFAULT 0,
                    estimated_compressed_tokens INTEGER NOT NULL DEFAULT 0,
                    estimated_saved_tokens INTEGER NOT NULL DEFAULT 0,
                    saved_ratio REAL NOT NULL DEFAULT 0,
                    compressed_items_count INTEGER NOT NULL DEFAULT 0,
                    passthrough_items_count INTEGER NOT NULL DEFAULT 0,
                    latency_ms INTEGER NOT NULL DEFAULT 0,
                    status_code INTEGER NOT NULL DEFAULT 0,
                    source TEXT NOT NULL DEFAULT '',
                    client_host TEXT NOT NULL DEFAULT '',
                    profile TEXT NOT NULL DEFAULT '',
                    provider_id TEXT NOT NULL DEFAULT '',
                    provider_name TEXT NOT NULL DEFAULT '',
                    provider_type TEXT NOT NULL DEFAULT '',
                    error TEXT,
                    actual_input_tokens INTEGER,
                    actual_output_tokens INTEGER,
                    actual_total_tokens INTEGER,
                    cached_input_tokens INTEGER,
                    actual_uncached_input_tokens INTEGER,
                    baseline_input_tokens INTEGER,
                    baseline_uncached_input_tokens INTEGER,
                    cache_aligned_saved_tokens INTEGER,
                    cache_aligned_saved_ratio REAL,
                    metering_source TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_request_stats_timestamp
                    ON request_stats(timestamp);
                CREATE INDEX IF NOT EXISTS idx_request_stats_error
                    ON request_stats(error);
                CREATE INDEX IF NOT EXISTS idx_request_stats_timestamp_host
                    ON request_stats(timestamp, client_host);
                CREATE INDEX IF NOT EXISTS idx_request_stats_timestamp_status
                    ON request_stats(timestamp, status_code);

                CREATE TABLE IF NOT EXISTS profile_rules (
                    client_host TEXT PRIMARY KEY,
                    profile TEXT NOT NULL,
                    label TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS compressed_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id TEXT NOT NULL,
                    item_index INTEGER NOT NULL,
                    field_path TEXT NOT NULL,
                    tool_name TEXT,
                    original_chars INTEGER NOT NULL,
                    compressed_chars INTEGER NOT NULL,
                    original_tokens INTEGER NOT NULL,
                    compressed_tokens INTEGER NOT NULL,
                    saved_tokens INTEGER NOT NULL,
                    output_hash TEXT NOT NULL,
                    FOREIGN KEY(request_id) REFERENCES request_stats(request_id)
                );
                CREATE INDEX IF NOT EXISTS idx_compressed_items_request
                    ON compressed_items(request_id);

                CREATE TABLE IF NOT EXISTS daily_rollups (
                    day TEXT PRIMARY KEY,
                    request_count INTEGER NOT NULL DEFAULT 0,
                    compressed_request_count INTEGER NOT NULL DEFAULT 0,
                    estimated_original_tokens INTEGER NOT NULL DEFAULT 0,
                    estimated_compressed_tokens INTEGER NOT NULL DEFAULT 0,
                    estimated_saved_tokens INTEGER NOT NULL DEFAULT 0
                );
                """
            )
            self._ensure_request_stats_columns(conn)
            self._ensure_request_stats_indexes(conn)
            self._ensure_profile_rules_columns(conn)
            conn.execute("PRAGMA optimize")
        enforce_private_file(self.path)
        enforce_private_file(Path(f"{self.path}-wal"))
        enforce_private_file(Path(f"{self.path}-shm"))

    def _ensure_request_stats_columns(self, conn: sqlite3.Connection) -> None:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(request_stats)").fetchall()}
        migrations = {
            "source": "ALTER TABLE request_stats ADD COLUMN source TEXT NOT NULL DEFAULT ''",
            "client_host": "ALTER TABLE request_stats ADD COLUMN client_host TEXT NOT NULL DEFAULT ''",
            "profile": "ALTER TABLE request_stats ADD COLUMN profile TEXT NOT NULL DEFAULT ''",
            "provider_id": "ALTER TABLE request_stats ADD COLUMN provider_id TEXT NOT NULL DEFAULT ''",
            "provider_name": "ALTER TABLE request_stats ADD COLUMN provider_name TEXT NOT NULL DEFAULT ''",
            "provider_type": "ALTER TABLE request_stats ADD COLUMN provider_type TEXT NOT NULL DEFAULT ''",
            "actual_input_tokens": "ALTER TABLE request_stats ADD COLUMN actual_input_tokens INTEGER",
            "actual_output_tokens": "ALTER TABLE request_stats ADD COLUMN actual_output_tokens INTEGER",
            "actual_total_tokens": "ALTER TABLE request_stats ADD COLUMN actual_total_tokens INTEGER",
            "cached_input_tokens": "ALTER TABLE request_stats ADD COLUMN cached_input_tokens INTEGER",
            "actual_uncached_input_tokens": "ALTER TABLE request_stats ADD COLUMN actual_uncached_input_tokens INTEGER",
            "baseline_input_tokens": "ALTER TABLE request_stats ADD COLUMN baseline_input_tokens INTEGER",
            "baseline_uncached_input_tokens": "ALTER TABLE request_stats ADD COLUMN baseline_uncached_input_tokens INTEGER",
            "cache_aligned_saved_tokens": "ALTER TABLE request_stats ADD COLUMN cache_aligned_saved_tokens INTEGER",
            "cache_aligned_saved_ratio": "ALTER TABLE request_stats ADD COLUMN cache_aligned_saved_ratio REAL",
            "metering_source": "ALTER TABLE request_stats ADD COLUMN metering_source TEXT NOT NULL DEFAULT ''",
        }
        for column_name, sql in migrations.items():
            if column_name not in columns:
                conn.execute(sql)

    def _ensure_request_stats_indexes(self, conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_request_stats_host_timestamp
                ON request_stats(client_host, timestamp DESC);
            CREATE INDEX IF NOT EXISTS idx_request_stats_errors_recent
                ON request_stats(timestamp DESC, client_host)
                WHERE error IS NOT NULL OR status_code >= 400;
            CREATE INDEX IF NOT EXISTS idx_request_stats_metering_recent
                ON request_stats(timestamp DESC)
                WHERE metering_source != '';
            """
        )

    def _ensure_profile_rules_columns(self, conn: sqlite3.Connection) -> None:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(profile_rules)").fetchall()}
        if "last_seen_at" not in columns:
            conn.execute("ALTER TABLE profile_rules ADD COLUMN last_seen_at TEXT NOT NULL DEFAULT ''")
        if "source" not in columns:
            conn.execute("ALTER TABLE profile_rules ADD COLUMN source TEXT NOT NULL DEFAULT ''")

    @staticmethod
    def default_profile_for_host(client_host: str) -> str:
        return PROFILE_SAFE if client_host in {"127.0.0.1", "::1", "localhost"} else PROFILE_OFF

    def resolve_profile(self, client_host: str, fallback: str | None = None) -> str:
        fallback_profile = normalize_profile(fallback, self.default_profile_for_host(client_host))
        if not client_host:
            return fallback_profile
        with self._lock, self._connection() as conn:
            row = conn.execute(
                "SELECT profile FROM profile_rules WHERE client_host = ?",
                (client_host,),
            ).fetchone()
        if not row:
            return fallback_profile
        return normalize_profile(row["profile"], fallback_profile)

    def set_profile_rule(self, client_host: str, profile: str, label: str = "") -> dict[str, Any]:
        if not client_host:
            raise ValueError("client_host is required")
        normalized = normalize_profile(profile, "")
        if normalized not in VALID_PROFILES:
            raise ValueError(f"invalid profile: {profile}")
        now = utc_now_iso()
        with self._lock, self._connection() as conn:
            current = conn.execute(
                "SELECT source, last_seen_at FROM profile_rules WHERE client_host = ?",
                (client_host,),
            ).fetchone()
            source = current["source"] if current else ""
            last_seen = current["last_seen_at"] if current else ""
            conn.execute(
                """
                INSERT INTO profile_rules (client_host, profile, label, updated_at, last_seen_at, source)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(client_host) DO UPDATE SET
                    profile = excluded.profile,
                    label = excluded.label,
                    updated_at = excluded.updated_at
                """,
                (client_host, normalized, label, now, last_seen, source),
            )
            row = conn.execute(
                """
                SELECT client_host, profile, label, updated_at, last_seen_at, source
                FROM profile_rules
                WHERE client_host = ?
                """,
                (client_host,),
            ).fetchone()
            return dict(row)

    def touch_profile_source(self, client_host: str, source: str, profile: str) -> None:
        if not client_host:
            return
        now = utc_now_iso()
        normalized = normalize_profile(profile, self.default_profile_for_host(client_host))
        with self._lock, self._connection() as conn:
            conn.execute(
                """
                INSERT INTO profile_rules (client_host, profile, label, updated_at, last_seen_at, source)
                VALUES (?, ?, '', ?, ?, ?)
                ON CONFLICT(client_host) DO UPDATE SET
                    last_seen_at = excluded.last_seen_at,
                    source = excluded.source
                """,
                (client_host, normalized, now, now, source),
            )

    def profile_sources(self) -> list[dict[str, Any]]:
        with self._lock, self._connection() as conn:
            seen_rows = conn.execute(
                """
                SELECT client_host,
                       MAX(source) AS source,
                       MAX(timestamp) AS last_request_at,
                       COUNT(*) AS request_count
                FROM request_stats
                WHERE client_host != ''
                GROUP BY client_host
                """
            ).fetchall()
            rule_rows = conn.execute(
                """
                SELECT client_host, profile, label, updated_at, last_seen_at, source
                FROM profile_rules
                """
            ).fetchall()

        by_host: dict[str, dict[str, Any]] = {}
        for row in seen_rows:
            by_host[row["client_host"]] = {
                "client_host": row["client_host"],
                "source": row["source"] or "",
                "configured_profile": "",
                "effective_profile": self.default_profile_for_host(row["client_host"]),
                "label": "",
                "updated_at": "",
                "last_seen_at": row["last_request_at"] or "",
                "request_count": int(row["request_count"] or 0),
            }
        for row in rule_rows:
            host = row["client_host"]
            item = by_host.setdefault(
                host,
                {
                    "client_host": host,
                    "source": "",
                    "configured_profile": "",
                    "effective_profile": self.default_profile_for_host(host),
                    "label": "",
                    "updated_at": "",
                    "last_seen_at": "",
                    "request_count": 0,
                },
            )
            item["source"] = row["source"] or item["source"]
            item["configured_profile"] = row["profile"] or ""
            item["effective_profile"] = normalize_profile(row["profile"], self.default_profile_for_host(host))
            item["label"] = row["label"] or ""
            item["updated_at"] = row["updated_at"] or ""
            item["last_seen_at"] = row["last_seen_at"] or item["last_seen_at"]
        return sorted(by_host.values(), key=lambda item: (item["last_seen_at"], item["client_host"]), reverse=True)

    def traffic_sources(self, since: str, until: str) -> list[dict[str, Any]]:
        with self._lock, self._connection() as conn:
            rows = conn.execute(
                """
                SELECT
                    client_host,
                    MAX(source) AS source,
                    COUNT(*) AS request_count,
                    SUM(CASE WHEN metering_source != '' THEN 1 ELSE 0 END) AS metered_requests,
                    COALESCE(SUM(cache_aligned_saved_tokens), 0) AS cache_aligned_saved_tokens,
                    SUM(CASE WHEN (error IS NOT NULL OR status_code >= 400)
                              AND NOT (path = '/v1/props' AND status_code = 404)
                             THEN 1 ELSE 0 END) AS error_count
                FROM request_stats
                WHERE timestamp >= ? AND timestamp <= ?
                  AND client_host != ''
                  AND NOT (path = '/v1/props' AND status_code = 404)
                GROUP BY client_host
                ORDER BY request_count DESC, client_host ASC
                """,
                (since, until),
            ).fetchall()
            return [
                {
                    "client_host": row["client_host"],
                    "source": row["source"] or "",
                    "request_count": int(row["request_count"] or 0),
                    "metered_requests": int(row["metered_requests"] or 0),
                    "cache_aligned_saved_tokens": int(row["cache_aligned_saved_tokens"] or 0),
                    "error_count": int(row["error_count"] or 0),
                }
                for row in rows
            ]

    def record_request(self, stat: RequestStat, items: list[CompressedItemStat]) -> None:
        with self._lock, self._connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO request_stats (
                    request_id, timestamp, model, path, stream,
                    original_chars, compressed_chars,
                    estimated_original_tokens, estimated_compressed_tokens,
                    estimated_saved_tokens, saved_ratio,
                    compressed_items_count, passthrough_items_count,
                    latency_ms, status_code, source, client_host, profile,
                    provider_id, provider_name, provider_type, error,
                    actual_input_tokens, actual_output_tokens, actual_total_tokens,
                    cached_input_tokens, actual_uncached_input_tokens,
                    baseline_input_tokens, baseline_uncached_input_tokens,
                    cache_aligned_saved_tokens, cache_aligned_saved_ratio, metering_source
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    stat.request_id,
                    stat.timestamp,
                    stat.model,
                    stat.path,
                    1 if stat.stream else 0,
                    stat.original_chars,
                    stat.compressed_chars,
                    stat.estimated_original_tokens,
                    stat.estimated_compressed_tokens,
                    stat.estimated_saved_tokens,
                    stat.saved_ratio,
                    stat.compressed_items_count,
                    stat.passthrough_items_count,
                    stat.latency_ms,
                    stat.status_code,
                    stat.source,
                    stat.client_host,
                    stat.profile,
                    stat.provider_id,
                    stat.provider_name,
                    stat.provider_type,
                    stat.error,
                    stat.actual_input_tokens,
                    stat.actual_output_tokens,
                    stat.actual_total_tokens,
                    stat.cached_input_tokens,
                    stat.actual_uncached_input_tokens,
                    stat.baseline_input_tokens,
                    stat.baseline_uncached_input_tokens,
                    stat.cache_aligned_saved_tokens,
                    stat.cache_aligned_saved_ratio,
                    stat.metering_source,
                ),
            )
            conn.execute("DELETE FROM compressed_items WHERE request_id = ?", (stat.request_id,))
            conn.executemany(
                """
                INSERT INTO compressed_items (
                    request_id, item_index, field_path, tool_name,
                    original_chars, compressed_chars,
                    original_tokens, compressed_tokens, saved_tokens, output_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        stat.request_id,
                        item.item_index,
                        item.field_path,
                        item.tool_name,
                        item.original_chars,
                        item.compressed_chars,
                        item.original_tokens,
                        item.compressed_tokens,
                        item.saved_tokens,
                        item.output_hash,
                    )
                    for item in items
                ],
            )
            if stat.client_host:
                normalized = normalize_profile(stat.profile, self.default_profile_for_host(stat.client_host))
                conn.execute(
                    """
                    INSERT INTO profile_rules (client_host, profile, label, updated_at, last_seen_at, source)
                    VALUES (?, ?, '', ?, ?, ?)
                    ON CONFLICT(client_host) DO UPDATE SET
                        last_seen_at = excluded.last_seen_at,
                        source = excluded.source
                    """,
                    (stat.client_host, normalized, stat.timestamp, stat.timestamp, stat.source),
                )

    def dashboard_summary(self, since: str, until: str) -> dict[str, Any]:
        with self._lock, self._connection() as conn:
            summary = conn.execute(
                """
                SELECT
                    COUNT(*) AS total_requests,
                    SUM(CASE WHEN compressed_items_count > 0 THEN 1 ELSE 0 END) AS compressed_requests,
                    COALESCE(SUM(estimated_original_tokens), 0) AS estimated_original_tokens,
                    COALESCE(SUM(estimated_compressed_tokens), 0) AS estimated_compressed_tokens,
                    COALESCE(SUM(estimated_saved_tokens), 0) AS estimated_saved_tokens,
                    COALESCE(AVG(CASE WHEN estimated_saved_tokens > 0 THEN estimated_saved_tokens END), 0) AS avg_saved_tokens,
                    SUM(CASE WHEN metering_source != '' THEN 1 ELSE 0 END) AS metered_requests,
                    COALESCE(SUM(actual_input_tokens), 0) AS actual_input_tokens,
                    COALESCE(SUM(actual_output_tokens), 0) AS actual_output_tokens,
                    COALESCE(SUM(actual_total_tokens), 0) AS actual_total_tokens,
                    COALESCE(SUM(cached_input_tokens), 0) AS cached_input_tokens,
                    COALESCE(SUM(actual_uncached_input_tokens), 0) AS actual_uncached_input_tokens,
                    COALESCE(SUM(baseline_input_tokens), 0) AS baseline_input_tokens,
                    COALESCE(SUM(baseline_uncached_input_tokens), 0) AS baseline_uncached_input_tokens,
                    COALESCE(SUM(cache_aligned_saved_tokens), 0) AS cache_aligned_saved_tokens,
                    SUM(CASE WHEN (error IS NOT NULL OR status_code >= 400)
                              AND NOT (path = '/v1/props' AND status_code = 404)
                             THEN 1 ELSE 0 END) AS error_count
                FROM request_stats
                WHERE timestamp >= ? AND timestamp <= ?
                """,
                (since, until),
            ).fetchone()
            total_original = int(summary["estimated_original_tokens"] or 0)
            total_saved = int(summary["estimated_saved_tokens"] or 0)
            baseline_uncached = int(summary["baseline_uncached_input_tokens"] or 0)
            cache_aligned_saved = int(summary["cache_aligned_saved_tokens"] or 0)
            total_requests = int(summary["total_requests"] or 0)
            metered_requests = int(summary["metered_requests"] or 0)
            actual_input_tokens = int(summary["actual_input_tokens"] or 0)
            baseline_input_tokens = int(summary["baseline_input_tokens"] or 0)
            return {
                "total_requests": total_requests,
                "compressed_requests": int(summary["compressed_requests"] or 0),
                "estimated_original_tokens": total_original,
                "estimated_compressed_tokens": int(summary["estimated_compressed_tokens"] or 0),
                "estimated_saved_tokens": total_saved,
                "saved_ratio": (total_saved / total_original) if total_original else 0,
                "avg_saved_tokens": float(summary["avg_saved_tokens"] or 0),
                "metered_requests": metered_requests,
                "metered_coverage_ratio": (metered_requests / total_requests) if total_requests else 0,
                "actual_input_tokens": actual_input_tokens,
                "actual_output_tokens": int(summary["actual_output_tokens"] or 0),
                "actual_total_tokens": int(summary["actual_total_tokens"] or 0),
                "cached_input_tokens": int(summary["cached_input_tokens"] or 0),
                "actual_uncached_input_tokens": int(summary["actual_uncached_input_tokens"] or 0),
                "baseline_input_tokens": baseline_input_tokens,
                "baseline_uncached_input_tokens": baseline_uncached,
                "cache_aligned_saved_tokens": cache_aligned_saved,
                "cache_aligned_saved_ratio": (cache_aligned_saved / baseline_uncached) if baseline_uncached else 0,
                "estimated_vs_metered_saved_delta": total_saved - cache_aligned_saved,
                "baseline_minus_actual_input_tokens": max(0, baseline_input_tokens - actual_input_tokens),
                "error_count": int(summary["error_count"] or 0),
            }

    def dashboard_trend(self, since: str, until: str) -> list[dict[str, Any]]:
        with self._lock, self._connection() as conn:
            rows = conn.execute(
                """
                SELECT
                    substr(timestamp, 1, 13) || ':00:00+00:00' AS bucket,
                    source,
                    client_host,
                    COUNT(*) AS request_count,
                    COALESCE(SUM(estimated_saved_tokens), 0) AS saved_tokens,
                    COALESCE(SUM(cache_aligned_saved_tokens), 0) AS cache_aligned_saved_tokens,
                    SUM(CASE WHEN metering_source != '' THEN 1 ELSE 0 END) AS metered_requests
                FROM request_stats
                WHERE timestamp >= ? AND timestamp <= ?
                GROUP BY bucket, source, client_host
                ORDER BY bucket ASC, source ASC, client_host ASC
                """,
                (since, until),
            ).fetchall()
            return [dict(row) for row in rows]

    def recent_requests_count(self, since: str, until: str, client_host: str | None = None) -> int:
        with self._lock, self._connection() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*)
                FROM request_stats
                WHERE timestamp >= ? AND timestamp <= ?
                  AND NOT (path = '/v1/props' AND status_code = 404)
                  AND (? IS NULL OR client_host = ?)
                """,
                (since, until, client_host, client_host),
            ).fetchone()
            return int(row[0] or 0)

    def recent_requests(
        self,
        since: str,
        until: str,
        limit: int = 80,
        offset: int = 0,
        client_host: str | None = None,
    ) -> list[dict[str, Any]]:
        with self._lock, self._connection() as conn:
            rows = conn.execute(
                """
                SELECT timestamp, model, path, stream, status_code, source, client_host,
                       estimated_original_tokens, estimated_compressed_tokens,
                       estimated_saved_tokens, compressed_items_count, passthrough_items_count,
                       latency_ms, profile, provider_id, provider_name, provider_type, error,
                       actual_input_tokens, actual_output_tokens, actual_total_tokens,
                       cached_input_tokens, actual_uncached_input_tokens,
                       baseline_input_tokens, baseline_uncached_input_tokens,
                       cache_aligned_saved_tokens, cache_aligned_saved_ratio, metering_source
                FROM request_stats
                WHERE timestamp >= ? AND timestamp <= ?
                  AND NOT (path = '/v1/props' AND status_code = 404)
                  AND (? IS NULL OR client_host = ?)
                ORDER BY timestamp DESC
                LIMIT ?
                OFFSET ?
                """,
                (since, until, client_host, client_host, limit, offset),
            ).fetchall()
            return [self._request_row_with_compression_state(row) for row in rows]

    @staticmethod
    def _request_row_with_compression_state(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        path = str(item.get("path") or "")
        profile = normalize_profile(str(item.get("profile") or ""), "")
        compressed_items = int(item.get("compressed_items_count") or 0)
        saved_tokens = int(item.get("estimated_saved_tokens") or 0)
        passthrough_items = int(item.get("passthrough_items_count") or 0)

        if compressed_items > 0:
            item["compression_status"] = "compressed"
            if saved_tokens > 0:
                item["compression_reason"] = f"已压缩 {compressed_items} 项，估算节省 {saved_tokens} token"
            else:
                item["compression_reason"] = f"已处理 {compressed_items} 个压缩候选项，但估算节省为 0"
        elif path != "/v1/responses":
            if path == "/v1/chat/completions":
                item["compression_status"] = "skipped"
                item["compression_reason"] = (
                    "Chat 请求经过 CTC，但未发现超过阈值的可压缩 tool 消息"
                    if passthrough_items > 0
                    else "Chat 请求经过 CTC，但未命中当前压缩规则"
                )
            else:
                item["compression_status"] = "transparent"
                item["compression_reason"] = "透明转发请求，不参与 Responses/Chat 压缩"
        elif profile == PROFILE_OFF:
            item["compression_status"] = "off"
            item["compression_reason"] = "当前来源 profile=off，按配置原样转发"
        elif passthrough_items > 0:
            item["compression_status"] = "skipped"
            item["compression_reason"] = "请求经过 CTC，但未发现超过阈值的可压缩工具输出"
        else:
            item["compression_status"] = "skipped"
            item["compression_reason"] = "请求经过 CTC，但未命中当前压缩规则"

        if item.get("metering_source"):
            actual_input = int(item.get("actual_input_tokens") or 0)
            baseline_input = int(item.get("baseline_input_tokens") or 0)
            cached_input = int(item.get("cached_input_tokens") or 0)
            saved_aligned = int(item.get("cache_aligned_saved_tokens") or 0)
            item["metering_reason"] = (
                f"同缓存状态基线 {baseline_input} / 实际 {actual_input} / 缓存命中 {cached_input} / 对齐节省 {saved_aligned} token"
            )
        else:
            item["metering_reason"] = "上游未返回 usage，无法做反事实基线对齐计量"
        return item

    def latest_requests_for_trend(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock, self._connection() as conn:
            rows = conn.execute(
                """
                SELECT timestamp, source, client_host, profile, estimated_saved_tokens, status_code
                     , COALESCE(cache_aligned_saved_tokens, 0) AS cache_aligned_saved_tokens
                FROM request_stats
                WHERE NOT (path = '/v1/props' AND status_code = 404)
                ORDER BY timestamp DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [dict(row) for row in reversed(rows)]

    def error_requests_count(self, since: str, until: str, client_host: str | None = None) -> int:
        with self._lock, self._connection() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*)
                FROM request_stats
                WHERE timestamp >= ? AND timestamp <= ?
                  AND (error IS NOT NULL OR status_code >= 400)
                  AND NOT (path = '/v1/props' AND status_code = 404)
                  AND (? IS NULL OR client_host = ?)
                """,
                (since, until, client_host, client_host),
            ).fetchone()
            return int(row[0] or 0)

    def error_requests(
        self,
        since: str,
        until: str,
        limit: int = 50,
        offset: int = 0,
        client_host: str | None = None,
    ) -> list[dict[str, Any]]:
        with self._lock, self._connection() as conn:
            rows = conn.execute(
                """
                SELECT timestamp, model, path, stream, status_code, source, client_host,
                       latency_ms, profile, provider_id, provider_name, provider_type, error,
                       actual_input_tokens, baseline_input_tokens, cached_input_tokens,
                       cache_aligned_saved_tokens, metering_source
                FROM request_stats
                WHERE timestamp >= ? AND timestamp <= ?
                  AND (error IS NOT NULL OR status_code >= 400)
                  AND NOT (path = '/v1/props' AND status_code = 404)
                  AND (? IS NULL OR client_host = ?)
                ORDER BY timestamp DESC
                LIMIT ?
                OFFSET ?
                """,
                (since, until, client_host, client_host, limit, offset),
            ).fetchall()
            return [dict(row) for row in rows]

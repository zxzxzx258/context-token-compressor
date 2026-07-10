from __future__ import annotations

import sqlite3

from ctc.compressor import CompressedItemStat
from ctc.storage import CtcStore, RequestStat, utc_now_iso


def test_store_records_stats_without_raw_output(tmp_path):
    db = tmp_path / "ctc.sqlite3"
    store = CtcStore(db)
    item = CompressedItemStat(
        item_index=0,
        field_path="$.output",
        tool_name="shell",
        original_chars=10000,
        compressed_chars=2000,
        original_tokens=2500,
        compressed_tokens=500,
        saved_tokens=2000,
        output_hash="abc123",
    )
    store.record_request(
        RequestStat(
            request_id="r1",
            timestamp=utc_now_iso(),
            model="gpt-5.5",
            path="/v1/responses",
            stream=True,
            original_chars=10000,
            compressed_chars=2000,
            estimated_original_tokens=2500,
            estimated_compressed_tokens=500,
            estimated_saved_tokens=2000,
            saved_ratio=0.8,
            compressed_items_count=1,
            passthrough_items_count=2,
            latency_ms=30,
            status_code=200,
            source="LAN 192.0.2.13",
            client_host="192.0.2.13",
            profile="dev",
            provider_id="example-provider-00000000",
            provider_name="示例上游",
            provider_type="openai_responses",
            error=None,
        ),
        [item],
    )
    with sqlite3.connect(db) as con:
        item_row = con.execute(
            "select output_hash, original_chars, compressed_chars from compressed_items where request_id = ?",
            ("r1",),
        ).fetchone()
        request_row = con.execute(
            "select source, client_host, profile, provider_id, provider_name, provider_type, error from request_stats where request_id = ?",
            ("r1",),
        ).fetchone()
    assert item_row == ("abc123", 10000, 2000)
    assert request_row == (
        "LAN 192.0.2.13",
        "192.0.2.13",
        "dev",
        "example-provider-00000000",
        "示例上游",
        "openai_responses",
        None,
    )
    summary = store.dashboard_summary("0000", "9999")
    assert summary["total_requests"] == 1
    assert summary["estimated_saved_tokens"] == 2000
    recent = store.recent_requests("0000", "9999")
    assert recent[0]["source"] == "LAN 192.0.2.13"
    assert recent[0]["client_host"] == "192.0.2.13"
    assert recent[0]["profile"] == "dev"
    assert recent[0]["provider_id"] == "example-provider-00000000"
    trend = store.dashboard_trend("0000", "9999")
    assert trend[0]["source"] == "LAN 192.0.2.13"
    assert trend[0]["client_host"] == "192.0.2.13"
    assert trend[0]["saved_tokens"] == 2000
    sources = store.profile_sources()
    assert sources[0]["client_host"] == "192.0.2.13"
    assert sources[0]["effective_profile"] == "dev"


def test_profile_rules_default_and_update(tmp_path):
    db = tmp_path / "ctc.sqlite3"
    store = CtcStore(db)

    assert store.resolve_profile("127.0.0.1") == "safe"
    assert store.resolve_profile("192.0.2.13") == "off"

    row = store.set_profile_rule("192.0.2.13", "dev")
    assert row["profile"] == "dev"
    assert store.resolve_profile("192.0.2.13") == "dev"

    store.touch_profile_source("192.0.2.13", "LAN 192.0.2.13", "dev")
    source = store.profile_sources()[0]
    assert source["client_host"] == "192.0.2.13"
    assert source["source"] == "LAN 192.0.2.13"
    assert source["effective_profile"] == "dev"


def test_store_initializes_wal_mode(tmp_path):
    db = tmp_path / "ctc.sqlite3"
    CtcStore(db)

    with sqlite3.connect(db) as con:
        journal_mode = con.execute("pragma journal_mode").fetchone()[0]

    assert journal_mode == "wal"

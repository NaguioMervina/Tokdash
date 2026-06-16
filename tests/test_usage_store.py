from __future__ import annotations

from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

import tokdash.sessions as sessions_module
from tokdash.sources.coding_tools import CodingToolsUsageTracker
from tokdash.usage_store import UsageEntryStore, build_source_signature, parser_code_signature


def test_usage_store_syncs_and_queries_by_range(tmp_path):
    store = UsageEntryStore(tmp_path / "usage.sqlite3")
    calls = {"count": 0}

    def parse_entries():
        calls["count"] += 1
        return [
            {
                "source": "codex",
                "model": "gpt-5.3-codex",
                "provider": "openai",
                "input": 10,
                "output": 5,
                "cacheRead": 7,
                "cacheWrite": 3,
                "reasoning": 2,
                "cost": 0.01,
                "timestamp": 1_700_000_000_000,
            },
            {
                "source": "codex",
                "model": "gpt-5.3-codex",
                "provider": "openai",
                "input": 1,
                "output": 1,
                "cacheRead": 0,
                "cacheWrite": 0,
                "reasoning": 0,
                "cost": 0.001,
                "timestamp": 1_800_000_000_000,
            },
        ]

    sig = build_source_signature(files=[["a.jsonl", 1, 2]], pricing=[3, 4], parser={"v": 1})

    assert store.sync_source("codex", sig, parse_entries) is True
    assert store.sync_source("codex", sig, parse_entries) is False
    assert calls["count"] == 1

    entries = store.query_entries(
        sources=["codex"],
        since=datetime.fromtimestamp(1_699_999_999, timezone.utc),
        until=datetime.fromtimestamp(1_700_000_001, timezone.utc),
    )

    assert len(entries) == 1
    assert entries[0]["source"] == "codex"
    assert entries[0]["cacheWrite"] == 3
    assert entries[0]["messageCount"] == 1


def test_usage_store_replaces_source_when_signature_changes(tmp_path):
    store = UsageEntryStore(tmp_path / "usage.sqlite3")

    store.sync_source(
        "claude",
        build_source_signature(files=[["old.jsonl", 1, 1]], parser={"v": 1}),
        lambda: [
            {
                "source": "claude",
                "model": "claude-sonnet-4",
                "timestamp": 1_700_000_000_000,
                "input": 10,
            }
        ],
    )
    store.sync_source(
        "claude",
        build_source_signature(files=[["new.jsonl", 2, 2]], parser={"v": 1}),
        lambda: [
            {
                "source": "claude",
                "model": "claude-sonnet-4",
                "timestamp": 1_700_000_001_000,
                "input": 20,
                "messageCount": 4,
            }
        ],
    )

    entries = store.query_entries(sources=["claude"])
    assert len(entries) == 1
    assert entries[0]["timestamp"] == 1_700_000_001_000
    assert entries[0]["input"] == 20
    assert entries[0]["messageCount"] == 4


def test_usage_store_aggregates_without_loading_raw_rows(tmp_path):
    store = UsageEntryStore(tmp_path / "usage.sqlite3")
    store.sync_source(
        "codex",
        build_source_signature(files=[["codex.jsonl", 1, 1]], parser={"v": 1}),
        lambda: [
            {
                "source": "codex",
                "model": "gpt-5.3-codex",
                "provider": "openai",
                "timestamp": 1_700_000_000_000,
                "input": 10,
                "output": 5,
                "cacheRead": 7,
                "cacheWrite": 3,
                "reasoning": 2,
                "cost": 0.1,
                "messageCount": 2,
            },
            {
                "source": "codex",
                "model": "gpt-5.3-codex",
                "provider": "openai",
                "timestamp": 1_700_000_100_000,
                "input": 20,
                "output": 10,
                "cacheRead": 0,
                "cacheWrite": 1,
                "reasoning": 4,
                "cost": 0.2,
                "messageCount": 3,
            },
        ],
    )

    data = store.aggregate_entries(sources=["codex"])

    assert data["total_tokens"] == 62
    assert data["total_messages"] == 5
    assert data["cache_hit_rate"] == round(7 / (34 + 7), 4)
    app = data["apps"]["codex"]
    assert app["tokens_in"] == 34
    assert app["tokens_cache"] == 7
    assert app["models"][0]["name"] == "openai/gpt-5.3-codex"


def test_usage_store_contribution_days_use_sql_date_window(tmp_path):
    store = UsageEntryStore(tmp_path / "usage.sqlite3")
    store.sync_source(
        "claude",
        build_source_signature(files=[["claude.jsonl", 1, 1]], parser={"v": 1}),
        lambda: [
            {
                "source": "claude",
                "model": "claude-sonnet-4",
                "provider": "anthropic",
                "timestamp": 1_700_000_000_000,
                "input": 10,
                "output": 5,
                "cacheRead": 2,
                "cacheWrite": 3,
                "reasoning": 1,
                "cost": 0.1,
            },
            {
                "source": "claude",
                "model": "claude-sonnet-4",
                "provider": "anthropic",
                "timestamp": 1_800_000_000_000,
                "input": 100,
                "output": 50,
                "cacheRead": 20,
                "cacheWrite": 30,
                "reasoning": 10,
                "cost": 1.0,
            },
        ],
    )

    days = store.contribution_days(
        sources=["claude"],
        since=datetime.fromtimestamp(1_699_999_999, timezone.utc),
        until=datetime.fromtimestamp(1_700_000_001, timezone.utc),
    )

    assert len(days) == 1
    assert days[0]["totals"]["tokens"] == 21
    assert days[0]["totals"]["messages"] == 1
    assert days[0]["tokenBreakdown"] == {
        "input": 13,
        "output": 5,
        "cacheRead": 2,
        "cacheWrite": 0,
        "reasoning": 1,
    }
    assert days[0]["sources"][0]["providerId"] == "anthropic"


def test_usage_store_sync_files_replaces_only_changed_files(tmp_path):
    store = UsageEntryStore(tmp_path / "usage.sqlite3")
    calls: list[str] = []

    def parse_file(file_sig):
        path, _mtime_ns, _size = file_sig
        calls.append(path)
        return [
            {
                "source": "codex",
                "model": "gpt-5.3-codex",
                "provider": "openai",
                "timestamp": 1_700_000_000_000 if path.endswith("a.jsonl") else 1_700_000_010_000,
                "input": 10 if path.endswith("a.jsonl") else 20,
                "output": 1,
            }
        ]

    files_v1 = (
        (str(tmp_path / "a.jsonl"), 1, 100),
        (str(tmp_path / "b.jsonl"), 1, 100),
    )
    files_v2 = (
        (str(tmp_path / "a.jsonl"), 1, 100),
        (str(tmp_path / "b.jsonl"), 2, 200),
    )

    assert store.sync_files("codex", files_v1, parser={"v": 1}, parse_file_entries=parse_file) is True
    assert calls == [files_v1[0][0], files_v1[1][0]]
    assert store.sync_files("codex", files_v1, parser={"v": 1}, parse_file_entries=parse_file) is False
    assert calls == [files_v1[0][0], files_v1[1][0]]

    assert store.sync_files("codex", files_v2, parser={"v": 1}, parse_file_entries=parse_file) is True
    assert calls == [files_v1[0][0], files_v1[1][0], files_v2[1][0]]

    data = store.aggregate_entries(sources=["codex"])
    assert data["total_tokens"] == 32
    assert data["total_messages"] == 2


def test_usage_store_sync_files_appends_from_safe_offset(tmp_path):
    store = UsageEntryStore(tmp_path / "usage.sqlite3")
    path = str(tmp_path / "a.jsonl")
    calls: list[tuple[str, int]] = []

    files_v1 = ((path, 1, 100),)
    files_v2 = ((path, 2, 160),)

    def parse_file(file_sig):
        calls.append(("full", file_sig[2]))
        return [
            {
                "source": "claude",
                "model": "claude-sonnet-4",
                "provider": "anthropic",
                "timestamp": 1_700_000_000_000,
                "input": 10,
                "output": 1,
                "entry_id": "msg-1",
            }
        ]

    def parse_tail(file_sig, start_offset):
        calls.append(("tail", start_offset))
        return (
            [
                {
                    "source": "claude",
                    "model": "claude-sonnet-4",
                    "provider": "anthropic",
                    "timestamp": 1_700_000_010_000,
                    "input": 20,
                    "output": 1,
                    "entry_id": "msg-2",
                }
            ],
            file_sig[2],
        )

    assert store.sync_files("claude", files_v1, parser={"v": 1}, parse_file_entries=parse_file) is True
    assert store.sync_files(
        "claude",
        files_v2,
        parser={"v": 1},
        parse_file_entries=parse_file,
        parse_file_tail_entries=parse_tail,
    ) is True

    assert calls == [("full", 100), ("tail", 100)]
    entries = store.query_entries(sources=["claude"])
    assert [e["entry_id"] for e in entries] == ["msg-1", "msg-2"]


def test_usage_store_durable_missing_file_keeps_rows(tmp_path):
    store = UsageEntryStore(tmp_path / "usage.sqlite3")
    path = str(tmp_path / "a.jsonl")
    store.sync_files(
        "codex",
        ((path, 1, 100),),
        parser={"v": 1},
        parse_file_entries=lambda _file_sig: [
            {
                "source": "codex",
                "model": "gpt-5.3-codex",
                "provider": "openai",
                "timestamp": 1_700_000_000_000,
                "input": 10,
                "output": 1,
                "entry_id": "codex-1",
            }
        ],
    )

    assert store.sync_files("codex", (), parser={"v": 1}, parse_file_entries=lambda _file_sig: [], durable=True) is True

    assert store.aggregate_entries(sources=["codex"])["total_tokens"] == 11
    status = store.status()
    assert status["files"][0]["missing_files"] == 1
    assert store.sync_files("codex", (), parser={"v": 1}, parse_file_entries=lambda _file_sig: [], durable=True) is False


def test_usage_store_non_durable_missing_file_deletes_rows(tmp_path):
    store = UsageEntryStore(tmp_path / "usage.sqlite3")
    path = str(tmp_path / "a.jsonl")
    store.sync_files(
        "codex",
        ((path, 1, 100),),
        parser={"v": 1},
        parse_file_entries=lambda _file_sig: [
            {
                "source": "codex",
                "model": "gpt-5.3-codex",
                "provider": "openai",
                "timestamp": 1_700_000_000_000,
                "input": 10,
                "output": 1,
                "entry_id": "codex-1",
            }
        ],
    )

    assert store.sync_files("codex", (), parser={"v": 1}, parse_file_entries=lambda _file_sig: [], durable=False) is True

    assert store.aggregate_entries(sources=["codex"])["total_tokens"] == 0


def test_usage_store_session_records_are_synced_and_retained(tmp_path):
    store = UsageEntryStore(tmp_path / "usage.sqlite3")
    path = str(tmp_path / "session.jsonl")
    files_v1 = ((path, 1, 100),)

    assert store.sync_session_files(
        "codex",
        files_v1,
        parser={"v": 1},
        parse_file_session=lambda _file_sig: {
            "tool": "codex",
            "session_id": "s1",
            "project": "tokdash",
            "turns": [{"turn_index": 1, "timestamp_ms": 1_700_000_000_000, "tokens": 10}],
        },
    ) is True
    assert store.sync_session_files("codex", files_v1, parser={"v": 1}, parse_file_session=lambda _file_sig: None) is False

    records = store.query_session_records("codex")
    assert len(records) == 1
    assert records[0]["session_id"] == "s1"

    assert store.sync_session_files("codex", (), parser={"v": 1}, parse_file_session=lambda _file_sig: None, durable=True) is True
    assert store.query_session_records("codex")[0]["session_id"] == "s1"


def test_usage_store_session_file_can_emit_multiple_records(tmp_path):
    store = UsageEntryStore(tmp_path / "usage.sqlite3")
    path = str(tmp_path / "opencode.db")
    files_v1 = ((path, 1, 100),)

    assert store.sync_session_files(
        "opencode",
        files_v1,
        parser={"v": 1},
        parse_file_session=lambda _file_sig: [
            {"tool": "opencode", "session_id": "s1", "project": "a", "turns": []},
            {"tool": "opencode", "session_id": "s2", "project": "b", "turns": []},
        ],
    ) is True

    records = store.query_session_records("opencode")
    assert [record["session_id"] for record in records] == ["s1", "s2"]
    status = store.status()
    assert status["sessions"][0]["tool"] == "opencode"
    assert status["sessions"][0]["sessions"] == 2


def test_usage_store_repair_recomputes_derived_counts(tmp_path):
    store = UsageEntryStore(tmp_path / "usage.sqlite3")
    store.sync_source(
        "codex",
        build_source_signature(files=[["a.jsonl", 1, 1]], parser={"v": 1}),
        lambda: [
            {
                "source": "codex",
                "model": "gpt-5.3-codex",
                "provider": "openai",
                "timestamp": 1_700_000_000_000,
                "input": 10,
            }
        ],
    )

    with store._connect() as conn:
        conn.execute("UPDATE source_state SET entry_count = 999 WHERE source = 'codex'")
        conn.commit()

    result = store.repair()

    assert result["ok"] is True
    assert "recomputed source_state.entry_count" in result["actions"]
    status = store.status()
    assert status["sources"][0]["entry_count"] == 1


def test_coding_tool_parsers_declare_sync_capabilities():
    tracker = CodingToolsUsageTracker()
    modes = {name: parser.sync_capability.mode for name, parser in tracker.parsers.items()}

    assert modes["opencode"] == "source_native_db"
    assert modes["codex"] == "file_replace"
    assert modes["claude"] == "file_replace"
    assert modes["copilot_cli"] == "source_replace"
    assert tracker.parsers["gemini_cli"].sync_capability.append_jsonl is True
    assert tracker.parsers["kimi"].sync_capability.append_jsonl is True
    assert tracker.parsers["opencode"].sync_capability.session_store is False


def test_parser_code_signature_unwraps_lru_cache_functions():
    @lru_cache(maxsize=1)
    def parser_fn():
        return "ok"

    signature = parser_code_signature(parser_fn)

    assert signature["object"].endswith(".parser_fn")


def test_codex_stored_session_duplicate_policy_matches_live_loader():
    records = [
        {"tool": "codex", "session_id": "dup", "project": "old", "turns": [{"tokens": 10}]},
        {"tool": "codex", "session_id": "dup", "project": "new", "turns": [{"tokens": 20}]},
    ]

    result = sessions_module._session_records_to_raw_sessions("codex", records)

    assert result["dup"]["project"] == "new"
    assert result["dup"]["turns"] == [{"tokens": 20}]


def test_opencode_signatures_include_wal_and_shm(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    opencode_dir = tmp_path / ".local" / "share" / "opencode"
    opencode_dir.mkdir(parents=True)
    for name in ("opencode.db", "opencode.db-wal", "opencode.db-shm"):
        (opencode_dir / name).write_text(name, encoding="utf-8")

    tracker = CodingToolsUsageTracker()
    signatures = tracker.parsers["opencode"]._file_signatures()

    assert {Path(path).name for path, _mtime, _size in signatures} == {
        "opencode.db",
        "opencode.db-wal",
        "opencode.db-shm",
    }
    assert {Path(path).name for path, _mtime, _size in sessions_module._opencode_db_signature()} == {
        "opencode.db",
        "opencode.db-wal",
        "opencode.db-shm",
    }

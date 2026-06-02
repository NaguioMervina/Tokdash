# History Snapshots — Deferred Design (Roadmap Reference)

> [!NOTE]
> **Status: Deferred — not on the active roadmap.** Tokdash addresses history loss with
> **config-based retention** (keep the client's own logs) rather than a built-in snapshot
> store. See **[HISTORY_RETENTION.md](HISTORY_RETENTION.md)** for the shipped approach and the
> disk-cost analysis that drove the decision.
>
> This document is retained as a **fully-reviewed design reference** for an in-app durable
> usage store, to revisit only if the trade-offs change — e.g. a client adds cleanup that
> *cannot* be disabled, or multi-machine history sync becomes a goal. It went through four
> rounds of review (the revision history below records each round); it is kept intact rather
> than deleted so that work isn't lost if the problem resurfaces.

**Original status when active:** Draft Rev 4 — Claude Code drafts; Howard's decisions; four rounds of Codex review.
**Related:** `README.md` → "History retention", `docs/HISTORY_RETENTION.md`, `src/tokdash/sources/coding_tools.py`, `src/tokdash/compute.py`, `src/tokdash/sessions.py`, `src/tokdash/api.py`

### Revision history
- **Rev 1** — initial aggregate-snapshot design (freeze closed months; serve open month live).
- **Rev 2** — Codex round 1: session scope, durable-source opt-out, first-writer-wins, month-scoped digest, local/UTC boundary, CLI subparser note.
- **Rev 3** — Howard's architecture decisions + Codex round 2. Continuous read/write-through (no cron); sessions in scope; cost frozen for positive prices, zeros auto-healed; §18 A/B validation.
- **Rev 4 (2026-06-01)** — Codex round 3 (C1–C8) + parallel gap review. Material changes:
  - **C1** Ingest must run **before** the 120s API response cache (cache hits would skip it). → §7a.
  - **C2/C3 + gaps** Per-file incremental ingest can't naively preserve **source-wide dedup/precedence**. Split parsers into **incremental-safe** vs **whole-source** (Copilot); make a stable **`id` mandatory** on every entry with a per-parser natural-id/fallback table. → §5, §6, §6a.
  - **C4** Session-turn merge keyed by a **stable `turn_id`**, not `turn_index`. → §8.
  - **C5** Frozen positive cost **contradicts "full parity with live"** after a pricing edit. New decision §0.4; parity narrowed to **tokens/messages**. → §0.4, §2, §7, §18.
  - **C6** Zero-cost heal must use the **`provider/model` fallback chain** that `compute.py` uses. → §7.
  - **C7** Bypass the 5 s file-signature TTL for ingest. → §6.
  - **C8 + gap** Corrupt snapshot must **fail-closed** (not silent zero) once it's the durable source; keep `.bak`; add repair CLI. → §6, §7, §11, §13.
  - **gaps** persist `messageCount` (§5); apply cost-heal to **session turns** (§8); spec the ingest **lock + timeout** (§10); document the same-size-rewrite watermark blind spot (§13).

---

## 0. Resolved decisions (Howard) — these supersede earlier revisions

1. **Cost: freeze known prices, auto-heal zeros.** A snapshot's stored **positive** `cost` is
   authoritative (oracle) and is never recomputed — even if the pricing DB later changes. A
   stored **`0.0`** is treated as *"unpriced at ingest"* (new-model rollout / pricing-DB lag) and
   is **recomputed from the current pricing DB on read** (§7), so a later price addition
   retroactively corrects history; a genuinely-free model stays `0.0`. (Refines Codex C1.)
2. **Sessions are in scope for v1** — the Session-explorer path (`sessions.py`) gets its own
   write-through store (§8).
3. **No cron — write-through on every request.** Each API call/refresh ingests changed source
   files and serves from the store (§4). Ingest runs **before** the response cache (§7a).
4. **Cost can diverge from live after a pricing-DB edit — by design.** The live path reprices
   everything when `pricing_db.json` changes (its cache key includes the pricing signature,
   `coding_tools.py:88`/`:117`); snapshots keep **positive** costs frozen. Therefore snapshot and
   live costs **will differ** after a pricing edit, and that is intended. **Parity is guaranteed
   only for tokens and messages** (and for cost only while pricing is unchanged since ingest).
   (Resolves Codex C5.)

---

## 1. Why we are doing this

### 1.1 The problem: tokdash's history silently erodes

Tokdash computes every number **live** from each client's local session logs and keeps **no
store of its own**. For Claude Code that source is `~/.claude*/projects/**/*.jsonl`
(`ClaudeParser`, `coding_tools.py:356`). Claude Code runs a **startup cleanup that deletes
session transcripts older than `cleanupPeriodDays` (default 30 days)**, so a past month's totals
**shrink as the files are pruned**. (Codex verified the premise against the official settings
docs: default 30, deleted at startup — https://code.claude.com/docs/en/settings)

### 1.2 The incident (2026-06)

| claude-opus-4-6, April | ~May 11 | 2026-06-01 |
| --- | --- | --- |
| Messages | 3,755 | 1,095 |
| Tokens | 615,037,255 | 83,944,974 |
| Cost | **$528.30** | **$73.80** |

Confirmed not a parser bug: `ClaudeParser` is unchanged since v0.4.0 and **ccusage, reading the
same files, reported the same shrunken numbers**. The data shrank, not the code.

### 1.3 Why the retention bump isn't enough

`cleanupPeriodDays: 3650` (Part 1) cannot restore deleted data, only protects this one
machine/config, and keeps unbounded transcripts on disk. Accounting correctness should not depend
on a client's retention policy tokdash doesn't own → tokdash should **own a durable record**.

### 1.4 Secondary benefit: performance

Every request re-globs and re-parses all JSONL (`compute_usage_with_comparison` parses twice;
`compute_stats` parses 365 days). Incremental ingest + serving from compact per-month files
removes most of that repeated I/O.

---

## 2. Goals and non-goals

### Goals
- **G1 — Durability:** once ingested, a month's data (aggregate **and** session) stays correct
  after the client deletes its logs.
- **G2 — Faithful capture:** snapshots reproduce the **tokens/messages** tokdash recorded at
  ingest, and the **cost** under the pricing in effect then (positive frozen, zero healed §0.1).
- **G3 — Both surfaces covered:** the entries path (**Overview**, **Stats**) *and* the
  **Session explorer** survive deletion (§5, §8).
- **G4 — Capture as a side effect of use (no cron):** normal API/app traffic keeps the snapshot
  current via write-through ingest (§6/§7a). Residual gap acknowledged (§16).
- **G5 — Cheap and inspectable:** plain per-month JSON a human can read, diff, and delete.

### Non-goals & invariants
- **NG1 — Recovering already-deleted data.** Impossible.
- **NG2 — Snapshotting durable sources.** OpenCode (SQL `collect()` override) and **Hermes**
  (SQLite, inherits `BaseParser.collect`) self-correct; they opt out via `snapshots_enabled = False`.
- **NG3 — A real database.** Per-month JSON + small index/metadata files; no SQLite/Postgres.
- **NG4 — Parity (narrowed, C5):** for any window whose source files still exist, snapshot
  **token and message** totals must equal today's live output. **Cost parity holds only while
  `pricing_db.json` is unchanged since ingest** — a pricing edit reprices the live path but not
  frozen positive snapshot costs (§0.4). This divergence is by design, not a regression.

---

## 3. Background — two independent data paths

| Path | Code | Produces | Snapshot store |
| --- | --- | --- | --- |
| **Entries** (Overview, Stats) | `coding_tools.py` → `compute.py` (`parse_entries_json` `:93`, `_contributions_from_entries` `:298`) | aggregate token/cost entries | **§5 entries store** |
| **Sessions** (Session explorer) | `sessions.py` (`_claude_sessions`/`_codex_sessions`/`_opencode_sessions` → `_parse_*_session_file`; `get_sessions_data` `:562`, `get_session_detail` `:600`) | per-session turn lists, ids, projects | **§8 sessions store** |

`/api/sessions` and `/api/session` (`api.py:215`, `:227`) **reparse raw transcripts directly** and
never read the entries stream — hence two stores. Both use the same write-through mechanism.

Shared facts:
- A normalized **entry**: `{id, source, model, provider, input, output, cacheRead, cacheWrite,
  reasoning, cost, timestamp}` (+ `messageCount` where a source emits it). `timestamp` = epoch ms UTC.
  **`id` is new and mandatory** (§6a).
- The in-memory `_entry_cache`/`_sig_cache` (`coding_tools.py:79`, `:31`) stay as a hot-path cache
  *above* the durable snapshot, **except** the sig cache is bypassed during ingest (§6, C7).

---

## 4. Design overview — continuous read/write-through

> On every request, **ingest then serve**:
> 1. **Incremental ingest** (§6) — parse only source files changed since the last ingest watermark
>    (by fresh mtime/size, + a small re-scan margin) and **merge** their entries into per-month
>    snapshot files. Freeze `cost` per entry at first ingest.
> 2. **Serve** the requested `[since, until)` **from the snapshot store** (§7).
>
> Deleted source files are never re-scanned; their already-ingested entries remain. Because serving
> reads the store, **positive costs are the frozen oracle and zero costs are re-priced on read**
> (§0.1). Months are just the file-bucketing unit.

Ingest is a **side effect** and must run even on a response-cache hit — see §7a (C1).

---

## 5. Entries store — layout, schema, scope

### Location & scope (Codex C4-r2)
- `TOKDASH_DATA_DIR` (default `~/.tokdash`).
- Layout: `~/.tokdash/snapshots/entries/<source>/<scope>/<YYYY-MM>.json` (+ `.json.bak`)
  plus `~/.tokdash/snapshots/entries/<source>/<scope>/_meta.json`.
- **`<scope>`** disambiguates config-dependent parsers whose data root can change:
  `KIMI_SHARE_DIR` (`coding_tools.py:667`), `PI_AGENT_DIR` (`:803`),
  `COPILOT_OTEL_FILE_EXPORTER_PATH` (`:989`), `HERMES_HOME` (`:1411`, opted-out).
  `snapshot_scope()` → `"default"` for home-dir parsers (Claude/Codex/Gemini), else a short stable
  hash of the resolved roots; `_meta.json` records the human-readable roots.

### `_meta.json`
```json
{ "schema": 1, "source": "claude", "scope": "default", "roots": ["…/.claude/projects"],
  "last_ingest_mtime_ns": 1751405000000000000,
  "ingested_files": { "<path>": {"mtime_ns": …, "size": …, "content_sig": "…"} } }
```
`ingested_files` carries `(mtime_ns, size)` and optionally a cheap `content_sig` to catch a
same-size rewrite (§13 limitation).

### Per-month file schema
```json
{
  "schema": 1, "source": "claude", "scope": "default", "month": "2026-04",
  "first_ingested_at": "…Z", "updated_at": "…Z",
  "usage_digest": "sha256:<usage fields + id only, cost excluded>",
  "entry_count": 1095,
  "entries": [
    { "id": "msg_01ABC…", "source": "claude", "model": "claude-opus-4-6",
      "provider": "anthropic", "input": 11, "output": 23, "cacheRead": 3963,
      "cacheWrite": 112, "reasoning": 0, "cost": 0.07, "timestamp": 1744545600000 }
  ]
}
```
- **`id`** — mandatory dedup key (§6a). **`messageCount`** — persisted verbatim when a source emits
  it (gap G1; currently only Hermes, which is opted-out, so forward-compatible). `parse_entries_json`
  (`compute.py:136`) keeps defaulting missing `messageCount` to 1.
- **`cost`** is frozen at ingest; positive = oracle, `0.0` = re-priced on read (§7). Sign encodes
  "known" vs "unpriced"; no separate flag.
- **`usage_digest` excludes cost** (C2-r2): `verify` re-derives usage from live files and compares,
  so reprice/pricing edits never cause false drift.

### Atomic write + backup (C8)
`upsert_month` writes a temp file then, **before** `os.replace`, copies the current month file to
`<YYYY-MM>.json.bak`. A torn or bad write can be restored from `.bak`; `os.replace` keeps readers
atomic.

---

## 6. Incremental ingest (the core mechanism)

Refactor each file-based parser so `_parse_all` is expressed as `_parse_files(paths)` over a given
list (most already loop `for path_str, _, _ in self._file_signatures()` —
`coding_tools.py:288/396/580/712/845`). Two ingest modes by parser:

| Mode | Parsers | Why | Ingest behavior |
| --- | --- | --- | --- |
| **incremental-safe** | Claude, Gemini, Kimi, Pi, Codex | messages live in **one** session file; ids are globally unique; no cross-file precedence | parse only **changed** files; **upsert-if-absent by `id`** into the target month file (the month file *is* the dedup set) |
| **whole-source** | **Copilot** | cross-file **precedence** between OTel buckets + `events.jsonl` suppression (`coding_tools.py:1217-1334`) is resolved across the *entire* parse, not per file | on **any** source change, re-parse **all** files and **atomically replace** the months it produces (rebuild), preserving exact precedence |
| **opted-out** | Hermes, OpenCode | durable SQLite, self-correcting (NG2) | never snapshotted |

```python
def _ingest_incremental(self):
    meta = store.load_meta(self.source_name, self.snapshot_scope())
    sigs = self._file_signatures(use_cache=False)      # C7: bypass the 5 s _sig_cache TTL
    if self.incremental_safe:
        changed = [p for (p, mt, sz) in sigs
                   if meta.is_new_or_changed(p, mt, sz)              # unseen / mtime|size differ
                   or mt >= meta.last_ingest_mtime_ns - RESCAN_MARGIN_NS]   # overlap margin
        if not changed: return
        for month, es in bucket_by_utc_month(self._parse_files(changed)).items():
            store.upsert_month(self.source_name, scope, month, es)   # insert-if-absent by id
    else:                                              # whole-source (Copilot)
        if not meta.any_changed(sigs): return
        months = bucket_by_utc_month(self._parse_files([p for p,_,_ in sigs]))
        store.replace_months(self.source_name, scope, months)        # rebuild affected months
    meta.record(sigs); store.save_meta(meta)
```

- **Sig-cache bypass (C7).** Ingest must see *current* file metadata, so it stats fresh
  (`use_cache=False`); the live `collect()` path keeps the 5 s `_timed_sigs` TTL for performance.
- **Watermark + margin.** Unchanged files are skipped (the perf win and the reason deleted files
  don't matter). The margin re-reads recently-touched files so an in-flight append is caught;
  upsert-by-`id` makes the overlap idempotent.
- **Global dedup without a separate index.** For incremental-safe parsers each `id` lives in one
  file → one UTC month; the month file's id-set reproduces the current per-source
  `seen_message_ids` (`coding_tools.py:392`) dedup. Copilot's cross-file precedence is preserved by
  the whole-source rebuild rather than a partial merge (C2 + gap G3).

### 6a. Stable `id` inventory (C3 + gap G6)

`id` is **mandatory** on every snapshot entry. Ingest validates it and synthesizes a deterministic
fallback if the upstream log lacks one (else the entry is dropped with a logged warning).

| Parser | Natural id | Fallback (deterministic) |
| --- | --- | --- |
| Claude | `message.id` (`coding_tools.py:412`) | `sha1(path + timestamp + tokens)` |
| Gemini | `message.id` UUID (`:594`) | `sha1(path + timestamp + tokens)` |
| Pi | outer `id` 8-hex (`:878`) | `path#index` |
| Kimi | `message_id` (`:735`) | `sha1(path + timestamp + tokens)` |
| Copilot | `gen_ai.response.id` ▸ `traceId` (`:1184`) | `sha1(timestamp + model + tokens)` |
| **Codex** | **none today** (`token_count` events carry no id, `:307-336`) | **`path#line_offset`** (append-only per session) — **§17 Q1** |

`path#line_offset` / `path#index` are stable for append-only session files. **Codex's choice is an
open question** (§17 Q1): line-offset is simplest but breaks if a session file is ever rewritten;
a `(timestamp, model, tokens)` hash collides on identical back-to-back turns.

---

## 7. Serving + cost

```python
def collect(self, since, until):
    if not snapshots_active(self):                      # kill-switch or durable source
        return <existing live behavior>
    self._ingest_incremental()                          # write-through (also hoisted to §7a)
    months = utc_months_overlapping(since, until)       # UTC ms bounds; all-time → list_months ∪ ingested
    out = store.read_months(self.source_name, self.snapshot_scope(), months)   # fail-closed, §7b
    return [e for e in out if in_ms_range(e, since, until)]
```

### 7a. Ingest before the response cache (C1)
`/api/usage`, `/api/tools`, `/api/stats`, `/api/sessions`, `/api/codex/sessions` wrap their compute
in `get_cached_or_fetch(... , TTL=120s)` (`api.py:92`, `:161`, `:192`, `:200`, `:220`, `:267`), so a
**cache hit never reaches `collect()`** and would skip ingest. Fix: call lightweight
`_ingest_all_sources()` / `_ingest_all_sessions()` (iterate snapshot-enabled parsers, run
`_ingest_incremental()`, return immediately) **before** `get_cached_or_fetch`. Ingest is cheap
(stat-only when idle), orthogonal to the response cache, and now runs on **every** request.

### 7b. Cost heal on read — use the aggregation lookup chain (C6)
After `store.read_months`, recompute cost for any entry whose stored `cost == 0`, using the **same
`provider/model`-then-bare fallback** that `parse_entries_json` uses (`compute.py:101`/`:105`/`:135`):
```python
for e in entries:
    if not e.get("cost"):
        m = f'{e["provider"]}/{e["model"]}' if e.get("provider") else e["model"]
        e["cost"] = self.pricing_db.get_cost(m, e["input"], e["output"], e["cacheRead"], e["cacheWrite"])
```
Positive costs are returned untouched (frozen oracle). Consequences: `parse_entries_json` needs no
change (already trusts `cost>0`); healing at the read layer keeps **Stats**
(`_contributions_from_entries`, `compute.py:324`, which trusts cost verbatim) consistent with
**Overview**; genuinely-free models stay `0`. **The same heal is applied to session turns** (§8, gap G5).

### 7c. Fail-closed reads (C8)
If a month's snapshot file is corrupt (JSON/schema error), `read_months` must **not** silently
return empty — after source deletion that masquerades as zero usage. It raises/marks the month
**"data unavailable"**, surfaced by the API as an explicit error (e.g. *"Snapshot for 2026-04 is
corrupted; run `tokdash snapshot repair`"*), never a silent zero. Live fallback applies only while
the source files for that month still exist.

### 7d. Parity invariant (narrowed, C5)
For any window whose files exist, snapshot **token/message** totals equal live. **Cost** equals live
**only while pricing is unchanged since ingest**; after a pricing edit, frozen positive costs
diverge from the repriced live path **by design** (§0.4).

---

## 8. Sessions store (in scope, Decision §0.2)

Mirror write-through over the `sessions.py` raw path.

- **Source:** per-session dicts from `_raw_sessions_for_tool(tool)` (full `turns`, `session_id`,
  `project`, timestamps).
- **Layout:** `…/snapshots/sessions/<tool>/<scope>/<session_id>.json` + `index.json`
  (`session_id → {first_ts, last_ts, tokens, cost, project}`).
- **Stable `turn_id` (C4).** Today turns carry only `turn_index` (`sessions.py:101`), which
  index-merge can duplicate/mis-merge after partial parses. Add a stable `turn_id` from the raw
  record: Claude `message.id` (available `sessions.py:372`), OpenCode DB message id (`:476`), Codex a
  per-event id or `path#line_offset` (`:279-280`, mirrors §6a). `_merge_raw_session`
  (`sessions.py:191-210`) keys by `turn_id` (composite key as backward-compat fallback for
  pre-`turn_id` snapshots).
- **Ingest:** on `/api/sessions` and `/api/session` (before the cache, §7a), incrementally re-parse
  changed session files, **upsert by `session_id`**, merge `turns` by `turn_id`, refresh the index.
- **Serve:** `/api/session?session_id=` → direct file read; `/api/sessions?period=` → select index
  rows overlapping the window, load, then `_summarize_session(raw, since_ms, until_ms)` as today.
- **Cost heal on turns (gap G5):** apply §7b's zero-cost heal to each turn's `cost` on read so the
  Session explorer and Stats agree for newly-priced models.
- **Durable tools:** OpenCode sessions come from its SQLite DB → opt out (NG2).
- **Size:** default `json.gz`; consider storing only token/cost/role/timestamp per turn (drop large
  message bodies) — **§17 Q3**.

---

## 9. Cost freeze, reprice, verify

- **Freeze (oracle) for positive costs;** never recomputed on read; a pricing edit does **not**
  retroactively change them (the source of the §0.4 divergence).
- **Auto-heal zeros** from the current pricing DB on read (§7b).
- **`snapshot reprice [--month … --source …]`** — explicit opt-in: recompute **all** stored costs
  from the current pricing DB and rewrite. The only path that mutates a stored positive cost; the
  tool to *re-sync* a month to live after a deliberate pricing correction.
- **`snapshot verify`** — re-derive usage from live files (while present), compare `usage_digest`
  (cost-excluded), report drift; never auto-apply.

---

## 10. Concurrency & write-on-read (gap G4)

Write-through means **reads mutate disk**:
- **Per-`(source, scope)` file lock** under `TOKDASH_DATA_DIR` (e.g. `fcntl`/`O_EXCL` lockfile) with a
  **timeout** (default 30 s). On timeout, **log + skip ingest** for that request and serve from the
  existing store / live; the next request retries. Never block a user request indefinitely.
- **Atomic month writes** (temp + `.bak` + `os.replace`) under the lock; no torn files.
- **Cold start** (first request after install / `snapshot build`) does the one-time full ingest;
  warm requests stat only changed files (usually 0–1). Cost is bench-tested (§14).
- **Read-only `TOKDASH_DATA_DIR`** → ingest fails soft, serving falls back to live parse (logged);
  the dashboard never breaks.

---

## 11. CLI (`cli.py`) — maintenance only, no cron

`snapshot` subcommand group (no scheduled job needed, G4):
- `tokdash snapshot build` — force a full ingest now (e.g. right after upgrade).
- `tokdash snapshot list` / `status` — months/sources, entry counts, sizes, valid/corrupt/backup state.
- `tokdash snapshot validate` — scan all month files for JSON/schema validity; report corrupt months (C8).
- `tokdash snapshot repair --month YYYY-MM [--from-live | --from-backup | --delete]` — re-ingest from
  live (if files exist), restore from `.bak`, or explicitly mark a data gap (C8).
- `tokdash snapshot verify` / `reprice` (§9).

**Argparse migration (Rev2 F7).** `cli.py:37` is a single positional `command` with
`choices=["serve","export"]` defaulting to `serve`. Move to **subparsers**, preserving exactly:
bare `tokdash` → serve, `tokdash serve …`, `tokdash export …`, plus the `snapshot` group.

---

## 12. Config / env

| Var | Default | Meaning |
| --- | --- | --- |
| `TOKDASH_DATA_DIR` | `~/.tokdash` | Root for `snapshots/` |
| `TOKDASH_SNAPSHOTS` | `1` | Master kill-switch (`0` → exact current behavior) |
| `TOKDASH_SNAPSHOT_RESCAN_MARGIN` | `15m` | Overlap window re-scanned each ingest |
| `TOKDASH_SNAPSHOT_LOCK_TIMEOUT` | `30s` | Ingest lock timeout before fail-soft |
| `TOKDASH_SNAPSHOT_FORMAT` | `json` (entries), `json.gz` (sessions) | per-store format |

Plus code-level `snapshots_enabled` (`False` for Hermes/OpenCode) and `incremental_safe`
(`False` for Copilot) and `snapshot_scope()` per parser.

---

## 13. Edge cases

- **Config-dependent root changes** → distinct `<scope>` dirs (C4-r2). ✓
- **Cost 0 for unpriced model** → re-priced on read with provider/model fallback (C6); free models stay 0. ✓
- **Pricing edit** → live reprices, snapshot positives stay frozen → **cost divergence by design** (§0.4, C5). ✓
- **`period=all`** → no empty-file fanout; `list_months ∪ ingested`; `_meta` watermark ⇒ 0 re-scans when idle. ✓
- **Local range across a UTC month edge** → UTC-bounds enumeration + ms filter (F6). ✓
- **Entry near a month boundary** → bucketed by UTC month; the exact ms-filter on read keeps any
  arbitrary range correct regardless of which month-file it sits in. ✓
- **Copilot cross-file precedence** → whole-source rebuild on change (§6), not partial merge. ✓
- **Missing/duplicate `id`** → synthesized fallback + ingest validation (§6a, G6). ✓
- **Corrupt snapshot after deletion** → fail-closed "data unavailable", `.bak`, repair CLI (C8). ✓
- **Same-size in-place rewrite outside the re-scan margin** → **known limitation**: not detected by
  `(mtime,size)`. Mitigations: the margin catches the common rotation case; an optional per-file
  `content_sig` in `_meta.json` detects it at extra I/O cost (gap G2, §17 Q4).
- **Concurrent ingests** → per-scope lock + 30 s timeout fail-soft (G4). ✓
- **Unwritable data dir** → fail-soft to live parse. ✓

---

## 14. Testing plan (`tests/test_snapshots.py`, `tests/test_session_snapshots.py`)

1. **Incident regression:** ingest 3 months, then make `_parse_files` return nothing ("deleted") →
   all three months still report original totals (the lost-$528 case).
2. **Token/message parity (NG4):** for files-present windows, snapshot token/message totals == live.
3. **Cost: freeze + heal (§0.1):** positive cost unchanged after a pricing edit; a stored `0.0` for a
   model later priced reads back non-zero on **both** Overview and Stats (provider/model lookup, C6);
   genuinely-free stays `0`.
4. **Cost divergence by design (C5):** a pricing edit makes snapshot cost differ from live while
   token/message counts stay identical.
5. **Ingest-before-cache (C1):** a request that hits the 120 s response cache still triggers ingest
   (assert the store advanced).
6. **Incremental watermark + sig bypass (C7):** unchanged files not re-parsed; a file appended <5 s
   after a prior ingest is picked up on the next request.
7. **Idempotent upsert / stable id (C3, G6):** re-ingesting the same files yields identical month
   files; every entry has a stable non-empty `id`.
8. **Copilot whole-source precedence (C2, G3):** ChatSpan vs InferenceLog for the same trace split
   across two files/ingests → exactly one entry, correct precedence.
9. **Local/UTC boundary (F6):** non-UTC `TZ`, `2026-04-01..2026-04-30` == UTC computation.
10. **Scope isolation (C4-r2):** two `PI_AGENT_DIR` roots write to different `<scope>` dirs.
11. **Sessions store (§8, C4, G5):** after "deleting" raw files, `/api/session` detail and
    `/api/sessions` listing still return captured sessions; appended sessions merge `turns` by
    `turn_id` without dupes; a newly-priced model's turn cost heals.
12. **messageCount (G1):** a source emitting `messageCount` round-trips through snapshot unchanged.
13. **Corrupt fail-closed (C8):** a corrupted month file → API surfaces "data unavailable", not zero;
    `.bak` restores it; `snapshot repair` works.
14. **Concurrency (G4):** two simultaneous ingests converge; a held lock times out to fail-soft.
15. **CLI (F7):** bare `tokdash`/`serve`/`export` unchanged; `snapshot build/list/validate/repair`.

---

## 15. Rollout

1. **Retention bump (done, Part 1)** — keeps files alive long enough to be ingested by normal use.
2. **Implement on a feature branch and validate via a ~1-week parallel A/B run** (§18).
3. **Release write-through snapshots (entries + sessions)** once the A/B gate passes. First request
   (or `snapshot build`) does the one-time full ingest; everything afterward is captured as a side
   effect of use. April locks at the surviving $73.80; nothing further is lost.
4. **(Later) lower `cleanupPeriodDays`** to reclaim disk once the store is trusted.

---

## 16. What this does NOT solve

- Already-deleted April data ($528 → $73.80) is **unrecoverable**.
- A **usage gap with zero tokdash traffic** before a prune is never ingested; the `cleanupPeriodDays`
  bump is the backstop that makes this vanishingly unlikely.
- **Cost divergence from live after a pricing edit** is intended (§0.4), not a defect; `reprice`
  re-syncs a month deliberately.

---

## 17. Open questions for review

1. **Codex stable id (§6a).** `token_count` events carry no id. Use `path#line_offset` (simple,
   breaks on file rewrite) or a `(timestamp, model, tokens)` hash (collides on identical back-to-back
   turns)? Same question for Codex session `turn_id` (§8).
2. **Reprice scope for positive costs.** Zeros auto-heal (§0.1). Keep positives fully frozen
   (explicit `reprice` only), or auto-reprice the **current** month each ingest so recent known-price
   edits propagate while history stays frozen?
3. **Session storage shape & payload.** Per-session files + index (proposed) vs per-month bundles;
   and store full turns vs strip large message bodies to bound size (§8)?
4. **Same-size-rewrite detection (§13, G2).** Accept the documented limitation (rely on the re-scan
   margin), or always store a per-file `content_sig` (extra stat/hash I/O per request)?
5. **Storage engine.** Per-month JSON (+gzip sessions) vs one SQLite per store. JSON stays
   inspectable; SQLite scales and simplifies per-scope locking if volumes explode.

---

## 18. Validation — parallel A/B run before release

The build is **not** released until it has run **side by side** with the production wheel for ~1
week and Howard judges it stable.

### Setup
- **Feature branch** (e.g. `feat/history-snapshots`); `main` and the published wheel stay untouched.
- **Isolated conda env + editable install** (separate from the base env + systemd wheel serving prod):
  ```bash
  conda create -n tokdash-dev python=3.11 -y
  conda activate tokdash-dev
  pip install -e .                      # from the feature-branch checkout, run at repo root
  ```
- **Distinct port** so both run at once:
  ```bash
  TOKDASH_PORT=55424 tokdash serve      # test build;  production stays on 55423
  ```
  Expose the test port via a second Tailscale Serve path for phone-side comparison.
- **Isolated snapshot store:** `TOKDASH_DATA_DIR=~/.tokdash-dev` so a week of experimentation never
  pollutes a future production store and can be wiped. Both builds read `~/.claude*` logs
  **read-only**; snapshot writes only ever touch `TOKDASH_DATA_DIR`.

### What to compare
- **Token/message parity (primary, = NG4).** For every period/date-range, the test build's
  token and message totals must **equal** the production wheel's for windows whose files still exist.
  Any divergence there is a regression to fix before release.
- **Cost:** expected to match **only while pricing is unchanged**; a pricing edit during the window
  will (by design, §0.4) make costs diverge — compare cost under unchanged pricing only.
- **Stability & latency.** Write-through mutates disk every request — watch p50/p95 latency, store
  growth, lock behavior, and that concurrent refreshes never corrupt the store.
- **Durability is proven elsewhere.** With `cleanupPeriodDays: 3650` preventing new deletion, the
  week won't *show* the retention win; durability is proven by the incident regression test (§14 #1).
  The A/B window proves **non-regression** and **stability**.

### Release gate
Promote to `main` + publish a new wheel (then `pip install --upgrade` + restart the systemd service
per `README` → "Updating Tokdash") **only** once token/message parity holds and the build is stable
across the week. Until then production keeps running the existing wheel, unaffected.

# ProoFix — Architecture Review

Audit of the working tree as of 2026-08-05 (branch `main`, HEAD `e9b6e43`, plus uncommitted UI work).
This document describes **what the system actually is**, not what it was designed to be, then proposes
the Context Engineering layer (A5.5) as an addition that fits the real structure.

No code was changed to produce this review.

> **Currency note (2026-08-08).** Parts of this document have been overtaken by
> implementation. Corrections are marked inline and in §8; the roadmap in §8
> carries a per-item status. Two things to know before reading further:
>
> 1. **Workspace V2 is cancelled and deleted.** V1
>    (`frontend/src/components/proofix/**`) is the only frontend. Never create
>    `src/components/v2`, `src/lib/v2`, `src/design`, or any `/v2/*` route.
> 2. **The live plan is `docs/V1_COMPLETE_IMPLEMENTATION_PLAN.md`**, backed by
>    `docs/V1_FRONTEND_AUDIT.md`, `docs/V1_BACKEND_AUDIT.md`,
>    `docs/V1_API_DATA_CONTRACT.md`, `docs/V1_BUG_REGISTER.md`,
>    `docs/V1_TEST_COVERAGE.md` and `docs/V1_PHASE_STATUS.md`. Where this
>    document and those disagree, **those win** — they were written against the
>    current tree.

---

## 1. What ProoFix is

An autonomous bug-repair pipeline. A run takes a repository path, walks eleven agent stages
coordinated by LangGraph, and ends in a trust-gated pull-request decision (`draft` /
`diff_only` / `auto_mergeable`). Evidence — a reproduced runtime failure, verified citations,
scoped validation — is what earns a merge recommendation.

Scale: ~13.5k lines of Python. 16 agent modules, 27 services, 9 model modules, 30 test files.
A TanStack Start frontend (untracked) consumes a REST + WebSocket projection of run state.

---

## 2. Actual architecture

### 2.1 Execution graph (as built in `backend/orchestrator/graph.py`)

```
                         ┌──────────────┐
                         │ prepare_repo │  clone/copy → source roots → base SHA
                         └──────┬───────┘
                                ▼
                    ┌───────────────────────┐
                    │    parallel_intel     │  asyncio.gather
                    │   A1  ‖  A2  ‖  A3    │  each re-loads state from Redis
                    └───────────┬───────────┘  manual merge of sig/cve/static
                                ▼
                        ┌───────────────┐
                        │ layer1_fan_in │  reclassify CVE reachability with SIG
                        └───────┬───────┘
                                ▼
                     ┌────────────────────┐
                     │  reproduction_gate │  A3.5 — full pytest run
                     └─────────┬──────────┘
                               ▼
                        ┌─────────────┐◄──────┐
                        │ investigate │       │ should_reinvestigate
                        │     A4      ├───────┘ (max 2, on unverified citations)
                        └──────┬──────┘
                               ▼
                        ┌─────────────┐
                        │ blast_scope │  A5 + target resolution
                        └──────┬──────┘
                               ▼
                        ┌─────────────┐
                        │ plan_fixes  │  A6 — FixDAGPlan
                        └──────┬──────┘
                               ▼
                   ┌───────────────────────┐◄─────────────┐
                   │    generate_code      │              │
                   │  A7 (+ patch_engine)  │              │
                   └───────────┬───────────┘              │
                               ▼                          │
                   ┌───────────────────────┐              │
                   │   validate_mutation   │  A8          │
                   └───────────┬───────────┘              │
                     ┌─────────┼─────────┐                │
              retry  │         │ pass    │ fail-final     │
                     ▼         ▼         ▼                │
            ┌────────────────┐ │    ┌──────────┐          │
            │ increment_retry├─┘    │ route_pr │          │
            └────────┬───────┘      └──────────┘          │
                     └───────────────────────────────────┘
                               ▼ (pass)
                   ┌───────────────────────┐
                   │   validate_security   │  A9 ── fail+budget ──► increment_retry
                   └───────────┬───────────┘
                               ▼
                        ┌─────────────┐
                        │  route_pr   │  trust gates → A10 → GitHub PR → END
                        └─────────────┘
```

**The reproduction gate is not a gate.** `reproduction_gate` unconditionally flows to
`investigate` (`graph.py:112-113`). A3.5 sets `force_draft_pr` on failure, and A10 later
converts that into a draft PR with a review note — but the pipeline still runs investigation,
blast analysis, patch generation, and validation on a bug it could not reproduce. The stated
principle "Only CONFIRMED bugs proceed" is enforced at *routing* time, not at *execution* time.
That is a defensible design (you still get a diff to look at), but it is not what the
architecture claims, and it costs a full LLM patch-generation cycle on unreproducible bugs.

### 2.2 State flow

`RunStateModel` (pydantic, 30 fields) is the single shared object. Redis is the real store;
LangGraph's state channel is a transport that gets overwritten at every node:

```
node(dict) → _load_model(store, dict) → RunStateModel from Redis
           → overlay every non-None key from the graph dict
           → agent.run(model) mutates model
           → model.model_dump() back into the graph dict
```

Twelve nearly identical closures in `graph.py:26-94` implement this. Consequences:

- **LangGraph is load-bearing for control flow only.** Checkpointing is `MemorySaver`, so a
  crashed run cannot resume; the Redis state survives but nothing replays it.
- **The overlay is last-writer-wins.** `_load_model` copies any non-`None` graph value over the
  Redis value. The `parallel_intel` fan-out already hand-merges `sig`/`cve_report`/`static_report`
  because of this (`nodes.py:70-79`) — the workaround exists, but only for those three fields.
- **Every typed artifact degrades to `dict`.** `sig`, `root_cause`, `blast_graph`, `fix_dag`,
  `patch_bundle`, `mutation_result`, `security_result`, `pr_decision`, `proof_bundle`,
  `retry_brief`, `validation_failure`, `reproduction` are all `dict | None`. Producers call
  `.model_dump(mode="json")`; consumers call `Model.model_validate(...)` or reach in with
  `.get("...")`. Type safety exists at both ends of every hop and nowhere in between.

The `RunState` TypedDict is a hand-maintained duplicate of `RunStateModel`. Any new field must
be added in two places or it is silently dropped at the graph boundary.

### 2.3 LLM call flow

Six call sites, all constructing `LLMService(settings)` inline per invocation:

| Caller | Purpose | Batched? | Fallback |
|---|---|---|---|
| `role_llm_classifier` (A1) | classify ambiguous files | **yes** — one call for all | local AST prediction |
| `a4_evidence_investigator` | root-cause brief + citations | no | deterministic `_stub_brief` |
| `a6_fix_dag_planner` | fix ordering | no | topological sort |
| `a7_code_generation` | patch generation | no (1 per file, ≤3 files) | `apply_stub_plan` (no-op) |
| `a7_code_generation` | integrity-failure retry | no | returns `None` |
| `run_chat` (UI) | free-text Q&A over run state | no | deterministic answers |

`LLMService` (`services/llm.py`, 106 lines) is a thin provider switch. It has **no** retry,
timeout, rate limiting, token accounting, cost metric, prompt/response logging, caching, or
concurrency limit. A1 counts its own `llm_calls` into a metrics dict; nothing else counts
anything. There is no global budget and no way to answer "what did this run cost?".

A7's prompt is the largest by far, and it sends the target file **twice** —
`build_runtime_patch_prompt` emits `source_for_prompt[:4000]` under "Relevant code" and
`complete_original[:8000]` under "Original complete file" (`runtime_patch_prompt.py:210-214`).
For any file under 4 KB that is pure duplication.

### 2.4 Validation flow

```
A8 ──► run_scoped_validation(repo, run_id, target_test, baseline_failures)
        │
        ├─ no target test ──► full-suite fallback ──► new_failures vs baseline
        │
        ├─ phase 1: pytest <target_test>
        │     └─ fail ──► parse_validation_failure ──► patch_retry_required=True ──┐
        │                                                                          │
        └─ phase 2: full pytest, diff failures against A3.5 baseline               │
              └─ new failures ──► regression_tests_passed=False                    │
                                                                                   │
A8 ──► if pytest_passed: mutmut run --paths-to-mutate <first patched file>          │
        └─ survived ──► synthesize ValidationFailure ──► retry brief ◄──────────────┘
        │
        └──► correctness_score ──► A9 security rescan ──► A10 axis scoring
```

> **✅ FIXED since this review (2026-08-08).** The paragraph below described the
> original defect and is kept because it explains *why* the current design is
> shaped as it is. A8 now parses real mutmut output via
> `services/mutation_parser.py`, reads `mutation_score` / `survived_mutants` /
> `total_mutants` / `status`, and leaves `correctness_score = None` when there is
> no patch to score. **The same class of defect is still open elsewhere:**
> `security_score` defaults to `0.0` for a re-scan that never ran, and that zero
> is averaged into the composite that gates auto-merge. See roadmap P0-7.

~~**The mutation score is fabricated.**~~ `A8._run_mutmut` returned
`score = 0.5 if not survived else 0.0` — a hardcoded constant, never parsed from mutmut
output. It flowed into `correctness_score = min(100, 60 + 0.5*40) = 80.0`, and
`a10_routing.SCORE_THRESHOLD = 80.0`, where the check is `axis.correctness < 80 → draft`.
So **auto-merge eligibility rested on a literal `0.5` that measured nothing** — it contradicted
"every merge decision must be evidence-backed" at exactly the point where the decision is made.
Note also that `stderr` from `mutmut run` was
searched for the word "survived" alongside the results output, so a mutmut crash message
containing that word read as a surviving mutant.

### 2.5 Service dependency graph

```
repo_layout ◄──── ast_import_graph ◄──── a1
    ▲    ▲              ▲                 │
    │    │              │                 ├─► role_classifier ─► role_llm_classifier ─► llm
    │    │         sig_cache ◄────────────┤
    │    │              │                 └─► git_service (churn)
    │    └──────────────┘
    │
    ├── a3, a9 (get_scan_targets)          ⚠ takes RunStateModel
    └── target_resolver, citation_verifier ⚠ target_resolver takes RunStateModel

subprocess_runner ◄── a3, a3.5, a8, a9, scoped_validation

reproduction_parser ◄── a3.5, scoped_validation
reproduction_commands ◄── a3.5, scoped_validation

citation_verifier ◄── citation_validator (pass-through) ◄── a4
root_cause_builder ◄── a4
blast_traversal, target_resolver ◄── a5
fix_dag_builder ◄── a6
runtime_patch_prompt ◄── a7_patch_engine ◄── a7   (a7 also imports it directly)
retry_brief_builder ◄── a7, a8, a9
scoped_validation ◄── a8
validation_failure_parser ◄── scoped_validation
mci_verifier ◄── a7 (diff gen), a10 (phantom detection)
proof_bundle, github_pr ◄── a10
llm ◄── a4, a6, a7, role_llm_classifier, run_chat
ws_broadcaster ◄── agents/base
ui_projection, run_chat ◄── api/routes/ui   (untracked)
```

Cycle-ish coupling to note: `services/target_resolver` and `services/repo_layout` import
`backend.state.schema.RunStateModel`. Services depending on the orchestration state type means
they cannot be tested, reused, or called from the future Context Engineering layer without
constructing a full run state.

---

## 3. Agent-by-agent findings

### A1 — Semantic Repository Mapper (`a1_semantic_mapper.py`, 227 lines)

**Does:** SIG construction — source-root discovery, one-pass AST parse, heuristic role
classification, batched LLM classification of ambiguous files only, churn weighting,
criticality scoring, Redis-cached payload keyed on `HEAD + worktree-diff-hash + source roots`.

**Matches intent:** Yes, and it is the best-engineered agent in the system. The heuristic-first /
batch-LLM / cache design genuinely delivers the claimed LLM reduction.

**Leakage:** None outbound. A1 owns SIG and nothing else.

**Debt:**
- `_build_imported_by` (`a1:219-227`) is O(n²) with substring matching:
  `any(Path(path).stem in imp or imp in path for imp in imps)`. On a 2,000-file repo that is
  4M string containments, and the matching is imprecise — `utils.py` matches `test_utils`,
  `my_utils`, and any import containing "utils".
- The metrics dict (22 keys, `a1:42-63`) is assembled inline and shipped through `logger.info(extra=...)`
  *and* the WS payload. It is a de-facto public contract with no schema.
- `compute_repo_hash` falls back to reading and hashing every production file when git metadata
  is absent — full I/O pass on every run for non-git repos.
- `"test" in path.lower()` (`a1:107`) short-circuits classification; a file named `latest_config.py`
  is classified `test-only`.

**Refactor:** invert `_build_imported_by` into a single pass over the import map building a
reverse index from resolved edges (already available in `graph.edges`). Extract the metrics dict
into a `SIGMetrics` model.

### A2 — Dependency Analyzer (`a2_dependency_analyzer.py`, 70 lines)

**Does:** parse `requirements.txt`, query OSV per package, classify each CVE as
Critical / Informational / Unknown by SIG reachability.

**Matches intent:** Partially. The brief says "dependency graph, import graph, module
relationships" — A2 produces none of those. The import graph is built by **A1**. A2 is really a
*CVE reachability analyzer*, and `layer1_fan_in` re-does its classification anyway
(`sig_helpers.reclassify_cve_report`) because A2 races A1 and may not see the SIG.

**Leakage:** A1 owns the dependency/import graph that A2's name claims. Either rename A2 to
`A2CVEReachabilityAgent` or move import-graph construction into it.

**Debt:**
- Sequential `await query_osv(package, version)` in a nested loop — network N+1. A 60-dependency
  project makes 60 serial round trips.
- Only `requirements.txt`. No `pyproject.toml`, `poetry.lock`, `Pipfile`, `uv.lock`.
- `severity` parsing takes the last CVSS_V3 entry and defaults to the string `"HIGH"`, so severity
  is sometimes a numeric score and sometimes a word.
- The double classification (A2 then `layer1_fan_in`) is duplicate logic that exists only to
  paper over the parallel-fan-out race.

### A3 — Static Analysis (`a3_static_analysis.py`, 212 lines)

**Does:** bandit + semgrep + ruff, cluster by file/line-bucket, rank by
`severity × criticality × log(tools) × (1+churn)`, keep top 8, store baseline for A9's diff.

**Matches intent:** Yes, plus ruff which the brief does not mention.

**Debt — significant:**
- **Stub findings masquerade as real ones.** `return results if results else self._stub_bandit(...)`
  (`a3:84`, `a3:128`). If bandit runs successfully and finds nothing, the agent substitutes
  hand-rolled `"pickle" in content` / `"secret" in content` scans. A clean repository therefore
  produces fabricated findings that feed A4's root cause and A7's fallback patch target. The stub
  path should be reachable only when the tool is genuinely absent (`code == -1`), which the
  bandit branch partly checks and the semgrep branch does not.
- `_run_bandit` and `_run_semgrep` are duplicated in **A9** with different severity handling
  (A3 maps HIGH/MEDIUM/LOW → 0.9/0.6/0.3; A9 hardcodes 0.7). The baseline-vs-post comparison in
  A9 is therefore comparing findings scored on two different scales.
- `f.get("file")` string-replaces `str(repo) + "/"` to relativize paths — three separate
  ad-hoc implementations of path normalization live in this file alone.
- Clustering key `f"{file}:{line // 5 * 5}"` buckets by 5 lines, so a finding at line 9 and one at
  line 10 never cluster while lines 5 and 9 do.

### A3.5 — Runtime Reproduction (`a3_5_reproduction.py`, 74 lines)

**Does:** full pytest with `--json-report`, parse into `ReproductionResult`, capture
`pre_existing_failures` baseline, build a targeted re-execution command.

**Matches intent:** Yes. Small, focused, well-factored — the parsing lives in
`reproduction_parser`, command construction in `reproduction_commands`. Good model for the
other agents.

**Leakage:** sets `state.force_draft_pr` directly (`a3_5:59-60`). Trust-gate authority belongs to
`orchestrator/trust_gating.py`; three separate places now write that flag (A3.5, A4,
`apply_trust_gates_before_pr`).

**Debt:** 120-second hard timeout regardless of suite size. No isolation — pytest runs in the
clone with the host interpreter, so a repo with unmet dependencies produces `INFRA_ERROR`
rather than a real reproduction attempt. That is the correct classification, but it means the
gate silently degrades to "cannot reproduce" for most real-world repositories.

### A4 — Root Cause Investigation (`a4_evidence_investigator.py`, 178 lines)

**Does:** collect evidence refs from runtime/static/CVE/stack, LLM (or deterministic) brief,
**verify every citation deterministically**, compute weighted confidence, decide whether to
re-investigate.

**Matches intent:** Yes. Citation verification before re-prompting is the strongest idea in the
codebase — `citation_verifier` resolves the path, then tries exact line → line window (±0,1,2,3,5)
→ AST function lookup → unique-fingerprint-anywhere. This is real determinism.

**Leakage:** writes `state.reinvestigation_exhausted` and `state.force_draft_pr` (`a4:80-81`).
Same trust-gate leak as A3.5.

**Debt:**
- The LLM prompt interpolates raw Python objects — `{findings[:8]}`, `{reproduction}`,
  `{[r.model_dump() for r in evidence_refs]}`. The entire `reproduction` dict, including the full
  traceback, is stringified into the prompt with no budget. **No secret masking anywhere.**
- `citation_validator.py` is now a pure pass-through: `validate_all_citations_with_metrics`
  literally returns `verify_all_citations_with_metrics(...)`. A dead indirection layer with its
  own test file. Only `coerce_llm_citations` still earns its place.
- Re-investigation re-runs the *entire* agent including `collect_evidence_refs`, which is
  deterministic and cannot change between attempts. The LLM sees the same prompt and is asked to
  do better with no feedback about *which* citations failed verification — the verification
  result is computed and then discarded rather than fed back.

**Refactor:** feed unverified-citation detail into the re-investigation prompt. That is a
cheap, high-value change and turns two wasted LLM calls into one informed one.

### A5 — Blast Radius Analysis (`a5_blast_graph.py`, 64 lines)

**Does:** resolve a patch target, BFS forward+backward over the SIG to 3 hops with 0.85 decay,
split scope into `auto_patch_scope` (confidence ≥ 0.7) and `human_review_required`.

**Matches intent:** Yes for traversal.

**Leakage — the big one:** `resolve_patch_target` (`target_resolver.py`, 418 lines) reads
`reproduction`, `root_cause`, `static_report`, and the SIG, and applies a five-stage resolution
cascade (stack frame → root cause → test-import mapping → SIG lookup → static fallback). That is
a substantial independent responsibility living inside the blast agent, and **A7 needs it too** —
A7 currently re-derives the target file from `blast.origins[0]` in
`enrich_patch_plan_from_runtime`. Two components deriving "which file do we fix" by different
routes is exactly the kind of divergence that produces a patch to the wrong file.

**Debt:**
- `blast_traversal._module_to_file` uses `module in path` substring matching. `import os` matches
  any path containing "os" — `services/oslo.py`, `models/post.py`. Blast scope is polluted by
  false edges.
- Backward traversal iterates every file × every import at every hop: O(files × imports) per node,
  O(n²·h) per run.
- `pin_resolved_target` mutates the traversal result to force the resolved target into
  `auto_patch_scope` with `propagation_confidence=1.0`, bypassing the confidence threshold that
  is the entire point of the traversal.

### A6 — Fix Planner (`a6_fix_dag_planner.py`, 77 lines)

**Does:** build `FixNode`s from static findings + CVE records + blast scope, derive dependency
edges, topologically order (or ask the LLM to order), detect conflict batches.

**Matches intent: no.** The brief says A6 "produces PatchPlan objects … repair order, execution
sequence, dependencies, acceptance criteria." In reality **`PatchPlan` objects are built inside
A7** (`a7_patch_engine.build_patch_plans`), and A7 consumes exactly one field from A6's output:

```python
issue_id = (fix_dag.get("execution_order") or ["fix-0"])[0]   # a7_code_generation.py:134
```

The execution order, dependency edges, and conflict batches are computed, stored, rendered in the
UI — and never influence what gets patched or in what order. A7 iterates `plans[:3]` derived
independently from `blast.auto_patch_scope`. **A6 is effectively decorative.** The LLM ordering
call is spent on an artifact nothing reads.

This is the largest architecture/implementation divergence in the system, and it is the reason
A5.5 has an obvious seam to slot into (see §6).

**Debt:** `_llm_order` dumps every node into the prompt and swallows all exceptions to `[]`.
Acceptance criteria — named in the brief as an A6 output — are synthesized in
`runtime_patch_prompt.derive_runtime_behaviors` instead.

### A7 — Runtime-aware Patch Generator (`a7_code_generation.py` 320 + `a7_patch_engine.py` 149)

**Does:** build patch plans, enrich them from runtime evidence, extract the target function,
build the runtime prompt, call the LLM with an integrity guard (one retry), validate
no-op/syntax/abbreviation, write the file, record contracts, generate the diff.

**Matches intent:** in spirit. The runtime-evidence-driven prompt and the integrity guard are
real innovations and are well tested (4 test files).

**Leakage — A7 has absorbed responsibilities from three neighbours:**
1. **Fix planning** (A6's job) — `build_patch_plans`.
2. **Context selection** (A5.5's job, unbuilt) — `extract_relevant_code`, `infer_target_function`,
   the `[:4000]` / `[:8000]` truncations.
3. **Acceptance-criteria derivation** (A6's job) — `derive_runtime_behaviors`.

**Debt:**
- **Repository-specific literals in a module documented as repository-agnostic.**
  `runtime_patch_prompt._expected_from_test_name` returns
  `"Reject tokens whose exp timestamp is earlier than time.time()"` when the test name contains
  "expired"; the default retry instruction says *"(e.g. expiry comparison if tokens must be
  rejected)"*. `retry_brief_builder._format_expected` rewrites `(token)` → `(expired_token)` and
  `_failure_context` returns `"Previous attempt still accepted expired JWT tokens."` These are
  the `vulnapi` fixture's semantics compiled into the engine. On any other repository they inject
  false expectations into the patch prompt.
- Redundant double write: `full.write_text(original)` immediately followed by
  `full.write_text(llm_output.patched_content)` (`a7:116-117`).
- No rollback. If plan 1 writes successfully and plan 2 fails, the clone is left half-patched and
  A8 validates a mixed state.
- `contract_from_plan` is imported by `a7_code_generation` and never called (it has a test, so it
  reads as live code); contracts are built inline from the LLM output instead.
- The Redis patch lock has a 60-second TTL while the operation is 1–3 LLM calls that routinely
  exceed it — the lock can expire mid-write.
- On LLM exception, falls back to `apply_stub_plan`, which returns the **original content
  unchanged** as a "patch". A no-op is then recorded as a `PatchCandidate` and validated as
  though it were a fix.

### A8 — Validation (`a8_mutation_validator.py`, 174 lines)

**Does:** scoped validation (target test → regression diff), mutmut, correctness scoring, retry
brief construction, state wiring for `retry_brief` / `validation_failure`.

**Matches intent:** the scoped two-phase design is right and well tested.

**Leakage:**
- **Scoring belongs to A10.** `correctness_score` — 100 / 70 / 60+score·40 / 40 / 0 — is computed
  here from magic constants and consumed as an axis score by A10. The scoring rubric is split
  across two agents.
- Retry-brief construction is shared with A9, and both agents perform the same awkward
  `model_copy(update=...)` back-patching dance to embed results into failures into briefs
  (`a8:94-105`, `a9:78-89`) — 25 duplicated lines.

**Debt:** the fabricated mutation score (§2.4). `mutmut results` output is never parsed. Only the
first patched file is mutated. `survived` matching against `stderr` conflates crashes with
surviving mutants.

### A9 — Security Verification (`a9_security_rescan.py`, 145 lines)

**Does:** re-run bandit + semgrep post-patch, diff against A3's baseline by
`file:line:message[:50]`, reject on any new finding, build a retry brief.

**Matches intent:** yes.

**Debt:**
- Duplicates A3's scanner invocation with a different severity scale (see A3).
- The finding key includes the **line number**, so a patch that shifts a pre-existing finding down
  by one line registers it as new and rejects the patch. Any insertion above an existing finding
  triggers a false rejection.
- `tools=["bandit" if f in post_bandit else "semgrep"]` uses list-membership on dicts — O(n) per
  finding and wrong when both tools report an identical dict.
- `security_score = 100 - 25·n` with no severity weighting: four style-level semgrep hits score
  the same as one RCE.

### A10 — Mergeability Intelligence (`a10_mci_scorer.py` 112 + `a10_routing.py` 172)

**Does:** MCI phantom-change verification, four axis scores, routing decision, proof-bundle
construction, **and GitHub branch creation + commit + PR publication**.

**Matches intent:** the routing logic in `a10_routing.py` is clean, well tested (159-line test
file), and correctly ordered — hard-draft reasons first, then technical validation, then
confidence downgrades.

**Leakage:** A10 does two jobs. Deciding mergeability is analysis; publishing a PR is an
irreversible external side effect. They are fused in one `run()`, so the decision cannot be
computed without also attempting publication (`github_dry_run` is the only brake), and the PR body
is built twice when the commit SHA changes (`a10:77-80`).

**Debt:** `title = f"[SENTINEL] ..."` — a stale product name. `scope_risk` and `fidelity` are
computed but never gate anything on their own; only `correctness` and `security` have hard
thresholds, so two of the four advertised axes are informational.

### A0 — Orchestrator (`a0_orchestrator.py`, 13 lines)

**Dead code.** Never imported by `nodes.py`, never instantiated, never run. Its stated
responsibility is performed by `PipelineRunner` + `GraphNodes`.

---

## 4. Cross-cutting technical debt

**T1 — Path normalization implemented three times.** `target_resolver.normalize_repo_path` +
`_match_sig_path`, `citation_verifier.normalize_path_token` + `resolve_citation_path` +
`_path_candidates`, and `mci_verifier.normalize_path_token` + `file_paths_equivalent`. Plus
ad-hoc `str(repo) + "/"` replacements in A3 and A9. Three independent notions of "is this the
same file", each with its own bugs. **This is the highest-value extraction in the codebase.**

**T2 — Repository-specific knowledge in generic code.** Grep results:
- `"vulnapi"` hardcoded as a path segment in `target_resolver.py:70` and `citation_verifier.py:124`
- `github_repo_name: str = "vulnapi"` default in `config.py:20`
- JWT/token/expiry semantics in `retry_brief_builder.py:97-101,122-123` and
  `runtime_patch_prompt.py:87-90,295`

`docs/REPO_ASSUMPTIONS_REMOVAL_REPORT.md` suggests a cleanup pass already happened; these are what
survived it. They directly violate the repository-agnostic principle and will silently mislead
the patch LLM on any non-JWT bug.

**T3 — Trust-gate authority is diffuse.** `force_draft_pr` is written by A3.5, A4, and
`apply_trust_gates_before_pr`. `reinvestigation_exhausted` by A4 and the gate function. Reasoning
about why a PR became a draft requires reading three files.

**T4 — `dict` blobs across every boundary.** See §2.2. Cost: no IDE completion, no refactor
safety, no schema validation, silent `KeyError`-as-`None` via `.get()`.

**T5 — Duplicated scanner invocation** (A3/A9) and **duplicated failure-embedding dance**
(A8/A9).

**T6 — Services depend on orchestration types.** `target_resolver`, `repo_layout` import
`RunStateModel`.

**T7 — Hidden global settings.** `edges.py` calls `get_settings()` (an `lru_cache` singleton)
instead of using the injected `Settings`, so routing thresholds ignore per-run configuration.

**T8 — `ui_projection.py` is 1,406 lines** and encodes the payload shape of every agent —
`_narrative_detail`, `_metrics_for`, `_evidence_for`, `_visualization_for` each branch on 11 agent
cards. It is the highest-coupling file in the repository: any agent payload change breaks the UI,
and nothing type-checks that link. This is untracked new work; it should be split per-agent
before it grows further.

**T9 — Working-tree hygiene.** `frontend/` is untracked and contains its own `.git` directory —
committing the parent as-is will produce either a broken embedded repo or an accidental
gitlink. Decide now: submodule, or remove `frontend/.git` and commit as a subdirectory.

---

## 5. Which embedded logic should become a reusable service

Six of the ten named candidates are **already services**; the question for those is whether they
have the right boundary. Verdicts:

| # | Concern | Today | Verdict | Rationale |
|---|---|---|---|---|
| 1 | **Runtime target resolution** | `services/target_resolver.py`, called only by A5 | **Extract properly — P0** | Already a service but takes `RunStateModel`, and A7 re-derives the target independently via `blast.origins[0]`. Make it take plain evidence inputs; make A5, A7, and A5.5 share one answer. |
| 2 | **Runtime prompt generation** | `services/runtime_patch_prompt.py` | **Split — P1** | Conflates four concerns: target-function inference, acceptance-criteria derivation, prompt templating, and patch-integrity validation. Inference and criteria belong to A5.5/A6; templating stays with A7. |
| 3 | **Citation verification** | `services/citation_verifier.py` | **Keep; delete the shim — P2** | Correct boundary and genuinely deterministic. `citation_validator.py` is a pass-through; fold `coerce_llm_citations` into the verifier and delete the rest. A5.5 will reuse the verifier directly. |
| 4 | **Patch integrity validation** | inside `runtime_patch_prompt.py` | **Extract — P1** | `validate_patch_integrity`, `has_semantic_diff`, `is_abbreviated_patch`, `top_level_definitions` have nothing to do with prompts. Own module `services/patch_integrity.py`; A7 and any future patch consumer use it. |
| 5 | **Scoped validation** | `services/scoped_validation.py` | **Keep — good** | Clean boundary, returns a dataclass, well tested. Model for the rest. |
| 6 | **Retry brief generation** | `services/retry_brief_builder.py` | **Keep; de-domain — P0** | Boundary is right; content is not. Strip JWT/token literals. Also pull the duplicated A8/A9 embedding dance into it. |
| 7 | **Semantic graph cache** | `services/sig_cache.py` + `RedisStore.get/set_sig_cache` | **Generalize — P1** | Cache *policy* is in `sig_cache`, cache *keys* leak into `RedisStore` with a SIG-specific method pair. Introduce a generic `CacheService(namespace, version, key, ttl)`; SIG becomes one namespace, context packages another. |
| 8 | **AST parser** | `services/python_ast_parser.py` | **Extend — P0 for A5.5** | Single-parse-per-file is right, but `ParsedModule` records only *names* — no line spans, no call graph, no per-function source. A5.5 needs function-level extraction. Add `FunctionSpan(name, lineno, end_lineno, calls, decorators)` and a repo-scoped `ASTIndex` cache. Additive; no existing caller changes. |
| 9 | **LLM gateway** | `services/llm.py`, constructed inline at 6 sites | **Extract — P0** | Currently a provider switch, not a gateway. Needs: singleton/injected instance, retry with backoff, timeout, concurrency semaphore, token + cost accounting per run, prompt/response tracing behind a flag, and optional response cache keyed on prompt hash. Without this there is no way to measure whether A5.5 actually reduces cost — which is its entire justification. |
| 10 | **Git helpers** | `services/git_service.py` | **Keep; harden — P2** | Right boundary. But every function swallows all exceptions into empty defaults, so a broken repo silently yields zero churn weights and an empty style exemplar with no signal. Add a typed result or logging. |

**Plus one not on the list — P0:** `services/path_resolution.py`, unifying T1. Everything above
depends on agreeing what "the same file" means.

### Recommended extraction order

```
1. path_resolution        ← unblocks target_resolver, citation_verifier, mci_verifier, A3, A9
2. llm_gateway            ← unblocks cost measurement for everything after
3. ast_index (extended)   ← unblocks A5.5 context extraction
4. target_resolution      ← unblocks shared A5/A7/A5.5 target
5. patch_integrity        ← mechanical, no dependents
6. cache_service          ← generalize sig_cache
7. de-domain retry_brief + runtime_patch_prompt
```

---

## 6. Context Engineering Layer (A5.5) — shipped

This section originally proposed A5.5 in detail (ranking weights, subsystem layout, full
`ContextPackage` schema, a five-phase migration plan, projected token savings, and a risk table).
Per the roadmap (§8, P2) it has shipped: it runs in the graph as `engineer_context` between
`blast_scope` and `plan_fixes`, publishes via `GET /api/runs/{id}/context`, and has a V1 context
panel. The code is now the source of truth for its exact behavior — see
`backend/services/context/` (`ranking.py`, `extraction.py`, `privacy.py`, `builder.py`,
`cache.py`), `backend/models/context.py` for the `ContextPackage` schema, and
`backend/agents/a5_5_context_engineer.py` for orchestration. It makes zero LLM calls by design;
if ranking ever needs LLM judgment, that judgment belongs in A4 as evidence, not in A5.5 as
inference.

Two defects surfaced post-ship and remain open — see roadmap items 7b (privacy guard doesn't
cover acceptance criteria/constraints/tracebacks, only extracted code) and 7c (per-citation
`verified` / per-file `propagation_confidence` are aggregated away before reaching the UI).

---

## 7. Bottlenecks and scalability

**Now:**
1. A3.5 full pytest, 120 s hard cap — the pipeline's wall-clock floor.
2. A8 runs pytest **twice** (target + full regression) plus mutmut. On a large suite this is the
   dominant cost, and it repeats per retry.
3. A1 `_build_imported_by` — O(n²) substring matching.
4. A5 backward traversal — O(files × imports) per hop.
5. A2 serial OSV queries — network N+1.
6. A7 duplicate file content in the prompt.

**At scale:**
- **Single-process.** `WSBroadcaster` is an in-memory dict; `PipelineRunner` holds a
  `MemorySaver`. Two API replicas means clients miss events depending on which replica they hit,
  and no run can resume after a restart. The Redis pub/sub path already exists in `ws.py` —
  finishing that migration and moving checkpointing to Redis is what unblocks horizontal scale.
- **No queue.** Runs execute in the request-handling process. No backpressure, no retry, no
  priority, no isolation between tenants.
- **Subprocess execution is unsandboxed.** pytest, bandit, semgrep, mutmut, and ruff run against
  cloned repository code with the host interpreter. For a hosted product this is the blocking
  security issue, ahead of everything in this document.
- **`clone_or_copy_repo` uses `shutil.copytree`** into a temp dir that is never cleaned up. Every
  run leaks a full repository copy.
- **Redis holds full state including patch bodies** (original + patched per file) with a 7-day
  TTL. Memory grows with run count × repo size.
- **`ui_projection` recomputes the entire projection per request** with no memoization, reading
  full state + up to 500 events each time.

---

## 8. Roadmap

> **Status refreshed 2026-08-08.** The audit of the working tree
> (`docs/V1_BACKEND_AUDIT.md` §4) found several P0 items already implemented;
> they are marked ✅ below rather than deleted, so the record of what was fixed
> survives. **The live plan is `docs/V1_COMPLETE_IMPLEMENTATION_PLAN.md`** — this
> section is the backend-debt view feeding into it.
>
> **Workspace V2 is cancelled and removed.** V1
> (`frontend/src/components/proofix/**`) is the only frontend. §6's Context
> Engineering plan (A5.5) shipped as backend work and is unaffected, but no V2
> frontend exists or will be built.

**P0 — correctness and trust**
1. ✅ **Done.** Mutation score parsed for real (`services/mutation_parser.py`); A8 reads
   `mutation_score`/`survived_mutants`/`total_mutants`, leaves `correctness_score = None`
   when there is no patch.
2. ✅ **Done.** A3's stub fallback is gated on `code == -1` (tool genuinely absent).
3. ✅ **Done.** Repository-specific literals (T2) removed — expected behaviour is built
   from evidence, not JWT/expiry keyword tables; `github_repo_name` no longer defaults
   to `vulnapi`.
4. ✅ **Done.** `services/path_resolution.py` extracted, with tests.
5. ✅ **Done.** A9's finding identity dropped the line number (was causing false
   rejections on insertions above existing findings); compares by multiplicity.
6. ✅ **Done.** `services/llm_gateway.py` extracted with token/cost telemetry.
7. ✅ **Done.** Unmeasured axes are `None`, not zero (`services/measurement.py`); an
   absent scanner used to score `security_score = 100.0` and clear the auto-merge gate,
   now scores `None`.
7a. 🔴 **Open (G9).** `run_id` never reaches `LLMGateway.complete()`, so every
   `AuditEvent.run_id` is `""` and per-run cost/token attribution — the entire
   justification for extracting the gateway (item 6) — is impossible.
7b. 🔴 **Open (privacy guard gap).** A JWT reached `acceptance_criteria[2]` with
   `privacy_guard_status: "clean"` and zero redactions. §6.4 specifies the guard
   runs on *every* string leaving the process; it appears to cover extracted code
   only. Criteria, constraints and tracebacks are unguarded.
7c. 🔴 **Open (G11).** Per-citation `verified` and per-file
   `propagation_confidence` are aggregated away before reaching any client, so
   the UI cannot distinguish verified from asserted evidence.

**P1 — architecture**
8. ✅ **Done.** `trust_gating.py` is the only writer of `force_draft_pr`; the
   agents record observations and the gate derives the decision.
9. 🔴 Extract shared target resolution; make A5, A7, A5.5 agree.
10. 🔴 Extract `patch_integrity` from `runtime_patch_prompt`.
11. 🔴 Move `correctness_score` out of A8 into A10's scoring module.
12. 🔴 Split A10: mergeability decision vs. PR publication.
13. ✅ **Done.** `a0_orchestrator.py` and `citation_validator.py` are both
    deleted; `coerce_llm_citations` moved into `citation_verifier.py`.
14. ✅ **Done.** A7 snapshots every file it writes and restores them when an
    exception interrupts generation, so a partial patch set cannot reach A8.

**P2 — Context Engineering** — ✅ shipped. A5.5 executes in the graph
(`engineer_context`), publishes to `GET /api/runs/{id}/context`, and has a V1
card with a context panel rendering the ranking and the redaction ledger
(Phase 4). The privacy guard's status is on screen, which was the point: it is
the only evidence that secrets did not reach the LLM.

**P3 — scale**
15. 🔴 Redis-backed broadcaster and checkpointer; remove single-process assumptions.
16. 🔴 Job queue for run execution.
17. 🔴 Sandbox subprocess execution. **Blocks any hosted multi-tenant deployment.**
18. 🔴 Clean up repo clones; move patch bodies out of Redis state.
19. 🔴 Split `ui_projection.py` per agent (T8).
20. ✅ **Done.** `frontend/.git` resolved (T9) — the frontend is tracked as an
    ordinary subdirectory, no submodule, no gitlink.

**P4 — reach**
21. 🔴 Non-`requirements.txt` dependency formats.
22. 🔴 Language support beyond Python — currently `ast`-bound end to end.

---

## 9. Conventions worth preserving

- Deterministic path first, LLM second, with an explicit stub fallback for every LLM call.
  Every agent honours this; keep it.
- Named weight constants at module top (`root_cause_builder`, `blast_traversal`,
  `ast_import_graph.ROLE_CRITICALITY`). A5.5's ranking must follow the same pattern.
- Services return typed models or dataclasses; agents own state mutation and event emission.
  `scoped_validation` is the reference implementation.
- One test file per service, named `test_<service>.py`. New services get one before they ship.
- `AgentBase.emit_status(state, status, message, payload)` — the only path to the UI. Payload
  keys are a contract; changing one breaks `ui_projection` silently.

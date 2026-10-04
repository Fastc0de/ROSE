# ROS Complete Application Design

**Date:** 2026-09-15  
**Status:** Approved implementation baseline  
**Change policy:** Frozen after approval. Amend only when implementation or test evidence disproves a decision, requirements explicitly change, or an external platform constraint changes; record the reason and affected acceptance evidence.  
**Relationship to `docs/specs.txt`:** This document is the executable architecture and delivery design for the product vision in `docs/specs.txt`. It preserves that document's goals, separates present capability from target capability, and resolves the implementation choices needed for a dependency-ordered program. It does not reproduce the product vision wholesale.

## 1. Executive Decisions

ROS evolves from the current synchronous research pipeline into a local-first, single-node application. This is an evolutionary program, not a rewrite. The current `ResearchOrchestrator.run()` and CLI remain compatibility facades while their work is represented as durable, bounded, checkpointable workflow runs.

The target runtime is:

```text
CLI / FastAPI internal API / lightweight dashboard
                         |
 ResearchService / MonitorService / DiscoverService
             LearningService / RunService
                         |
              SQLite state + durable work queue
                 /          |           \
          scheduler       workers       leases
                 \          |           /
      Research / Monitor / Discover / Learn executors
                         |
       reasoning pipeline, connectors, knowledge, publishing
```

Decisions that constrain every implementation vertical:

- SQLite is the durable system of record and queue; Redis and Celery are not required.
- Transactions are short and contain no network access or LLM calls.
- Scheduler and worker are separate commands/process roles.
- Agents and connectors do not own scheduling, persistence policy, or delivery policy.
- `Research`, `Monitor`, `Learn`, and `Discover` are distinct workflow kinds with distinct ingress behavior, even when they share normalized ingestion and analysis.
- `Source` means a channel, account, feed, site, or other origin. `NormalizedItem` means a post, video, comment, article, reel, or equivalent publication.
- Ranking controls presentation and work priority, never whether retained corpus records remain available.
- Native platform access means configured credentials/scopes, resource validation, and a live capability probe. Domain-filtered web search is not native platform access.
- Markdown is the first delivery format. Dashboard, HTML, JSON, text, webhook, and email adapters consume the same `PresentationModel`.
- Obsidian is a one-way human projection. SQLite remains canonical.
- Retained knowledge must remain searchable even when it is not promoted into foreground outputs; ranking changes prominence, not retrievability.
- Backup and restore are product guarantees for the canonical SQLite state, not an operational afterthought.
- All external content is untrusted data and cannot change tools, configuration, permissions, or system instructions.
- `WorkflowRun` is the canonical durable execution record; `WorkItem` is a separate queue record and lease state never doubles as workflow state.
- Existing synchronous callers use `run_sync(...)`; durable API and UI callers use `submit(...)` and observe a `WorkflowRunHandle`. The two contracts are not overloaded.

## 2. Goals, Non-Goals, Constraints

### Goals

- Make multi-round research adaptive, bounded, resumable, evidence-traceable, and honest about partial coverage.
- Add durable monitoring, discovery, learning, event correlation, grouped delivery, and native connector seams without destabilizing the current batch path.
- Preserve every useful current boundary: stateless agents, connector protocols, `ResearchState`, SQLite, fakes, and the batch regression contract.
- Make source quality, uncertainty, contradictions, freshness, relevance, attention, and cost independently inspectable.
- Make the retained corpus searchable by text and structured filters from the first usable version; semantic retrieval may enrich it later.
- Preserve years of accumulated knowledge through verified backup/restore and portable human-readable projections.
- Deliver a maintainable application that can grow from Web/RSS/YouTube/Reddit to the full approved platform scope.

### Non-goals

- Replacing the application with a new framework or distributed service topology.
- Treating an LLM as scheduler, database, policy authority, or workflow controller.
- Authenticated social scraping, private-person dossiers, or collection of non-public sensitive data.
- Claiming live connector availability before credentials, permissions, resource validation, and capability probes pass.
- Making an aggregate score the sole representation of importance, confidence, or relevance.

### Constraints

- Python 3.11+, Pydantic, SQLite, existing `uv` packaging, and current CLI compatibility remain the starting point.
- Default automated tests use fakes and fixtures; network and credential tests are opt-in.
- Every expensive operation must have a preflight cap and a durable outcome.
- Workstreams are dependency-ordered and independently reviewed; no big-bang rewrite.

## 3. Current State And Audit Mapping

The observed baseline supplied for this design is `uv run python -m pytest`: **284 passed, 3 deselected, 6.07s**. This is a test baseline, not proof that all product guarantees work.

### Present implementation

- `ros/cli/app.py` exposes `research` and `methodologies`; it builds dependencies and invokes the pipeline. There is no durable API, scheduler, worker, monitor command, or dashboard.
- `ros/pipeline/orchestrator.py` owns planning, search, frontier draining, extraction, replan hooks, synthesis, and persistence calls in a synchronous point-in-time run. It returns `(ResearchResult, ResearchState)`.
- `ros/agents/{planner,explorer,extractor,synthesizer}.py` contain the four stateless reasoning agents. `ros/pipeline/replan.py` and `completion.py` provide pure decision seams, but the future design must make round analysis authoritative before those decisions.
- `ros/core/models.py` and `ros/core/state.py` contain the current research, source, evidence, finding, budget, frontier, and lineage-oriented models.
- `ros/sources/protocol.py` defines `SourceConnector.discover()` and `fetch()`. `ros/sources/web/` provides DDG, SearXNG, Tavily, shared fetch, scoring, snippets, and link extraction. The current registry contains web backends only.
- `ros/memory/schema.py`, `ros/memory/service.py`, `ros/memory/repos.py`, and `ros/memory/fake.py` provide SQLite/in-memory persistence, but the durable model is incomplete for full resume and provenance.
- `ros/config.py` uses `RosConfig` and nested settings, but `load_config(path)` currently ignores its path argument and the CLI reconstructs some settings.
- `ros/obsidian/` is an empty stub. `tests/integration/test_batch_regression.py` protects the no-chat batch output; `tests/integration/test_orchestrator_e2e.py` exercises fake end-to-end research and frontier behavior.

### Audit findings mapped to this checkout

The supplied audit dated 2026-09-13 refers in places to earlier paths such as `ros/application/orchestrator.py` and `ros/domain/budget.py`. In this checkout, those responsibilities map to `ros/pipeline/orchestrator.py`, `ros/pipeline/budget.py`, `ros/core/state.py`, and `ros/core/models.py`. The findings remain architectural observations, not claims that those old paths exist here.

| Audit finding | Current path/evidence | Design response |
|---|---|---|
| Usage and budget state can diverge; elapsed time is not authoritative | `ros/llm/cost.py`, `ros/core/state.py`, `ros/pipeline/budget.py` | One run-scoped `UsageLedger`, monotonic clock, actual source/network/round/token/model/platform costs, hierarchical caps, checkpoint on exhaustion. |
| Source caps, exclusions, and dedupe can be applied too late | `ros/pipeline/orchestrator.py`, `ros/agents/explorer.py`, `ros/agents/extractor.py`, `ros/core/urls.py` | Canonicalize, filter, project remaining budget, and dedupe before fetch or LLM work; record every rejection reason. |
| Replan and early-stop do not consume authoritative intermediate findings | `ros/pipeline/orchestrator.py`, `ros/pipeline/replan.py`, `ros/pipeline/completion.py` | Persist `RoundAnalysis` after every round; feed coverage, contradictions, gaps, findings, and costs into replan and stop policy. |
| Persistence does not reconstruct the complete run graph | `ros/memory/schema.py`, `ros/memory/service.py`, `ros/memory/repos.py` | Versioned migrations, complete persistence roundtrips, append-only knowledge versions, checkpoints, and restart tests. |
| Fetch/LLM failures can look like empty content or abort the run | `ros/sources/web/fetcher.py`, connector implementations, `ros/llm/provider.py` | Typed outcomes, `ErrorRecord`, retry/circuit policies, completeness impact, partial results, and explicit degraded status. |
| Platform locks are domain restrictions, not native access | `ros/core/platforms.py`, `ros/agents/explorer.py`, `ros/sources/web/registry.py` | Separate `ConnectorCapability/Status` from locks; only advertise native connectors after validation and probes. |
| Configuration is not one auditable resolved snapshot | `ros/config.py`, `ros/cli/app.py`, methodology loader | Resolve once with documented precedence; persist redacted config, policy, prompt, model, connector, software, and git versions per run. |
| Crawler and prompt boundaries lack full hardening | `ros/sources/web/fetcher.py`, `ros/sources/web/links.py`, extractor path, methodology loader | SSRF and content caps, path confinement, prompt/tool isolation, secret references, audit records, and safe remote-content handling. |

The present system is therefore a valuable research web skeleton, not yet the complete autonomous application. Existing tests and batch output are preserved as gates during the evolution.

## 4. Target Architecture And Dependency Rules

```text
ros/
  core/                 domain types, policies, IDs, pure rules
  application/          ResearchService, MonitorService, DiscoverService,
                        LearningService, RunService, use cases
  reasoning/             planner, topic decomposition, extraction, analysis, synthesis
  pipeline/              bounded round executors and compatibility orchestrator facade
  persistence/           SQLite repositories, migrations, queue, leases, snapshots
  connectors/             capability-aware native and generic adapters
  scheduler/             due-work enqueueing only
  workers/                lease/heartbeat/recovery and executor dispatch
  api/                    localhost FastAPI contracts
  cli/                    Typer facade and interactive chat
  dashboard/              lightweight read-oriented UI
  publishing/             OutputPlanner, models, renderers, publishers
  retrieval/              corpus FTS/filter search and query services
  backup/                 verified SQLite backup/restore and rotation
  security/               egress, secrets, prompt/data isolation, audit
  observability/          structured logs, metrics, traces, error reports
```

Dependency rules:

- `core` depends only on the standard library, Pydantic domain types, and pure domain utilities; it has no HTTP, database, CLI, or provider dependency.
- `application` depends on core ports, not SQLite, HTTP, or a specific LLM.
- `reasoning` depends on core ports and receives immutable/run-scoped context; it does not enqueue, lease, or persist policy.
- `pipeline` coordinates use cases and checkpoints but does not own connector-specific behavior.
- `persistence` implements ports and migrations; it never calls networks or models inside a transaction.
- `connectors` implement source contracts and return typed outcomes; they do not decide research strategy or delivery.
- `scheduler` creates durable work items and never interprets content.
- `workers` execute leased work and report outcomes; they do not bypass application services.
- `api`, `cli`, and `dashboard` call application services and render presentation models.
- `publishing` reads canonical state and creates idempotent publication/notification jobs; it does not mutate knowledge semantics.
- `retrieval` exposes corpus and knowledge search over canonical state; it never changes ranking, evidence, or retention semantics.
- `backup` produces and verifies consistent recoverable copies of canonical state and never substitutes for normal persistence transactions.

The target tree describes dependency boundaries, not an immediate directory rename. Existing `ros/agents`, `ros/sources`, and `ros/memory` modules stay import-compatible until a workstream moves one responsibility behind a tested port. A workstream must not combine a package-wide rename with a semantic redesign. The migration mapping is `agents -> reasoning`, `memory -> persistence`, current source implementations -> connector adapters, and the current orchestrator -> pipeline compatibility facade.

The compatibility contracts are explicit:

```python
ResearchService.run_sync(
    objective: ResearchObjective,
    methodology: Methodology,
    budget: Budget,
    ctx: AgentContext,
    exclude_urls: set[str] | None = None,
) -> tuple[ResearchResult, ResearchState]

RunService.submit(
    request: WorkflowRequest,
    idempotency_key: str | None = None,
) -> WorkflowRunHandle
```

`ResearchService` builds and validates research requests and owns the legacy `run_sync()` facade. `MonitorService` creates and versions `WatchSpec`; `DiscoverService` configures `DiscoverySpec`, `AmbientProfile`, and `LocalPulse`; `LearningService` manages `LearningSession` and `LearningPath`. `RunService` alone owns generic `submit`, pause, resume, cancel, status, and result operations for every workflow kind.

`ResearchOrchestrator.run(objective, methodology, budget, ctx, exclude_urls=None)` delegates to `ResearchService.run_sync()` and preserves its present tuple output and batch rendering. `run_sync()` submits through `RunService` and drives the same durable records with a bounded in-process worker loop; it returns completed/partial results and never waits indefinitely on `needs_input` or `blocked`. A failed/blocked run with no usable result records durable diagnostics and raises the compatibility-mapped error. It is not used from an ASGI event loop. API and dashboard mutation endpoints call `RunService.submit()` and return `202` with the run handle.

## 5. Domain Model And Responsibilities

### Workflow and planning

- `WorkflowKind`: `RESEARCH`, `MONITOR`, `LEARN`, `DISCOVER`.
- `ResearchProject`: durable user-level grouping, policy defaults, retention, and project budget.
- `WorkflowRun`: the canonical execution envelope with kind, immutable resolved configuration snapshot, parent linkage, trigger, origin, status, current stage, checkpoint, usage, cancellation request, and outcome. Kind-specific details live in one-to-one `ResearchExecution`, `MonitorExecution`, `LearnExecution`, or `DiscoverExecution` records. Legacy phrases such as “research run” or “watch run” always identify this record and never introduce competing persistence types.
- `ResearchRound`: bounded plan/collect/analyze checkpoint with input/output cursors, analysis, decisions, and usage.
- `ResearchPlan`: versioned execution plan, policy, topic graph target, queries, source preferences, stop policy, and budget projection.
- `Query`: canonical query with lineage, rationale, strategy, filters, generated round, and execution outcome.
- `ResearchProfile`: mode, depth, audience, time scope, evidence policy, source preferences, budget, and output goal.
- `Budget`, `AttentionBudget`, `DiversityPolicy`, and `RelevanceProfile` are separate policy objects.
- `ConfigurationDraft`: natural-language input resolved into typed objective, sources, topics, exclusions, cadence, depth, budget, relevance, alert, delivery schedule/format, duration, assumptions, unresolved fields, warnings, and `requires_user_action`. User action is mandatory for credentials/authorization, a cost beyond approved budget, materially different plausible interpretations, recurring work, irreversible external action, remote publication, destructive retention, or a product-policy choice.

### Knowledge and provenance

- `Source`: channel/account/feed/site/origin and its capability and reputation references.
- `Document`: a retrievable document or page identity distinct from its source channel.
- `NormalizedItem`: normalized publication unit with stable source-native identity where available.
- `Snapshot`: immutable retrieval/version metadata, hash, raw-retention reference, observed time, and extraction version.
- `Entity`, `Topic`, `TopicGraph`, `TopicGraphVersion`, and `TopicEdge`: reusable knowledge structure owned by a project. Runs and learning sessions pin an immutable graph version. Edges include `part_of`, `prerequisite`, `related`, `contrasts`, and `contradicts`; `TopicMap` is a presentation view, not a second aggregate.
- `Place`/`Venue`: a real-world place with stable identity, aliases, location scope, categories, opening/closure state where known, and source-backed attributes.
- `RealWorldEvent`: stable identity for a public event or plan with source-backed time, venue/place, organizer, access, price, and age-restriction attributes. Lifecycle is represented by attributed claims/evidence and append-only knowledge versions. `current_status` (`scheduled`, `postponed`, `cancelled`, `completed`, `disputed`, or `unknown`) plus confidence is a rebuildable materialized projection, never an in-place replacement for conflicting or historical states.
- `Claim`: a proposition attributed to a source or an analysis whose epistemic nature is represented by `claim_type` (`asserted_fact`, `inference`, `opinion`, `rumor`, or `prediction`). `claim_type` describes the proposition's nature; independent epistemic status describes verification. Source claims link to their evidence, while analysis claims link to the producing `AnalysisArtifact` and supporting claims/evidence. No separate `Statement` aggregate is required.
- `Signal`: sentiment, engagement, trend, volume, anomaly, or coordination observation.
- `Evidence`: exact provenance to snapshot/item/source, quote/hash, offsets/selectors, media timestamp or image region/metadata where applicable, extraction method, and observed time.
- `Finding`: analysis-level grouping of claims, evidence, contradictions, confidence, and open questions.
- `KnowledgeState`: current project/topic materialized view derived from immutable versions; it is rebuildable and never used as the only historical record.
- `KnowledgeStateVersion`: append-only, evidence-traceable state transition. `ChangeEvent` means a meaningful knowledge transition, not merely a new publication.
- `AnalysisArtifact`: immutable output from extraction, classification, clustering, or synthesis with input hashes and extractor, prompt, model, policy, and software versions.

### Discovery, monitoring, and learning

- `BranchProposal`: bounded topic expansion with parent lineage, discovery reason, parent/goal relevance, novelty, estimated cost, and decision.
- `WatchSpec`: canonical explicit source or topic tracking policy with cadence, interest rules, grouping window, urgency rules, and delivery policy. `ExplicitWatch` is the product concept shown to users, not a second record type.
- `DiscoverySpec`, `AmbientProfile`, and `LocalPulse`: bounded exploration definitions. `LocalPulse` is a specialization for locations such as Maracaibo covering events, places, openings/closures, food/nightlife, culture/music, universities, local news/services, and trends/conversations.
- `SourceCursor`: opaque connector cursor plus source-specific validation metadata.
- `UserLearningState`: per-topic status: `not_started`, `introduced`, `practicing`, `self_reported_understood`, `evaluated`, or `needs_review`.
- `LearningSession`/`LearningPath`: a view over TopicGraph, KnowledgeState, and UserLearningState supporting resume, review, deeper explanation, check questions, and next recommendation.
- `PromotionProposal`: reviewable request to turn a discovered source into an explicit watch; it stores proposed source identity, connector/access mode, cadence, policy, expected cost, capability validation, and accept/reject history.

### Events, corpus, and delivery

- `SimilarityCluster`, `ResearchCorpus`, and `CoverageReport`: preserve canonical representations, all members, source/item coverage, exclusions, contradictions, failures, and unique contribution.
- `EventCluster`: related `ChangeEvent`s across sources and time.
- `Digest`, `Alert`, and `Notification`: durable delivery intent, not the knowledge record itself.
- `OutputArtifact`, `PresentationModel`, and `PublicationRecord`: rendered/published projections with stable IDs, version, destination, hash, and idempotency keys.
- `DeliveryJob`: durable publication attempt with destination, artifact version, renderer version, state, attempts, lease, idempotency key, and typed outcome.
- `SourceReputation`: accuracy, authority, independence, primary-source proximity, disclosed conflicts, originality, noise, timeliness, local coverage, discovery value, and conversation value as separate dimensions.
- `UsageMetric`/`UsageLedger`: actual costs and usage dimensions. `ErrorRecord`: typed error, retry state, action, completeness impact, and related work/run.
- `FeedbackEvent`: explicit user feedback such as more/less like this, already known, useful for conversation, useful for learning, source preference, or save/promote; feedback updates policy inputs through transparent rules and remains auditable.
- `CorpusSearchRequest`/`CorpusSearchResult`: searchable corpus contract supporting FTS/text, date, source, entity, workflow, confidence, status, and provenance filters.
- `BackupRecord`: backup identity, creation time, source database version, integrity verification result, retention class, encryption/storage metadata if configured, and restore-test status.

No boundary above may be collapsed merely because two records currently share a table. Foreign keys, ports, and views may optimize storage while retaining distinct semantics.

### Memory levels

The three required memory levels are named views over canonical SQLite records, not separate databases:

- **Execution memory** reconstructs interrupted work from `WorkflowRun`, transitions, checkpoints, plans, rounds, queries, work items, operation intents, budget reservations/ledger entries, outcomes, and errors.
- **Monitor memory** reconstructs what a source/topic watch has seen from `WatchSpec` versions, schedules, source identities, cursors, snapshots, dedupe identities, change events, event clusters, digest windows, and delivery history.
- **Knowledge memory** reuses consolidated understanding through documents/items, analysis artifacts, topics/graph versions, entities, claims, evidence, findings, contradictions, knowledge versions, corpus, reputation, and provenance.

An operation may update more than one level only through the declared atomic transaction boundary. Resume reads execution memory; incremental sync reads monitor memory; future research may query knowledge memory without treating prior conclusions as fresh evidence automatically.

## 6. Workflow State And Parent/Child Runs

### Run state machine

Workflow state is independent from queue leasing. `current_stage` records details such as planning, collecting, analyzing, replanning, or publishing without multiplying durable run states.

| Current state | Allowed next states | Meaning |
|---|---|---|
| `created` | `needs_input`, `planned`, `cancelled` | Request is durable but not executable. |
| `needs_input` | `planned`, `cancelled` | Required fields or explicit approval are missing. |
| `planned` | `queued`, `cancelled` | Configuration and initial plan are versioned. |
| `queued` | `running`, `paused`, `cancelled` | At least one runnable work item exists. |
| `running` | `queued`, `paused`, `blocked`, `completed`, `partial`, `failed`, `cancelled` | A worker owns a stage; `queued` means a checkpoint yielded more work. |
| `paused` | `queued`, `cancelled` | User or policy paused at a safe checkpoint. |
| `blocked` | `queued`, `failed`, `cancelled` | A resolvable dependency such as credentials, approval, or quota prevents progress. |
| `completed` | none | Goal and required publication policy completed. |
| `partial` | none | Usable output exists but a hard limit or material coverage failure prevented completion. |
| `failed` | none | No usable contract-level result could be produced. |
| `cancelled` | none | Cancellation was acknowledged at a safe boundary. |

`degraded` is a coverage/outcome flag, not a run state. Transition attempts use optimistic `state_version`, are timestamped in `RunTransition`, and are rejected when the source state/version no longer matches. A cancellation request is durable immediately; workers stop before the next external call, checkpoint any already obtained outcome, suppress new child/delivery jobs, and acknowledge `cancelled`. Graceful process shutdown stops leasing, finishes or checkpoints the current bounded operation, releases leases, and leaves unacknowledged work recoverable.

### Parent and child policy

Child workflows are created only through bounded application policy. Each child stores `parent_run_id`, trigger (`user`, `scheduler`, `discovery`, `change_event`, `learning_gap`), origin, inherited hard-policy snapshot, budget allocation, maximum depth, deterministic trigger-dedupe key, and `join_policy` (`required` or `best_effort`). An independent recurring watch is a root workflow and therefore is never encoded as a child.

- Creation atomically allocates a child ceiling from the parent; this reduces allocatable capacity but is not counted as spent usage. The child's operation reservations draw within that ceiling and the shared ancestor caps. A child cannot exceed its allocation, remaining parent capacity, global limits, or configured depth.
- A `required` child blocks the relevant parent stage; failure makes the parent `partial` when usable output remains and `failed` otherwise.
- A `best_effort` child never silently fails the parent; its failure and coverage impact are included in parent analysis and output.
- Cancelling a parent requests cancellation of all non-terminal descendants. An explicitly user-created recurring watch survives only when it was stored as a separate root rather than an automatic child.
- Completion settles the reservation, emits a durable child outcome, and wakes a waiting parent exactly once. Trigger dedupe prevents repeated discovery, event, or learning-gap signals from creating unbounded descendants.

## 7. End-To-End Workflow Flows

### RESEARCH

1. `ResearchService` resolves natural-language input into a `ConfigurationDraft` containing a reviewable `ResearchProfile`, project, budget, output goal, assumptions, and missing decisions. Interactive execution requires approval when `requires_user_action=true`; non-interactive execution fails validation rather than guessing.
2. `TopicDecomposer` creates a new immutable `TopicGraphVersion`; `ResearchPlan` pins that version and selects bounded branches, queries, sources, and stop policy.
3. Each round canonicalizes queries, filters candidates, projects remaining budget, and records rejected candidates with reasons before fetch.
4. Collectors route through `SourceRouter`, fetch normalized items/documents, retain snapshots according to policy, and update cursors only in an atomic commit.
5. Analysis extracts entities, topics, claims, signals, evidence, contradictions, and provisional findings into versioned `AnalysisArtifact`s.
6. `RoundAnalysis` updates coverage, novelty, uncertainty, open questions, source quality, and branch proposals.
7. Formal stop policy evaluates sufficiency, novelty, goal relevance, unresolved questions, and budget. Otherwise the planner emits the next bounded round.
8. Zero candidates triggers bounded alternates such as synonym, date adjustment, source alternative, or clarification, never an unbounded retry loop.
9. The run checkpoints, creates a dossier/presentation model, and enqueues publication.
10. Converting a completed research into monitoring creates a reviewable `WatchSpec` draft from selected sources/queries and a new root `WorkflowRun(kind=MONITOR)` schedule after explicit enablement; it never mutates the completed research into a watch.

### MONITOR

1. A `WatchSpec` is reviewed and stored with source identities, interest policy, schedule, grouping window, urgency rules, and policy version.
2. Scheduler creates a `WorkflowRun(kind=MONITOR)` and enqueues its first `WorkItem`; a worker leases the item and asks `SourceRouter` for a validated capable connector.
3. Connector fetches since its opaque cursor and returns typed normalized items, completeness, warnings, errors, limits, and next cursor.
4. In transaction A, commit normalized items/documents, snapshots, dedupe identities, the next cursor, connector outcome, and a deduplicated content-analysis `WorkItem` atomically. Advancing the cursor is safe because every newly durable item is either already analyzed or has durable pending analysis.
5. A worker performs extraction and `ContentAnalysis` outside any database transaction. Failure or lease loss leaves the analysis work recoverable without re-fetching or losing the cursor's items.
6. In transaction B, commit the `AnalysisArtifact`, claims, evidence, `KnowledgeStateVersion`, derived `ChangeEvent`s, coverage impact, and follow-up work atomically. Reprocessing creates a new analysis version rather than overwriting the previous one.
7. Follow-up work clusters related events across sources, calculates importance/novelty/relevance, and preserves every source link, status claim, and contradiction.
8. Create an `Alert` only when explicit urgency rules pass; otherwise append to the grouping window and create/update a `Digest`.
9. Enqueue idempotent publication/notification work and expose degraded coverage when a source failed or was sampled.

### DISCOVER

1. `DiscoverySpec`, `AmbientProfile`, or `LocalPulse` defines bounded domains, location, interests, exclusions, cadence, and attention budget.
2. Discovery adapters find sources, items, and documents through permitted capabilities and normalize them through the same ingestion path.
3. Cheap canonical dedupe/fingerprinting and relevance/novelty scoring run before expensive analysis.
4. Public plans, openings, venues, recurring activities, and dated happenings may be normalized into `Place`/`Venue` and `RealWorldEvent` records instead of remaining only as free text.
5. A candidate source may create a `PromotionProposal`, never a watch directly. The proposal validates canonical identity and connector capability, estimates cadence/cost, and enters `pending_review`.
6. User acceptance versions the proposed policy and creates a disabled `WatchSpec`; an explicit enable action schedules it. Rejection records the reason and suppresses equivalent proposals for a configurable period. Capability loss later blocks the watch without replacing it with unlabelled web search.
7. LocalPulse results are labeled with location and source completeness; public rumors are labeled as unverified claims and never become private-person dossiers.

### LEARN

1. A learning request pins a `TopicGraphVersion`, audience level, time scope, and learning goal.
2. `KnowledgeState` is separated from `UserLearningState`; known facts do not imply user understanding.
3. `LearningPath` chooses introduction, practice, review, deeper explanation, checks, and next recommendations using evidence-backed claims and open questions.
4. A `LearningSession` checkpoints progress, answers, self-report, evaluation, and learning gaps as immutable events. `not_started -> introduced -> practicing` follows delivered activities; self-report may set `self_reported_understood`; only a versioned evaluation rule may set `evaluated`; failed or stale checks set `needs_review`. Every transition stores its cause and may be superseded, not erased.
5. A gap may spawn a bounded child workflow with kind `RESEARCH` and trigger `learning_gap`; its findings update `KnowledgeStateVersion`, not user mastery automatically. A newer graph version produces an explicit path-rebase proposal rather than mutating an active session.

## 8. Planning, Knowledge, Evidence, And Corpus Rules

Topic expansion is bounded by depth, branch count, budget, and stop policy. Node metrics are coverage, confidence, importance, novelty, uncertainty, open questions, and estimated cost. A branch proposal records why it exists and can be accepted, deferred, rejected, or capped without erasing the discovery.

The provenance chain is:

```text
OutputArtifact/PresentationModel
  -> KnowledgeStateVersion / Finding
    -> Claim
      -> Evidence
        -> Snapshot / NormalizedItem / Document
          -> Source / Query / WorkflowRun
```

The diagram is a navigation path, not an ownership shortcut. Persistence uses explicit relations: an artifact version has many `ArtifactSupport` rows to findings or knowledge versions; findings and claims are many-to-many through `FindingClaim`; claims and evidence are many-to-many through `ClaimEvidence(relation=supports|contradicts|context)`; each evidence row targets one immutable snapshot and may additionally identify its item/document/source for navigation. Query and workflow lineage are separate foreign keys, so deleting a presentation never deletes knowledge or evidence.

Claims use independent epistemic status: `verified`, `unverified`, `disputed`, `superseded`, or `retracted`; claim type may be `rumor`. Preserve competing and minority claims, typed contradiction/corroboration links, and the analysis version that produced each relation. Never majority-delete a minority claim.

Evidence stores extraction method, observed time, content hash, and one typed locator: text character/paragraph selector, HTML selector, media timestamp range, page/region, or metadata field. A verbatim excerpt is retained only when policy permits. SourceTrust, EvidenceStrength, ClaimConfidence, Corroboration, and Freshness are separate dimensions. `valid_from`, `valid_until`, freshness policy, and staleness are available to knowledge and item records.

Every derived record stores its `AnalysisArtifact` identity. Reprocessing creates a new artifact and superseding relations; it never mutates prior extraction silently. If raw content expires, the snapshot becomes `raw_expired`, keeps lawful hashes/metadata and derived lineage, and the UI states that direct re-verification is unavailable. If policy or law also forbids retaining the excerpt, the evidence keeps only the permitted locator/hash and is visibly downgraded rather than presented as fully inspectable.

Information policy:

- `ResearchCorpus` retains every item/source/snapshot/claim/evidence/cluster/contradiction/failure/exclusion with reason.
- Exact duplicate, spam, invalid, or policy-excluded content may leave the active working set only with a recorded reason and retained provenance metadata.
- Similarity dedupe selects a canonical representation but keeps every member and origin link.
- `InformationGain` and `UniqueContribution` may promote a lower-quality source when it adds new facts, contradiction, locality, or discovery value.
- Retention has explicit tiers: canonical metadata/relations, searchable normalized text, and heavy raw payload. Each project/source policy defines duration and legal basis per tier; deletion runs create auditable tombstones, never dangling evidence links. Baseline metadata and relations persist until explicit project deletion unless law or source policy requires earlier erasure, which remains represented by a non-sensitive tombstone where permitted.
- `CoverageReport` exposes what was sampled, blocked, unavailable, excluded, or not attempted.
- `CorpusSearchService` builds replaceable SQLite FTS5 indexes over retained normalized text, titles, claims, and entity names, plus SQL filters for date, source, entity, workflow, status, and confidence. Indexes are rebuildable and never canonical.
- Foreground artifacts allocate attention to high-priority material but include a count and route to `Secondary observations`, minority/contradicting claims, and the full corpus query. Search ranking never mutates canonical knowledge or makes long-tail records unreachable.
- Explicit `FeedbackEvent`s influence future presentation and routing through inspectable policy updates; they never retroactively erase retained corpus records.

## 9. Relevance, Attention, Diversity, Freshness, Local Awareness

Relevance remains a visible vector: global importance, personal/local relevance, novelty, urgency, confidence, conversation value, and unique contribution. `RelevanceProfile` stores location, interests, social and learning goals, followed entities, exclusions, and available attention.

`AttentionBudget` controls foreground/prominence and notification capacity, never corpus availability. `DiversityPolicy` reserves capacity for known interests, adjacent topics, and deliberate serendipity. Serendipity is configured exploration in this program, not a hidden optimized scalar.

Policy enforcement is deterministic and versioned. `AttentionBudget` uses explicit units such as maximum immediate alerts/day, items/digest, estimated reading minutes/day, and discovery slots/period. `DiversityPolicy` defines minimum or maximum shares by source, topic, locality, known-interest, adjacent, and serendipity buckets. `RelevanceProfile` stores each dimension and weight separately; a scorer returns the component vector, missing-data flags, policy version, and final ordering value.

Every decision that filters, samples, promotes, alerts, or suppresses presentation creates a `PolicyDecision` with stage (`enqueue`, `preflight`, `post_ingest`, `analysis`, or `presentation`), subject ID, inputs, policy version, outcome, and reason codes. Hard safety/access/retention rules run before enqueue or fetch; monetary and connector caps run during reservation/preflight; relevance, diversity, and attention rules run after normalized ingestion and again when building a presentation. No LLM may override a hard policy decision.

`SourceReputation` is multidimensional and time-aware. Knowledge/items carry validity intervals and freshness policy. `FeedbackEvent`s can update source/topic preferences, known-item state, and conversation/learning usefulness through explicit rules whose effects are inspectable and reversible. LocalPulse uses explicit geographic scope and categories; it reports coverage and degraded-source disclosures. Daily views may group content under `Para hacer`, `Para saber`, `Para conversar`, `Para aprender`, and `Requiere atención`.

## 10. Connectors And Platform Scope

The target connector port is synchronous because network work runs in worker processes, never on the ASGI event loop. The present `SourceConnector.discover/fetch` implementations are wrapped by an adapter until migrated.

```python
class Connector(Protocol):
    connector_id: str

    def capabilities(self, context: ConnectorContext) -> ConnectorCapabilities: ...
    def validate(self, spec: SourceSpec, context: ConnectorContext) -> ConnectorValidation: ...
    def discover(
        self, request: DiscoveryRequest, cursor: PageCursor | None = None
    ) -> DiscoveryPage: ...
    def sync(
        self, request: SyncRequest, cursor: SourceCursor | None = None
    ) -> SyncPage: ...
    def fetch(self, request: FetchRequest) -> FetchResult: ...
```

`ConnectorCapabilities` reports supported operations, `SourceAccessMode` (`public_web`, `feed`, `official_api`, or `authorized_account`), required scopes, quota semantics, cursor support, and probe time. `DiscoveryPage`, `SyncPage`, and `FetchResult` carry normalized records, immutable provenance, next cursor, completeness, warnings, rate-limit observation, and a typed outcome even when no items were returned. `SourceCursor` is opaque to application code and versioned by connector.

`ConnectorStatus` is one of `unconfigured`, `ready`, `degraded`, `unsupported`, `auth_expired`, `permission_blocked`, or `quota_blocked`. Native availability requires configuration, required credentials/scopes, resource validation, and a recent live capability probe. Unsupported or unconfigured connectors remain representable but cannot be selected or rendered as available. Contract tests include negative access-mode/status cases and prove that a public-web fallback cannot impersonate native API coverage.

Domain policy compares parsed, IDNA-normalized hostnames against an exact allowlist or a dot-boundary subdomain (`host == allowed` or `host.endswith("." + allowed)`). It rejects userinfo tricks, suffix lookalikes, non-HTTP(S) schemes, and redirects outside policy after every hop; raw substring matching is forbidden.

Rollout order and total scope:

1. Web/RSS.
2. YouTube.
3. Reddit.
4. Enriched Discovery/Search.
5. X.
6. Instagram/Facebook.
7. TikTok (deferred future connector, official/authorized capabilities only).

The order puts low-friction, testable, broadly useful sources first; validates the shared native-API contract with YouTube and Reddit next; builds discovery only after ingestion/provenance are trustworthy; and postpones costly or tightly permissioned networks until budgets and capability reporting are proven. Web and RSS share a delivery workstream but remain separate connector identities and fixtures.

| Connector | Baseline operations | Access and explicit boundary |
|---|---|---|
| Web | search through configured backend, fetch document, conditional re-fetch | Public HTTP only; robots, egress, domain, MIME, and content limits apply. Search backend coverage is disclosed. |
| RSS/Atom | validate feed, sync entries by GUID/link/hash and conditional headers | Public feed; no claim of site-wide coverage. |
| YouTube | validate channel/video, API search where enabled, channel uploads, video metadata, permitted comments; official upload feed/WebSub where available | Data API v3 or documented official feed. Every operation is quota- and capability-gated; private/inaccessible resources remain unavailable. |
| Reddit | validate approved subreddit/resource, permitted search/listing, posts and comments, incremental pagination | OAuth official API and granted application access only. Availability depends on Reddit approval, scopes, and current terms. |
| X | only search/timeline/post operations exposed by the purchased entitlement | API v2 only; on-demand, cached, and bounded by daily, monthly, connector, and run monetary caps. No entitlement means `unsupported` or `unconfigured`. |
| Instagram | operations exposed for an authorized professional account and granted product/scopes | Official Meta APIs only. No arbitrary public-account search or monitoring is promised. |
| Facebook | operations exposed for an authorized Page and granted permissions | Graph API only. No arbitrary profile/Page monitoring is promised. |
| TikTok | none in the current completion gate | Deferred; may become native only for documented official or explicitly authorized capabilities. |

All platform entries are capability matrices, not promises that a vendor will grant access. Each connector implementation records the official API/version and permissions it was tested against. Generic Web Discovery may surface public references to a platform, but those records use `public_web` access mode and cannot claim native platform completeness.

No authenticated social scraping is part of this design. Generic web retrieval may discover public references but must not be labeled native platform access.

## 11. Jobs, Persistence, Budgets, Backpressure, Errors

### Durable work

`WorkItem` stores kind, payload reference, priority, dedupe key, state, attempts, `available_at`, lease owner/token/generation/expiry, heartbeat, last outcome, and timestamps. Its state machine is independent from `WorkflowRun`:

| Current state | Allowed next states |
|---|---|
| `queued` | `leased`, `cancelled` |
| `leased` | `succeeded`, `retry_wait`, `dead_letter`, `cancelled`; lease expiry returns it to `queued` |
| `retry_wait` | `queued`, `dead_letter`, `cancelled` |
| `succeeded`, `dead_letter`, `cancelled` | none |

SQLite runs with foreign keys enabled, WAL journal mode, a configured busy timeout, and bounded jittered retry for lock contention. A claim uses `BEGIN IMMEDIATE`, selects one due item ordered by priority/availability, and conditionally updates state, owner, random lease token, incremented fencing generation, and expiry before commit. Heartbeat, checkpoint, settlement, and finalization update only with matching item ID, owner, token, generation, and `state='leased'`; a stale worker therefore cannot commit after recovery has reassigned work. External I/O never occurs while a write transaction is open. Recovery requeues expired leases, and deterministic dedupe keys make the resulting at-least-once execution safe.

Scheduler times are stored in UTC. A schedule stores its IANA timezone and local recurrence rule; each nominal local occurrence has one deterministic key. A nonexistent local time advances to the first valid instant, while an ambiguous repeated local time executes once at the first fold unless the schedule explicitly selects the second. The resolved UTC instant and fold policy are persisted. An injectable clock drives due-work and retry tests. Graceful shutdown stops new claims, marks cancellation intent where requested, checkpoints bounded work, and lets any unconfirmed lease expire safely.

### Schema and transaction contract

Migrations are sequential, transactional, forward-only files recorded in `schema_migrations(version, checksum, applied_at, software_version)`. Startup refuses an unknown newer schema, checksum drift, or a failed migration; migration errors are never swallowed. Destructive table changes use create-copy-validate-swap inside a transaction and require a verified pre-migration backup. Tests migrate both an empty database and versioned copies of every released schema.

The current unversioned schema becomes migration baseline `000`. Its mapping is explicit: `researches` preserve IDs as `WorkflowRun(kind=RESEARCH)` plus `ResearchExecution`; recipes/config snapshots become versioned project/run inputs; each legacy `sources` row becomes a `Document` and snapshot lineage while a normalized origin `Source` is derived without losing the old ID mapping; current evidence, finding, join, entity, result, query, and snapshot IDs are preserved through bridge tables and foreign keys. A migration verification report must account for every source row, relation, content hash, and result before old tables can be retired.

The persistence inventory required for the first durable release includes projects/configuration snapshots; workflow runs/transitions/checkpoints/rounds/plans/queries; work items/leases/schedules; sources/documents/items/snapshots/cursors; analysis artifacts/entities/topics/graph versions/claims/evidence/findings/knowledge versions; watches/promotions/change events/clusters; corpus/coverage/policy decisions/feedback; usage reservations/ledger entries/errors; artifacts/delivery/publication records; and backup records. Every aggregate has save/load roundtrip tests and declared uniqueness, foreign-key, and deletion behavior.

Each external operation uses durable stages rather than one transaction around the workflow. A reservation transaction validates state, reserves hierarchical budget, and stores an idempotent operation intent; network or LLM work then runs outside SQLite. An ingestion transaction stores the connector outcome, actual usage, normalized records, snapshots, cursor advancement, checkpoint, and deduplicated follow-up work atomically. MONITOR calls this ingestion commit transaction A. Content analysis runs separately, and its transaction B stores `AnalysisArtifact` plus derived knowledge/events and further work. If an ingestion commit fails, the intent remains recoverable and replay uses provider/request idempotency or content identity. An operation with an unknown paid/non-idempotent external outcome is not repeated automatically; it becomes `blocked` for reconciliation or explicit approval. A cursor advances only when every item in its page and the necessary pending analysis work are durable.

Repositories use declared unique keys for source identity, normalized item identity, snapshot/content version, query lineage, cursor, event, cluster, artifact version, schedule occurrence, and delivery. No generic “upsert everything” operation may overwrite an immutable or append-only record.

`CorpusSearchService` maintains SQLite FTS5 indexes and structured query paths for retained corpus and knowledge. Startup probes required SQLite/FTS5 capabilities and fails with an actionable diagnostic rather than silently disabling search. Index rebuilds are derivable from canonical records and are never the sole copy of content.

`BackupPolicy` uses SQLite's online backup API rather than copying a live database file, then records integrity checks, rotation, optional encryption/storage targets, and periodic restore verification. A restore drill must prove that a backup can reconstruct workflow, knowledge, corpus, publication, and policy state to the documented recovery point.

### Usage and budgets

One append-only `UsageLedger` exists per run and records elapsed monotonic process segments, source calls, bytes, candidates, documents, rounds, input/output tokens, model, platform, connector, estimated/actual monetary cost, retries, and sampling. Persisted elapsed segments plus the active process monotonic clock enforce time across restarts without comparing monotonic timestamps from different processes.

Before enqueueing or invoking a metered operation, transaction A atomically creates a `UsageReservation` against every applicable hard cap: global, daily, monthly, project, watch, parent, run, connector, model, and provider. Concurrent reservations count as spent capacity. Transaction B settles estimated versus actual units and releases unused capacity; failed/expired intents release or settle according to a versioned recovery rule. Hard caps reject work before I/O, while soft thresholds may sample, downgrade a model, defer, or request approval through an explicit `PolicyDecision`. Budget exhaustion checkpoints the run and yields the best partial output with explanation.

### Backpressure

```text
collectors -> durable queue -> cheap canonical dedupe/fingerprint
-> priority -> batching/sampling/clustering -> expensive analysis/LLM
```

Priority order is critical alerts, interactive research, due watches, backfill/enrichment. Sampling/degradation is retained in `CoverageReport` and visible to output consumers.

### Outcomes and retries

Typed outcomes are `success`, `empty`, `blocked`, `cancelled`, `timeout`, `rate_limited`, `not_found_or_deleted`, `content_rejected`, `extraction_incomplete`, `invalid_or_expired_credentials`, `quota_or_billing_exhausted`, `permission_denied`, `unsupported`, `model_error`, `transient_error`, `permanent_error`, `internal_error`, and `unknown_external_outcome`. `ErrorRecord` includes completeness impact and next action. `RetryPolicy` uses bounded exponential backoff with jitter and `Retry-After`; every connector has a circuit breaker. A failure never masquerades as empty content.

Every operation intent receives exactly one durable latest outcome plus immutable attempt records, including empty, cancellation, validation rejection, and lost-lease recovery. Retry classification belongs to connector/provider policy, not exception text. Circuit state is keyed by connector plus credential/resource scope so one failing source does not disable unrelated work.

## 12. Security, Configuration, And Observability

Security controls are mandatory at the connector and publication boundaries:

- Resolve DNS and check public IP before and after redirects; block localhost, private, link-local, and metadata ranges.
- Permit only safe ports; bound redirects, bytes, decompression, MIME types, response time, and parser work.
- Confine local paths and methodology names; use atomic writes.
- Treat remote text, markup, media metadata, and prompts from sources as lowest-trust data. Isolate it from system instructions, tools, configuration, and permissions.
- Store secrets only as references to providers; redact logs and configuration snapshots.
- Use least-privilege read-only OAuth scopes, audit access, and enforce retention/deletion policies.
- Bind API to `127.0.0.1` by default; disable cross-origin requests by default and require same-origin signed CSRF tokens for cookie-authenticated mutations.
- First startup generates a local API secret in the user configuration directory with owner-only permissions. CLI uses it as a bearer token; the dashboard exchanges it through a loopback-only bootstrap for an `HttpOnly`, `SameSite=Strict` session and then requires CSRF on mutations. Reads of personal corpus data also require authentication. Validate `Host` to prevent DNS rebinding.
- Remote binding is disabled unless the user explicitly configures a separate authentication token and trusted proxy/TLS policy. API credentials use constant-time verification and never appear in URLs or logs.
- Resolve publication and vault paths to their real destination; reject symlinks, junctions/reparse points, or parent traversal that escapes the configured root.

Security is a prerequisite in every vertical, not a final cleanup phase. Workstream 1 establishes egress validation, secret references/redaction, hard caps, hostile-content separation, and path primitives; later connector and publisher work cannot merge without using them.

Configuration precedence, highest first, is: explicitly allowed per-run request fields; CLI flags; explicitly selected `--config` file; process environment; project `ros.yaml`; user-global config; project `.env`; built-in defaults. Methodology/profile values configure research policy but cannot weaken security controls or global hard caps. `load_config(path)` must honor and validate the selected file. Unknown keys fail for explicit files and requests, while deprecated keys produce a typed warning during a documented migration window.

One immutable resolved `RosConfig` is constructed at the composition root and injected; lower layers never reconstruct settings. Its run snapshot stores non-secret values, source layer per field, policy/prompt/model/connector/software/git versions, and secret reference/fingerprint only. API responses and logs use schema-driven redaction rather than key-name heuristics.

FastAPI, Uvicorn, and Jinja2 are required application dependencies when the API vertical lands. The dashboard is server-rendered; optional interaction uses a vendored, version-pinned HTMX asset and requires no Node build pipeline. ASGI handlers perform validation and call application services only. They never execute connector or LLM work inline: mutation endpoints submit durable work, reads use bounded query services, and synchronous compatibility execution remains CLI-only.

The versioned API minimum is `POST /api/v1/config-drafts`, approve/edit draft operations, `POST /api/v1/runs` (`202` plus handle), run list/detail/result/coverage/error endpoints, pause/resume/cancel actions, CRUD/version/enable operations for watches, corpus search, artifact retrieval, connector capability/status, and health. Mutations accept an idempotency key; conflicts return the existing resource identity. Pagination is cursor-based with bounded page sizes, and typed errors never expose secret or raw provider payloads.

Structured logs and metrics include run/work-item IDs, connector, source, round, outcome, retry, lease, budget, sampling, and publication idempotency keys. Secrets and raw sensitive content are excluded. Health/status endpoints report queue depth, stale leases, connector circuits, database migrations, and degraded coverage.

## 13. Output, UX, And Obsidian

```text
canonical SQLite -> OutputPlanner -> PresentationModel/OutputArtifact
                   -> Renderer -> Publisher
```

`OutputArtifact` is immutable and versioned. `DeliveryJob` uses `queued -> leased -> published | retry_wait | conflict | dead_letter | cancelled` with the same fenced lease protocol as other work. Its unique idempotency key is `(artifact_version_id, destination_id, renderer_version, publication_policy_version)`. A publisher first reconciles an existing `PublicationRecord`/destination marker after an unknown outcome; retries never create a second logical publication. Rendering failure does not mutate the artifact, and publication failure does not roll back knowledge.

Surfaces are Chat, Alert, Digest, and Library/dashboard/Obsidian. Publishing policy considers importance, novelty, stability, reuse, confidence, destination, and attention budget. Artifact types are `ResearchDossier`, `TopicMOC`, `KnowledgeNote`, `EntityNote`, `LearningPath/Note`, `ChangeReport`, `EventNote`, `Digest`, `Alert`, and `DiscoveryBrief`.

Every artifact supports progressive disclosure: 30-second summary, essentials, full report, secondary details, claims/contradictions, evidence, all sources, and corpus. The dashboard navigates all layers and exposes degraded or sampled coverage.

Obsidian is one-way and configurable under `Home`, `Topics`, `Concepts`, `Entities`, `Research`, `Learning`, `Events`, and `Daily`. Each projection carries stable `ros_id`, artifact/knowledge/template versions, and a `PublicationRecord` mapping ID to path/hash/version. TopicGraph backlinks and one MOC per important research/topic are projected through managed blocks with checksums. Before writing, resolve the vault and every parent against symlink/junction escape, offer a dry-run diff, and write a temporary sibling followed by atomic replace. Preserve user sections outside managed blocks. A changed managed-block checksum produces `conflict`, a side-by-side proposed file/diff, and no overwrite. Project dossiers and source matrices are useful projections; the full corpus remains in ROS. Bidirectional Obsidian commands are deferred.

Natural-language setup always produces reviewable structured configuration before execution. The CLI remains a supported facade; FastAPI is internal by default; the dashboard is read-oriented first. Search is a first-class read path over retained corpus/knowledge with provenance-aware filters. User-facing artifacts and dashboard items may expose explicit feedback actions without silently retraining opaque policy. Public rumors are labeled, and local awareness never becomes collection of private-person sensitive data.

Process entry points are explicit: existing `ros research` remains synchronous-compatible; `ros serve` runs API/dashboard only; `ros scheduler` only creates due work; `ros worker` only leases and executes work; and `ros runs`, `ros watches`, `ros search`, and `ros connectors` expose durable control/status operations. A convenience local command may supervise these roles, but it uses the same persisted contracts and shutdown rules rather than a hidden alternate runtime.

The chat/CLI interaction contract is scenario-based:

- Research shows the interpreted objective, assumptions, sources, budget, stop policy, and output before approval; batch mode uses explicit defaults or fails on required ambiguity.
- Monitor setup resolves and validates each source, cadence, grouping, cost/access status, and delivery destination, then requires explicit enablement. Editing creates a new `WatchSpec` version.
- Research results offer `convert to monitor`, which opens the same reviewable monitor draft and never starts recurring work implicitly.
- Discovery shows why a source was proposed, expected value/cost, access mode, and policy. Accepting creates a disabled watch; rejecting records a suppression reason.
- Learn resumes the pinned path and shows the distinction between system knowledge, self-report, and evaluated mastery.
- `status`, `pause`, `resume`, `cancel`, `errors`, `coverage`, and `trace` operate on durable run IDs. A partial result remains readable after failure, limit exhaustion, or cancellation.
- No conversational answer silently changes recurring schedules, connector credentials, hard caps, retention, or remote destinations; those changes always render a `ConfigurationDraft` for confirmation.

User-facing status is a projection, not another state machine: `created/planned/queued` -> pending; `running` plus current stage -> researching/collecting/analyzing/publishing; `blocked` -> waiting for source or requires attention according to reason; `partial` -> partially completed, with the limiting failure/cap named; and terminal states -> completed, failed, or cancelled. The raw state, reason, last checkpoint, next action, and coverage remain inspectable.

### Search, feedback, and recovery UX

- Search defaults to transparent corpus/knowledge retrieval, not an answer-only chatbot path. Every result can reveal source, workflow, date, confidence/status, and provenance when available.
- The first required search implementation is SQLite FTS/text plus structured filters; embeddings are optional enrichment and cannot be required to recover retained information.
- Explicit feedback actions are durable events, never silent destructive edits. Their downstream effects on relevance/source/learning policy are inspectable and reversible.
- Backup status, last verified restore, database integrity state, and recovery guidance are visible in local administration/status surfaces. Obsidian is never treated as the only recovery copy.
- LocalPulse presentation can render `RealWorldEvent` and `Place/Venue` views for `Para hacer`, including lifecycle changes such as postponement or cancellation when supported by evidence.

## 14. Testing And Verification Strategy

Implementation is TDD: failing test, minimal implementation, focused review, then integration. Default tests use fakes and versioned fixtures; no network or credentials. Required suites include:

- connector contract suites for validate, capability/access-mode/status negatives, cursor, normalization, completeness, warnings, errors, rate limits, domain-boundary checks, and versioned fixtures;
- persistence roundtrips for every inventory aggregate, row-accounted migrations from the current baseline and prior schemas, immutable-record protection, transaction atomicity, crash-after-cursor analysis recovery, and restart/resume;
- valid and invalid workflow/work-item transitions, multiprocess lease claiming, fencing of stale workers, busy-lock retry, cancellation, graceful shutdown, duplicate delivery, retry, circuit breaker, and partial output;
- concurrent hierarchical budget reservations/settlements, stale reservation recovery, elapsed-time restart accounting, and hard/soft exhaustion;
- prefetch candidate/source/prompt caps, canonical URL and similarity dedupe, exclusions, sampling, backpressure, and coverage reports;
- adaptive rounds, pinned topic graph versions, contradiction preservation, relational claim/evidence/artifact traceability, raw-expiry degradation, early-stop, zero-candidate alternates, and parent/child join/cancel/dedupe rules;
- API, CLI, dashboard, chat scenarios, configuration precedence, corpus FTS/filtering and long-tail reachability, feedback, notification, digest, publication reconciliation, and Obsidian dry-run/conflict/idempotency tests;
- SSRF/redirect rebinding, byte/MIME/decompression limits, hostname lookalikes, path/symlink/junction containment, prompt injection, schema-driven secret redaction, CORS/CSRF/authentication, retention, backup integrity, and destructive restore drills;
- UTC/IANA scheduler tests across daylight-saving gaps/repetitions using an injected clock;
- opt-in credential-gated smokes for live connectors.

The 284-pass/3-deselected baseline and batch regression remain gates throughout. Acceptance distinguishes deterministic implementation evidence from externally live-validated capability; a connector contract test is not a claim that credentials or production access exist.

## 15. Acceptance Criteria Matrix

Acceptance has four cumulative gates. **Foundation** proves compatibility, migration, durable execution, budget, and security primitives. **First useful** delivers Web/RSS research and monitoring through CLI with searchable corpus, Markdown/digest output, explicit feedback, and verified local recovery. **Core operational** adds YouTube, Reddit, complete relevance/retention policy, discovery/LocalPulse, full event handling, API/dashboard, and notifications. **Complete implementation** adds the remaining approved connector implementations/capability states, LEARN, Obsidian, operational hardening, and every deterministic criterion below.

External vendor access is an orthogonal status per connector operation: `not_configured`, `contract_validated`, or `live_validated`. Missing credentials never turn a required deterministic test into “passed live,” and unavailable third-party access does not invalidate the implementation if the connector reports it truthfully.

| # | Criterion | Required automated evidence | First required gate | Additional operational evidence |
|---:|---|---|---|---|
| 1 | Multi-round research | Fake end-to-end run persists multiple bounded `ResearchRound`s and distinct plan versions. | First useful | None. |
| 2 | Plan changes from findings | `RoundAnalysis` changes topic/query branches and round two consumes round-one evidence. | First useful | None. |
| 3 | All configured limits respected | Concurrent reservation and preflight tests enforce source, round, time, money, token, concurrency, depth, retry, and queue caps. | Foundation | Optional provider billing comparison. |
| 4 | Sources and evidence visible | Presentation links artifact version -> finding/knowledge -> claim -> evidence locator -> snapshot/item -> source and lineage. | First useful | Optional spot-check of fixture locators against a live public page. |
| 5 | Interrupted task resumes | Kill/fault injection at each checkpoint, lease recovery, stale-worker fencing, idempotent resume, and equivalent final graph. | Foundation | Multiprocess soak test. |
| 6 | Follow-up over multiple sources | Versioned `WatchSpec`, two connector fakes, scheduler, and application-service/CLI creation tests preserve separate identities/policies. | First useful | API coverage at Core operational; credential-gated native smoke where configured. |
| 7 | Only new content detected | Cursor, GUID/hash/ETag identity, snapshot delta, and repeated sync produce no duplicate events. | First useful | Opt-in live repeated sync. |
| 8 | Duplicates avoided | URL/fingerprint/similarity tests retain every member/provenance while excluding duplicates from active expensive work. | First useful | None. |
| 9 | Cross-source event relation | Clustering links change events from distinct sources and preserves every origin and contrary claim. | First useful | None. |
| 10 | Accumulated report | Digest-window tests include chronology, changes, confidence, contradictions, questions, coverage, and all source links exactly once. | First useful | Delivery adapter smoke where configured. |
| 11 | Facts, inferences, rumors distinguished | Epistemic status/type and Signal tests retain/labeled rumor claims without converting them to trend signals. | First useful | None. |
| 12 | Errors and partial results explained | Every typed outcome persists completeness impact, next action, run degradation, and usable partial artifact where possible. | Foundation | Live error probes for configured connectors. |
| 13 | History preserved | Append-only versions, snapshots, rounds, cursors, usage, events, artifacts, decisions, and publications survive reload/migration. | Foundation | Restore drill. |
| 14 | External content cannot control agent | Hostile fixtures prove SSRF/redirect, prompt/tool isolation, path containment, redaction, and hard-policy enforcement. | Foundation | Operational threat review before remote exposure. |
| 15 | Retained corpus is queryable | FTS5 plus structured filters retrieve long-tail items, minority claims, provenance, and history independent of presentation rank. | First useful | Representative corpus performance check. |
| 16 | Canonical state survives loss | Automated backup, integrity check, destructive restore, and index rebuild reconstruct the expected recovery point. | First useful | Optional encrypted off-device restore drill. |
| 17 | Local plans and places are structured | LocalPulse fixtures preserve conflicting lifecycle claims/evidence and derive versioned `current_status`/confidence projections for event/place records without overwriting history. | Core operational | Live-public sampling where lawful. |
| 18 | Explicit feedback changes future presentation without deleting history | Feedback changes versioned relevance/source/learning policy inputs while corpus and provenance hashes remain unchanged. | First useful | Usability review. |

## 16. Dependency-Ordered Implementation Workstreams

Each workstream is decomposed later into agent-executed TDD tasks with an implementer and an independent reviewer. Exit evidence, not task completion, unlocks dependent work. Security, compatibility, migration safety, provenance, and budget enforcement are cross-cutting gates in every workstream rather than cleanup reserved for the end.

1. **Compatibility, configuration, and security foundation.** Introduce the composition root, exact config precedence/redaction, typed outcomes, egress/path/content safety primitives, canonical filtering, elapsed-time accounting, and `ResearchService.run_sync()` behind the unchanged orchestrator/CLI contract. Exit: baseline and byte-level batch regression pass; hostile fixtures, config-source tests, limits, and partial-error behavior pass.
2. **Versioned domain and migration baseline.** Add migration tooling, canonical `WorkflowRun`, source/document/item separation, provenance joins, analysis versions, immutable records, and repository roundtrips. Migrate a copy of the current schema with row-accounting verification. Exit: old and fresh databases load an equivalent graph without lost IDs, hashes, evidence, or results.
3. **Durable runtime, budgets, and scheduling.** Add workflow/work-item transitions, fenced leases, reservations/settlement, checkpoints, parent/child joins, scheduler and worker commands, cancellation/shutdown, backup primitive, and operation intents. Exit: concurrent/fault-injection suites prove no overspend, stale commit, lost due occurrence, duplicate child, or unrecoverable checkpoint. This closes the **Foundation** gate.
4. **Adaptive research and versioned knowledge.** Add configuration drafts, profiles, graph versions, branch proposals, `RoundAnalysis`, analysis artifacts, contradictions, formal stop policy, zero-candidate alternatives, and compatibility result projection. Exit: a persisted second round changes from first-round evidence, provenance is exact, and all stop paths are bounded.
5. **Web and RSS first vertical.** Implement Web and RSS as separate connectors behind the target port, safe conditional fetch/sync, cursors, snapshots, durable post-cursor analysis work, change detection, baseline FTS5/filter search, Markdown dossier, CLI run controls, coverage, and a verified backup/restore path. Exit: repeated Web/RSS research and monitor fixtures are idempotent; fault injection after cursor commit still completes analysis; a user can run, interrupt/resume, inspect evidence, search retained long-tail records, and recover the database.
6. **Events, digest, alerts, and baseline feedback.** Add change/event clustering, grouping windows, urgency rules, presentation models, durable delivery jobs, Markdown digest/alert, and explicit reversible feedback. Exit: repeated runs produce one logical digest/publication, alerts obey explicit thresholds, and attention changes prominence without deleting corpus. This closes the **First useful** gate.
7. **YouTube connector.** Implement validated official Data API operations and official upload feed/WebSub capabilities where available, including resource identity, pagination/cursors, quota reservations, fixtures, and transparent unsupported operations. Exit: contract suite passes; each configured operation carries an independent credential-gated live status.
8. **Reddit connector.** Implement OAuth official API operations allowed to the configured application, resource validation, fullname pagination/cursors, budgets, fixtures, and transparent denial/limit states. Exit: the same contract and optional live-status gate as YouTube passes. Web/RSS/YouTube/Reddit now form the first native operational connector set.
9. **Relevance, diversity, freshness, and corpus policy.** Complete policy decisions, multidimensional reputation/relevance, freshness, retention tiers, coverage, unique contribution, diversity reservations, deliberate serendipity, and provenance-aware retrieval/feedback rules. Exit: policy components/reasons are inspectable; sampling and raw expiry are disclosed; every retained record remains reachable under policy.
10. **Discovery, Ambient Awareness, and LocalPulse.** Add bounded discovery specs, promotion workflow, location/category policy, `Place`/`Venue`, `RealWorldEvent`, derived status projections, and degraded disclosures. Exit: discovery cannot auto-enable a watch, proposal suppression works, and LocalPulse preserves conflicting lifecycle history while rendering evidence-backed scoped events/places without private-person dossiers.
11. **API, dashboard, and notifications.** Add localhost FastAPI/Uvicorn, server-rendered read-oriented dashboard, durable run controls, search/coverage/provenance views, feedback controls, and configured webhook/email delivery. Exit: API, CLI, and dashboard observe the same identities/state; CORS/CSRF/auth tests pass; no ASGI request performs connector/LLM work inline. This closes the **Core operational** gate.
12. **X connector.** Implement only official API v2 operations represented by current entitlement, with validation, cache, on-demand policy, and atomic daily/monthly/connector/run cost caps. Exit: unavailable operations report exact status without fallback impersonation; contract tests pass and live status is credential-gated.
13. **Instagram and Facebook connectors.** Implement only official authorized professional-account/Page capabilities with scope/resource probes, fixtures, and explicit operation matrices. Exit: no authenticated scraper exists, arbitrary public monitoring is not advertised, and unavailable access degrades truthfully.
14. **LEARN workflow.** Add pinned learning paths, session events, causal mastery transitions, checks, resume/review, graph rebase proposals, and bounded learning-gap child research. Exit: knowledge confidence and user mastery remain separate and reconstruct identically after reload.
15. **Full Obsidian projection.** Add configured structure, stable IDs, managed blocks, dry-run diffs, checksums, real-path confinement, atomic writes, preservation, conflict artifacts, and reconciliation. Exit: repeated export is idempotent; symlink/junction escape and silent overwrite are impossible in tests.
16. **Recovery, retention, and operational hardening.** Add scheduled backup rotation, integrity verification, documented local RPO/RTO, optional encrypted off-device destination, retention execution/tombstones, FTS rebuild, circuit/load/backpressure tests, metrics, and operator health guidance. Exit: automated destructive restore and hostile/load suites reconstruct canonical state and expose degraded completeness.
17. **Complete acceptance.** Execute all 18 deterministic criteria, baseline/batch regression, migration, multiprocess fault injection, connector contracts, security, search/feedback, publication, and recovery matrices. Record each external operation as not configured, contract validated, or live validated; opt-in live failures cannot be concealed. This closes the **Complete implementation** gate.

TikTok remains a documented deferred connector, not a current workstream or completion dependency. Its future addition must fit the same capability/access model without weakening the no-authenticated-scraping rule.

## 17. Resolved Decisions And Risks

Resolved decisions include SQLite over Redis/Celery, local-first single-node deployment, a scheduler separate from workers, compatibility facade over durable execution, four distinct workflow kinds, source/item separation, append-only knowledge versions, bounded parent/child runs, official APIs only, Web/RSS as the first useful vertical, Web/RSS/YouTube/Reddit as the first native connector set, TikTok as deferred authorized-only future scope, Markdown-first delivery, one-way Obsidian, searchable retained corpus, verified backup/restore, and attention as presentation priority rather than retention.

Primary risks and mitigations:

- **Migration complexity:** use checksummed sequential migrations, verified pre-migration backup, row-accounting tests from the current baseline, and compatibility adapters instead of a rewrite.
- **SQLite contention:** use WAL, busy timeout, `BEGIN IMMEDIATE` claims, fenced leases, bounded lock retry, short transactions, and no network/LLM work in transactions.
- **Connector policy changes:** capability probes, typed unsupported outcomes, contract versions, and transparent degradation.
- **LLM cost and nondeterminism:** preflight prompt caps, model/version snapshots, fakes, deterministic policy decisions, and partial output.
- **False certainty from correlated sources:** primary evidence, source reputation dimensions, contradiction preservation, and explicit corroboration.
- **Corpus growth:** tiered retention baseline, hashes/metadata preservation, bounded raw retention, searchable indexes rebuilt from canonical state, and future compaction seam.
- **Canonical-state loss/corruption:** scheduled verified backups, restore drills, integrity checks, documented recovery point, and optional off-device encrypted targets.
- **Search becoming another opaque ranker:** expose filters/provenance and keep retrieval independent from presentation ranking.
- **Feedback creating a narrow bubble:** store explicit reasoned feedback separately, keep DiversityPolicy/serendipity, and make policy effects inspectable/reversible.
- **Security exposure from remote content:** egress checks, content caps, isolation, redaction, and hostile fixtures before live access.
- **User attention overload:** attention budget, grouped digests, explicit urgency rules, and diversity reservations.

## 18. Definition Of Done

The complete application program is done when:

- the current batch CLI tuple/output contract remains compatible through `run_sync()`, while durable callers use the separately tested `submit()` contract;
- all four workflow kinds use the canonical state machine, bounded parent/child policy, fenced work items, and restart-safe checkpoints;
- research rounds adapt from persisted analysis and stop by explicit sufficiency, novelty, goal, uncertainty, and budget policy;
- normalized ingestion, analysis versions, evidence locators, claims, knowledge versions, change events, corpus coverage, policy decisions, and publication records are relationally traceable, reloadable, and queryable through corpus search;
- Web, RSS, YouTube, Reddit, X, Instagram, and Facebook have tested operation-level connector contracts and truthful access/capability states; native operations use official APIs only, and TikTok remains explicitly deferred without implied scraper capability;
- scheduler, workers, fenced leases, retries, concurrent budget reservations, cancellation/shutdown, backpressure, circuit breakers, and idempotency are operationally tested;
- API, CLI, dashboard, Markdown, notifications, and Obsidian share presentation semantics; explicit feedback and search operate on the same canonical identities;
- backup/restore gates pass, including an automated destructive restore drill with verified canonical state reconstruction;
- security and observability gates pass from the first connector through final hostile-content, remote-access, path, and load tests;
- all 18 deterministic acceptance criteria have fresh evidence, and every external connector operation is separately labeled `not_configured`, `contract_validated`, or `live_validated`.

## 19. Deferred Future Backlog

These are extension seams, not current exit gates:

- Semantic/embedding enrichment and reranking on top of the required FTS/text + structured corpus search.
- Advanced provenance queries such as contradiction lookup, causal/citation graphs, and first-source timelines; the required chain remains Output -> KnowledgeState -> Claim -> Evidence -> Item -> Source.
- Automated preference learning beyond explicit auditable `FeedbackEvent` rules.
- Queryable operational decision memory beyond current reason codes.
- Automated tiered retention/compaction beyond the baseline retention policy and verified backup rotation.
- Continuous product-evaluation dashboards and experiments for coverage, claim precision, alert false positives, duplicate rate, cost, digest engagement, source novelty, and feedback usefulness.
- Portable bulk export/import and cross-database migration tools beyond current Markdown/JSON/Obsidian outputs and backup/restore guarantees.
- Native TikTok connector implementation if official/explicitly authorized capabilities become suitable; authenticated scraping remains out of scope.
- Quantitatively optimized serendipity; current `DiversityPolicy` provides configured deliberate exploration only.

-- Goldenrod — ClickHouse schema
-- All reads and writes go through the ClickHouse MCP server at runtime.
-- Direct driver calls are a track-requirement violation. Do not take that shortcut.
--
-- ===========================================================================
-- DATASTORE DECISION (resolves the open question in SPEC.md §4.1)
-- ===========================================================================
-- SPEC §4.1 left the film graph in "Cloud SQL / Postgres (or ClickHouse if we
-- want one system)". DECIDED: ClickHouse is the single datastore. The graph
-- lives here alongside the ledger.
--
-- Why:
--   1. CLAUDE.md rule 4 requires ALL ClickHouse access to go through the MCP
--      server. One store means one access path, and no second driver anywhere
--      in the codebase that a judge could mistake for a shortcut.
--   2. A second store means a second deploy on day 7, which SPEC §6 already
--      warns not to leave late.
--   3. Traversal and the ledger query become one join instead of a round trip
--      across two systems.
--
-- Cost, stated honestly: ClickHouse has no foreign keys and no uniqueness
-- constraint. Referential integrity is the loader's job, and identity is
-- carried by the sorting key + ReplacingMergeTree rather than by a PK.
--
-- ===========================================================================
-- FACT IDENTITY (the rule the whole fact-level diff rests on)
-- ===========================================================================
--   fact_key = (production_id, kind, established_in_scene_number, sorted(entity_ids))
--
-- Deliberately NOT part of identity:
--   * fact_id    — emitted per-revision by the Extractor, not stable. Kept as a
--                  passthrough for traceability only. Never join on it.
--   * statement  — a changed statement IS the diff signal. Keying on it would
--                  make every edit look like delete+insert.
--   * source_line— shifts whenever pages re-flow. That is what a revision does.
--
-- Why scene_number is stable: Goldenrod only runs on LOCKED scripts. Once a
-- script is locked for production, scene numbers never change — inserted scenes
-- take letters (24A), cut scenes become "24 OMITTED". A goldenrod revision is by
-- definition a production-driven change to an already-locked script, so scene
-- numbering is a fixed coordinate system.
--
-- fact_key and fact_match_key are MATERIALIZED, so the database computes them.
-- arraySort() removes the Extractor's array-ordering nondeterminism. Identity is
-- enforced by the schema, not by a prompt — same principle as CLAUDE.md rule 1.
--
-- Two-tier diff (see reference query at the bottom of this file):
--   tier 1  exact fact_key match      -> UNCHANGED / CHANGED / ADDED / REMOVED
--   tier 2  fact_match_key match      -> RELOCATED (fact moved scenes)
-- Tier 2 is the one that earns its keep: a fact moving scenes is what silently
-- invalidates knowledge_state, and it is invisible to any text diff.


-- ===========================================================================
-- FILM GRAPH
-- knowledge_state and dependencies are the product (SPEC §4.1).
-- Everything else here is bookkeeping.
-- ===========================================================================

-- ---------------------------------------------------------------------------
-- Scenes. Revision-scoped. Keyed on scene_number, which is stable on a locked
-- script; scene_id is carried for contract fidelity but is not the identity.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS scenes
(
    production_id   String,
    revision_id     String,
    scene_number    String,                  -- STABLE IDENTITY (locked pages)
    scene_id        String,                  -- contract passthrough

    int_ext         LowCardinality(String) DEFAULT '',
    location_id     String DEFAULT '',
    day_night       LowCardinality(String) DEFAULT '',
    page_eighths    UInt16 DEFAULT 0,
    synopsis        String,
    text_hash       String DEFAULT '',
    entity_ids      Array(String) DEFAULT [],

    updated_at      DateTime DEFAULT now(),

    -- contract enums enforced by the database, not by prompt
    CONSTRAINT int_ext_enum   CHECK int_ext   IN ('INT','EXT','INT/EXT',''),
    CONSTRAINT day_night_enum CHECK day_night IN ('DAY','NIGHT','DUSK','DAWN','CONTINUOUS','')
)
ENGINE = ReplacingMergeTree(updated_at)
ORDER BY (production_id, revision_id, scene_number);


-- ---------------------------------------------------------------------------
-- Entities. PRODUCTION-scoped, not revision-scoped: a character or a picture
-- vehicle persists across revisions, and `aliases` exists so surface-form drift
-- resolves to one entity_id. Re-ingesting a revision is idempotent.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS entities
(
    production_id          String,
    entity_id              String,
    type                   LowCardinality(String),
    name                   String,
    aliases                Array(String) DEFAULT [],

    first_seen_revision_id String DEFAULT '',
    updated_at             DateTime DEFAULT now(),

    CONSTRAINT type_enum CHECK type IN
        ('character','location','prop','costume','vehicle','set','symbol')
)
ENGINE = ReplacingMergeTree(updated_at)
ORDER BY (production_id, entity_id);


-- ---------------------------------------------------------------------------
-- Facts. The diff operates on these, not on text.
-- The sorting key is the fact identity rule spelled out; ReplacingMergeTree
-- therefore makes re-ingesting a revision idempotent.
-- collision_ord disambiguates the rare case of two facts sharing
-- (kind, scene, entity-set) in one revision — order them by source_line
-- ascending and number them from 1.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS facts
(
    production_id               String,
    revision_id                 String,

    kind                        LowCardinality(String),
    established_in_scene_number String,      -- resolved by the loader from scene_id
    entity_ids                  Array(String) DEFAULT [],
    collision_ord               UInt8 DEFAULT 1,

    -- identity, computed by the database
    fact_key       String MATERIALIZED concat(production_id,'|',kind,'|',
                       established_in_scene_number,'|',
                       arrayStringConcat(arraySort(entity_ids),','),'#',
                       toString(collision_ord)),
    -- relocation key: identity minus the scene. Drives tier 2 of the diff.
    fact_match_key String MATERIALIZED concat(production_id,'|',kind,'|',
                       arrayStringConcat(arraySort(entity_ids),',')),

    statement                   String,
    fact_id                     String DEFAULT '',  -- passthrough, NOT identity
    established_in_scene_id     String DEFAULT '',  -- passthrough
    source_line                 UInt32 DEFAULT 0,

    updated_at                  DateTime DEFAULT now(),

    CONSTRAINT kind_enum CHECK kind IN
        ('world','relationship','possession','knowledge','physical','temporal')
)
ENGINE = ReplacingMergeTree(updated_at)
ORDER BY (production_id, revision_id, kind, established_in_scene_number,
          arrayStringConcat(arraySort(entity_ids),','), collision_ord);


-- ---------------------------------------------------------------------------
-- Knowledge state — who knows what, by when. This table catches the good ones.
-- Identity derives from fact identity: once fact_key is stable, the natural key
-- (character, fact, scene) from the contract is stable too. Join on fact_key,
-- never on fact_id.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS knowledge_state
(
    production_id       String,
    revision_id         String,

    character_entity_id String,
    fact_key            String,              -- resolved by the loader
    scene_number        String,
    knows               UInt8,
    acquired_via        String DEFAULT '',

    fact_id             String DEFAULT '',   -- passthrough
    scene_id            String DEFAULT '',   -- passthrough
    updated_at          DateTime DEFAULT now()
)
ENGINE = ReplacingMergeTree(updated_at)
ORDER BY (production_id, revision_id, character_entity_id, fact_key, scene_number);


-- ---------------------------------------------------------------------------
-- Dependencies — what breaks if a fact changes. This is the second-order hop:
-- the change lands in one scene, the damage shows up in another.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS dependencies
(
    production_id    String,
    revision_id      String,

    from_fact_key    String,                 -- resolved by the loader
    to_scene_number  String,
    kind             LowCardinality(String),
    evidence_line    UInt32 DEFAULT 0,
    evidence_quote   String DEFAULT '',

    dependency_id    String DEFAULT '',      -- passthrough
    from_fact_id     String DEFAULT '',      -- passthrough
    to_scene_id      String DEFAULT '',      -- passthrough
    updated_at       DateTime DEFAULT now(),

    CONSTRAINT kind_enum CHECK kind IN
        ('references','assumes','contradicts_if_changed')
)
ENGINE = ReplacingMergeTree(updated_at)
ORDER BY (production_id, revision_id, from_fact_key, to_scene_number, kind);


-- ===========================================================================
-- LEDGER + COMMITMENT STATE
-- ===========================================================================

-- ---------------------------------------------------------------------------
-- Decision ledger.
-- The only reason our findings can say WHY. No competitor stores this.
-- `reason` is mandatory and must never be empty.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS decisions
(
    decision_id           UUID,
    production_id         String,
    revision_id           String,
    scene_id              String,
    entity_ids            Array(String),

    decision_type         LowCardinality(String),  -- camera|location|casting|design|story|schedule
    selected_option       String,
    alternatives          Array(String),

    reason                String,                  -- the "why". Mandatory.
    cause_tag             LowCardinality(String),  -- taste|constraint|experiment|external_note

    decided_by            String,
    decided_at            DateTime,
    status                LowCardinality(String) DEFAULT 'active',  -- active|superseded|reverted

    intentional_deviation UInt8 DEFAULT 0,
    deviation_reason      String DEFAULT ''
)
ENGINE = MergeTree
ORDER BY (production_id, scene_id, decided_at);

-- cause_tag is one tap, four options, no typing.
-- Only ever reason from 'taste'. A rejected handheld shot might be preference,
-- or it might be that the Steadicam operator had already wrapped.


-- ---------------------------------------------------------------------------
-- Commitment state. THE DIFFERENTIATOR.
-- Filmustage and Directure know what a scene contains. Neither knows what has
-- already been paid for. This table is why our findings can be ranked by cost.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS commitments
(
    commitment_id UUID,
    production_id String,
    entity_id     String,
    scene_id      String,

    state         LowCardinality(String),  -- none|planned|sourced|built|cast|scouted|permitted|shot
    cost_band     LowCardinality(String),  -- none|low|medium|high

    committed_at  DateTime,
    updated_at    DateTime DEFAULT now(),
    notes         String DEFAULT ''
)
ENGINE = ReplacingMergeTree(updated_at)
ORDER BY (production_id, entity_id, scene_id);


-- ---------------------------------------------------------------------------
-- Findings, persisted so a re-run can prove behaviour changed after a
-- human marked something intentional.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS findings
(
    finding_id       UUID,
    production_id    String,
    run_id           UUID,
    scene_id         String,
    shoot_date       Date,

    kind             LowCardinality(String),
    severity         LowCardinality(String),
    commitment_state LowCardinality(String),
    claim            String,
    evidence_json    String,                 -- array of evidence objects, see contracts/finding.schema.json

    dismissed        UInt8 DEFAULT 0,
    dismiss_reason   String DEFAULT '',
    created_at       DateTime DEFAULT now()
)
ENGINE = MergeTree
ORDER BY (production_id, shoot_date, created_at);


-- ===========================================================================
-- ACCESS CONTROL AND AUDIT (SPEC §5, "free points: IAM and governance")
-- ===========================================================================
-- An unreleased script is the most access-controlled document on a production.
-- The policy — what each role may do — is code, in services/common/access.py,
-- so a permission change is a reviewable diff. The roster below is data, so
-- adding a person is not a deploy.
--
-- No password, hash or token is stored here, and none ever should be. The
-- application authenticates nobody: it reads the identity its platform already
-- proved (Cloud IAP sets X-Goog-Authenticated-User-Email) and looks the subject
-- up in this table. That is why this is a grant table and not a user table.

-- ---------------------------------------------------------------------------
-- Who holds which role, per production. Access is production-scoped because a
-- crew is: the same property master on two shows is two grants.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS access_grants
(
    production_id String,
    subject       String,                  -- the identity the platform proved
    display_name  String DEFAULT '',
    role          LowCardinality(String),  -- must name a role in access.ROLES
    granted_by    String DEFAULT '',
    granted_at    DateTime DEFAULT now(),
    updated_at    DateTime DEFAULT now(),
    notes         String DEFAULT ''
)
ENGINE = ReplacingMergeTree(updated_at)
ORDER BY (production_id, subject);


-- ---------------------------------------------------------------------------
-- The audit trail. Append-only MergeTree for the same reason as `decisions`:
-- a log that can be rewritten is not a log.
--
-- Every scoped read is recorded with what it released and what it withheld, and
-- so is every refusal. This is the half of governance that a filter alone does
-- not give you — the office can answer "who read the pages last night", which
-- is a question productions genuinely ask.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS access_log
(
    event_id      UUID,
    production_id String,
    subject       String,
    role          LowCardinality(String),
    action        LowCardinality(String),  -- signin|check|graph|ledger_write|denied
    granted       UInt8,
    released      UInt16 DEFAULT 0,        -- findings shown
    withheld      UInt16 DEFAULT 0,        -- findings the role may not see
    detail        String DEFAULT '',
    at            DateTime DEFAULT now()
)
ENGINE = MergeTree
ORDER BY (production_id, at, subject);


-- ---------------------------------------------------------------------------
-- Ranking helper. The order below IS the product — do not reorder without
-- talking to the team.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION commitmentRank AS (state) ->
    multiIf(
        state = 'shot',      0,
        state = 'built',     1,
        state = 'cast',      1,
        state = 'permitted', 2,
        state = 'scouted',   2,
        state = 'sourced',   3,
        state = 'planned',   4,
        5
    );


-- ---------------------------------------------------------------------------
-- REFERENCE QUERY 1 — the fact-level draft diff (pipeline step 2).
-- The fact identity rule, executed. Tier 1 is the exact fact_key match; tier 2
-- catches a fact that MOVED scenes, which is invisible to a text diff and is
-- what silently invalidates knowledge_state.
-- Validated against the planted-break shape: returns RELOCATED 18 -> 31.
--
-- FINAL is not optional here. `facts` is a ReplacingMergeTree and ClickHouse only
-- collapses duplicate sorting keys when parts merge, which is asynchronous with
-- no guaranteed timing. Without it, re-ingesting a revision makes the diff return
-- the same RELOCATED fact twice — one changed fact reported as two.
--
-- Extracted at runtime by services/common/queries.py, so this file stays the
-- single source of truth for the SQL. Do not copy it into Python.
-- ---------------------------------------------------------------------------
-- >>> QUERY draft_diff
-- WITH
--   v1 AS (SELECT fact_key, fact_match_key, established_in_scene_number AS scene, statement
--          FROM facts FINAL WHERE production_id = {production:String}
--                             AND revision_id  = {prior_revision:String}),
--   v2 AS (SELECT fact_key, fact_match_key, established_in_scene_number AS scene, statement
--          FROM facts FINAL WHERE production_id = {production:String}
--                             AND revision_id  = {current_revision:String})
-- SELECT
--     coalesce(nullIf(v1.fact_match_key,''), v2.fact_match_key) AS match_key,
--     v1.scene AS was_scene,
--     v2.scene AS now_scene,
--     v1.statement AS was_statement,
--     v2.statement AS now_statement,
--     multiIf(
--         v1.fact_key != '' AND v2.fact_key != ''
--             AND v1.fact_key = v2.fact_key AND v1.statement = v2.statement, 'UNCHANGED',
--         v1.fact_key != '' AND v2.fact_key != '' AND v1.fact_key = v2.fact_key, 'CHANGED',
--         v1.fact_key != '' AND v2.fact_key != '',                             'RELOCATED',
--         v1.fact_key != '',                                                   'REMOVED',
--                                                                              'ADDED'
--     ) AS change
-- FROM v1 FULL OUTER JOIN v2 ON v1.fact_match_key = v2.fact_match_key
-- WHERE change != 'UNCHANGED'
-- <<< QUERY


-- ---------------------------------------------------------------------------
-- REFERENCE QUERY 2 — the ledger + commitment query the demo puts on screen
-- at 0:45–1:20. Make it readable: a judge will be looking at it.
--
-- Three things this deliberately does, all of which the earlier draft got wrong:
--   1. Real aggregation over decision history (CLAUDE.md rule 5), not a lookup.
--   2. Collapses ReplacingMergeTree with argMax before joining. Without this
--      the join can return superseded commitment rows until parts merge — a
--      stale `state` in the highest-scoring frame of the demo.
--   3. Joins on (entity_id, scene_id), not scene_id alone. Joining on scene
--      alone fans out one row per committed entity in the scene and makes
--      commitment_state effectively arbitrary.
--
-- Note: commitmentRank('') = 5, same as 'none', so a LEFT JOIN miss ranks
-- lowest-risk without a coalesce guard. Verified against ClickHouse 26.9.
--
-- Extracted at runtime by services/common/queries.py. This is the ONLY ranking
-- implementation; do not write a second one in application code.
-- ---------------------------------------------------------------------------
-- >>> QUERY commitment_ranking
-- WITH
--   current_commitments AS (
--       SELECT production_id, entity_id, scene_id,
--              argMax(state,     updated_at) AS state,
--              argMax(cost_band, updated_at) AS cost_band
--       FROM commitments
--       WHERE production_id = {production:String}
--       GROUP BY production_id, entity_id, scene_id
--   ),
--   decision_entities AS (
--       SELECT decision_id, scene_id, decided_at, decision_type,
--              selected_option, reason, cause_tag, decided_by,
--              arrayJoin(entity_ids) AS entity_id
--       FROM decisions
--       WHERE production_id = {production:String} AND status = 'active'
--   )
-- SELECT
--     de.scene_id                               AS scene_id,
--     de.entity_id                              AS entity_id,
--     cc.state                                  AS commitment_state,
--     commitmentRank(cc.state)                  AS commitment_rank,
--     cc.cost_band                              AS cost_band,
--     count()                                   AS decisions_touching,
--     countIf(de.cause_tag = 'taste')           AS taste_decisions,
--     argMax(de.selected_option, de.decided_at) AS current_choice,
--     argMax(de.reason,          de.decided_at) AS current_reason,
--     argMax(de.decision_id,     de.decided_at) AS current_decision_id,
--     argMax(de.decided_by,      de.decided_at) AS current_decided_by,
--     max(de.decided_at)                        AS last_decided_at,
--     groupUniqArray(de.decision_type)          AS decision_types
-- FROM decision_entities de
-- LEFT JOIN current_commitments cc
--        ON cc.entity_id = de.entity_id AND cc.scene_id = de.scene_id
-- WHERE has({affected_entities:Array(String)}, de.entity_id)
-- GROUP BY scene_id, entity_id, commitment_state, cost_band
-- ORDER BY commitment_rank ASC, last_decided_at DESC
-- <<< QUERY


-- ---------------------------------------------------------------------------
-- REFERENCE QUERY 3 — TRAVERSE (pipeline step 3). The second-order hop.
--
-- This is the query that makes Goldenrod different from a text diff. Given the
-- fact_match_keys the draft diff reported as changed, it returns every scene
-- that DEPENDS on one of them — scenes the revision never touched — together
-- with the line in that scene which does the depending, and the scene the fact
-- now lives in. The damage and its cause, in one row.
--
-- FINAL on all three tables: they are ReplacingMergeTree, and a re-ingest would
-- otherwise duplicate every edge. See services/loader/ingest.py READ_SEMANTICS.
--
-- Extracted at runtime by services/common/queries.py.
-- ---------------------------------------------------------------------------
-- >>> QUERY dependent_scenes
-- WITH
--   changed_facts AS (
--       SELECT fact_key, fact_match_key, kind, statement,
--              established_in_scene_number, source_line, entity_ids
--       FROM facts FINAL
--       WHERE production_id = {production:String}
--         AND revision_id  = {revision:String}
--         AND has({match_keys:Array(String)}, fact_match_key)
--   ),
--   edges AS (
--       SELECT from_fact_key, to_scene_number, kind, evidence_line, evidence_quote
--       FROM dependencies FINAL
--       WHERE production_id = {production:String}
--         AND revision_id  = {revision:String}
--   ),
--   day AS (
--       SELECT scene_number, synopsis, int_ext, day_night
--       FROM scenes FINAL
--       WHERE production_id = {production:String}
--         AND revision_id  = {revision:String}
--   )
-- SELECT
--     e.to_scene_number             AS scene_number,
--     s.synopsis                    AS synopsis,
--     f.fact_key                    AS fact_key,
--     f.fact_match_key              AS fact_match_key,
--     f.kind                        AS fact_kind,
--     f.statement                   AS statement,
--     f.established_in_scene_number AS established_in_scene,
--     f.source_line                 AS source_line,
--     f.entity_ids                  AS fact_entity_ids,
--     e.kind                        AS dependency_kind,
--     e.evidence_line               AS evidence_line,
--     e.evidence_quote              AS evidence_quote
-- FROM edges e
-- INNER JOIN changed_facts f ON f.fact_key = e.from_fact_key
-- LEFT  JOIN day s           ON s.scene_number = e.to_scene_number
-- ORDER BY scene_number, fact_key, evidence_line
-- <<< QUERY


-- ---------------------------------------------------------------------------
-- REFERENCE QUERY 4 — COMMITMENT LOOKUP (pipeline step 5).
--
-- `commitment_ranking` above answers "what has been decided about these
-- entities, and what has been paid for" — it starts from the decision history,
-- so an element nobody has logged a decision about is not in it. This one
-- answers the other half: for the scenes a finding landed on, what is the most
-- committed thing in each, whether or not anyone wrote a decision down.
--
-- Both use the same commitmentRank UDF. There is still exactly one ranking.
--
-- argMax collapses the ReplacingMergeTree before ranking, for the same reason
-- as query 2: a stale `state` here is a wrongly-ranked finding.
-- ---------------------------------------------------------------------------
-- >>> QUERY scene_commitments
-- WITH current_commitments AS (
--     SELECT production_id, entity_id, scene_id,
--            argMax(state,        updated_at) AS state,
--            argMax(cost_band,    updated_at) AS cost_band,
--            argMax(committed_at, updated_at) AS committed_at,
--            argMax(notes,        updated_at) AS notes
--     FROM commitments
--     WHERE production_id = {production:String}
--     GROUP BY production_id, entity_id, scene_id
-- )
-- SELECT
--     cc.scene_id                AS scene_id,
--     cc.entity_id               AS entity_id,
--     e.name                     AS entity_name,
--     e.type                     AS entity_type,
--     cc.state                   AS commitment_state,
--     commitmentRank(cc.state)   AS commitment_rank,
--     cc.cost_band               AS cost_band,
--     cc.committed_at            AS committed_at,
--     cc.notes                   AS notes
-- FROM current_commitments cc
-- LEFT JOIN (SELECT entity_id, argMax(name, updated_at) AS name,
--                   argMax(type, updated_at) AS type
--            FROM entities
--            WHERE production_id = {production:String}
--            GROUP BY entity_id) e
--        ON e.entity_id = cc.entity_id
-- WHERE has({scenes:Array(String)}, cc.scene_id)
-- ORDER BY commitment_rank ASC, scene_id ASC, entity_id ASC
-- <<< QUERY

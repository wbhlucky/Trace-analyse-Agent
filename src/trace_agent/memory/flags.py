from __future__ import annotations

import getpass
import os

MEMORY_ENABLED_ENV = "TRACE_AGENT_MEMORY"
MEMORY_ENABLED_DEFAULT = True

MEMORY_ROOT_ENV = "TRACE_AGENT_MEMORY_ROOT"

# Unverified single-run guesses stay out of the prompt unless explicitly asked
# for. Injecting them by default is what let a wrong trial-1 answer become a
# prior for trial-2.
MEMORY_INCLUDE_CANDIDATE_ENV = "TRACE_AGENT_MEMORY_INCLUDE_CANDIDATE"
MEMORY_INCLUDE_CANDIDATE_DEFAULT = False

# Quality gate thresholds for ``candidate -> active``.
MEMORY_GATE_RUNS_ENV = "TRACE_AGENT_MEMORY_GATE_RUNS"
MEMORY_GATE_RUNS_DEFAULT = 2
MEMORY_GATE_CONFIDENCE_ENV = "TRACE_AGENT_MEMORY_GATE_CONFIDENCE"
MEMORY_GATE_CONFIDENCE_DEFAULT = 0.6

# Independent runs required before ``active -> verified`` without a human
# sign-off. Section 5.2 sets this higher than the ``candidate -> active``
# threshold on purpose: ``active`` means "corroborated", ``verified`` means
# "safe to reorder an investigation around", and the second claim needs more
# evidence than the first.
MEMORY_GATE_VERIFIED_RUNS_ENV = "TRACE_AGENT_MEMORY_GATE_VERIFIED_RUNS"
MEMORY_GATE_VERIFIED_RUNS_DEFAULT = 5

# Set by the eval harness for the duration of one eval run. Episodes projected
# while it is set carry the id, which is what lets a later trial's recall be
# audited for memories minted inside the same experiment (section 8.4).
EVAL_RUN_ID_ENV = "TRACE_AGENT_EVAL_RUN_ID"

# Freshness decay for reranking. A memory older than one half-life contributes
# half the recency signal of a brand-new one, so stale priors drift down the
# ranking instead of squatting on top-k forever.
MEMORY_HALF_LIFE_DAYS_ENV = "TRACE_AGENT_MEMORY_HALF_LIFE_DAYS"
MEMORY_HALF_LIFE_DAYS_DEFAULT = 90.0

# Minimum relevance a hit must reach before trust/confidence/freshness are
# allowed to contribute. Without this floor, an unrelated query would still
# return hits purely because a memory is confident and recent.
MEMORY_MIN_SCORE_ENV = "TRACE_AGENT_MEMORY_MIN_SCORE"
MEMORY_MIN_SCORE_DEFAULT = 0.15

# Storage backend for the long-term corpus. ``sqlite`` is the default from P1
# on: it gives indexed retrieval and the final table shape without adding a
# dependency. ``json`` keeps the pre-P1 file backend and exists so an operator
# can roll back with one environment variable.
MEMORY_STORE_ENV = "TRACE_AGENT_MEMORY_STORE"
MEMORY_STORE_SQLITE = "sqlite"
MEMORY_STORE_JSON = "json"
# ``postgres`` is the section-3.1 target state: one ACID database holding the
# structured columns, the JSONB metadata and (with pgvector) the embeddings. It
# is a *known* kind rather than a typo that silently falls back to SQLite, but
# it is never the default - choosing it requires a DSN, and an unreachable
# server degrades to the local adapter with the reason recorded on the store.
MEMORY_STORE_POSTGRES = "postgres"
MEMORY_STORE_KINDS = frozenset(
    {MEMORY_STORE_SQLITE, MEMORY_STORE_JSON, MEMORY_STORE_POSTGRES}
)
MEMORY_STORE_DEFAULT = MEMORY_STORE_SQLITE

# Connection for store=postgres. Both the URI form (``postgresql://user@host/db``)
# and the keyword form (``host=... dbname=...``) are accepted, because operators
# paste whichever their platform issued. The DSN is only ever logged through
# :func:`~trace_agent.memory.store.postgres_store.safe_dsn`, which strips the
# password: a store that cannot connect has to be diagnosable from a report, and
# the report must not be where the credential leaks.
MEMORY_DSN_ENV = "TRACE_AGENT_MEMORY_DSN"

# Schema the corpus lives in. ``public`` needs no privilege to create tables in;
# a dedicated schema is how one database hosts several environments.
MEMORY_SCHEMA_ENV = "TRACE_AGENT_MEMORY_SCHEMA"
MEMORY_SCHEMA_DEFAULT = "public"

# Bound on a connection attempt. Recall sits inside an analysis run, so a
# PostgreSQL server that is down must cost memory, not the run.
MEMORY_CONNECT_TIMEOUT_ENV = "TRACE_AGENT_MEMORY_CONNECT_TIMEOUT"
MEMORY_CONNECT_TIMEOUT_DEFAULT = 5.0

# Isolation domain stamped on every record a store writes and enforced on every
# read. Retrieval never crosses a namespace, which is what keeps an eval run
# from recalling the production corpus (and vice versa).
MEMORY_NAMESPACE_ENV = "TRACE_AGENT_MEMORY_NAMESPACE"
MEMORY_NAMESPACE_DEFAULT = "project:default"

# ------------------------------------------------------------------ ownership
# ``namespace`` answers "which corpus"; these answer "whose record is this".
# Section 8's isolation keeps an eval run out of the production corpus, but
# inside one namespace a session still belongs to an agent instance and a user,
# and an episode still belongs to a project. Stamping them at write time is what
# makes "show me this project's sessions" a filter instead of a scan, and it is
# why the columns exist in section 4's ``sessions``/``episodes`` DDL.
#
# Every one of them is optional and derived rather than required: a CLI run on a
# laptop has no user directory to consult, and refusing to write a session
# because one was missing would turn bookkeeping into an outage.
MEMORY_SESSION_ENV = "TRACE_AGENT_MEMORY_SESSION"
MEMORY_PROJECT_ENV = "TRACE_AGENT_MEMORY_PROJECT"
MEMORY_AGENT_ENV = "TRACE_AGENT_MEMORY_AGENT"
MEMORY_AGENT_DEFAULT = "diting-agent"
MEMORY_USER_ENV = "TRACE_AGENT_MEMORY_USER"

# Seconds between dreaming beats. Read here rather than at the one call site in
# ``web/server.py`` so the dashboard loop and any future scheduler agree on one
# knob - and so a typo degrades to the documented default instead of raising
# inside a daemon thread nobody is watching.
MEMORY_DREAM_INTERVAL_ENV = "TRACE_AGENT_MEMORY_DREAM_INTERVAL"
MEMORY_DREAM_INTERVAL_DEFAULT = 30.0

# Size of the candidate pool an indexed store hands to the reranker. The pool is
# ordered by the store's own lexical rank (bm25), which correlates with but is
# not identical to the reranker's coverage-based score, so it is deliberately
# much larger than ``top_k``: too small and a good match outside the pool can
# never be scored, too large and recall degrades towards a scan.
MEMORY_CANDIDATE_LIMIT_ENV = "TRACE_AGENT_MEMORY_CANDIDATE_LIMIT"
MEMORY_CANDIDATE_LIMIT_DEFAULT = 200

# How many top-k slots one functional type (semantic / procedural / episodic /
# summary) may claim in the first pass. Section 6.3's diversity rule: without it
# a corpus full of similar patterns can fill every slot with one kind of memory
# and a procedure that answers "how should I investigate this" never reaches the
# prompt. It is a *ordering* constraint, not a ceiling - leftover slots are
# filled by the best remaining hits, so the quota can never cost a recall a hit
# it would otherwise have returned. ``0`` turns the rule off.
MEMORY_TYPE_QUOTA_ENV = "TRACE_AGENT_MEMORY_TYPE_QUOTA"
MEMORY_TYPE_QUOTA_DEFAULT = 2

# ---------------------------------------------------------------- lifecycle
# Retention is the half of the lifecycle that keeps the corpus useful. A memory
# store that can only grow eventually drowns recall in priors nobody has
# believed in for a year, so stale records get an ``expires_at`` and are then
# archived (never deleted: section 7 keeps retired memory auditable).
MEMORY_RETENTION_DAYS_ENV = "TRACE_AGENT_MEMORY_RETENTION_DAYS"
MEMORY_RETENTION_DAYS_DEFAULT = 365.0

# Unverified single-run guesses are the most dangerous kind of memory, so they
# get a shorter leash than corroborated ones. A candidate nobody has
# re-confirmed within this window is archived instead of waiting forever for a
# second run that will never come.
MEMORY_CANDIDATE_TTL_DAYS_ENV = "TRACE_AGENT_MEMORY_CANDIDATE_TTL_DAYS"
MEMORY_CANDIDATE_TTL_DAYS_DEFAULT = 180.0

# Two records this similar inside one scope are the same memory written twice,
# so the loser is superseded and recall stops spending budget on it. Deliberately
# high: superseding hides a record from recall, and losing a real memory is
# worse than keeping a near-duplicate that ``_dedupe_hits`` already collapses.
MEMORY_DUPLICATE_SIMILARITY_ENV = "TRACE_AGENT_MEMORY_DUPLICATE_SIMILARITY"
MEMORY_DUPLICATE_SIMILARITY_DEFAULT = 0.92

# Below this similarity, two records that share a topic fingerprint but came
# from independent runs are treated as disagreeing about the same thing.
MEMORY_CONFLICT_SIMILARITY_ENV = "TRACE_AGENT_MEMORY_CONFLICT_SIMILARITY"
MEMORY_CONFLICT_SIMILARITY_DEFAULT = 0.60

# Below this *overlap coefficient*, two low-similarity records share so little
# substance that they are describing different mechanisms, which is the only kind
# of disagreement a lexical measure can actually see. Between the two floors sit
# paraphrases: an LLM writing the same conclusion twice in a corpus of long
# Chinese sentences measured 0.28 Jaccard / 0.47 overlap, and calling that a
# contradiction penalised the very records that corroborated each other. Two
# genuinely different mechanisms sharing a boilerplate opener ("白屏根因指向...")
# measured 0.18, which is why the floor sits above them and not at zero.
MEMORY_CONFLICT_OVERLAP_ENV = "TRACE_AGENT_MEMORY_CONFLICT_OVERLAP"
MEMORY_CONFLICT_OVERLAP_DEFAULT = 0.25

# Relatedness floor for the *blocking* step, i.e. for deciding which pairs are
# worth comparing at all. The sweep used to bucket records by an exact topic
# fingerprint, which is sound in a unit test and useless against a real corpus:
# an LLM restating a finding in a fresh run also restates its title, so all 53
# records of one scenario landed in 53 singleton buckets and the sweep compared
# nothing, leaving every memory a single-run ``candidate`` forever. Salient-token
# blocking admits the paraphrases, and this floor is what stops that from turning
# a shared symptom word into a claim that two runs disagree. Measured on the real
# corpus: every pair whose topics a human reads as the same conclusion scored
# >= 0.40, and the 103 pairs below 0.30 share a word and nothing else.
MEMORY_RELATED_OVERLAP_ENV = "TRACE_AGENT_MEMORY_RELATED_OVERLAP"
MEMORY_RELATED_OVERLAP_DEFAULT = 0.40

# A second, weaker floor on plain Jaccard, existing only for degenerate topics.
# The overlap coefficient divides by the smaller token set, so a two-token topic
# contained in a long one scores 1.0 while asserting almost nothing. Jaccard
# cannot be inflated that way, because the shared tokens also have to be a
# substantial part of the longer subject. Both floors must hold.
MEMORY_RELATED_SIMILARITY_ENV = "TRACE_AGENT_MEMORY_RELATED_SIMILARITY"
MEMORY_RELATED_SIMILARITY_DEFAULT = 0.18

# Dream ticks between full governance sweeps. The beat itself is short (30s by
# default) because queued jobs should drain promptly, but ``maintain()`` re-reads
# the corpus for conflicts, gate evidence and retention, so it rides along only
# every Nth tick. 20 ticks at the default interval is roughly ten minutes.
MEMORY_MAINTAIN_EVERY_ENV = "TRACE_AGENT_MEMORY_MAINTAIN_EVERY"
MEMORY_MAINTAIN_EVERY_DEFAULT = 20

# Whether a completed analysis drains governance itself. The dashboard beat and
# ``memory maintain`` both sweep the corpus, and a deployment that only ever runs
# the CLI has neither: every memory it wrote stayed a single-run ``candidate``
# forever, because nothing was left to link corroborating runs and re-gate them.
MEMORY_INLINE_GOVERNANCE_ENV = "TRACE_AGENT_MEMORY_INLINE_GOVERNANCE"
MEMORY_INLINE_GOVERNANCE_DEFAULT = True

# Total character budget for the recalled-memory prompt section. Owned here
# rather than in ``agent/core/prompts.py`` so the composer can size its three
# trust partitions against the same number the prompt enforces.
MEMORY_PROMPT_CHARS_ENV = "TRACE_AGENT_MEMORY_PROMPT_CHARS"
MEMORY_PROMPT_CHARS_DEFAULT = 3_000
MEMORY_PROMPT_CHARS_MAXIMUM = 20_000

# ------------------------------------------------------------------ semantics
# Which embedding provider backs hybrid recall (P3). ``local`` is the default
# because it is deterministic, offline and dependency-free, so semantic recall is
# a property of the product rather than of whether a key happens to be
# configured. ``http`` points at any OpenAI-compatible ``/embeddings`` endpoint;
# ``off`` restores pure lexical recall.
MEMORY_EMBEDDING_ENV = "TRACE_AGENT_MEMORY_EMBEDDING"
MEMORY_EMBEDDING_LOCAL = "local"
MEMORY_EMBEDDING_HTTP = "http"
MEMORY_EMBEDDING_OFF = "off"
MEMORY_EMBEDDING_DEFAULT = MEMORY_EMBEDDING_LOCAL
_EMBEDDING_OFF_ALIASES = frozenset(
    {"", "off", "none", "no", "0", "false", "disabled", "lexical"}
)
_EMBEDDING_HTTP_ALIASES = frozenset(
    {"http", "https", "openai", "remote", "url", "api", "deepseek", "ollama"}
)

# Dimensionality of the offline recipe. Stored vectors record the model name,
# which embeds this value, so changing it rebuilds the corpus instead of mixing
# widths.
MEMORY_EMBEDDING_DIM_ENV = "TRACE_AGENT_MEMORY_EMBEDDING_DIM"
MEMORY_EMBEDDING_DIM_DEFAULT = 256

# Remote provider wiring. The model name is the stored identity: two different
# models must not share a corpus.
MEMORY_EMBEDDING_MODEL_ENV = "TRACE_AGENT_MEMORY_EMBEDDING_MODEL"
MEMORY_EMBEDDING_MODEL_DEFAULT = "text-embedding-3-small"
MEMORY_EMBEDDING_BASE_URL_ENV = "TRACE_AGENT_MEMORY_EMBEDDING_BASE_URL"
MEMORY_EMBEDDING_API_KEY_ENV = "TRACE_AGENT_MEMORY_EMBEDDING_API_KEY"
MEMORY_EMBEDDING_TIMEOUT_ENV = "TRACE_AGENT_MEMORY_EMBEDDING_TIMEOUT"
MEMORY_EMBEDDING_TIMEOUT_DEFAULT = 10.0
MEMORY_EMBEDDING_BATCH_ENV = "TRACE_AGENT_MEMORY_EMBEDDING_BATCH"
MEMORY_EMBEDDING_BATCH_DEFAULT = 16

# Relevance floor for the semantic channel, separate from ``MIN_SCORE``. Cosine
# over a bag of symptom features sits well above zero for loosely related text,
# so the lexical floor (0.15) would admit noise: a hit must clear this on
# semantics *or* clear ``MIN_SCORE`` on lexical overlap, never both at once.
MEMORY_MIN_SEMANTIC_ENV = "TRACE_AGENT_MEMORY_MIN_SEMANTIC"
MEMORY_MIN_SEMANTIC_DEFAULT = 0.35

# How many records one dreaming beat may embed. Bounded because the beat is on a
# timer and a first run over a migrated corpus must not turn one tick into
# thousands of provider calls.
MEMORY_EMBED_LIMIT_ENV = "TRACE_AGENT_MEMORY_EMBED_LIMIT"
MEMORY_EMBED_LIMIT_DEFAULT = 32

# Size of the vector candidate pool merged into the lexical one per recall.
MEMORY_VECTOR_LIMIT_ENV = "TRACE_AGENT_MEMORY_VECTOR_LIMIT"
MEMORY_VECTOR_LIMIT_DEFAULT = 50

# ------------------------------------------------------------------ cache (P4)
# Recall cache for repeated identical queries - the dashboard refreshing a panel,
# a multi-instance deployment answering the same question twice, an eval that
# recalls once per trial. Section 6 puts Redis here, and section 12 puts the
# source of truth in SQL: the cache is a read accelerator only, which is why the
# default is ``off`` and why every mode degrades to a correct (uncached) recall.
#
# Entries are keyed on the store's ``corpus_epoch``, so a write invalidates the
# whole namespace's cache without any writer having to know which queries it
# affected. That is a deliberately blunt instrument: a corpus that changes is a
# corpus whose cached answers are wrong, and computing a precise invalidation set
# would cost more than the recall it saves.
MEMORY_CACHE_ENV = "TRACE_AGENT_MEMORY_CACHE"
MEMORY_CACHE_OFF = "off"
MEMORY_CACHE_MEMORY = "memory"
MEMORY_CACHE_REDIS = "redis"
MEMORY_CACHE_MODES = frozenset(
    {MEMORY_CACHE_OFF, MEMORY_CACHE_MEMORY, MEMORY_CACHE_REDIS}
)
MEMORY_CACHE_DEFAULT = MEMORY_CACHE_OFF

# Redis URL for ``cache=redis``. Unset means ``redis://localhost:6379/0``, the
# client's own default, so a single-node deployment needs no configuration.
MEMORY_CACHE_URL_ENV = "TRACE_AGENT_MEMORY_CACHE_URL"
MEMORY_CACHE_URL_DEFAULT = "redis://localhost:6379/0"

# Entry lifetime. Short by default: correctness comes from the epoch, the TTL
# only bounds how long a *stale server clock* or a hand-edited corpus can keep
# serving an old answer.
MEMORY_CACHE_TTL_ENV = "TRACE_AGENT_MEMORY_CACHE_TTL"
MEMORY_CACHE_TTL_DEFAULT = 300.0

# Bound on the in-process cache, in entries. A dashboard that queries every
# namespace in turn must not grow without limit; eviction is least-recently-used,
# because the panels that refresh most often are also the ones worth keeping.
MEMORY_CACHE_SIZE_ENV = "TRACE_AGENT_MEMORY_CACHE_SIZE"
MEMORY_CACHE_SIZE_DEFAULT = 256

_TRUTHY = {"1", "true", "yes", "on"}


def _parse_bool(raw: str | None, default: bool) -> bool:
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in _TRUTHY


def _parse_int(raw: str | None, default: int, *, minimum: int = 1) -> int:
    """Integer config that falls back to ``default`` on anything unusable.

    Both failure modes - unparseable and below ``minimum`` - return the
    documented default rather than a clamped floor, so one rule explains every
    knob here. Clamping would make ``GATE_RUNS=0`` silently mean "1 run" and
    ``PROMPT_CHARS=0`` silently mean "1 char", i.e. two new undocumented
    settings an operator never asked for. Memory tuning must never be able to
    fail a run either, which is why this cannot raise; ``memory stats`` prints
    the effective values so a typo stays visible.
    """
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        return default
    return value if value >= minimum else default


def _parse_float(raw: str | None, default: float) -> float:
    if raw is None or raw.strip() == "":
        return default
    try:
        return min(1.0, max(0.0, float(raw.strip())))
    except ValueError:
        return default


def _parse_positive_float(raw: str | None, default: float) -> float:
    """Float config for quantities that are not ratios (timeouts, seconds).

    ``_parse_float`` clamps to ``[0, 1]`` because every knob it was written for
    is a similarity or a confidence. A timeout needs the same "never raise, fall
    back to the documented default" behaviour without that ceiling.
    """
    if raw is None or raw.strip() == "":
        return default
    try:
        value = float(raw.strip())
    except ValueError:
        return default
    return value if value > 0 else default


def memory_enabled_explicit() -> bool | None:
    """Optional explicit override; None means use the default."""
    raw = os.environ.get(MEMORY_ENABLED_ENV)
    if raw is None or raw == "":
        return None
    return raw.strip().lower() in _TRUTHY


def memory_include_candidate() -> bool:
    """Whether unverified candidate memories may be recalled at low weight."""
    return _parse_bool(
        os.environ.get(MEMORY_INCLUDE_CANDIDATE_ENV),
        MEMORY_INCLUDE_CANDIDATE_DEFAULT,
    )


def memory_gate_min_runs() -> int:
    """Distinct independent runs required before a candidate becomes active."""
    return _parse_int(os.environ.get(MEMORY_GATE_RUNS_ENV), MEMORY_GATE_RUNS_DEFAULT)


def memory_gate_min_confidence() -> float:
    """Minimum diagnosis confidence required for ``candidate -> active``."""
    return _parse_float(
        os.environ.get(MEMORY_GATE_CONFIDENCE_ENV),
        MEMORY_GATE_CONFIDENCE_DEFAULT,
    )


def memory_gate_verified_runs() -> int:
    """Independent runs required for the automatic ``active -> verified`` path."""
    return _parse_int(
        os.environ.get(MEMORY_GATE_VERIFIED_RUNS_ENV),
        MEMORY_GATE_VERIFIED_RUNS_DEFAULT,
    )


def eval_run_id() -> str | None:
    """Eval run this process belongs to, or ``None`` for a production run.

    Read per call rather than cached at import: the harness sets it after the
    module is already loaded, and a cached ``None`` would silently strip eval
    provenance from every episode the experiment writes.
    """
    raw = os.environ.get(EVAL_RUN_ID_ENV)
    if raw is None:
        return None
    cleaned = raw.strip()
    return cleaned or None


def memory_half_life_days() -> float:
    """Recency half-life in days used by the freshness component."""
    raw = os.environ.get(MEMORY_HALF_LIFE_DAYS_ENV)
    if raw is None or raw.strip() == "":
        return MEMORY_HALF_LIFE_DAYS_DEFAULT
    try:
        value = float(raw.strip())
    except ValueError:
        return MEMORY_HALF_LIFE_DAYS_DEFAULT
    return value if value > 0 else MEMORY_HALF_LIFE_DAYS_DEFAULT


def memory_min_score() -> float:
    """Relevance floor below which a hit is discarded before reranking."""
    return _parse_float(os.environ.get(MEMORY_MIN_SCORE_ENV), MEMORY_MIN_SCORE_DEFAULT)


def memory_store_kind() -> str:
    """Which ``MemoryStore`` adapter backs the corpus.

    An unrecognised value falls back to the default instead of raising, because
    resolving memory must never be able to fail an analysis run. ``memory
    stats`` prints the effective kind so a typo is still visible.
    """
    raw = os.environ.get(MEMORY_STORE_ENV)
    if raw is None or raw.strip() == "":
        return MEMORY_STORE_DEFAULT
    kind = raw.strip().lower()
    return kind if kind in MEMORY_STORE_KINDS else MEMORY_STORE_DEFAULT


def memory_dsn() -> str:
    """PostgreSQL connection string for ``store=postgres``; ``""`` when unset.

    Read per call rather than cached: an operator switching a corpus over sets
    the variable in the environment of the process they are about to start, and a
    value captured at import would make ``TRACE_AGENT_MEMORY_DSN=... diting-agent
    memory stats`` behave differently from exporting it first.
    """
    raw = os.environ.get(MEMORY_DSN_ENV)
    if raw is None:
        return ""
    return raw.strip()


def memory_schema() -> str:
    """Schema the PostgreSQL corpus lives in."""
    raw = os.environ.get(MEMORY_SCHEMA_ENV)
    if raw is None or raw.strip() == "":
        return MEMORY_SCHEMA_DEFAULT
    return raw.strip()


def memory_connect_timeout() -> float:
    """Seconds a PostgreSQL connection attempt may block."""
    return _parse_positive_float(
        os.environ.get(MEMORY_CONNECT_TIMEOUT_ENV),
        MEMORY_CONNECT_TIMEOUT_DEFAULT,
    )


def memory_namespace() -> str:
    """Isolation domain stamped on writes and enforced on every read."""
    raw = os.environ.get(MEMORY_NAMESPACE_ENV)
    if raw is None or raw.strip() == "":
        return MEMORY_NAMESPACE_DEFAULT
    return raw.strip()


def memory_session_id() -> str | None:
    """Session an episode or turn belongs to, when the caller did not say.

    ``None`` means "no session context", which is a legitimate state: a bare CLI
    analysis has no conversation around it. Callers pass an explicit id when they
    have one (the dashboard does), so this is the fallback, not the source.
    """
    raw = os.environ.get(MEMORY_SESSION_ENV)
    if raw is None:
        return None
    cleaned = raw.strip()
    return cleaned or None


def memory_project_id() -> str | None:
    """Project a record belongs to, derived from the namespace when unset.

    The default namespace is ``project:default``, so the ordinary deployment gets
    a project id for free instead of writing ``NULL`` into every row and losing
    the one filter section 8 says a corpus needs. A namespace that is not
    project-shaped - ``eval:<run>:<case>`` - yields ``None``: an eval run belongs
    to an experiment, and inventing a project for it would let eval records look
    like production ones in a project-scoped query.
    """
    raw = os.environ.get(MEMORY_PROJECT_ENV)
    if raw is not None and raw.strip():
        return raw.strip()
    namespace = memory_namespace()
    prefix = "project:"
    if namespace.startswith(prefix):
        derived = namespace[len(prefix) :].strip()
        return derived or None
    return None


def memory_agent_id() -> str:
    """Identity of the agent instance writing the record."""
    raw = os.environ.get(MEMORY_AGENT_ENV)
    if raw is None or raw.strip() == "":
        return MEMORY_AGENT_DEFAULT
    return raw.strip()


def memory_user_id() -> str | None:
    """Operating-system user, or ``None`` when it cannot be determined.

    ``getpass.getuser`` consults several environment variables and then the
    password database, and it raises rather than returning ``None`` when all of
    them are unavailable (a service account with no ``HOME``, a container with a
    stripped passwd). Ownership metadata is never worth an exception on the write
    path, so the failure is swallowed.
    """
    raw = os.environ.get(MEMORY_USER_ENV)
    if raw is not None and raw.strip():
        return raw.strip()
    try:
        user = getpass.getuser()
    except (KeyError, OSError, ValueError):
        return None
    cleaned = (user or "").strip()
    return cleaned or None


def memory_dream_interval() -> float:
    """Seconds the background dreaming beat sleeps between ticks."""
    return _parse_positive_float(
        os.environ.get(MEMORY_DREAM_INTERVAL_ENV),
        MEMORY_DREAM_INTERVAL_DEFAULT,
    )


def memory_candidate_limit() -> int:
    """How many indexed candidates may reach the reranker per recall."""
    return _parse_int(
        os.environ.get(MEMORY_CANDIDATE_LIMIT_ENV),
        MEMORY_CANDIDATE_LIMIT_DEFAULT,
    )


def memory_type_quota() -> int:
    """First-pass slot count per functional type; ``0`` disables the rule.

    A negative value is a typo, not a policy, so it is clamped to ``0`` (off)
    rather than interpreted as "unlimited".
    """
    return max(
        0,
        _parse_int(
            os.environ.get(MEMORY_TYPE_QUOTA_ENV),
            MEMORY_TYPE_QUOTA_DEFAULT,
        ),
    )


def _parse_days(raw: str | None, default: float) -> float:
    """Day-valued config. ``0`` disables the rule, negatives are a typo."""
    if raw is None or raw.strip() == "":
        return default
    try:
        value = float(raw.strip())
    except ValueError:
        return default
    return value if value >= 0 else default


def memory_retention_days() -> float:
    """Days without an update before a memory is stamped with ``expires_at``.

    ``0`` disables retention stamping, which is what an operator wants on a
    corpus that must never shrink; already-stamped records still expire.
    """
    return _parse_days(
        os.environ.get(MEMORY_RETENTION_DAYS_ENV),
        MEMORY_RETENTION_DAYS_DEFAULT,
    )


def memory_candidate_ttl_days() -> float:
    """Days a single-run candidate may stay unconfirmed before it is archived."""
    return _parse_days(
        os.environ.get(MEMORY_CANDIDATE_TTL_DAYS_ENV),
        MEMORY_CANDIDATE_TTL_DAYS_DEFAULT,
    )


def memory_duplicate_similarity() -> float:
    """Similarity at which two same-scope records are merged by supersession."""
    return _parse_float(
        os.environ.get(MEMORY_DUPLICATE_SIMILARITY_ENV),
        MEMORY_DUPLICATE_SIMILARITY_DEFAULT,
    )


def memory_conflict_similarity() -> float:
    """Similarity below which same-topic records from different runs conflict."""
    return _parse_float(
        os.environ.get(MEMORY_CONFLICT_SIMILARITY_ENV),
        MEMORY_CONFLICT_SIMILARITY_DEFAULT,
    )


def memory_conflict_overlap() -> float:
    """Overlap below which two same-topic records describe different mechanisms.

    A second, stricter floor under :func:`memory_conflict_similarity`: Jaccard
    alone cannot tell a paraphrase from a refutation, so the sweep also asks how
    much of the *smaller* record the larger one covers. Records that clear this
    floor are arguing about the same mechanism in different words.
    """
    return _parse_float(
        os.environ.get(MEMORY_CONFLICT_OVERLAP_ENV),
        MEMORY_CONFLICT_OVERLAP_DEFAULT,
    )


def memory_related_overlap() -> float:
    """Topic overlap two records must reach before the sweep compares them."""
    return _parse_float(
        os.environ.get(MEMORY_RELATED_OVERLAP_ENV),
        MEMORY_RELATED_OVERLAP_DEFAULT,
    )


def memory_related_similarity() -> float:
    """Topic Jaccard floor keeping a degenerate short topic from pairing up."""
    return _parse_float(
        os.environ.get(MEMORY_RELATED_SIMILARITY_ENV),
        MEMORY_RELATED_SIMILARITY_DEFAULT,
    )


def memory_maintain_every() -> int:
    """Dream ticks between corpus-wide governance sweeps.

    Falls back to the default rather than raising, like every other knob here:
    a mistyped interval must not stop the background beat, and ``memory stats``
    is where an operator goes to see the effective value.
    """
    return _parse_int(
        os.environ.get(MEMORY_MAINTAIN_EVERY_ENV),
        MEMORY_MAINTAIN_EVERY_DEFAULT,
    )


def memory_inline_governance() -> bool:
    """Whether a finished analysis sweeps the corpus before returning."""
    return _parse_bool(
        os.environ.get(MEMORY_INLINE_GOVERNANCE_ENV),
        MEMORY_INLINE_GOVERNANCE_DEFAULT,
    )


def memory_prompt_chars() -> int:
    """Character budget for the recalled-memory section of the system prompt."""
    return min(
        MEMORY_PROMPT_CHARS_MAXIMUM,
        _parse_int(
            os.environ.get(MEMORY_PROMPT_CHARS_ENV),
            MEMORY_PROMPT_CHARS_DEFAULT,
        ),
    )


def memory_embedding_kind() -> str:
    """Configured embedding provider: ``local``, ``http`` or ``off``.

    Aliases are folded here rather than at each call site so that ``openai`` and
    ``http`` cannot end up meaning different things in the recall path and in the
    CLI. An unrecognised value falls back to the default instead of raising,
    matching :func:`memory_store_kind`: a typo in a memory knob must never be
    able to fail an analysis run.
    """
    raw = os.environ.get(MEMORY_EMBEDDING_ENV)
    if raw is None:
        return MEMORY_EMBEDDING_DEFAULT
    value = raw.strip().lower()
    if value in _EMBEDDING_OFF_ALIASES:
        return MEMORY_EMBEDDING_OFF
    if value in _EMBEDDING_HTTP_ALIASES:
        return MEMORY_EMBEDDING_HTTP
    if value == MEMORY_EMBEDDING_LOCAL:
        return MEMORY_EMBEDDING_LOCAL
    return MEMORY_EMBEDDING_DEFAULT


def memory_embedding_dim() -> int:
    """Dimensionality of the offline recipe."""
    return _parse_int(
        os.environ.get(MEMORY_EMBEDDING_DIM_ENV),
        MEMORY_EMBEDDING_DIM_DEFAULT,
    )


def memory_embedding_model() -> str:
    """Model name recorded beside every remotely produced vector."""
    raw = os.environ.get(MEMORY_EMBEDDING_MODEL_ENV)
    if raw is None or raw.strip() == "":
        return MEMORY_EMBEDDING_MODEL_DEFAULT
    return raw.strip()


def memory_embedding_base_url() -> str:
    """``/embeddings`` root for the remote provider.

    Falls back to the OpenAI-compatible variables the LLM layer already uses, so
    an operator who configured a gateway for chat does not have to configure it
    twice for embeddings.
    """
    for name in (
        MEMORY_EMBEDDING_BASE_URL_ENV,
        "OPENAI_BASE_URL",
        "OPENAI_API_BASE",
    ):
        raw = os.environ.get(name)
        if raw and raw.strip():
            return raw.strip().rstrip("/")
    return ""


def memory_embedding_api_key() -> str:
    """Credential for the remote provider, from memory-specific or OpenAI env."""
    for name in (MEMORY_EMBEDDING_API_KEY_ENV, "OPENAI_API_KEY"):
        raw = os.environ.get(name)
        if raw and raw.strip():
            return raw.strip()
    return ""


def memory_embedding_timeout() -> float:
    """Per-request timeout in seconds for the remote provider."""
    return _parse_positive_float(
        os.environ.get(MEMORY_EMBEDDING_TIMEOUT_ENV),
        MEMORY_EMBEDDING_TIMEOUT_DEFAULT,
    )


def memory_embedding_batch_size() -> int:
    """Texts per remote request."""
    return _parse_int(
        os.environ.get(MEMORY_EMBEDDING_BATCH_ENV),
        MEMORY_EMBEDDING_BATCH_DEFAULT,
    )


def memory_min_semantic() -> float:
    """Cosine a hit must reach to be admitted on semantics alone."""
    return _parse_float(
        os.environ.get(MEMORY_MIN_SEMANTIC_ENV),
        MEMORY_MIN_SEMANTIC_DEFAULT,
    )


def memory_embed_limit() -> int:
    """Records one dreaming beat may embed."""
    return _parse_int(
        os.environ.get(MEMORY_EMBED_LIMIT_ENV),
        MEMORY_EMBED_LIMIT_DEFAULT,
    )


def memory_vector_limit() -> int:
    """Vector candidates merged into the lexical pool per recall."""
    return _parse_int(
        os.environ.get(MEMORY_VECTOR_LIMIT_ENV),
        MEMORY_VECTOR_LIMIT_DEFAULT,
    )


def memory_cache_mode() -> str:
    """Which recall cache is in force: ``off``, ``memory`` or ``redis``.

    An unrecognised value means ``off``, not the default mode: turning a cache on
    is a decision an operator makes, and a typo in its name must not silently
    start buffering recall results in a process nobody intended to be stateful.
    """
    raw = os.environ.get(MEMORY_CACHE_ENV)
    if raw is None or raw.strip() == "":
        return MEMORY_CACHE_DEFAULT
    mode = raw.strip().lower()
    return mode if mode in MEMORY_CACHE_MODES else MEMORY_CACHE_OFF


def memory_cache_url() -> str:
    """Redis URL for ``cache=redis``."""
    raw = os.environ.get(MEMORY_CACHE_URL_ENV)
    if raw is None or raw.strip() == "":
        return MEMORY_CACHE_URL_DEFAULT
    return raw.strip()


def memory_cache_ttl() -> float:
    """Seconds a cached recall stays valid."""
    return _parse_positive_float(
        os.environ.get(MEMORY_CACHE_TTL_ENV),
        MEMORY_CACHE_TTL_DEFAULT,
    )


def memory_cache_size() -> int:
    """Maximum entries held by the in-process cache."""
    return _parse_int(
        os.environ.get(MEMORY_CACHE_SIZE_ENV),
        MEMORY_CACHE_SIZE_DEFAULT,
    )

# DitingAgent Memory 优化方案（v3）

> 状态：P0/P1/P2 已实施并通过验证（附录 C/D）；P3 语义检索已实施（附录 E）；
> §7 trust 契约与 §8 eval 隔离/自污染审计已实施（附录 F）；
> 生产链路冒烟见附录 G；P4（`PostgresStore` + 召回缓存 + 隔离域运维/备份）已实施，
> 并在真实 PostgreSQL 17.10 上端到端跑通（附录 H）
> 真实语料验证、A/B 收口与人工签署通道见附录 I；审计轨迹随迁移搬运见附录 J；
> §2.3 六类记忆职能、§9 召回可观测性、§12.4 迁移期信任降级三处收尾见附录 K；
> **H 的 live 数字已在真实 PostgreSQL 17.10 + Redis 7.2.5 上复验，并修掉一个序号冲突见附录 L**
> 至此正文（§2~§12）条目已全部实施；有意收窄或裁剪的偏差逐条记在各附录的“与设计稿的偏差”小节
> （分布式锁与 namespace 鉴权见 H.4，迁移降级口径见 K.2，摘要类记忆豁免闸门见 K.3）
> 适用范围：`src/trace_agent/memory/**`、`src/trace_agent/application/analyze.py`、
> `src/trace_agent/agent/core/prompts.py`、`src/trace_agent/web/server.py`、`src/trace_agent/eval/**`
> 对齐目标：Session 短期记忆 + 结构化长期 Memory + Extraction/Consolidation +
> Hybrid Retrieval + Scope/Namespace + Trust/生命周期治理

---

## 0. 结论摘要

现有 `memory/` 模块的**骨架方向是对的**：episode 投影、curated 晋升、provenance、
candidate/active 状态、dreaming 延迟整合、audit 注入记录都已经存在。问题不在"要不要换数据库"，
而在三处：

1. **Trust 闸门被单次 run 击穿**（已实测复现）：一个 `suspected`、confidence=0.45 的结论，
   在**一次 run + 一次 dream beat** 之后就变成 `active`、`occurrences=2`，
   与"两个独立案例交叉验证过的规律"在数据上完全无法区分。这就是设计稿第十节指的 self-contamination。
2. **中文召回近乎失效**（已实测复现）：`tokenize()` 把连续 CJK 当成**一个 token**，
   任何重新分词或改写都命中不到。`"冷启动 白屏"`、`"启动白屏问题排查"` 对含 `"冷启动白屏"` 的记忆
   **返回 0 条**。而 `_memory_query()` 恰恰是用空格拼接中文字段构造查询的。
3. **召回是 O(N) 全量扫盘 + 单一饱和分数**：每次 recall 都把 `curated/`、`episodes/`、`sessions/`
   下所有 JSON 文件读一遍并反序列化；打分 `min(score, 1.0)` 导致大量并列 1.0，排序失去区分度；
   `confidence / occurrences / status / 时效` 全部没有参与排序。

因此本方案的落地顺序是：**先修生命周期与召回语义（P0，纯逻辑，不动存储）→ 再换存储底座与检索（P1/P2）
→ 最后才是 pgvector / Redis（P3/P4，按触发条件再上）**。数据库是底座，不是第一步。

---

## 1. 现状审计

### 1.1 现有链路

```text
AnalyzeApplication.run
  └─ recall:  analyze.py:258-272  → MemoryRuntime.recall → RecallService.recall
                                    → prompts.memory_section → system prompt
                                    → audit.record_memory_injection
  └─ capture: analyze.py:987-1010 → build_episode → remember_episode(consolidate=True)
                                  → dreaming.enqueue_episode（同一条 episode 再排一次队）
web/server.py:1005-1015           → 每 30s dreaming.run_once → ConsolidationService.consolidate
存储: JsonMemoryBackend           → .trace-agent/memory/{episodes,curated,sessions,dreaming}
```

### 1.2 实测复现（附录 A 有可直接运行的脚本）

**缺陷 A：单次 run 直通 active**

```text
after immediate consolidate: mem-white_screen-45a0c1fa9553 status=candidate occurrences=1 confidence=0.45
dream batch: processed=1 created=['mem-white_screen-45a0c1fa9553'] errors=0
after dream beat:            mem-white_screen-45a0c1fa9553 status=active   occurrences=2 confidence=0.45 validated_by=pending
```

成因链：

- `analyze.py:987-1010` 对同一条 episode **既立即 `consolidate=True`，又 `dreaming.enqueue_episode`**；
- `dreaming.py:56-67` 消费队列时对**同一 episode 再 consolidate 一次**（`consolidate` 调用在 63 行）；
- `consolidation.py:78-118` `_promote()` 把"同 id 被写了第二次"当成"被第二次确认"：
  `occurrences += 1`，且 `status ∈ {CANDIDATE, ACTIVE} → ACTIVE`；
- `dreaming.py:79-88` `_deep_promote()` 的 `occurrences >= 2 → active` 因此也形同虚设；
- `validated_by="pending"`（`consolidation.py` 候选构造处）只是写进去的哨兵字符串，**没有任何消费方**。

**缺陷 B：CJK 召回失效**

```text
'冷启动白屏'            -> hits=2 scores=[1.0, 1.0]     # 仅因为字符串逐字相同
'冷启动 白屏'           -> hits=0 scores=[]             # 多一个空格就全丢
'启动白屏问题排查'        -> hits=0 scores=[]             # 自然改写全丢
'白屏'                 -> hits=2 scores=[0.25, 0.25]   # 只剩 phrase bonus
```

成因：`text.py:9-24` 的 `re.findall(r"[\u3400-\u9fff]+", text)` 把一整段中文压成单 token；
`_keyword_score` 是集合覆盖率 `matched/total`，token 不对齐就是 0。
而 `analyze.py:92-105` `_memory_query()` = `" ".join([scenario_type, scenario, symptom, process, device, build])`，
中文 query 天然带空格 → **生产路径上的真实召回率接近 0**，只有完全同句复现才命中。

**缺陷 C：分数饱和 + 重复注入**

```text
recall hits=2 scores=[1.0, 0.5]
context_prompt:
  Prior curated project knowledge:
  - [Pattern] Main thread blocked by sync IO: sync file read on main thread during launch
  Similar prior analyses:
  - [white_screen] main thread blocked by sync IO      # 与上一条同源，重复占预算
```

`recall.py:73-114` 用 `min(score, 1.0)` 截断，多个 hit 并列 1.0；curated 与其来源 episode
同时进 prompt，没有去重。

### 1.3 结构性问题清单

| # | 问题 | 位置 | 影响 | 设计稿对应 |
|---|---|---|---|---|
| 1 | 单次 run 即可晋升 active | `consolidation.py:78-118`、`analyze.py:987-1010`、`dreaming.py:60-88` | 自污染，错误结论变成先验 | 十、十一 |
| 2 | candidate 与 active 同权注入 | `recall.py:42`（`active_only=True` 只排除 superseded/archived） | 未验证结论以"历史项目知识"措辞进 prompt | 十、十一 |
| 3 | CJK 分词失效 | `text.py:9-24` | 中文召回≈0 | 九 |
| 4 | 无索引，每次全量扫盘+反序列化 | `backend.py:76-92,114-140,176-190` | 召回延迟随记忆数线性增长 | 四、五 |
| 5 | 打分饱和、无 rerank | `recall.py:73-114` | 排序不可信，top-k 近似随机 | 九 |
| 6 | confidence/importance/时效不参与排序 | `recall.py`、`models.py` | 高置信与低置信同权 | 九 |
| 7 | 无 importance / last_accessed_at / expires_at | `models.py:CuratedMemory` | 无法做衰减、淘汰、retention | 八 |
| 8 | 无版本、无 supersedes 链路落地 | `models.py`（有 `superseded_by` 字段但无写入方） | 冲突无法追溯 | 八 |
| 9 | 无冲突检测 | `consolidation.py:_extract_candidates` | 同 topic 的矛盾结论并存 | 二、五 |
| 10 | scope 只有 `scenario_type` | `recall.py:43-59` | 无 project/user/namespace 隔离 | 一、八 |
| 11 | 记忆根目录依赖 `Path.cwd()` | `analyze.py:258`、`web/server.py:592` | 换目录静默换记忆库；eval 与生产共用 | 三 |
| 12 | eval 未做记忆隔离 | `eval/**` 无 `memory_enabled` 引用 | trial-0 的错误结论被 trial-1 召回 | 十 |
| 13 | 记忆类型与设计稿不对齐 | `models.py:MemoryKind` | 无 profile/project/summary 类型 | 一、八 |
| 14 | 无语义检索 / embedding | 全模块 | 同义改写召不回 | 九 |
| 15 | 召回无 EventBus 事件 | `runtime/events.py:EventType` 无 memory.* | 线上无法归因"记忆是否影响了结论" | 三（Observability） |

---

## 2. 目标架构

### 2.1 分层（与设计稿第十二节对齐）

```text
                      DitingAgent Runtime
                              │
                    Context Manager / Composer
                              │
        ┌─────────────────────┼─────────────────────┐
        │                     │                     │
        ▼                     ▼                     ▼
  Working / Session     Long-Term Memory      Raw Evidence
  (会话轮次 + 摘要)      (结构化结论)          (trace / evidence.json / report.html)
                              │                     │
              ┌───────────────┼──────────────┐      │
              ▼               ▼              ▼      ▼
          semantic       episodic      procedural   文件系统 / 对象存储
          profile        project       summary      （保持现状，不入库）
              └───────────────┴──────────────┘
                              │
                     MemoryStore (Port)
              ┌───────────────┼───────────────┐
              ▼               ▼               ▼
        JsonStore       SqliteStore      PostgresStore
        (兼容/回滚)      (默认, P1)       (+pgvector, P3)
                              │
                        RedisCache (P4, 可选)
```

**保留现有正确设计**：Memory 只存"从 Trace 学到了什么"，原始 trace / `evidence.json` /
`report.html` 继续留在 run 目录，通过 `Provenance.references` 引用（设计稿第七节）。

### 2.2 组件映射：设计稿 → 现有代码 → 目标模块

| 设计稿组件 | 现状 | 目标 |
|---|---|---|
| MemoryExtractor | 埋在 `consolidation.py:_extract_candidates` | `memory/extraction/candidate_extractor.py` |
| Episode Builder | `memory/episode.py` ✅ | 保留，补 `namespace` / `session_id` / `outcome` |
| Dedup / Conflict | 仅 `memory_id` 哈希精确去重 | `memory/extraction/dedupe.py`（规范化 + 相似去重 + 冲突检测） |
| MemoryConsolidator | `consolidation.py` ✅（语义有缺陷） | `memory/lifecycle/consolidator.py` |
| Quality Gate / Promotion | `dreaming._deep_promote`（阈值失效） | `memory/lifecycle/quality_gate.py` + `promoter.py` |
| MemoryRetriever | `recall.py` + `deep_recall.py` | `memory/retrieval/{query_builder,lexical,vector,retriever}.py` |
| MemoryReranker | 无 | `memory/retrieval/reranker.py` |
| MemoryLifecycle | 无（有状态枚举无治理） | `memory/lifecycle/retention.py`（衰减/过期/GC） |
| MemoryContextBuilder | `recall.render_context` + `prompts.memory_section` | `memory/context/composer.py`（按 trust 分区） |
| Session Store | `session.py` ✅ | 保留，补 `summary` 与滚动摘要 |
| Checkpoint | `execution.py` ✅ | 保留（属执行态，不是长期记忆） |
| EventBus / Observability | 仅 `audit.record_memory_injection` | 新增 `memory.*` 事件 + `run.json.memory_trace` |

> **实际落地布局**（与上表“目标”列的目录名略有出入，以代码为准）：实施时没有新建
> `extraction/`、`retrieval/` 两个目录，而是把职责并入既有模块，避免为分层而分层——
> MemoryExtractor / Dedup / Conflict → `consolidation.py` + `lifecycle/conflicts.py`；
> MemoryConsolidator / Quality Gate / Promotion → `lifecycle/quality_gate.py` + `lifecycle/promoter.py`；
> MemoryRetriever → `recall.py` + `deep_recall.py` + `store/*`（过滤下推到 store）；
> MemoryReranker → `rerank.py`；MemoryContextBuilder → `context/composer.py`；
> MemoryLifecycle（衰减 / 过期 / GC）→ `lifecycle/retention.py`。上表“目标”列保留为设计意图，
> 实际文件映射以本段为准。

### 2.3 记忆类型对齐

现有 `MemoryKind = {pattern, fact, procedure, anti-pattern}` 是**内容形态**分类，
设计稿的 `semantic/profile/episodic/procedural/project/summary` 是**记忆职能**分类。两者正交，都保留：

```python
class MemoryType(StrEnum):      # 职能（新增，参与检索过滤与注入分区）
    SEMANTIC   = "semantic"     # 稳定事实/领域知识      ← kind=fact
    EPISODIC   = "episodic"     # 完整历史案例            ← EpisodicMemory
    PROCEDURAL = "procedural"   # 这类问题怎么查          ← kind=procedure / anti-pattern
    PROFILE    = "profile"      # 设备/构建/项目稳定属性   ← 新增
    PROJECT    = "project"      # 当前项目长期状态         ← 新增
    SUMMARY    = "summary"      # session/task 摘要       ← SessionMemory.summary

class MemoryKind(StrEnum):      # 形态（保留，不动现有数据）
    PATTERN / FACT / PROCEDURE / ANTI_PATTERN
```

映射规则由 extractor 决定：`finding.status == suspected → (PATTERN, SEMANTIC)`；
`recommendation 非空 → (PROCEDURE, PROCEDURAL)`；`device/build 事实 → (FACT, PROFILE)`；
`episode 摘要 → (PATTERN, EPISODIC)`。

> ✅ 六格职能已全部有生产者：`PROFILE` 由 consolidation 从 episode 的 device/build/target_process
> 产出，`PROJECT` 由 dreaming beat 的项目摘要 job 产出（`memory/project.py`）。两者的取舍见附录 K.3。

---

## 3. 存储方案

### 3.1 为什么分两跳，而不是一步到 PostgreSQL + pgvector

设计稿的目标态（Postgres + pgvector 做 source of truth，Redis 做缓存，对象存储放原始 trace）
**方向认可**，但当前仓库的现实约束是：

- `pyproject.toml` 运行时依赖只有 5 个：`jinja2 / pydantic / pyyaml / rich / typer`，**零数据库驱动、零 embedding 依赖**；
- 仓库已经在用 **stdlib `sqlite3`**（`database/repository.py:SQLiteTraceRepository`、`database/perf*.py`），
  本机 SQLite 版本 3.45.3，FTS5 可用；
- 部署形态是单机 CLI + 本地 dashboard（`web/server.py`），单进程写入；
- 当前记忆量级是几十~几千条。

所以：**先用 SQLite 拿到与 Postgres 完全相同的表结构、语义和检索能力（零新增依赖），
把 Postgres+pgvector 做成同一个 `MemoryStore` Port 的第二个 Adapter，等触发条件到了直接切配置。**
这样设计稿第八节的表结构从 P1 起就是最终形态，不会白做。

**切到 PostgreSQL + pgvector 的触发条件**（满足任一即启动 P3）：

1. 多进程/多机并发写入同一记忆库（当前 SQLite 单写者会成为瓶颈）；
2. 记忆条数 > ~50 万，或需要真正的 ANN（HNSW/IVFFlat）而非精确/暴力检索；
3. 需要跨项目/跨用户共享记忆与权限治理（namespace + RBAC）；
4. 需要与业务库做联表分析（记忆 × 用例 × 缺陷单）。

**不引入 Milvus / Elasticsearch**：本项目是"结构化状态 + 过滤 + 排序"为主、向量为辅的检索，
不是纯向量问题；额外基础设施不划算（与设计稿第五节一致）。

**Redis 只做缓存，不做 source of truth**（与设计稿第六节一致），且仅在 P4、多实例部署时才引入。
单进程阶段用进程内 LRU + SQLite 页缓存即可，实测足够。

### 3.2 MemoryStore Port（关键抽象）

```python
# memory/ports.py
class MemoryStore(Protocol):
    # 写
    def put_episode(self, episode: EpisodicMemory) -> None: ...
    def upsert_memory(self, item: MemoryItem, *, reason: str) -> str: ...   # 返回新版本号
    def add_source(self, memory_id: str, *, run_id: str, episode_id: str) -> bool: ...
    def set_trust(self, memory_id: str, trust: MemoryTrust, *, reason: str, actor: str | None) -> None: ...
    def link(self, left: str, right: str, relation: str) -> None: ...
    # 读
    def search(self, query: MemoryQuery) -> list[ScoredMemory]: ...   # 过滤下推到 store
    def get(self, memory_id: str) -> MemoryItem | None: ...
    def stats(self) -> MemoryStats: ...
    # 治理
    def touch(self, memory_id: str) -> None: ...      # last_accessed_at
    def expire(self, now: datetime) -> int: ...
```

三个实现：`JsonStore`（包装现有 `JsonMemoryBackend`，用于回滚与离线导出）、
`SqliteStore`（默认）、`PostgresStore`（P3）。`MemoryRuntime` 只依赖 Port，
**现有 9 个 memory 测试不需要改**（继续注入 Json/Sqlite store）。

### 3.3 SQLite 检索：FTS5 + CJK bigram（已实测）

SQLite 3.45.3 实测结论：

```text
tokenize='trigram'  : '白屏问' -> 1 行 ；'白屏'(2字) -> 0 行   # trigram 要求 >=3 字符
unicode61 + 客户端 bigram 展开: '白屏' -> 1 ；'冷启' -> 1 ；'启动' -> 2 ；'耗时' -> 1
```

即：**默认 `unicode61` 分词 + 写入/查询两侧做 CJK bigram 展开**，才能让"白屏/冷启/卡顿"这类
双字中文症状词命中。注意 FTS5 的 `MATCH` 默认是隐式 AND，多词查询要显式构造 `OR` 并用 `bm25()` 排序，
否则 `'白屏 耗时'` 这类跨词查询会 0 命中（已实测）。

```python
# memory/retrieval/lexical.py
def cjk_bigrams(text: str) -> str:
    """把连续 CJK 展开成 bigram + 原串，ASCII 词原样保留。写入与查询共用同一函数。"""
```

```sql
CREATE VIRTUAL TABLE memories_fts USING fts5(
  memory_id UNINDEXED, topic, content, keywords,
  tokenize='unicode61'
);
-- 检索：bm25 提供 lexical_score，过滤条件走 memories 主表 JOIN，避免全量扫盘
SELECT m.*, bm25(memories_fts) AS rank
FROM memories_fts f JOIN memories m ON m.id = f.memory_id
WHERE memories_fts MATCH ? AND m.namespace = ? AND m.trust IN ('active','verified')
ORDER BY rank LIMIT 50;
```

### 3.4 原始数据边界

| 数据 | 存储 | 说明 |
|---|---|---|
| Session 状态 / 轮次 | MemoryStore | `sessions` + `session_messages` |
| Long-term Memory | MemoryStore | `memories` |
| Embedding | MemoryStore | P3：pgvector `vector(dim)`；P1：可选 BLOB 列 |
| Memory metadata | MemoryStore | 结构化列 + `payload` JSON 列 |
| Memory version / link | MemoryStore | `memory_versions` / `memory_links` |
| 原始 trace / `evidence.json` / `report.html` / SQLite trace DB | **文件系统（现状不变）** | 记忆只存 `references` 指针 |
| 热点 recall 缓存 | 进程内 LRU（P4 可换 Redis） | key = `(namespace, query_hash, filter_hash)` |

---

## 4. 数据模型与表结构

按设计稿第八节落地，补 `namespace / trust / importance / source_run` 与幂等约束。
SQLite 与 Postgres 使用**同一套 DDL 语义**（仅类型与索引语法不同）。

```sql
CREATE TABLE sessions (
  id            TEXT PRIMARY KEY,
  namespace     TEXT NOT NULL,
  agent_id      TEXT,
  user_id       TEXT,
  project_id    TEXT,
  title         TEXT DEFAULT '',
  topic         TEXT DEFAULT '',
  summary       TEXT DEFAULT '',          -- 新增：滚动摘要（SUMMARY 记忆来源）
  scenario_type TEXT,
  device        TEXT,
  build         TEXT,
  status        TEXT NOT NULL DEFAULT 'open',
  started_at    TEXT NOT NULL,
  ended_at      TEXT,
  updated_at    TEXT NOT NULL
);

CREATE TABLE episodes (
  id             TEXT PRIMARY KEY,
  session_id     TEXT,
  namespace      TEXT NOT NULL,
  project_id     TEXT,
  run_id         TEXT NOT NULL,
  trace_id       TEXT NOT NULL,
  scenario_type  TEXT NOT NULL,
  scenario       TEXT DEFAULT '',
  symptom        TEXT DEFAULT '',
  target_process TEXT,
  device         TEXT,
  build          TEXT,
  summary        TEXT DEFAULT '',
  root_causes    TEXT DEFAULT '[]',       -- JSON array
  outcome        TEXT DEFAULT 'completed',-- completed | failed | inconclusive
  confidence     REAL NOT NULL DEFAULT 0,
  eval_run_id    TEXT,                    -- 新增：eval 隔离与自污染审计
  graded_pass    INTEGER,                 -- 新增：eval 判分结果，作为晋升证据
  source_run_dir TEXT,                    -- 原始证据指针（不入库）
  created_at     TEXT NOT NULL,
  UNIQUE (namespace, run_id)              -- 幂等：同一 run 只能投影一次
);

CREATE TABLE memories (
  id                TEXT PRIMARY KEY,
  namespace         TEXT NOT NULL,
  type              TEXT NOT NULL,        -- semantic|episodic|procedural|profile|project|summary
  kind              TEXT NOT NULL,        -- pattern|fact|procedure|anti-pattern
  topic             TEXT NOT NULL,
  content           TEXT NOT NULL,
  keywords          TEXT NOT NULL DEFAULT '[]',
  scope             TEXT NOT NULL DEFAULT '{}',   -- JSON: {project_id, scenario_type, device, build, process}
  trust             TEXT NOT NULL DEFAULT 'candidate',  -- candidate|active|verified|archived
  status_reason     TEXT,                 -- 最近一次 trust 变更原因（可审计）
  confidence        REAL NOT NULL DEFAULT 0,
  importance        REAL NOT NULL DEFAULT 0.5,
  severity          TEXT,
  occurrences       INTEGER NOT NULL DEFAULT 1,   -- = COUNT(DISTINCT run_id)，由 store 维护
  distinct_runs     INTEGER NOT NULL DEFAULT 1,
  contradictions    INTEGER NOT NULL DEFAULT 0,
  version           INTEGER NOT NULL DEFAULT 1,
  source_episode_id TEXT,
  supersedes_id     TEXT,
  superseded_by     TEXT,
  provenance        TEXT NOT NULL DEFAULT '{}',   -- JSON: source_type/references/created_by/validated_by
  payload           TEXT NOT NULL DEFAULT '{}',   -- JSON: 扩展位，避免频繁改表
  embedding         BLOB,                 -- P3 换成 vector(dim)
  embedding_model   TEXT,
  created_at        TEXT NOT NULL,
  updated_at        TEXT NOT NULL,
  last_accessed_at  TEXT,
  expires_at        TEXT
);
CREATE INDEX idx_memories_scope ON memories (namespace, type, trust, updated_at DESC);
CREATE INDEX idx_memories_expires ON memories (expires_at) WHERE expires_at IS NOT NULL;

-- 幂等与 distinct_runs 的唯一真相来源：一条记忆 × 一个 run 只算一次
CREATE TABLE memory_sources (
  memory_id  TEXT NOT NULL,
  run_id     TEXT NOT NULL,
  episode_id TEXT NOT NULL,
  namespace  TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (memory_id, run_id)
);

CREATE TABLE memory_versions (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  memory_id  TEXT NOT NULL,
  version    INTEGER NOT NULL,
  topic      TEXT NOT NULL,
  content    TEXT NOT NULL,
  trust      TEXT NOT NULL,
  confidence REAL NOT NULL,
  reason     TEXT NOT NULL,     -- consolidate|conflict|decay|expert|eval
  actor      TEXT,
  created_at TEXT NOT NULL,
  UNIQUE (memory_id, version)
);

CREATE TABLE memory_links (
  memory_id        TEXT NOT NULL,
  related_memory_id TEXT NOT NULL,
  relation         TEXT NOT NULL,   -- supports|contradicts|supersedes|duplicate-of|derived-from
  evidence         TEXT,            -- JSON: 触发该关系的 run/episode 引用
  created_at       TEXT NOT NULL,
  PRIMARY KEY (memory_id, related_memory_id, relation)
);

CREATE TABLE session_messages (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL,
  role       TEXT NOT NULL,
  content    TEXT NOT NULL,
  references TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL
);
CREATE INDEX idx_messages_session ON session_messages (session_id, id);
```

与现有 Pydantic 模型的兼容：`CuratedMemory` 保留为**领域对象**，`MemoryItem` 为其超集
（新增 `type / trust / scope / importance / distinct_runs / last_accessed_at / expires_at / version`）；
`MemoryStatus` 保留枚举值但由 `MemoryTrust` 承担召回语义，映射：
`CANDIDATE→candidate`、`ACTIVE→active`、`CONSOLIDATED→verified`（仅在有人工/eval 证据时）、
`SUPERSEDED/ARCHIVED→archived`。

**与实现的偏差**（落地见 `memory/store/schema.py`、`memory/store/pg_schema.py`，schema v3）：

1. **`namespace` 进主键**。设计稿是 `id TEXT PRIMARY KEY`，实现是
   `PRIMARY KEY (namespace, id)`，`memory_versions` / `memory_links` / `memory_sources` 同样带
   namespace。§8 的隔离要求同一个物理库放多个 namespace，单列主键会让两个项目撞 id。
2. **多两张表**。`store_meta`（`schema_version` + 每 namespace 的 `corpus_epoch`，召回缓存靠它失效）
   与 `memory_sources`（§5.1 幂等要求的落地：一条记忆 × 一个 run 只算一次，`distinct_runs` 由它派生）。
3. **每张表多一个 `payload` 列**，存完整 Pydantic dump。读取以 payload 为准、结构化列只供
   `WHERE` / `ORDER BY`。这是"三个 adapter 对等写入必须返回相等对象"的唯一可行做法：
   否则 JSON 侧能表达的字段在 SQL 侧会被列裁剪掉，回滚就变成有损迁移。
4. **列级增补**：`memories.archived_reason`（区分"过期归档"与"反证归档"，retention 统计要用）；
   `memory_versions.seq`（排序）与 `from_trust`（跃迁起点，§7 要求 `from → to` 都可审计）；
   `episodes.embedding` / `embedding_model`（P3 语义检索要能召回 episode，不只是 curated）；
   `episodes.session_id` / `project_id`（ownership 归属，§10.1 的 namespace 推导要用）。
5. **没有单独的 `MemoryItem` 类型**。设计稿的"超集模型"改成：`CuratedMemory` 上
   `type` / `trust` / `importance` / `scope` 是**派生属性**（由 `kind` / `severity` / `flags` 算出），
   `version` / `last_accessed_at` 是 `Field(exclude=True)` 字段（store-owned，读取时回填，
   **不进 payload**）。理由：派生而非存储，记录就不可能与自己矛盾；`exclude=True` 让
   JSON 语料的 payload 保持字节级不变，`TRACE_AGENT_MEMORY_STORE=json` 才是真回滚。
   代价是 JSON adapter 无法持久化 `last_accessed_at`（它的 `touch()` 是 no-op），
   这是有意的——文件布局下"召回即重写文件"会让 freshness 自己给自己加热。

---

## 5. Write 路径

```text
Run 完成
  → Episode Builder（幂等，UNIQUE(namespace, run_id)）
  → Candidate Extractor（findings / root_causes / device-build 事实 / 排查步骤）
  → Normalize + Dedup（规范化指纹 + 相似度去重）
  → Conflict Detection（同 scope 内矛盾结论 → contradicts 链接）
  → Consolidation（新建 / 更新 / 合并 / 丢弃，写 memory_versions）
  → Quality Gate（评估，不自动跨级）
  → Promotion（candidate → active → verified）
  → 异步 Reflection / Decay（dreaming beat）
```

### 5.1 P0 必修：拆掉双重整合

`analyze.py:_remember_episode` 改为**只做一次候选生产**，晋升交给 gate：

```python
# 目标语义
self._memory_runtime.remember_episode(episode, consolidate=True)   # 只产生/更新 candidate
# 删除：self._memory_runtime.dreaming.enqueue_episode(episode.episode_id)
self._memory_runtime.dreaming.enqueue_gate_review(episode.episode_id)  # dream 只评估 gate
```

`ConsolidationService.consolidate` 改为幂等：`add_source(memory_id, run_id)` 命中
`PRIMARY KEY (memory_id, run_id)` 冲突时**不增加 occurrences**，`distinct_runs` 由
`SELECT COUNT(*) FROM memory_sources WHERE memory_id = ?` 派生。
这样"同一 run 重复整合"不再伪装成"多案例交叉验证"。

### 5.2 Quality Gate（明确阈值，可配置）

| 跃迁 | 条件（全部满足） | 证据来源 |
|---|---|---|
| → `candidate` | 抽取通过 schema 校验、内容非空、非纯推测性空话 | extractor |
| `candidate → active` | `distinct_runs >= 2` **且** 跨 run 结论一致（无 contradicts 链接）**且** `confidence >= 0.6` **且** 至少一个来源 run 的 finding `status ∈ {confirmed, observed}` | memory_sources + memory_links + episode.findings |
| `active → verified` | 人工签署（`provenance.validated_by` 为真实身份）**或** `distinct_runs >= 5` 且零矛盾 **或** 来源 episode `graded_pass = true` | expert / eval grader |
| 任意 → `archived` | 出现更强反证（同 scope 内 confirmed 的矛盾结论）**或** 过期 **或** 人工下线 | conflict detector / retention |

**降级同样重要**：新 run 在同一 scope 产出 confirmed 且矛盾的 root cause 时，
旧记忆 `contradictions += 1`、`confidence *= 0.5`，`contradictions >= 2` 直接 `archived`
并写 `memory_links(relation='contradicts')`，保证记忆库不会只增不减地积累错误先验。

### 5.3 冲突检测与合并

- 精确指纹：`sha1(normalize(topic) + "\n" + normalize(content))`（现有 `_canonical_content` 保留）；
- 相似指纹：同 `scope` + 同 `type` 下 lexical 相似度 > 0.85 → 合并（`occurrences` 相加，
  `supersedes_id` 指向旧 id，写 version 快照）；P3 后改用 embedding 余弦 > 0.92；
- 矛盾判定：同 `scope` + 同 `topic` 指纹，但 `root_cause` 归一化后不同 → 不合并，建 `contradicts` 链接，
  两条都留在 `candidate`，由 gate 用后续证据裁决。

佐证带（P2 实测后补充）：同 `scope` + 同 `topic` 指纹、来源 run 互不相交，且 lexical 相似度落在
conflict_ceiling 与 duplicate_floor 之间（默认 0.60 到 0.92，均可配）时，既不够相似到归并、也不够
分歧到矛盾，属于两次独立分析用不同措辞得出同一结论。此时建双向 `supports` 链接，不取代、不降置信；
Quality Gate 通过 `supporting_runs_for()` 读取对端的 run id（扣除自身 run，避免自证）计入
`corroborated_runs`，使 `candidate -> active` 的 distinct-runs 门槛不再只能靠逐字节相同的结论归并来满足。
该带闭合了实测中的死区：此前相似度 0.895 的两条一致结论既不归并也不矛盾，永远停在 `candidate`。

### 5.4 Reflection（dreaming beat 扩展）

现有 `dreaming.run_once` 扩展为四类 job：`gate_review`（跑 5.2）、
`deep_promote`（跨 episode 归纳出 procedural 记忆，例如"冷启动白屏优先查主线程 IO"）、
`decay`（importance/freshness 衰减、过期归档）、`summarize`（session 滚动摘要 → SUMMARY 记忆）。
全部异步、best-effort，失败不影响主链路（保持现有失败语义）。

---

## 6. Read 路径

```text
任务 → Query Builder → Scope/Namespace 过滤 → Hybrid Retrieval(lexical + vector + metadata)
     → Rerank → Dedup/冲突处理 → Top-K → Context Composer(按 trust 分区) → Prompt
```

### 6.1 Query Builder

`_memory_query()` 从"拼一个字符串"升级为结构化查询对象，避免中文空格污染：

```python
@dataclass(frozen=True, slots=True)
class MemoryQuery:
    text: str                       # 自由文本（走 FTS/向量）
    namespace: str
    scope: Scope                    # project_id / scenario_type / device / build / process
    types: frozenset[MemoryType] = ALL_TYPES
    trusts: frozenset[MemoryTrust] = frozenset({ACTIVE, VERIFIED})   # candidate 需显式开启
    top_k: int = 5
    min_score: float = 0.15
    budget_chars: int = 3000
```

**与实现的偏差**（落地见 `memory/ports.py:MemoryQuery`）：

```python
@dataclass(frozen=True, slots=True)
class MemoryQuery:
    text: str = ""
    namespace: str | None = None
    scenario_type: str | None = None
    statuses: frozenset[MemoryStatus] = field(
        default_factory=lambda: TRUSTED_STATUSES
    )                                                      # = {ACTIVE, VERIFIED, CONSOLIDATED}
    types: frozenset[MemoryType] | None = None             # None = 不过滤
    include_episodic: bool = True
    limit: int = 50                                        # 候选池大小，不是 top_k
```

1. **`trusts` → `statuses`**。语料里叫 `status`，SQL 列叫 `trust`；查询对象保留领域名，
   才能不经翻译表直接对上 `CuratedMemory.trust`（它本身就是 `status` 的别名属性）。
   默认值不是设计稿写的 `{ACTIVE, VERIFIED}`，而是 `models.TRUSTED_STATUSES`
   = `{ACTIVE, VERIFIED, CONSOLIDATED}`；`candidate` 依旧不在其中，要靠
   `memory_include_candidate()` 显式打开后由 `RecallService._allowed_statuses()` 追加。
2. **`scope: Scope` → `scenario_type: str | None`**。当前召回真正下推过滤的只有这一个维度，
   `device` / `build` / `process` 参与打分而不参与过滤。在 Port 上先冻结一个没人调用过的
   `Scope` 结构，等于把一个未验证的形状写进契约。`project_id` 走 namespace（§8），不走查询。
3. **`top_k` / `min_score` / `budget_chars` 不在查询对象上**。它们分别属于
   `RecallService.recall(top_k=5)`（调用方参数，没有对应的 flag）、
   reranker（`memory_min_score()` 作为相关性下限）与 `context/composer.py`
   （`memory_prompt_chars()` 预算、`memory_type_quota()` 配额）。一个知道 prompt
   预算的 store 就是一个 prompt 改动就得跟着改的 store；store 只拿 `limit`
   （`memory_candidate_limit()`，默认 50），而且它约束的是**候选池**，不是最终答案。
4. **`include_episodic`** 是设计稿没有的开关：§6.3 的同源去重要求"curated 命中时剔除其来源
   episode"，反过来也有调用方只想要 curated。它从 `MemoryRuntime.recall` 一路透传到
   `RecallService._scan_candidates` / `MemoryStore.search`，放在查询上而不是让调用方后处理，
   才能让 store 少扫一张表（SQL 侧直接省掉 `episodes` 的 JOIN，JSON 侧省掉一次全量反序列化）。
   当前生产路径恒为 `True`，`False` 只有测试与未来"只要规律不要案例"的注入模式会用到。

### 6.2 Rerank 打分（替换单一 keyword score）

所有分量归一化到 `[0,1]`，**去掉 `min(score, 1.0)` 截断**，改为加权和天然有界：

```text
final_score =
    w_sem   * semantic_similarity     # P3 前恒为 0，权重转移到 lexical
  + w_lex   * lexical_score           # bm25 归一化：1 - rank/(rank + k), k=10
  + w_conf  * confidence
  + w_imp   * importance
  + w_fresh * freshness               # exp(-age_days / half_life), half_life 默认 90 天
  + w_trust * trust_prior             # verified 1.0 / active 0.7 / candidate 0.25
```

| 模式 | sem | lex | conf | imp | fresh | trust |
|---|---|---|---|---|---|---|
| 无 embedding（P1/P2 默认） | 0 | 0.50 | 0.15 | 0.10 | 0.10 | 0.15 |
| 有 embedding（P3） | 0.35 | 0.25 | 0.10 | 0.10 | 0.10 | 0.10 |

权重全部走配置，默认值仅作起点，需用第 14 节的指标调参（设计稿第九节同样强调权重只是示例）。

### 6.3 Dedup 与 Top-K

- 同源去重：curated 记忆命中时，剔除其 `source_episode_id` 对应的 episodic hit（修复 1.2 缺陷 C）；
- 近重复去重：同一 `topic` 指纹只保留分数最高的一条，其余转 `memory_links(duplicate-of)`；
- 配额：按 `type` 分配名额（如 procedural ≤2、episodic ≤2、semantic ≤2），避免单一类型刷满 top-k。

**实施状态（附录 G.3）**：三条都在。两处与原稿不同：

1. 近重复去重同时覆盖 **episode 之间**：指纹取"这条 episode 会渲染成的那句话"
   （`composer.episode_body`），因为同一 case 跑两次会写两条内容完全相同的 episode，
   而 candidate 分区只有 2 个名额——不折叠就会出现"同一句话占满两个坑，真正不同的观察被挤掉"。
   折叠不丢信息："在 N 个独立 run 里出现过"是 gate 的产物，它以 curated 记忆的 `runs=` 进 prompt，
   比把 episode 印两遍更强。
2. type 配额实现为**两趟填充**而不是硬上限：第一趟每种 type 最多 `quota`（默认 2）条，
   第二趟用被推迟的 hit 按分数补满 `top_k`。因此配额只改变"谁进 top-k"，
   永远不会让一次召回少返回结果——当相关记忆本来就只有一种 type 时，输出与没有配额时逐条相同。
   `TRACE_AGENT_MEMORY_TYPE_QUOTA=0` 关闭该规则。

### 6.4 Context Composer（按 trust 分区注入）

替换 `recall.render_context` 的单一"Prior curated project knowledge"措辞，改为三段、各自预算：

```text
【已验证规律 verified】(≤40% 预算)  可作为排查顺序依据，仍须以本次取证为准
【历史相似案例 active】 (≤45% 预算)  仅作先验假设，不得直接写入 Draft
【未验证候选 candidate】(≤15% 预算，最多 2 条)
   来自历史单次分析的未验证推测，仅作发散提示；与本次取证冲突时无条件丢弃
```

每条附带可追溯标识：`[mem-id | trust | conf=0.72 | runs=3]`，让模型与人工复盘都能看出先验的强度。
`prompts.memory_section` 的总预算与"取证优先"前言保留（`prompts.py:13-44`），
`candidate` 段在 eval 与 `--no-memory-priors` 模式下整段关闭。

episode 行是 `[ep-id | episode {派生 trust} | conf=… | score=…]`：episode 没有自己的 confidence 列，
`conf` 由其 findings 求均值得到（`rerank.episode_confidence`），rerank 的合成分单独标为 `score`。
两个通道的 `conf` 因此含义一致——早先的版本把合成分印在 `conf=` 后面，
等于把检索产物冒充成置信度声明（附录 G.3 有实测对照）。

---

## 7. Memory Trust 契约 — ✅ 已实施（见附录 F）

```text
candidate  仅低权重召回；默认不进 prompt；进入时必须显式标注未验证
active     正常召回，作为先验假设
verified   高置信度，可影响排查顺序
archived   不参与默认召回，仅供审计与回溯
```

不可违反的约束（写进模块 docstring 与测试）：

1. `trust=candidate` 的记忆**不得**以高权重先验形式注入，也不得参与 `deep_promote` 的自动跨级；
2. trust 跃迁**只能**由 Quality Gate 触发，且必须写 `memory_versions` + `status_reason`；
3. 召回结果的 trust 分布必须落到 observability，便于事后判断"这次结论是否被未验证记忆带偏"；
4. **闸门必须覆盖 episode 通道**：原始 episode 不是 candidate 记忆的旁路。未佐证的 episode
   （既无 `confirmed`/`observed` finding，也无 `graded_pass`）按派生 trust `candidate` 处理，
   默认不召回；派生 trust **封顶 `active`**，一次运行的原始记录不该拿到"可影响排查顺序"的档位；
5. **`verified` 没有捷径**：`graded_pass` 与 `distinct_runs >= 5` 两条自动通道都以
   `active_earned` 为前提（见附录 F.3），人工签署是唯一例外。

---

## 8. Eval 隔离与自污染防护 — ✅ 已实施（见附录 F）

现状：`eval/**` 从不设置 `memory_enabled`，`analyze.py:258` 用 `Path.cwd()` 解析记忆根，
因此**多个 trial 与日常运行共享同一个记忆库**，trial-0 的错误结论会被 trial-1 召回，
评测分数无法归因于模型能力。

方案：

1. ✅ `namespace` 强制隔离：eval 运行时 `namespace = f"eval:{eval_run_id}"`，
   生产运行 `namespace = f"project:{project_id}"`；检索永不跨 namespace。
   **与原设计的偏差**：粒度是 **run 级**而非 `eval:{run}:{case}` 的 case 级——case 级会同时切断
   trial 之间与 case 之间的交叉验证链，实验写出的记忆将永远停在 `candidate`，
   A/B 对照测的就变成"一个学不到任何东西的记忆系统"。run 级既保证生产语料读不到实验语料，
   又保留 trial 之间的交互，而这正是自污染审计要测量的对象。
   操作者显式设置的 `TRACE_AGENT_MEMORY_NAMESPACE` 优先（与 `TRACE_AGENT_MEMORY_ROOT` 同一个逃生口）。
2. ✅ eval 默认 `memory_enabled=False`，需要评测记忆收益时显式开启对照组（A/B 两次跑）。
   **与原设计的偏差**：开关是环境变量 `TRACE_AGENT_MEMORY=1` 而非 `--memory` flag，
   因为 `memory_enabled_explicit()` 是三态语义（未设置 / 显式开 / 显式关），
   `analyze.py`、`web/server.py`、eval 三处共用同一判定，加 flag 会引入第二个真相来源。
3. ✅ 记忆根目录改为显式配置：`TRACE_AGENT_MEMORY_ROOT` > 项目根推导 > `Path.cwd()`（消除 #11）；
   eval 开启记忆时若未指定 root，自动重定向到 `<output-root>/memory/eval-<hex>` 的独立语料。
4. ✅ eval 报告新增每 trial 的 `memory_trace`（注入了哪些 mem-id、trust、score），
   并统计**自污染率** = 出现在 prompt 中的、来源于同一 eval run 的未验证记忆条数 ÷ 注入总数，目标恒为 0；
   `clean` 同时要求 `trusted_from_same_run == 0`，因为"把同源记忆晋升到 active 换来的 0"是更坏的结果。
5. ✅ `episodes.eval_run_id` + `graded_pass` 让 eval 判分结果反过来成为 `active → verified` 的证据；
   但 `verified` 收紧为**必须先 `active_earned`**（见附录 F.3），单次 graded pass 不得跳级。

---

## 9. Observability — ✅ 已实施（`latency_ms` / `truncated` 见附录 K.4）

沿用 `EventType` 的 `domain.action` 命名，新增：

```python
MEMORY_RECALLED     = "memory.recalled"      # data: query, namespace, hits[{id,trust,score,type}], latency_ms, truncated
MEMORY_INJECTED     = "memory.injected"      # data: mem_ids, trust_mix, chars, budget_chars
MEMORY_EPISODE      = "memory.episode"       # data: episode_id, run_id, outcome
MEMORY_CONSOLIDATED = "memory.consolidated"  # data: created, updated, merged, dropped
MEMORY_TRUST        = "memory.trust"         # data: memory_id, from, to, reason, gate_evidence
```

同时：`run.json` 落一份 `memory_trace`（召回 query、命中列表、trust 分布、注入字符数），
扩展 `audit.record_memory_injection`（`agent/core/audit.py:98-127`）记录 mem-id 而不只是字符数；
dashboard 增加"记忆"页签：按 trust/type 统计、最近晋升与归档、矛盾对列表、召回延迟。

---

## 10. 配置项

下表与实现逐项核对过（`memory/flags.py`、`web/server.py`）。状态列 ✅ 表示已落地，
"未落地"表示设计位保留但当前没有对应 env，原因写在说明里。

### 10.1 开关、隔离与 eval

| 变量 | 默认 | 状态 | 说明 |
|---|---|---|---|
| `TRACE_AGENT_MEMORY` | `1` | ✅ | 总开关。eval 侧走三态判定 `memory_enabled_explicit()`：未设置=关（eval 默认），显式 `1`=开 |
| `TRACE_AGENT_MEMORY_ROOT` | 项目根推导 | ✅ | 记忆根目录，消除 `Path.cwd()` 依赖（§1.3 #11）。eval 开启记忆且未指定时，重定向到 `<output-root>/memory/eval-<hex>` |
| `TRACE_AGENT_MEMORY_STORE` | `sqlite` | ✅ | `json` / `sqlite` / `postgres`（§10.5）。非法值回落默认而不抛错——解析记忆永远不能失败掉一次分析 |
| `TRACE_AGENT_MEMORY_NAMESPACE` | `project:default` | ✅ | 隔离域。两个 adapter 都在**构造时快照**，避免同进程中途切换把一个语料劈成两半；eval 自动设为 `eval:{eval_run_id}`，显式设置优先 |
| `TRACE_AGENT_EVAL_RUN_ID` | 空 | ✅ | 由 `eval run` 铸造、`finally` 清除。写入 episode provenance，供自污染审计比对"注入的记忆是否出自本次实验" |
| `TRACE_AGENT_MEMORY_DSN` | 空 | ✅ | Postgres DSN，`store=postgres` 时必填；缺失或不可达**不抛错**，而是回落本地语料并在 `degraded_reason` 里写明原因（§10.5、附录 H.2）。驱动不在主依赖里，需 `uv sync --extra postgres`（附录 L.1） |
| `TRACE_AGENT_MEMORY_TRUST` | `active,verified` | 未落地 | 召回 trust 集合由代码常量表达，放宽只通过 `INCLUDE_CANDIDATE` 一个口子，避免两个真相来源打架 |

### 10.2 召回、打分与预算

| 变量 | 默认 | 状态 | 说明 |
|---|---|---|---|
| `TRACE_AGENT_MEMORY_INCLUDE_CANDIDATE` | `0` | ✅ | 是否低权重注入 `candidate`（含未佐证的原始 episode）。开启即接受自污染风险，审计仍会如实报告 |
| `TRACE_AGENT_MEMORY_PROMPT_CHARS` | `3000` | ✅ | 注入总预算，三段式分区各自配额（§6.4） |
| `TRACE_AGENT_MEMORY_MIN_SCORE` | `0.15` | ✅ | 相关性下限；低于此值的 hit 在 rerank 前丢弃。**同时决定走索引还是全量扫描**：仅当它 > `PHRASE_WEIGHT`(0.10) 时才用 FTS 下推，否则退回扫描以保证不漏召回 |
| `TRACE_AGENT_MEMORY_CANDIDATE_LIMIT` | `200` | ✅ | 索引交给 reranker 的候选池大小。按 store 自己的 bm25 排序，与 reranker 的覆盖率打分不同序，故须远大于 `top_k` |
| `TRACE_AGENT_MEMORY_TYPE_QUOTA` | `2` | ✅ | 第一趟每种功能 type 可占的 top-k 名额（§6.3）。第二趟按分数补满，故不会减少返回条数；`0` 关闭；负数按 `0` 处理 |
| `TRACE_AGENT_MEMORY_HALF_LIFE_DAYS` | `90` | ✅ | freshness 半衰期 |

### 10.3 Quality Gate 与生命周期

| 变量 | 默认 | 状态 | 说明 |
|---|---|---|---|
| `TRACE_AGENT_MEMORY_GATE_RUNS` | `2` | ✅ | `candidate → active` 所需的 distinct runs（按 `memory_sources` 去重计数） |
| `TRACE_AGENT_MEMORY_GATE_CONFIDENCE` | `0.6` | ✅ | `candidate → active` 最低置信 |
| `TRACE_AGENT_MEMORY_GATE_VERIFIED_RUNS` | `5` | ✅ | `active → verified` 的自动通道所需 distinct runs；且**必须先 `active_earned`**（附录 F.3） |
| `TRACE_AGENT_MEMORY_RETENTION_DAYS` | `365` | ✅ | active/verified 记忆的保留期，超期由 retention 归档 |
| `TRACE_AGENT_MEMORY_CANDIDATE_TTL_DAYS` | `180` | ✅ | candidate 的 TTL：长期拿不到第二个独立 run 的猜测应消失，而不是永远占召回候选池 |
| `TRACE_AGENT_MEMORY_DUPLICATE_SIMILARITY` | `0.92` | ✅ | 归并阈值（语义余弦），命中则合并并写 `duplicate-of` 链接 |
| `TRACE_AGENT_MEMORY_CONFLICT_SIMILARITY` | `0.60` | ✅ | 冲突检测的相似下限：同 scope 下足够像但结论不同 → `contradicts` 链接 |
| `TRACE_AGENT_MEMORY_MAINTAIN_EVERY` | `20` | ✅ | 每 N 次 dream beat 跑一次 retention/decay 全量维护 |
| `TRACE_AGENT_MEMORY_DREAM_INTERVAL` | `30` | ✅ | dashboard 后台 dreaming beat 秒数 |

### 10.4 语义检索（P3）

| 变量 | 默认 | 状态 | 说明 |
|---|---|---|---|
| `TRACE_AGENT_MEMORY_EMBEDDING` | `local` | ✅ | provider 选择：`local`（离线确定性 `HashedNgramEmbedder`）/ `http`（OpenAI 兼容，需 base URL）/ `off`。**默认非空**：语义召回是产品属性，不取决于是否恰好配了 key（附录 E.4） |
| `TRACE_AGENT_MEMORY_EMBEDDING_DIM` | `256` | ✅ | 离线 recipe 的向量维度；变更会使旧向量 stale，由 `rebuild()` 收敛 |
| `TRACE_AGENT_MEMORY_EMBEDDING_MODEL` | `text-embedding-3-small` | ✅ | `http` provider 的模型名，写入 `embedding_model` 列用于 stale 判定 |
| `TRACE_AGENT_MEMORY_EMBEDDING_BASE_URL` | 空 | ✅ | `http` provider 端点；缺失时不静默降级，而是带 reason 回报"你要了 http 但没给地址" |
| `TRACE_AGENT_MEMORY_EMBEDDING_API_KEY` | 空 | ✅ | `http` provider 凭证 |
| `TRACE_AGENT_MEMORY_EMBEDDING_TIMEOUT` | `10` | ✅ | 单次请求超时（秒） |
| `TRACE_AGENT_MEMORY_EMBEDDING_BATCH` | `16` | ✅ | 回填时每批向量数 |
| `TRACE_AGENT_MEMORY_EMBED_LIMIT` | `32` | ✅ | 每个 dream beat 最多回填多少条（有界，避免 beat 变长事务） |
| `TRACE_AGENT_MEMORY_VECTOR_LIMIT` | `50` | ✅ | 向量通道交给合并层的候选上限 |
| `TRACE_AGENT_MEMORY_MIN_SEMANTIC` | `0.35` | ✅ | 语义相似度下限，低于此值不作为独立召回理由 |

### 10.5 Postgres 存储与召回缓存（P4）

| 变量 | 默认 | 状态 | 说明 |
|---|---|---|---|
| `TRACE_AGENT_MEMORY_DSN` | 空 | ✅ | Postgres 连接串，`store=postgres` 时必填。支持 URI 与 keyword 两种写法；写进日志与 `memory stats` 前一律经 `safe_dsn()` 去密（URI 去掉 `user:pass@` 的密码段，keyword 形式保留字段名、值替换为 `<redacted>`） |
| `TRACE_AGENT_MEMORY_SCHEMA` | `public` | ✅ | 表所在 schema；非 `public` 时 DDL 自动 `CREATE SCHEMA IF NOT EXISTS` 并全限定引用，便于同库多环境共存 |
| `TRACE_AGENT_MEMORY_CONNECT_TIMEOUT` | `5` | ✅ | 连接超时（秒），注入 DSN 而不是另开连接参数；下界 1 秒，避免 `0` 变成"无限等待"把一次分析挂死 |
| `TRACE_AGENT_MEMORY_CACHE` | `off` | ✅ | `off` / `memory`（进程内 LRU）/ `redis`。默认关：正确性来自 `corpus_epoch` 失效，缓存只是读加速（§6 的 Redis 定位） |
| `TRACE_AGENT_MEMORY_CACHE_URL` | `redis://localhost:6379/0` | ✅ | `cache=redis` 的连接串；未设置即用客户端默认，单节点部署零配置 |
| `TRACE_AGENT_MEMORY_CACHE_TTL` | `300` | ✅ | 条目寿命（秒）。失效由 epoch 负责，TTL 只兜住 epoch 覆盖不到的情况（服务器时钟漂移、手工改库） |
| `TRACE_AGENT_MEMORY_CACHE_SIZE` | `256` | ✅ | 进程内 LRU 条目上限，防止 dashboard 轮询多个 namespace 时无界增长 |

pgvector 不是配置项而是**探测结果**：`apply_schema` 连上后查 `pg_available_extensions`，
有扩展则 embedding 列建为 `vector(dim)`，无则退化为 `BYTEA` + 应用层余弦。
两条路都能召回（`memory stats` 的 `vectors` 恒为真），区别只在 ANN 索引，
`pgvector` 字段报告实际生效的是哪一种（附录 H.1）。

---

## 11. 分阶段路线图

### P0 · 止血（纯逻辑，不改存储，1~2 天） — ✅ 已实施（见附录 C）

**改动**

1. `text.py`：`tokenize()` 增加 CJK bigram 展开，写入/查询共用；
2. `recall.py`：去掉 `min(score, 1.0)` 饱和；引入 `trust_prior`、`confidence`、freshness 的最小加权；
   curated 与其来源 episode 去重；
3. `consolidation.py`：`_promote()` 不再因重复写入而 `occurrences += 1`，改为按 distinct run 派生；
   `validated_by="pending"` 哨兵移除，改由 `status_reason` 表达；
4. `analyze.py:_remember_episode`：删除同一条 episode 的二次 enqueue（改 enqueue gate_review）；
5. `recall.py` / `runtime.py`：默认只召回 `active`，`candidate` 需显式开关；
6. `eval/**`：默认关闭记忆，或强制 eval namespace。

**验收**

- 附录 A 脚本输出：单次 run 后记忆仍为 `candidate`、`distinct_runs=1`；
- `'冷启动 白屏'`、`'启动白屏问题排查'` 对含 `'冷启动白屏'` 的记忆 recall@5 ≥ 1；
- 同一 prompt 中不再出现同源重复条目；
- 现有 `tests/test_memory.py`、`tests/test_memory_v2.py` 全绿。

### P1 · 存储底座与索引（3~5 天） — ✅ 已实施（见附录 D）

`MemoryStore` Port + `SqliteStore`（第 4 节 DDL）+ FTS5/bigram 索引 + `JsonStore` 适配 +
`memory migrate/reindex/stats` CLI + 迁移脚本（JSON → SQLite，双向可导出）。
**验收**：1 万条记忆下召回 P95 < 150ms；迁移后 `stats` 与 JSON 计数一致；`TRACE_AGENT_MEMORY_STORE=json` 可回滚。

实测结果：10 000 条记忆下 `store.search` **P50 = 19.7ms / P95 = 30.9ms / P99 = 39.3ms**（阈值 150ms，
余量约 5 倍）；JSON → SQLite 迁移后 `stats` 的 memories/episodes/sessions/by_trust/by_kind 与源端逐项相等，
重跑不产生重复行、不抬升 `distinct_runs`；`TRACE_AGENT_MEMORY_STORE=json` 与 SQLite 对同一批查询返回
**完全相同**的 hit id 与分数。详见附录 D。

### P2 · 生命周期与 Trust 治理（3~5 天） — ✅ 已实施

Quality Gate、Promoter、Conflict Detector、`memory_versions`/`memory_links`、Retention/Decay、
Composer 三段式注入、`memory.*` 事件与 `run.json.memory_trace`、dashboard 记忆页签。
**验收**：构造"两次独立 run 一致 → active"、"出现反证 → 降级 archived"、
"candidate 不得高权重注入"三组测试；trust 跃迁 100% 有 version 与 reason 记录。

实测结果：`tests/test_memory_lifecycle.py` 覆盖"两次独立 run 一致 → active"（含佐证带 supports 路径）、
"出现反证 → 降级/归档"、"candidate 不得高权重注入"、trust 跃迁 100% 带 version+reason+actor；
dashboard 记忆页签（`/api/memory/overview` + `POST /api/memory/maintain`）展示 trust 分布、版本、
矛盾/佐证/归并链接与待处理队列。佐证带（见 5.3）闭合后，两条措辞不同但结论一致的独立 run 可经
`supports` 链接互相佐证并晋升 `active`，reason 明确标注 "via supports link"、actor 为 conflict-detector。

### P3 · 语义检索（5~8 天，按触发条件启动） — ✅ 已实施（见附录 E）

`EmbeddingProvider` Port（`memory/embedding/ports.py`）、离线确定性 provider
（`HashedNgramEmbedder`，默认 `local`）、OpenAI 兼容 HTTP provider（`http`，仅经环境变量开启）、
`EmbeddingCache` 查询向量 LRU、`SemanticIndex` 作为 provider 与 store 之间的唯一接缝、
`embedding_model` 变更时的 `rebuild()/prune_stale()`、向量+lexical 混合召回
（`RecallService._hybrid_candidates`）、dreaming beat 的 `embed` job 异步回填、
`memory embed` CLI 与 `memory stats` / dashboard 的 coverage 展示。
**验收**：同义改写查询集 recall@5 ≥ 0.8；embedding 不可用时自动退化为 lexical-only 且不报错。
两条范围裁剪（有意为之，详见附录 E.4）：`PostgresStore`+pgvector 推迟（触发条件未满足）；
`lifecycle/conflicts.py` 保持纯词法、不引入语义 OR-gate。

### Eval 隔离与自污染审计（跨 P0/P2/P3） — ✅ 已实施（见附录 F）

§8 的五条全部落地：eval 默认关记忆、开启时自动重定向到独立记忆根并打上 `eval:{eval_run_id}`
namespace、`run.json.memory_trace` 逐 trial 记录注入的 record 与 trust、eval 报告输出
**自污染率**与 `clean` 标志、`graded_pass` 作为 `active → verified` 的证据通道。
另外补齐了 §7 trust 契约的最后一个缺口：**episode 通道同样受闸门约束**——未佐证的原始
episode 不再被当作"历史相似案例"注入（此前它是绕过 candidate 闸门的后门）。
**验收**：默认配置下冒烟 eval 的 `self-contamination` 恒为 `0.0 [clean]`；
显式 `INCLUDE_CANDIDATE=1` 时审计必须报 `CONTAMINATED` 并给出可核对的 reason（不能因为
操作员主动放宽闸门就失去诚实性）；`graded_pass=False` 与 `None` 都不得成为 verified 证据。

### P4 · 多实例与缓存（按需） — ✅ 已实施（见附录 H）

`PostgresStore`（tsvector 全文检索 + CJK bigram、可选 pgvector、schema 版本戳、连接超时与去密 DSN）、
`RecallCache`（`off` / 进程内 LRU / Redis 三态，按 `corpus_epoch` 整域失效）、
`memory namespaces` 与 `memory backup` 两个运维命令、跨适配器 export/import 分发，
以及"postgres 不可达即带 reason 回落本地语料"的降级路径。
**验收**：缓存命中率与失效策略可观测；缓存不可用时链路仍正确（source of truth 始终在 SQL）。

实测结果：真实 PostgreSQL 17.10 上，同一套治理链（consolidate → 冲突检测 → gate → 分区注入）
给出与 SQLite 相同的判定，中文改写召回命中并按 trust 分区注入（附录 H.1）；
`tests/test_memory_postgres.py` 42 项全绿，其中 2 项 live-server 用例在未设 DSN 时**显式 skip**
而不是静默通过；本地 Redis 7.2.5 下 `cache=redis` 的命中、epoch 失效、服务不可达降级三条路径实测通过
（`tests/test_memory_cache.py` 24 项）。范围裁剪：未实现分布式锁与 namespace 级鉴权，理由见附录 H.4。

---

## 12. 迁移与兼容

1. `MemoryRuntime` 公开方法签名保持向后兼容（`recall / remember_episode / append_session_message / resume_plan / list_*`），
   内部改为委托 Retriever + Composer；`RecallService` 保留为薄壳，`web/server.py` 与测试无需改动；
2. 迁移命令幂等：`diting-agent memory migrate --from json --to sqlite [--dry-run]`，
   以 `(namespace, memory_id)` 为键 upsert，`memory_sources` 从 `provenance.references` 里的 `run:*` 反推；
   **审计轨迹（`memory_versions` / `memory_links`）随 bundle 一起搬运**，且导入顺序在记录之前，
   重放不增长（见附录 J）；
3. 旧字段兼容：`MemoryStatus` 保留，读取时映射到 `MemoryTrust`；`CuratedMemory` 仍可序列化导出为 JSON；
4. 历史数据默认全部降级为 `candidate`，需重新过 gate 才能回到 `active`
   （因为旧 `occurrences` 不可信，见 1.2 缺陷 A）——一次性成本换取记忆库可信。
   **实施口径收窄为“降级无法解释的信任”**：审计轨迹能证明这一跳、或自身证据重跑闸门仍能到
   同一档的记录原样保留，派生摘要豁免，`--keep-trust` 可整体关闭。理由与规则表见附录 K.2。

---

## 13. 风险与回滚

| 风险 | 缓解 |
|---|---|
| bigram 索引膨胀、召回噪声上升 | bm25 + `min_score` 阈值 + type 配额；索引仅覆盖 topic/content/keywords |
| gate 过严导致记忆库长期全是 candidate | `memory stats` 暴露 trust 分布；阈值全部可配置；verified 支持人工签署通道 |
| SQLite 并发写冲突（web + CLI 同时跑） | WAL 模式 + 短事务 + 写入失败降级为跳过（保持 best-effort 语义） |
| 迁移丢数据 | 迁移前自动导出 `memory-export-<ts>.json`；`--dry-run`；JsonStore 可回滚 |
| embedding 成本/延迟 | 仅在 dreaming beat 异步生成；召回侧带缓存；不可用时退化 lexical-only |
| prompt 预算被记忆挤占 | 三段式配额 + 现有 3000 字上限 + `_truncate_items` 整条截断（已有） |
| Postgres 不可达 / DSN 配错 | 不抛错：回落本地语料继续跑，`degraded_reason` 写明原因并由 `memory stats`、dashboard 暴露（附录 H.2） |
| 缓存服务挂了或返回脏数据 | 缓存只是读加速：任何异常都退化为无缓存召回；失效由 `corpus_epoch` 负责，TTL 仅兜住时钟漂移与手工改库 |
| 备份产物被误当成可恢复的语料 | 每个产物带 `kind` 与恢复命令，manifest 记录 sha256/条数/`stats()`；SQLite 快照是整库副本这一点在 CLI 与 manifest 里显式声明（附录 H.3） |

---

## 14. 成功度量

| 指标 | 改前（实测） | 目标 | 当前（实测） |
|---|---|---|---|
| CJK 同义改写 recall@5 | 0（`'冷启动 白屏'` 0 命中） | ≥ 0.8 | **1.0**（改写句均命中，score 0.497~0.631）；语义通道再补 0.41~0.64 余弦，无关句 0 命中（附录 C.2 / E.2） |
| 单次 run 直通 active 比例 | 100% | 0 | **0**（附录 A 重跑为 `candidate` / distinct_runs=1；episode 通道同样受闸门，附录 F） |
| eval 自污染率（prompt 含同 eval 内未验证记忆） | 未隔离，>0 | 0 | **0.0 `[clean]`**（默认配置冒烟，附录 F.2）；主动 `INCLUDE_CANDIDATE=1` 时审计如实报 `1.0 [CONTAMINATED]` |
| 召回延迟 P95 @1 万条 | 全量扫盘，随条数线性 | < 150ms | **30.9ms**（P50 19.7 / P99 39.3，余量约 5 倍，附录 D.2） |
| top-k 分数区分度（并列 1.0 的比例） | 高（实测多条 1.0） | < 5% | **0**（加权和天然有界，实测区间 0.497~0.666，无并列） |
| trust 跃迁可审计率 | 0（无版本表） | 100% | **100%**（`memory_versions` 逐条带 reason + actor，dashboard 可查） |
| 开启记忆后的 eval 诊断一致率 | 无 A/B | 相对 baseline 提升且不引入回归 | **0.40 vs 0.40（Δ+0.0，等价）**：claude 真实 provider 两轮 A/B（附录 I.2），两组 `self_contamination_rate=0.0 [clean]`，开启组注入 1 条 episodic 先验（194 字符）；对照可信但本批次未见分数收益（生产库 46 条全 candidate，无 active/verified 先验可注入） |

A/B 方法：同一批用例跑 `TRACE_AGENT_MEMORY` 未设置（关）与 `=1`（开）两组，比较 grader 通过率、
root cause 命中率、平均工具调用次数；结果写入 eval 报告，作为 P2/P3 是否继续投入的依据。
两组的 `self_contamination_rate` 与 `clean` 是先决条件——对照被污染时，分数差异无法归因于记忆收益。

---

## 附录 A：缺陷复现脚本

```python
# 单次 run 直通 active（缺陷 A）
import sys, tempfile
from pathlib import Path
sys.path.insert(0, "src")
from trace_agent.memory.models import EpisodicMemory
from trace_agent.memory.runtime import MemoryRuntime

runtime = MemoryRuntime(Path(tempfile.mkdtemp()) / "memory")
episode = EpisodicMemory(
    episode_id="ep-probe-1", run_id="run-probe-1", trace_id="t",
    scenario_type="white_screen", scenario="white screen after launch",
    symptom="app shows blank page", summary="suspected main thread block",
    root_causes=["main thread blocked by sync IO"],
    findings=[{"title": "Main thread blocked by sync IO", "severity": "high",
               "status": "suspected", "confidence": 0.45,
               "analysis": "sync file read on main thread during launch",
               "recommendation": "", "evidence_ids": ["ev-1"]}],
)
runtime.remember_episode(episode, consolidate=True)     # candidate, occurrences=1
runtime.dreaming.enqueue_episode(episode.episode_id)    # analyze.py 的真实行为
runtime.dreaming.run_once(limit=20)
for m in runtime.list_curated():
    print(m.memory_id, m.status.value, m.occurrences, m.confidence, m.provenance.validated_by)
# 实测输出：mem-white_screen-45a0c1fa9553 active 2 0.45 pending
```

```python
# CJK 召回失效（缺陷 B）：写入 scenario="冷启动白屏" 后
for q in ("冷启动白屏", "冷启动 白屏", "启动白屏问题排查", "白屏"):
    print(q, len(runtime.recall(q, scenario_type="cold_start").hits))
# 实测输出：2 / 0 / 0 / 2(仅 0.25 分)
```

## 附录 B：SQLite FTS5 分词实测

```text
sqlite 3.45.3
tokenize='trigram'                 : '白屏问'->1  '白屏'(2字)->0  '启动'->0
unicode61 + 客户端 CJK bigram 展开   : '白屏'->1  '冷启'->1  '启动'->2  '耗时'->1
注意：MATCH 默认隐式 AND，多词查询需显式 OR + bm25 排序
```

## 附录 C：P0 实施记录（已完成）

> 本节回填 §11 P0 与 §14 度量的实测结果。P0 为纯逻辑改动：**未引入任何新依赖、未改动存储底座**
> （仍为 `JsonMemoryBackend`），因此可随时回滚，且不影响 §12.1 的公开 API 兼容性。

### C.1 实际改动清单

| §11 P0 项 | 落地文件 | 关键实现 |
|---|---|---|
| 1. CJK bigram 分词 | `memory/text.py` | 新增 `cjk_bigrams()`；`tokenize()` 把连续 CJK 展开为重叠 bigram，写入/查询共用同一函数；移除失效的 `_keyword_score`/`_phrase_bonus` |
| 2. 去饱和 + 加权 + 去重 | `memory/recall.py`、`memory/rerank.py`(新) | 去掉 `min(score,1.0)`；`rerank.py` 提供 `lexical_score`、`freshness`(真半衰期 `0.5**(age/hl)`)、`weighted_score`、`TRUST_PRIOR`；`_dedupe_hits` 剔除被 curated 覆盖的同源 episode，并对同 topic 收敛 |
| 3. promote 幂等 | `memory/consolidation.py` | `_promote()` 改为按 provenance / `confirmed_run_ids` 并集派生 `distinct_runs`，重放同一 run 不再 `occurrences += 1`；移除 `validated_by="pending"` 哨兵，改由 `status_reason` 表达 |
| 4. 拆双重整合 | `application/analyze.py` | `_remember_episode` 删除二次 `enqueue_episode`，改为 `enqueue_gate_review`（dreaming beat 只评估 gate，不再重复 consolidate 同一 run） |
| 5. 默认只召回 active | `memory/recall.py`、`memory/deep_recall.py`、`memory/flags.py` | `_allowed_statuses()` 默认 `TRUSTED_STATUSES`；candidate 需 `TRACE_AGENT_MEMORY_INCLUDE_CANDIDATE=1` 显式开启，注入时单列 “Unverified candidate” 区块并标注 `[role | trust conf= runs=]` |
| 6. eval 隔离 | `eval/cli.py`、`memory/paths.py` | eval 默认关闭记忆；开启时自动重定向到 `<output_root>/memory/eval-<hex>` 隔离根；`TRACE_AGENT_MEMORY_ROOT` 全局可覆盖记忆根，消除 `Path.cwd()` 依赖（#11） |

**新增模块**：`memory/quality_gate.py`（`evaluate_trust`/`apply_trust_gate`：contradictions≥2→archived、真实签署→verified、`distinct_runs≥2 且 confidence≥0.6 且至少一个 confirmed/observed 来源`→active，否则维持 candidate；retired 不复活；legacy `pending` 哨兵不能伪造 verified）、`memory/rerank.py`（打分与权重）。

**模型/配置增量**：`memory/models.py` 增 `MemoryStatus.VERIFIED`、`TRUSTED_STATUSES`/`RETIRED_STATUSES`、`confirmed_run_ids`/`status_reason` 字段与 `distinct_runs`/`source_run_ids`/`source_episode_ids` 派生属性；`memory/flags.py` 增 §10 配置表中的 gate/half-life/min-score/include-candidate/root 环境变量。

### C.2 实测结果（对照 §14）

| 指标 | 改前 | 改后（实测） |
|---|---|---|
| 单次 run 直通 active | 100%（附录 A：`active` / occ=2 / conf=0.45） | **0**（附录 A 重跑：`candidate` / distinct_runs=1 / conf=0.45） |
| 两次独立 confirmed run → active | 无法区分 | 通过 gate 晋升 `active` / distinct_runs=2 |
| CJK `'冷启动 白屏'` recall@5 | 0 | **1**（score 0.631） |
| CJK `'启动白屏问题排查'` recall@5 | 0 | **1**（score 0.497） |
| CJK 无关查询 `'帧率卡顿掉帧'` | — | 正确返回 **0**（未误召回） |
| top-k 分数并列 1.0 | 多条 | 消除（加权和天然有界，实测 0.497~0.666，无并列 1.0） |
| 同源重复注入（curated + 其 episode） | 有 | 去重，同一 prompt 不再重复占预算 |
| eval 自污染 | 未隔离，>0 | 默认隔离 / 关闭 |

### C.3 测试

- `tests/test_memory.py`、`tests/test_memory_v2.py` 全绿；
- 新增 `tests/test_memory_trust.py`（**19** 项）：gate 阈值、contradiction→archived、retired 不复活、legacy `pending` 哨兵 ≠ verified、真实签署 → verified、单次 run 保持 candidate（走 analyze 真实序列）、重放 / deep-promote 不能绕过 gate、candidate 默认排除 / 开启时标注、curated 覆盖 episode 去重、5 组 CJK 查询 + 无关查询返回空、分数有界、rerank 排序、真半衰期、`MEMORY_ROOT` 覆盖、eval 隔离；
- memory 相关测试合计 **33** 项通过。
- 全量套件仅 4 项失败（`test_eval_contracts`、`test_grader_robustness`×2、`test_sdk_pluggability`）：经 `git worktree add HEAD` 在干净 HEAD 上复现同样失败，确认为 P0 之前既有、与 memory 无关（golden-case yaml 含 2 个 root_causes + 环境已安装 openai SDK）。

### C.4 与设计稿的偏差

1. §11 P0 第 6 项原设想用 `--memory` CLI 开关。实际采用**环境变量**方案：eval 默认关闭记忆，需 `TRACE_AGENT_MEMORY=1` 显式开启，开启后自动落到隔离根。原因是 `eval run` 命令签名中的中文 help 字符串使新增 typer flag 易出错，且环境变量与 §10 配置表一致、便于 A/B 脚本化。功能等价，仅入口形态不同。
2. §11 P0 第 5 项提到改 `runtime.py`；实际 trust 过滤落在检索层（`recall.py`/`deep_recall.py`），`runtime.py` 仅做委托，公开签名不变（满足 §12.1 向后兼容）。

### C.5 下一步

P1（SQLite `MemoryStore` Port + FTS5/bigram 索引 + 迁移 CLI）为自然下一阶段：表结构见 §4，切换 PostgreSQL+pgvector 的触发条件见 §3.1。P0 已修复“信任闸门 + 召回语义”这两个与存储无关的核心缺陷，P1 起更换底座不会改变此处已验证的行为契约。

> **更新**：P1 已实施完毕，落地内容、实测数据与偏差见附录 D。本节判断得到验证——P0 固定的行为契约在换底座后逐条保持不变（现由 `tests/test_memory_store.py` 的适配器对等测试持续守护）。

---

## 附录 D：P1 实施记录（存储底座与索引）

### D.1 交付内容

| 模块 | 作用 |
|---|---|
| `memory/ports.py` | `MemoryStore` Protocol + `MemoryQuery` / `ScoredMemory` / `MemoryStats` / `MemoryType` / `MemoryTrust`。Port **包含**旧 backend 全部读写方法，因此适配器是 drop-in 的；只有 `search` 是可选成员（用 `store_supports_pushdown()` 探测） |
| `memory/store/schema.py` | 第 4 节 DDL 的 SQLite 实现，`SCHEMA_VERSION=1`，`apply_schema()` 幂等 |
| `memory/store/sqlite_store.py` | 默认适配器。thread-local 连接 + WAL + `BEGIN IMMEDIATE` + 可重入 `_tx()`；FTS5 过滤下推；治理面（`add_source` 幂等、`distinct_runs` 派生、`set_trust`、`link`、`touch`、`expire`、`stats`、`reindex`、`batch`） |
| `memory/store/json_store.py` | 回滚适配器，继承既有 `JsonMemoryBackend`；**故意不实现 `search`**（回滚就该是回滚：全量扫描）；治理事件落 `governance/*.jsonl` 边车 |
| `memory/store/fts.py` | `fts_body()` / `fts_match()`：写入侧与查询侧共用同一 CJK bigram 展开，多词显式 `OR` |
| `memory/store/__init__.py` | `build_memory_store()`：构造时探测 schema 版本，FTS5 缺失/根目录不可写时**降级到 JsonStore 并记录 `degraded_reason`**；`store_diagnostics()` |
| `memory/migrate.py` | `export_bundle` / `write_export` / `import_bundle` / `migrate_corpus` / `migrate_store` / `auto_backup`，返回结构化 `MigrationReport` |
| `memory/cli.py` | `diting-agent memory {stats,migrate,reindex,export,import}` |
| `tests/test_memory_store.py` | 20 项 P1 验收测试 |

服务层改动：`consolidation.py` / `session.py` / `dreaming.py` / `deep_recall.py` / `execution.py` 的构造参数类型从 `JsonMemoryBackend` 改为 `MemoryStore`；`MemoryRuntime` 增加 `store=` / `namespace=` 注入参数与 `store` 属性别名，**公开方法签名不变**（满足 §12.1）。

### D.2 实测数据

**检索延迟（10 000 条记忆，300 次查询，`limit=200`）**

```text
store.search  P50 = 19.7 ms   P95 = 30.9 ms   P99 = 39.3 ms   max = 42.2 ms
```

对比 P1 前：每次 recall 反序列化 `curated/` + `episodes/` 下全部 JSON 文件，延迟随条数线性增长。
测试中的阈值取设计稿的 150ms，实测余量约 5 倍（CI 机器慢于开发机，宁可阈值松也不要 flaky）。

**写入 / 迁移吞吐**

```text
逐条事务：  ~0.92 ms/行  → 10 000 条约 9.2 s
batch() 单事务：10 000 条约 6.0 s（含 pydantic 序列化与 FTS bigram 建索引）
```

因此新增 `SqliteStore.batch()`：`_tx()` 改为可重入，嵌套调用共享外层事务、不重复提交。
`import_bundle` 探测到 `batch` 就用它——迁移是原子的，中途失败整体回滚，不会留下"迁移了一半"的语料。

**CLI 实测输出（真实运行，非构造）**

```text
$ diting-agent memory migrate --from json --to sqlite --root <root>
来源     : json
目标     : sqlite
命名空间 : project:default
记忆     : 1
片段     : 0
会话     : 0
备份     : <root>\backups\memory-export-20260926T121029Z.json
校验     : 已比对目标 stats 计数
耗时     : 7.6 ms
完成。切换方式：设置 TRACE_AGENT_MEMORY_STORE=sqlite
```

### D.3 验收逐条对照

| §11 P1 验收项 | 结果 | 守护测试 |
|---|---|---|
| 1 万条记忆召回 P95 < 150ms | ✅ P95 = 30.9ms | `test_recall_latency_at_ten_thousand_memories` |
| 迁移后 `stats` 与 JSON 计数一致 | ✅ memories/episodes/sessions/by_trust/by_kind 逐项相等 | `test_migration_preserves_counts_and_is_idempotent` |
| `TRACE_AGENT_MEMORY_STORE=json` 可回滚 | ✅ 且回滚后 CJK 召回仍正确 | `test_store_environment_variable_selects_the_adapter`、`test_json_rollback_still_recalls` |
| JSON 与 SQLite 对同一查询返回相同结果 | ✅ hit id **与分数**逐项相等（时间戳已钉死，排除 freshness 漂移） | `test_json_and_sqlite_recall_are_identical` |
| 索引不改变答案，只改变速度 | ✅ 同一 store 上 pushdown 与 scan 结果一致 | `test_pushdown_and_scan_agree_on_one_store` |
| 双向可导出 | ✅ SQLite → JSON 快照 + 导入，`updated_at` 不失真 | `test_export_import_roundtrip_between_adapters`、`test_migration_preserves_updated_at` |
| 迁移幂等 | ✅ 重跑不增行、不抬 `distinct_runs` | `test_migration_preserves_counts_and_is_idempotent`、`test_governance_counters_agree_across_adapters` |
| namespace 隔离在存储层生效 | ✅ | `test_namespaces_do_not_cross` |
| 分词/索引字段变更后可恢复 | ✅ `reindex()` 从 payload 重建，不复用旧索引 | `test_reindex_rebuilds_the_full_text_index` |
| 现有 memory 测试全绿 | ✅ 53 项（含新增 20 项） | — |

**全量套件**：153 项，148 通过 / 1 跳过 / 4 失败。4 项失败与 P0 阶段完全相同（`test_eval_contracts`、`test_grader_robustness`×2、`test_sdk_pluggability`），三个文件**零处** memory 引用，成因是工作区里新增的 `evals/cases/*` golden 含 2 个 root_causes、以及本机已安装 openai SDK，与本次改动无关。

### D.4 与设计稿的偏差

1. **`--root` 语义**：`memory` 子命令的 `--root` 指**记忆根目录**（含 `memory.db` 与 `curated/`），不是项目根。若复用 `resolve_memory_root()` 会在其后追加 `.trace-agent/memory`，导致迁移命令对着一个空语料操作——这是迁移工具最坏的失败模式，故显式区分。
2. **`episodes` 的 run 折叠**：`UNIQUE(namespace, run_id)` 会让"同一 run 的两份投影"合并成一行。这是设计意图（一个 run 只投影一次），但会让计数校验看起来像丢数据，因此 `MigrationReport.collapsed_runs` 单独计数并从期望值中扣除，而不是悄悄吞掉。
3. **dry-run 仍会创建空库文件**：`build_memory_store()` 需要连接才能探测 FTS5 是否可用，探测即建文件。测试把这一点**固定下来**（断言文件存在但 0 条记录），避免日后被当成 bug 反复"修复"。
4. **新增 `batch()`**：设计稿未列。逐条事务下 10 000 条迁移要约 9 秒，且失败会留下半成品语料；改为单事务后既快又原子。
5. **`JsonStore` 补 `import_curated` / `import_session`**：`write_curated` 会重打 `updated_at`，而 freshness 是排序信号，SQLite → JSON 导出若重打时间戳会静默改变回滚后的召回顺序。

### D.5 下一步

P2（生命周期与 Trust 治理）的存储依赖已全部就位：`memory_versions` / `memory_links` / `memory_sources` 三张表、`set_trust` / `link` / `add_source` / `distinct_runs` / `expire` / `touch` 六个治理方法、以及 `status_reason` 列都已在两个适配器上实现并有测试覆盖。P2 只需在其上写 Quality Gate / Promoter / Conflict Detector / Retention 的业务规则，以及 `memory.*` 事件与三段式 Composer。

PostgreSQL + pgvector（P3）的切换触发条件见 §3.1；由于表结构与 Port 从 P1 起就是最终形态，`PostgresStore` 是同一 Port 的第三个适配器，不需要再动服务层。

---

## 附录 E：P3 实施记录（语义检索）

### E.1 交付清单

| 模块 | 内容 |
|---|---|
| `memory/embedding/ports.py` | `EmbeddingProvider` Protocol；`cosine`（clamp 到 [0,1]）；`pack/unpack_vector`（float32 小端）；`round_trip`（量化到存储宽度，保证双适配器余弦逐位一致） |
| `memory/embedding/local.py` | `HashedNgramEmbedder`：`RECIPE=hashed-cjk-v1`，默认 dim=256，model 名内嵌 dim（`hashed-cjk-v1:d256`）；token+unigram+概念词三类特征，概念词权重最高以连接中英 |
| `memory/embedding/remote.py` | `HttpEmbeddingProvider`：OpenAI 兼容 `/embeddings`，失败返回 `None` 不抛 |
| `memory/embedding/cache.py` | `EmbeddingCache`：按 model 分桶的查询向量 LRU |
| `memory/embedding/index.py` | `SemanticIndex`：`available/model/dim/query_vector/candidates/vectors_for/similarity_for/backfill/rebuild/prune_stale/coverage/clear_cache`；provider 与 store 均可能缺失，全部方法在 `available=False` 时返回空答案 |
| `memory/embedding/factory.py` | `build_embedding_provider() -> EmbeddingSetup(provider, kind, reason)`，永不抛；`reason` 解释"请求了但不可用"与"从未请求"的区别 |
| `store/{json,sqlite}_store.py` | 两个适配器补齐 `pending_embeddings/set_embedding/vectors_for/vector_search/prune_embeddings/embedding_coverage` |
| `memory/recall.py` | `_hybrid_candidates`：lexical 池与 vector 通道合并，同记录只出现一次且只带一个余弦；`_weights_for`：仅当有候选 `semantic>0` 才切 `SEMANTIC_WEIGHTS` |
| `memory/dreaming.py` | `JOB_EMBED` + `enqueue_embed` + `_run_embed`；`maintain()` 末位回填；`DreamBatch.embedded` |
| `application/analyze.py` | `_remember_episode` 追加 `enqueue_embed()`；`_memory_trace` 记录每个 hit 的 `semantic` |
| `memory/cli.py` | `memory embed` 命令（`--limit/--rebuild/--json`）；`memory stats` 并入 coverage；`maintain` 传入 semantic |
| `web/server.py` + `web/static/app.js` | `_memory_overview` 增加 `embedding=coverage()`；记忆页签新增"语义索引"卡片、`JOB_LABEL.embed`、batch 摘要"向量回填" |

### E.2 实测（探针，离线 `local` provider，dim=128/256）

```text
改写句 vs 基准句余弦：冷启动 白屏 0.66 / 启动白屏问题排查 0.42 /
                     应用冷启动后长时间白屏怎么排查 0.49 / 冷启动白屏 0.64 /
                     主线程阻塞导致启动白屏 0.60
无关句余弦        ：帧率卡顿掉帧 0.03 / GPU 进程崩溃未上报帧 0.00 / 内存泄漏 OOM 崩溃 0.00
beat 前 recall    ：纯词法，semantic=0（DEFAULT_WEIGHTS）
maintain() 后     ：memories/episodes 全量回填；改写查询 semantic 0.41~0.64；无关句 0 命中
双适配器 parity   ：vector_search 余弦逐位相同（0.43378637），最终召回分数逐位相同
模型变更 128→64   ：stale=3 → rebuild → stale=0、models 收敛为单一新模型
无 provider/抛错  ：recall 仍返回词法命中，semantic=0，全程不抛
```

### E.3 验收逐条对照

| §11 P3 验收项 | 结果 | 守护测试 |
|---|---|---|
| 同义改写查询集 recall@5 ≥ 0.8 | ✅ 三个改写查询均命中且 `semantic ≥ 0.4` | `test_paraphrase_recalls_after_backfill`、`test_semantic_beats_pure_lexical_on_rewording` |
| embedding 不可用时退化 lexical-only 且不报错 | ✅ 无 provider 与抛错 provider 两路均验证 | `test_semantic_degrades_without_a_provider`、`test_raising_provider_degrades_to_lexical` |
| 未回填语料不被静默重排 | ✅ 与纯词法打分逐位相同 | `test_unembedded_corpus_scores_identically_to_lexical_only` |
| 双适配器向量 parity | ✅ id/余弦/最终分数逐项相等 | `test_vector_search_parity_across_adapters`、`test_recall_parity_across_adapters_with_semantics` |
| `embedding_model` 变更触发重建 | ✅ stale 计数 + rebuild/prune 收敛 | `test_model_change_marks_corpus_stale_then_rebuilds`、`test_prune_stale_drops_other_models` |
| 回填有界、走 dreaming beat | ✅ `backfill(limit=1)` 每次只嵌一条 | `test_backfill_respects_the_per_beat_limit`、`test_maintain_backfills_the_corpus` |
| 无关句不被语义拉入 | ✅ 无关句 0 命中；provider 层余弦 ≤0.10 | `test_unrelated_query_finds_nothing`、`test_offline_recipe_separates_paraphrase_from_unrelated` |
| coverage 可观测 | ✅ `memory stats` / dashboard / `run.json` 同源 | `test_coverage_reports_embedding_state` |

### E.4 与设计稿的偏差

1. **`PostgresStore` + pgvector 推迟**：§3.1 的四个触发条件（多写者并发 / >50 万条需 ANN / 跨项目权限治理 / 与业务库联表）目前一个都不满足，单机 SQLite + 精确向量检索已覆盖当前量级。`MemoryStore` Port 已预留全部向量方法，触发条件满足时新增第三个适配器即可，不动服务层。
2. **`lifecycle/conflicts.py` 保持纯词法，不引入语义 OR-gate**：`supports` 判定的两条 fixture 仅一词之差（cold/warm launch，词法 Jaccard≈0.86），而本地 `HashedNgramEmbedder` 对这种近似串余弦≈1.0；若用"余弦≥0.92 判 duplicate"的语义门，会把"独立案例互相佐证"误判为"重复归并"，击穿 P2 的 supports→active 链路。语义相似度只用于**召回**与显式 duplicate 归并（阈值 0.92），不参与 conflict/support 判定。
3. **默认 provider 为 `local` 而非 `http`**：离线、确定性、零依赖，使"语义召回可用"成为产品属性而非"恰好配了 key"。`http` 仅经 `TRACE_AGENT_MEMORY_EMBEDDING=http` + base URL 显式开启。

### E.5 下一步

P4（多实例与缓存）：Redis 缓存层、多 namespace 权限治理、记忆导入导出与备份。在此之前，单进程阶段用进程内 LRU（`EmbeddingCache`）+ SQLite 页缓存即可。若 §3.1 触发条件先于 P4 满足，则优先做 `PostgresStore`。

---

## 附录 F：Eval 隔离、自污染审计与 episode 闸门（§7/§8 实施记录）

### F.1 交付清单

| 模块 | 内容 |
|---|---|
| `memory/lifecycle/contamination.py` | `audit_injection()` / `ContaminationAudit`：给定一个 trial 注入的 record id 与 prompt 实际呈现的 trust，回答"其中哪些出自本次 eval run"，并按未验证 / 已信任同源分桶 |
| `memory/eval_probe.py` | `MemoryEvalProbe`：harness 唯一能碰的记忆面，只有 `mark_graded` / `review` / `audit` 三个动词（不能 consolidate、promote、编辑、删除）；`build_eval_probe()` 在记忆关闭、无 run id、store 打不开时一律返回 `None` |
| `eval/memory_audit.py` | `MemoryProbe` Protocol（结构化，eval 包永不 import store）、`record_trial_memory()`（先审计后打分，顺序即语义）、`summarize_memory()`（§14 headline 指标）、`_parse_contamination()` |
| `eval/models.py` | `TrialMemoryHit` / `TrialMemory` / `MemoryContamination` 三个 StrictModel；`TrialMemory.from_trace()` 把 `run.json.memory_trace` 投影成报告对象，分量分数经 `_clamp_unit` 钳制到字段声明的 `[0,1]` |
| `eval/runner.py` / `eval/harness.py` | 每个 trial 读取自身 `memory_trace` 并调用 `record_trial_memory`，成功与失败两条路径都走（失败 trial 同样需要被审计） |
| `eval/cli.py` | `_prepare_eval_memory()`（默认关 + 独立记忆根）、`_eval_namespace()`（run 级 namespace，显式设置优先）、`_eval_memory_env()`（contextmanager：在同一个作用域内铸造 namespace、构建 probe、打上 `TRACE_AGENT_EVAL_RUN_ID`，并在正常与异常两条路径上都恢复环境）、`_echo_memory()`（`clean` 也打印，附 namespace） |
| `memory/rerank.py` | `episode_is_corroborated` / `episode_trust` / `episode_trust_prior`：episode 通道的派生 trust（封顶 `active`） |
| `memory/models.py` | `CORROBORATED_FINDING_STATUSES = {"confirmed", "observed"}` 单一常量，`episode.py`、`consolidation.py`、`rerank.py` 共用 |
| `memory/recall.py` | `_episode_admitted()`：在 `_scan_candidates`、SQL 下推、向量合并三处一致过滤未佐证 episode |
| `memory/context/composer.py` | `_PARTITION_FOR`：按**派生** trust 把 episode 路由进 candidate / active 分区，并在行内显示 `episode {trust}` |
| `memory/deep_recall.py` | 同一闸门，未佐证 episode 标注 "unverified candidate" |
| `application/analyze.py` | `_memory_trace` 记录每个 hit 的派生 trust（此前 episode 一律硬编码 `"episodic"`，审计拿不到可信输入） |
| `memory/store/{json,sqlite}_store.py` | `mark_episode_graded()`：把 `graded_pass` / `graded_score` / `eval_run_id` 落到 episode 行 |
| 测试 | `tests/test_eval_memory_report.py`、`tests/test_memory_eval_feedback.py`、`tests/test_memory_trust.py`（"episode channel gating"、"root / eval isolation" 两组，含 namespace 隔离与 `_eval_memory_env` 的环境卫生——异常路径同样必须恢复） |

### F.2 实测

```text
默认（TRACE_AGENT_MEMORY=1，local agent，white_screen_001，trials=2）：
  Memory: trials=2 injected=0 graded=2
    namespace: eval:eval-536726da54f6
    self-contamination: 0.0 [clean] (unverified=0, trusted-same-run=0)

主动放宽闸门（追加 TRACE_AGENT_MEMORY_INCLUDE_CANDIDATE=1）：
  Memory: trials=2 injected=1 graded=2
    namespace: eval:eval-f10d243ac4f8
    self-contamination: 1.0 [CONTAMINATED] (unverified=1, trusted-same-run=0)
    ! white_screen_001/trial-3771fc8edcc7 ep-814569f8152a: raw episode from
      another trial of eval run eval-f10d243ac4f8 was injected verbatim as an
      unverified guess

run.json.memory_trace.namespace = eval:eval-f10d243ac4f8（此前恒为 project:default）
episode 闸门修复前，默认配置的 rate = 1.0；修复后 = 0.0
```

第二组是刻意保留的对照：审计的价值不在于"默认配置下报 0"，而在于**操作员放宽闸门时它必须照样报出来**。
一个只会输出 0 的指标不叫指标。

### F.3 为什么 `verified` 必须先 `active_earned`

§5.2 把 verified 写成 `active → verified` 的跃迁，但实现时 `graded_pass` 一度是独立捷径：
一次 eval 判分通过就能把记忆直接推到 `verified`，也就是"可影响排查顺序"的最高档。
这等价于用更强的标签重新引入闸门本要防的自污染——一个 trial 猜对了一次，
下一条 trial 就把它当作已验证规律来读。

现在 `evaluate_trust()` 的顺序是：退休状态不自动晋升 → 矛盾达上限归档 → 真实人工签署 verified →
已 verified 粘滞 → 计算 `active_earned`（`corroborated_runs >= 2` 且 `confidence >= 0.6` 且
`confirmed_runs >= 1`）→ **只有 `active_earned` 且零矛盾时**，才看 `corroborated >= 5`
或 `graded_pass` 这两条 verified 通道。grade 表达的是"这个结论在它被评分的那条 trace 上是对的"，
它只有在同一结论也跨过了佐证档之后才可推广。

同理，episode 通道的派生 trust **刻意封顶 `active`**：episode 是一次运行的原始记录，
不是跨案例归纳出的规律，不该拿到"可影响排查顺序"的档位。

### F.4 过程中修掉的三个静默 bug

1. **`_parse_contamination` 被派生键噎住**：`ContaminationAudit.as_dict()` 输出 `same_run_total`，
   而它在报告模型上是计算属性不是字段，`StrictModel` 拒收 → 异常被 `except` 吞掉 →
   §14 的 headline 指标对每个 trial 静默变成 `None`。改为只投影已声明字段：
   记忆侧可以继续反规范化，报告侧继续拒绝真正未知的键。
2. **`TrialMemory.from_trace` 未钳制分量分数**：rerank 的加权和可以越出 `[0,1]`，
   一个越界分量会让整条 `TrialMemory` 构造失败，进而丢掉这个 trial 的全部记忆观测。
   加 `_clamp_unit`，让"报告一个略超界的分数"不至于变成"没有报告"。
3. **失败 trial 被写成 `graded_pass=False`**：超时或异常的 trial 根本没产出可评分的结论，
   写 `False` 等于往 episode 里塞一句"这个结论是错的"，而 verified 通道会读它。
   改为 `None`：仍然审计（被展示了什么是关于这次运行的事实），但不打分。

### F.5 与设计稿的偏差

1. **namespace 粒度是 run 级**（`eval:{eval_run_id}`），不是 §8-1 原文的 `eval:{run}:{case}`，
   理由见 §8-1 的偏差说明：case 级会让实验语料永远无法晋升，A/B 失去意义。
2. **eval 记忆开关是 `TRACE_AGENT_MEMORY=1`**，不是 `--memory` flag，理由见 §8-2。
3. **§8-1 的隔离由两道独立的墙组成**：独立记忆根（默认）+ run 级 namespace。
   前者防"实验写进生产语料"，后者防"操作者显式把 root 指向持久语料做 A/B 时，
   实验记忆混进生产 namespace"。两道墙都在，才允许 §8-2 的 A/B 对照组存在。

### F.6 下一步

P4（多实例与缓存）。与记忆治理直接相关的两项：多 namespace 的权限治理（当前 namespace 只做隔离，
不做鉴权），以及记忆导入导出与备份（`memory migrate` 已具备 bundle 能力，缺的是跨实例分发）。
在那之前，§14 的 A/B 方法（`TRACE_AGENT_MEMORY` 关 / 开各跑一次同一批用例）已经可以直接执行，
报告里的 `self_contamination_rate` 与 `clean` 就是判断这轮对照是否可信的前置条件。

---

## 附录 G：生产路径冒烟与注入语义修正

单测是"手工搭一个语料，然后断言它"，它证明闸门、composer、store 三者自洽，
但不证明**分析主链路真的调用了它们**。本附录记录一次真实 trace 的端到端冒烟，以及它暴露出的两个问题。

### G.1 方法

脚本：`scripts/probe_memory_production_path.py`（可重复执行）。
真实 118MB htrace（`jd_white_screen.htrace`）、`--agent local`、同一 case 连跑两次 `diting-agent analyze`，
记忆根隔离到 `.codex_tmp/prod-memory`，namespace 走生产默认 `project:default`。

耗时：每次 analyze 都要把 htrace 转成自己的 work database，约 5 分钟/次，整体约 15 分钟。
第一次执行时脚本被 900s 超时杀掉，但 `run.json`、`memory.sqlite3`、dreaming 队列都已落地——
**是慢，不是卡死**。因此脚本提供 `--inspect-only`，只回读语料与 `run.json`，不重跑转换。

### G.2 实测输出（`--inspect-only`）

```text
diagnostics: {"kind":"sqlite","namespace":"project:default","pushdown":true,"degraded_reason":null}
memories=0 episodes=2 sessions=0 by_trust={}
  episode ep-48d5c87f56db run=run-f806bbc16e53 scenario=frame-jank findings=0 trust=candidate conf=0.50 graded=None eval=None
  episode ep-8072325ec088 run=run-7b0e4f576134 scenario=frame-jank findings=0 trust=candidate conf=0.50 graded=None eval=None

run1.memory_trace: {"query":"frame-jank 京东进入商品详情白屏 …","namespace":"project:default",
  "store":"sqlite","enabled":true,"hits":[],"trust_mix":{},"context_chars":0,
  "budget_chars":3000,"latency_ms":2.99,"episode_id":"ep-8072325ec088","created":[],"trust_changed":[]}
run2.memory_trace: 同上，latency_ms=2.18，episode_id=ep-48d5c87f56db

recall default      : hits=0 chars=0
recall candidate-on : hits=1 ids=['ep-48d5c87f56db']
  【未验证候选 candidate】来自历史单次分析的未验证推测，仅作发散提示；与本次取证冲突时无条件丢弃：
  - [ep-48d5c87f56db | episode candidate | conf=0.50 | score=0.66] frame-jank: Trace 已接入分析框架…
```

三点判读，都是"符合预期"而不是"跑通了就行"：

1. **`memories=0` 是正确结果**。`--agent local` 是端到端 smoke agent，`findings.json` 里
   `findings: []`、`root_causes` 为空，`_extract_candidates` 没有可抽取的结论，所以只投影 episode、不产候选。
   要用真实语料验证 gate 晋升需要接真实 provider；该逻辑由
   `tests/test_memory_trust.py::test_second_independent_run_promotes_to_active` 覆盖。
2. **run2 默认 `hits: []` 也是正确结果**：run1 的 episode 无 findings → 派生 trust = `candidate` →
   默认不召回（附录 F 的 episode 闸门）。dreaming 队列里是 `gate_review ×2 + embed ×2`，
   **没有第二次 episode 整合**，说明 §5.1 拆掉双重整合在生产路径上生效。
3. **`candidate-on` 那一步是必须的**。只看默认结果，"被闸门正确挡住"和"通道静默坏了"输出完全一样；
   放宽闸门后同一条 episode 立刻出现并带着未验证标注，才证明空结果是决策而非故障。

### G.3 冒烟暴露并修掉的两个注入语义问题

**问题 1：`conf=` 在两个通道含义不同。**

```text
修正前：- [ep-48d5c87f56db | episode candidate | conf=0.66] frame-jank: Trace 已接入分析框架…
修正后：- [ep-48d5c87f56db | episode candidate | conf=0.50 | score=0.66] frame-jank: Trace 已接入分析框架…
```

0.66 是 rerank 合成分，0.50 才是这条 episode 的置信度（findings 为空 → 落到 `DEFAULT_IMPORTANCE`）。
curated 行的 `conf=` 一直是真实置信度，于是同一个标签在 prompt 里有两个含义，
而 §6.4 承诺的恰恰是"让模型与人工复盘都能看出先验的强度"。
测试：`tests/test_memory_lifecycle.py::test_composer_reports_an_episode_confidence_not_its_rank_score`。

**问题 2：episode 之间不做近重复折叠，candidate 的 2 个名额被同一句话占满。**

```text
修正前：recall candidate-on → hits=2，prompt 里两行完全相同的 "frame-jank: Trace 已接入分析框架…"
修正后：recall candidate-on → hits=1，腾出的名额让结论不同的 episode 能进来
```

同一 case 跑两次会写两条内容完全相同的 episode，而 candidate 分区只有 2 个名额。
指纹取"这条 episode 会渲染成的那句话"（`composer.episode_body` 因此转为公开函数），
因为判据应当是"prompt 会不会重复说同一件事"，而不是"记录是否同源"。
测试：`tests/test_memory_trust.py::test_identical_episodes_collapse_instead_of_filling_the_budget`
（三条 episode：两条同文 + 一条不同结论 → 同文只留一条，不同结论那条仍在 prompt 里）。

**顺带补齐 §6.3 列出但尚未实现的 type 配额**（`recall._apply_type_quota`，
`TRACE_AGENT_MEMORY_TYPE_QUOTA` 默认 2）。实现为两趟填充而非硬上限，理由见 §6.3 的实施状态说明。
测试：`tests/test_memory.py::test_type_quota_gives_a_minority_type_a_slot`
（4 条 semantic + 1 条 procedural，`top_k=4` 时 procedural 顶掉最低分的那条 semantic）、
`tests/test_memory.py::test_type_quota_never_shrinks_the_cut`
（同 type 语料仍返回 `top_k` 条；`quota=0` 与原始分数序逐条一致）。

### G.4 回归

```text
pytest tests/ -q --ignore=tests/webui  →  397 项，4 failed
  4 项全部是与记忆无关的既有失败：test_load_case_discovers_golden_case、
  test_bottleneck_correct_without_canonical_literal、test_metric_correct_but_diagnosis_wrong、
  test_openai_adapter_guards_optional_sdk
记忆相关 8 个文件 153 项全绿
```

（这是 P3 收尾时的快照。P4 之后的最新数字见附录 H.6：全量 470 passed / 4 failed 同一组既有失败，
记忆相关 11 个文件 232 passed。）

### G.5 剩余

（更新：F.6 的两项——`PostgresStore`+pgvector 与 P4 的缓存/多实例——已在附录 H 落地。）
生产冒烟的两个未完成动作均已在附录 I 收口：**真实 provider 的 agent 已跑通**
（claude A/B 两轮 + 一次冒烟，`_extract_candidates` 在真实语料上产出 findings），
**真实语料上的 `candidate → active` 跃迁已实测**（语料副本 sweep 晋升 4 条、第二 beat 幂等 0 条，附录 I.4）。
`active → verified` 的自动通道（`graded_pass` 或 5 次佐证）在生产语料上仍为 0 条——
约束点是 confirmed 证据缺失（46 条里 confirmed>0 记录为 0，附录 I.5），而非 run 数；
人工签署通道（附录 I.6）已补上这条缺口。剩余仅运营侧的 Redis 缓存（H.6 第 3 项）。

---

## 附录 H：P4 实施记录（Postgres 适配器、召回缓存与运维命令）

设计稿第四~六节把目标态定为"PostgreSQL + pgvector 做 source of truth，Redis 做缓存，
对象存储放原始 trace"。§3.1 当时判断触发条件未满足，P1 先落 SQLite，并承诺
"表结构从 P1 起就是最终形态，不会白做"。本附录是那个承诺的兑现：Postgres 适配器
复用同一套 DDL 语义与同一个 `MemoryStore` Port，缓存层按"只做读加速"的定位补齐，
外加两个让 §8 的隔离与 §13 的回滚变得**可检查**的运维命令。

### H.1 交付清单

| 组件 | 位置 | 说明 |
|---|---|---|
| `PostgresStore` | `memory/store/postgres_store.py` | 线程局部连接（dashboard 多线程各自一条，不共享 cursor）、autocommit + 显式 `BEGIN` 短事务（避免只读召回长期 "idle in transaction" 钉住快照）、tsvector 全文检索与过滤下推、`memory_sources`/`memory_versions`/`memory_links` 与 SQL 适配器同构、`corpus_epoch`、`list_namespaces`、`close()` |
| DDL 与 SQL 纯函数 | `memory/store/pg_schema.py` | `apply_schema`（幂等、逐语句容错、`PG_SCHEMA_VERSION` 版本戳）、`tsquery_match`（CJK bigram 展开 + 引号转义 + 显式 OR）、`pgvector_literal`/`parse_pgvector_literal`、`embedding_column_type`、`pgvector_available`、`server_version`。全部不导入驱动，可离线单测 |
| 召回缓存 | `memory/cache.py` | `off` / 进程内 LRU / Redis 三态，key 含 store kind + namespace + `corpus_epoch` + 策略指纹；空结果不缓存；`cache_stats()` 暴露 hits/misses/entries 供 dashboard |
| 语料备份 | `memory/backup.py` | 每种存储给出它**真能恢复**的产物形态，外加 manifest（条数、sha256、当时的 `stats()`） |
| 隔离域运维 | `memory/cli.py` | `memory namespaces`（表格/`--json`，`*` 标记当前域，只列元数据不打印记录内容）、`memory backup`（`--out`/`--bundle`/`--json`，打印恢复命令） |
| 降级路径 | `memory/store/__init__.py` | `store=postgres` 但驱动缺失/DSN 未设/服务器不可达 → 回落本地语料，`degraded_reason` 写明原因 |
| 配置 | `memory/flags.py` | §10.5 的 7 个新变量 |
| 测试 | `tests/test_memory_postgres.py`(42)、`tests/test_memory_cache.py`(24)、`tests/test_memory_ops_cli.py`(15) | 离线部分不依赖任何服务；live 部分未设 DSN 时显式 skip |

### H.2 Postgres 实测（真实服务器，非 mock）

环境：本机 `initdb` 起一个临时集群（`-A trust`，端口 55432），PostgreSQL **17.10**，
psycopg2 2.9.12，**未安装 pgvector**（因此走 BYTEA 降级路径；pgvector 后续已从源码编译并跑通 vector 路径，见 L.7），库名 `diting_memory`。

```text
$ memory stats --store postgres
存储     : postgres        索引下推 : 是
命名空间 : project:pg-e2e  pgvector : 否（vectors: 是，应用层余弦）
server   : postgresql://postgres@127.0.0.1:55432/diting_memory   ← safe_dsn 去密后

psql> \dt   episodes episodes_fts memories memories_fts memory_links
            memory_sources memory_versions session_messages sessions store_meta
psql> select id, namespace, kind, topic, trust from memories;
 mem-pg-live   | project:pg-test | pattern | 冷启动白屏            | active
 mem-pg-live-2 | project:pg-test | fact    | 启动阶段主线程同步 IO | active
```

治理链在 Postgres 上的判定，与 SQLite 逐字一致（探针 `.codex_tmp/probe_pg_cli.py`）：

```text
run-1（suspected, 0.45）→ mem-cold_start-72b91e856777  candidate  distinct_runs=1
改写召回 "冷启动 白屏 排查" → hits=0        # candidate 默认不进 prompt（§7 契约生效）
run-2（confirmed, 0.90，结论不同）→ 冲突检测：
   mem-...72b91e856777  candidate  conf 0.45 → 0.225  contradicts=1
   status_reason = "gate not met: runs=1/2 confidence=0.23/0.60 confirmed_runs=0"
   version 2 reason = "conflict: contradicted by mem-cold_start-6f6a5eb55d98"
            actor  = "conflict-detector"
run-11/12（两次一致 confirmed, 0.85）→ gate：
   mem-cold_start-949bcc330593  active  distinct_runs=2
   status_reason = "2 independent runs agree (confidence 0.85, 2 confirmed)"
改写召回 "冷启动 白屏 排查" → hits=2（active 记忆 + 相关 episode）
注入分区 → 【历史相似案例 active】仅作先验假设，不得直接写入 Draft：
           - [mem-cold_start-949bcc330593 | active | Procedure | conf=0.85 | runs=2] …
```

运维命令（同一台服务器，`--store postgres`）：

```text
namespaces → *project:pg-e2e(3/4/0)  project:pg-restored(3/4/0)  合计 : 2 个隔离域 / 6 条记忆
backup     → 产物 memory-bundle-project_pg-e2e-<ts>.json（bundle，4 条记录）
             校验和 96e43a49…  清单 manifest-project_pg-e2e-<ts>.json
             说明   : postgres 存储没有可复制的本地文件，已改为导出逻辑快照（可用 memory import 恢复）
             恢复方式: diting-agent memory import --bundle <该文件>
export     → 已导出 3 条记忆 / 4 个片段 / 0 个会话
import     → 来源 postgres / 目标 postgres / 命名空间 project:pg-restored，36.9 ms，重跑幂等
```

pytest（设了 DSN，live 用例真跑）：

```text
TRACE_AGENT_MEMORY_DSN=postgresql://postgres@127.0.0.1:55432/diting_memory
pytest tests/test_memory_postgres.py -q   →  42 passed（含 2 项 live-server）
不设 DSN 时                                →  40 passed, 2 skipped（skip 原因写在 -rs 里）
```

### H.3 备份语义：三种存储，三种"真能恢复"的产物

| 存储 | 产物 `kind` | 为什么是这个形态 |
|---|---|---|
| SQLite | `sqlite-snapshot` | `VACUUM INTO` 的事务一致单文件副本。用文件系统直接拷 `memory.sqlite3` 会与 WAL 竞态，可能得到一个需要先恢复才能打开的文件 |
| JSON | `file-tree` | 没有事务可对齐，逐文件复制当前 namespace 的目录树，manifest 记录捕到了什么 |
| PostgreSQL | `bundle` | 本进程摸不到服务器的 data directory，也不该假装摸得到——物理 dump 是 `pg_dump` 的活。这里能诚实产出的是 `memory import` 可在任何适配器上恢复的逻辑快照 |

`--bundle` 可以给前两者**追加**一份逻辑快照，那是把 SQLite 语料搬到别的适配器的唯一途径。
SQLite 快照有一个必须说清的性质：一个库文件装所有 namespace，所以 `VACUUM INTO` 是**整库副本**，
产物 `records` 是全库计数而 `manifest.stats` 是当前命名空间计数。CLI 与 manifest 的 `notes`
都显式声明这一点，否则运维对不上两个数字会合理地怀疑备份坏了。

### H.4 与设计稿的偏差（有意裁剪）

1. **没有分布式锁。** 设计稿第六节把 distributed lock 列进 Redis 的用途。当前写者语义由
   Postgres 事务与 dreaming beat 的幂等性覆盖，引入锁意味着所有部署都要多一个外部依赖，
   而收益要等到真的多实例写入才出现——那时再加，接口已经在 `RecallCache` 的位置上。
2. **没有 namespace 级鉴权（RBAC）。** `memory namespaces` 会列出同库所有域的名字与计数
   （只列元数据，不打印任何记录内容，测试 `test_namespaces_does_not_leak_across_domains` 守着这条线）。
   跨项目共享一个记忆库之前，需要在 store 之上补一层授权，这是 §3.1 触发条件 3 的真正前置。
3. **pgvector 降级为可选加速，而不是方案的一部分。** 没有扩展时 embedding 存 `BYTEA`、
   余弦在应用层算：召回结果正确，只是没有 ANN 索引。设计稿把 pgvector 画进目标态，
   实现把它变成"有则用、无则退"，因为强制一个扩展会让本地开发与 CI 都变重。
4. **Redis 默认关闭。** 与 §6"Redis 只做缓存不做 source of truth"一致，但比设计稿更保守：
   设计稿把 Redis 画进架构图，实现里它是 `TRACE_AGENT_MEMORY_CACHE=redis` 的显式选择，
   默认 `off`。正确性靠 `corpus_epoch`，缓存挂了链路照常工作（`tests/test_memory_cache.py` 有降级用例）。
5. **原始 trace / evidence / report 仍留在文件系统**（设计稿第七节）。Postgres 只存
   `provenance.references` 指针，这一点没有因为换了数据库而改变。

### H.5 live 测试逼出来的三个问题

离线测试跑不到的路径，就是没被测过的路径。这三个都是把真实服务器接上之后才现形的：

**问题 1：postgres 的备份产出了一个"声称是语料快照、里面却没有语料"的目录。**
`_physical_snapshot` 原本靠"有没有 `root` 属性"判断存储是否本地，而 `PostgresStore`
也有 `root`——dream 队列与 checkpoint sidecar 在每个适配器上都是文件。于是备份把那个
sidecar 目录拷成了 `file-tree` 产物，manifest 煞有介事地记下 sha256 与文件数。
修复：按 `store_kind` 判定，远端适配器直接走 bundle。
一个会说谎的备份比没有备份更糟，因为它正是运维在其他一切都已经坏掉时唯一信任的东西。

**问题 2：两个 live-server 用例永久 skip，等于不存在。**
`tests/test_memory_postgres.py` 的 autouse fixture `_clean_env` 会删掉 `TRACE_AGENT_MEMORY_DSN`
（为了不让开发机环境里的 DSN 影响离线断言），而 `_live_store` 又正好从 `os.environ` 读它。
结果是：设了 DSN 也 skip，"Postgres 契约与 SQLite 一致"这句话从未被验证过。
修复：import 期把三个环境变量快照成模块常量，fixture 只清进程环境。
顺带修掉 live 分支里 `MemoryQuery(top_k=…)` 的错误关键字（正确是 `limit`）——
它同样只在那条从未执行过的路径上。

**问题 3：同一秒内两次备份，manifest 互相覆盖。**
产物名早就有 `_unique` 兜底（`VACUUM INTO` 与 `copytree` 都拒绝覆盖），但 manifest 名没有，
于是第二次备份抹掉了描述第一次产物的唯一清单：文件还在，没人能验证它。
修复：一次备份的所有产物共享同一个去重后的 stem，manifest 名由它派生。

### H.6 回归与下一步

```text
全量回归  pytest tests/ --tb=no --ignore=tests/webui
  → 4 failed, 470 passed, 3 skipped, 1 xfailed in 40.46s
  4 项失败全部是与记忆无关的既有失败（见 G.4 同一组）：
    test_load_case_discovers_golden_case、test_bottleneck_correct_without_canonical_literal、
    test_metric_correct_but_diagnosis_wrong、test_openai_adapter_guards_optional_sdk
  3 项 skip：2 项是 test_memory_postgres 的 live-server 用例（未设 DSN 时显式 skip），
             1 项是 test_skill_runtime_wiring（本机无 claude_agent_sdk）

记忆相关 11 个文件  pytest tests/test_memory*.py tests/test_eval_memory_report.py
  → 232 passed, 2 skipped in 33.45s（G.4 时是 8 文件 153 项，P4 净增 81 项）

Postgres live（集群在跑时）
  TRACE_AGENT_MEMORY_DSN=postgresql://postgres@127.0.0.1:55432/diting_memory
  TRACE_AGENT_MEMORY_STORE=postgres
  pytest tests/test_memory_postgres.py → 42 passed in 1.87s（含 2 项 live-server）
```

一个踩过的观测坑：`pyproject.toml` 的 `addopts = "-q"` 会与命令行再传的 `-q` 叠加成 `-qq`，
而 `-qq` **不打印最后那行 `N passed, M failed` 统计**。跑回归时不要再手动加 `-q`，
否则会以为"跑完了但没有结果"。

下一步（P4 之后没有新阶段，剩下的是验证与运营）：

1. ~~**§14 的 A/B**~~ ✅ 已完成（附录 I.2 / I.3）：claude 真实 provider 两轮 + local 两轮，
   两组 `self_contamination_rate=0.0 [clean]`；claude 批次 0.40 vs 0.40 等价（注入 1 条 episodic
   先验，未见分数收益但对照可信），local 批次因空语料注入 0 条判为“暂不可信”。
2. ~~**G.5 的生产冒烟**~~ ✅ 已完成（附录 I.1 / I.4 / I.5）：真实 provider agent 跑通，
   真实语料副本 sweep 实测 `candidate → active` 4 条、第二 beat 幂等；生产库 scan-only
   would-transition=0（约束点是 confirmed 证据，非 run 数）。`active → verified` 自动通道
   在生产语料仍为 0，已由人工签署通道（附录 I.6）补齐。
3. **多实例部署时再打开 `cache=redis`**，并观测 `cache_stats()` 的命中率；
   命中率不划算就关掉——缓存是可选项，不是架构承诺。（运营侧，非代码缺口。）

---

## 附录 I：真实语料验证、A/B 收口与人工签署通道

本附录把 G.5 / H.6 留下的两个验收缺口（真实 provider 冒烟、§14 A/B）收口，并补上自动闸门在
真实语料上够不到 `verified` 时的人工签署通道。所有数字均为实测：探针脚本在 `.codex_tmp/`
（`probe_final_corpus.py` / `probe_prod_copy.py` / `probe_confirmed.py`），A/B 与冒烟日志在
`.codex_tmp/{ab.log,ab_local.log,run2.log}`，eval 产物在 `.trace-agent/eval/eval-*`。

### I.1 生产路径冒烟（真实 provider，默认配置）

`eval-a771864cfe51`（white_screen_001，claude，默认配置未显式开 candidate）：

```text
总体评分 0.4；Memory: trials=1 injected=0 graded=1
query     : frame-jank 京东进入商品详情白屏 点击进入商品详情后长时间白屏，首屏渲染明显延后
namespace : project:default   store: sqlite
recall    : latency 115.067ms，命中 5 条 curated（trust_mix={candidate:5}）+ 3 条 episodic
注入       : injected_ids=[]，context_chars=0（5 条候选 curated 全部 injected=false，未进 prompt）
自污染     : rate=0.0 [clean]（injected=0, contaminated=0, trusted_from_same_run=0）
```

要点：默认配置下 **candidate 记忆只召回、不注入**（§7 契约）。冒烟证明隔离与审计在生产路径上正确，
但本身不构成“记忆有收益”的结论——收益要看 I.2 的 A/B。

### I.2 §14 A/B：claude 真实 provider（收口 H.6 第 1 项）

同一批用例（white_screen_001）、同一 agent（claude）、trials=1，记忆关 / 开各跑一次：

| 组 | eval run | 记忆 | 总分 | 注入条数 | 自污染率 | 平均工具调用 |
|---|---|---|---|---|---|---|
| 基线 A | `eval-ffbed7c160a8` | off | 0.40 | 0 | - | 8.0 |
| 对比 B | `eval-1f9ea6dcb763` | on（含低权重候选） | 0.40 | 1 | 0.0 `[clean]` | 10.0 |

`eval compare`：`+0.0`（grader 通过率 0→0、root cause 命中率 0→0、平均总分 0.40→0.40）。
B 组注入的是 1 条 **episodic** 先验（`ep-a8f2543d32ec`，context_chars=194，latency 80.584ms），
5 条 curated 候选被召回但 `injected=false`（trust_mix={candidate:5}）；contamination 审计
`injected=1, contaminated=0, trusted_from_same_run=0, rate=0.0, clean=true`。

结论判定 **“可信”**（两组 trial 数一致、对照组无自污染），但本批次 **Δ=+0.0，未见分数收益**：
注入的 episodic 先验没有把 grader 通过率或 root cause 命中率抬起来。这与语料现状一致——生产库
46 条记忆全是 candidate（I.5），真正能作为高权重先验的 active/verified 为 0，A/B 只能反映
“低权重候选 + 1 条 episodic”的边际效果。收益结论需要语料里先沉淀出 active/verified 记忆后再复跑。

### I.3 §14 A/B：local（空语料对照，方法论验证）

为验证 A/B 链路本身（不依赖 claude 配额），用 local agent 跑了一组，但 **未指定 `--memory-root`**，
对照组读到空语料：

| 组 | eval run | 记忆 | 总分 | 注入条数 | 自污染率 |
|---|---|---|---|---|---|
| 基线 A | `eval-e2cec6f9ade5` | off | 0.25 | 0 | - |
| 对比 B | `eval-c943337f39bd` | on | 0.25 | 0 | 0.0 `[clean]` |

`eval compare`：`+0.0`。结论判定 **“暂不可信”**——B 组开了记忆但一条都没注入（空语料），两组实际等价。
这条恰好验证了 compare 的结论判据能正确识别“注入 0 条 ⇒ 等价 ⇒ 不可信”，即 A/B 报告不会把
“没注入”误判成“记忆无收益”。

### I.4 真实语料副本 sweep：candidate → active（收口 G.5 跃迁观察）

在 A/B 快照语料 `.trace-agent/eval/memory/ab-1791093317/corpus` 的 **临时副本** 上跑
`ConflictDetector.run()` 两个 beat（不写原语料），观察真实 findings 抽出的记忆能否过闸门：

```text
thresholds: runs>=2 confidence>=0.60
episodes=252  memories=53  trust(before)={candidate:53}

beat 1: scanned=53 blocks=181 supports=16 contradictions=0 duplicates=0 superseded=0 transitions=4
  k-cb6ddc9dd6e9 candidate->active conf=0.90  (2 independent runs agree, 1 confirmed, 1 via supports)
  k-f9a9f00a69fd candidate->active conf=0.85  (同上)
  k-60e56d58931b candidate->active conf=0.70  (同上)
  k-c870ebb5f99b candidate->active conf=0.65  (同上)
  trust(after beat 1)={active:4, candidate:49}

beat 2: scanned=53 ... transitions=0          # 幂等：同一批证据不再重复晋升
  trust(after beat 2)={active:4, candidate:49}
```

要点：

1. 真实语料上 **`candidate → active` 确实发生**（4 条），且都满足“≥2 个独立 run 一致 + confidence≥0.6
   + ≥1 confirmed”的闸门（§5.2），不是单次 run 直通——P0 修掉的自污染缺陷在真实数据上没有复发；
2. **第二 beat transitions=0**，证明 `add_source` 幂等（`PRIMARY KEY(memory_id, run_id)`）与 distinct_runs
   派生生效，重复整合不再伪装成多案例佐证；
3. 晋升靠的是 `supports` 关系把相邻记忆的 run 并起来（16 条 supports），即“同一现象被多条记忆从不同角度
   记录”也能凑够独立 run 证据，符合 §5.3 的合并 / 佐证语义。

### I.5 生产库 scan-only dryrun：would-transition=0

对生产库 `.trace-agent/memory` 的 **副本** 做只读闸门 dryrun（`scan()` 不写、`apply_trust_gate` 纯函数）：

```text
prod-copy scan: memories=46 blocks=139 supports=13 contradictions=0 duplicates=0
trust distribution: {candidate:46}
would-transition=0
closest to the gate (corroborated_runs, confirmed_runs, confidence, trust):
   (5, 0, 0.88, candidate)
   (5, 0, 0.86, candidate)
   (5, 0, 0.85, candidate)
   ...
confirmed>0 records: 0
records with >=2 corroborating runs: 11
```

要点：生产库 46 条 **全是 candidate，闸门一条都不会晋升**。约束点不是 run 数（11 条已有 ≥2 个佐证 run，
最高 5 个），而是 **confirmed 证据缺失**（confirmed>0 的记录为 0）——真实诊断结论大多是 `suspected`，
拿不到 `confirmed`，于是卡死在 candidate。这正是 §13“闸门过严”风险预言的情形，也是 I.6 人工签署通道
存在的理由：不能为了放行就调低阈值（那会削弱全库抗自污染），而应让人为单条结论背书。

### I.6 人工签署通道（sign_off / revoke_sign_off / `memory verify`）

§5.2 把人工签署列为 `active → verified` 的三条通道之一，§13 把它列为“闸门过严”的缓解手段。本段把它
真正落地（此前 `validated_by="pending"` 只是没人消费的哨兵字符串）。

**语义（`lifecycle/promoter.py`）**

- `sign_off(store, memory_id, *, validated_by, note="", actor=None) -> SignOff`
  - 身份先过 `normalize_validator`：`pending/none/null/空` 一律拒签（`accepted=False`，不抛异常）；
  - 拒签情形：无身份、记忆不存在、记忆已 `archived/superseded`（签署不能把退役记忆拉回召回）；
  - 签署 **先写 `provenance.validated_by`（`store.revise`，带审计版本行），再由 `commit_gate` 读回作为
    证据判 trust**，不直接写 `status=VERIFIED`——这样仍遵守 §7“只有闸门能改 trust”，且反证检查照样生效；
  - 多签署人追加为 `"alice, bob"`（`_SIGNATORY_SEPARATOR=", "`）；同一人重复签署幂等（无新版本行）；
  - actor 默认 `expert:<identity>`（`EXPERT_ACTOR_PREFIX`）。
- `revoke_sign_off(store, memory_id, *, revoked_by, note="", actor=None) -> SignOff`
  - 撤回 **清空整个签署**（不是删一个名字），因为 verified 基于“有人背书”而非法定人数；
  - 因 verified 在闸门里是粘滞的，撤回后 **从 candidate 基线重判**：5 个独立 run 一致的结论撤回签署后仍留
    verified，单次 run 的猜测撤回后掉回 candidate——签署可逆，且可逆后由证据说话；
  - 无签署记录时拒绝撤回。
- 反证压过签署：gate 顺序是 `contradictions>=2 → ARCHIVED` 排在 `validated_by → VERIFIED` 之前，故两条
  独立反证能把已签署记忆归档（人工背书不能推翻证据）。

**SQLite 审计修复（`store/sqlite_store.py`）**

新增模块级 `_stored_validator(raw)` 解析 provenance JSON 里的 `validated_by`；`_put_curated` 的 `previous`
SELECT 补 `provenance` 列，`changed` 判定加入 `_stored_validator(previous) != normalize_validator(new)`。
否则 SQLite 侧签署不会写 version 行，与 JSON 适配器不一致——trust 跃迁可审计率（§14）会漏掉签署这一类变更。

**CLI（`memory verify`）**

```text
diting-agent memory verify <memory_id> --by <身份> [--note <理由>] [--revoke] [--json]
                                       [--root ...] [--namespace ...] [--store ...]
```

- `--by` 必填，占位身份（pending 等）拒绝并 `Exit(code=1)`；
- 输出记忆 id / 操作（人工签署 | 撤回签署）/ 身份 / 理由（有 note 时）/ 信任级别 / 信任跃迁 / 依据；
- `--json` 输出 `SignOff.as_dict()`（memory_id, accepted, validated_by, status, transition, detail）。

**测试**：`tests/test_memory_trust.py` +10（含 1 个 5 参数化）、`tests/test_memory_ops_cli.py` +7（含 2 个
参数化），两文件单跑 74 passed。覆盖：空 / 占位身份拒签、缺记忆拒绝、archived 拒签、签署→VERIFIED 且写
“签署”+“跃迁”两条 version（actor=`expert:alice`）、重复签署幂等、第二签署人追加、2 条 contradicts 压过
签署→ARCHIVED、撤回单 run 猜测→CANDIDATE、撤回证据充足（5 run+confirmed）→留 VERIFIED、撤回无签署拒绝、CLI 三态。

### I.7 confirmed union 语义（为什么 sweep 能晋升而生产库不能）

I.4 的语料副本能晋升 4 条、I.5 的生产库 0 条，差别在 confirmed 证据的分布。`probe_confirmed.py` 在语料
副本上对比两种 confirmed-run 计法（scan-only，不写）：

```text
thresholds: runs>=2 confidence>=0.60
memories total          : 53
blocked by runs<2       : 39
blocked by confidence   : 2
pass, own-confirmed     : 2     # 只看记忆自己的 confirmed_run_ids
pass, union-confirmed   : 4     # 把 supports 邻居的 confirmed_run_ids 并进来
delta                   : 2
```

即：把“佐证邻居”的 confirmed run 并入后，可晋升数从 2 → 4（delta=2）。新增的 2 条（`60e56d58931b`
conf=0.70、`cb6ddc9dd6e9` conf=0.90）自身 `own_confirmed=0`，但各有一个 `confirmed=1` 的 supports 邻居
（`c870ebb5f99b`、`f9a9f00a69fd`）。这解释了 I.4 的 4 条晋升：闸门用的是 **union 语义**（佐证组的 confirmed
共享），而生产库 46 条里 confirmed>0 的记录为 0（I.5），union 也无 confirmed 可并，故 0 晋升。union 语义是
有意设计：同一现象被多条记忆从不同角度记录时，confirmed 证据应在佐证组内共享，否则“角度不同”会惩罚掉本应
晋升的记忆。

### I.8 回归

```text
全量  pytest tests/ --tb=line --ignore=tests/webui
  → 3 failed, 524 passed, 5 skipped, 1 xfailed
  3 项失败均为与记忆无关的既有基线失败：
    test_load_case_discovers_golden_case、test_bottleneck_correct_without_canonical_literal、
    test_metric_correct_but_diagnosis_wrong
webui  pytest tests/webui
  → 10 passed
签署两文件  pytest tests/test_memory_trust.py tests/test_memory_ops_cli.py
  → 74 passed
```

（G.4 时全量是 4 failed / 397 项、H.6 时是 4 failed / 470 passed；本段净增签署相关测试后为 3 failed /
524 passed。第 4 项既有失败 `test_openai_adapter_guards_optional_sdk` 在本机环境已不再复现——单跑 1 passed，
故 failed 从 4 降到 3，与记忆改动无关。）

### I.9 与设计稿的偏差

- 设计稿第十 / 十一节的“专家验证”通道，本段用 `sign_off/revoke_sign_off` + `memory verify` 落地，并刻意让
  **反证压过签署**（gate 顺序保证），即人工背书不能推翻证据，只能补证据够不到的 confirmed 缺口；
- §14 的 A/B 收益项：本批次 claude 0.40 vs 0.40 等价、local 空语料不可信，**如实记为“对照可信但未见收益”**，
  没有为了好看而把等价写成提升。收益复跑的前提是语料里先有 active/verified 记忆（靠真实 confirmed 证据积累
  或人工签署），这是运营侧的长期动作，不是代码缺口。

---

## 附录 J：审计轨迹随迁移搬运，以及一个只在 JSON→SQL 方向出现的幂等 bug

### J.1 问题

§7 要求“trust 跃迁 100% 可审计”，§12.2 要求“迁移命令幂等”。这两条在治理数据入 bundle 之前都不成立：
`export_bundle` 只搬 episodes / memories / sessions，`memory_versions` 与 `memory_links` 留在源库。
一次 JSON→SQLite 迁移之后，目标库的记忆带着 `trust=verified`，却答不出“谁签的、什么时候签的、
中间经过哪几跳”，§14 的“trust 跃迁可审计率”在迁移那一刻归零。

### J.2 改动

1. `BUNDLE_VERSION = 2`：bundle 新增 `versions`（`recent_versions` 是**新→旧**，导出时反转成时间序，
   否则目标库的序号顺序与跃迁发生顺序相反）与 `links`；v1 bundle 缺这两个键时按空导入，不报错。
2. `import_bundle` 的顺序是 **episodes → versions/links → memories → sessions**。审计轨迹必须先进：
   每个适配器的 `_put_curated` 靠“刚载入的历史是否已经停在当前状态”识别重导入，
   memories 先进就没有可比对的尾行，于是每次重跑迁移都会给语料补一条伪造的编辑记录。
3. 三个适配器各实现 `import_governance(versions, links) -> (新增版本数, 新增边数)`：
   SQLite 单事务 `INSERT OR IGNORE`、Postgres 同构 `ON CONFLICT DO NOTHING`、
   JSON 走 sidecar 去重键 `(memory_id, version, trust, reason, created_at)`。
   计数一律用前后 `COUNT(*)` 差值而不是 rowcount —— `INSERT OR IGNORE` 在不同驱动下对“被忽略的行”
   的 rowcount 语义并不一致，猜出来的迁移报告比量出来的更糟。
4. **不进 `MemoryStore` Port**：这是只有 `migrate` 调用的入口，放进 Port 就等于要求未来每个适配器
   都实现一个没人再调的方法。`migrate` 用 `getattr` 探测，没有就报 `(0, 0)`，CLI 打印“未搬运审计轨迹”
   而不是假装搬到了。
5. `MigrationReport` 增 `versions` / `links` 字段，`_verify_governance` 在迁移后按三元组比对边、
   按 `stats().versions` 下限比对版本数；`memory migrate` 只在计数非零时打印这两行。

### J.3 实测复现的 bug：JSON→SQL 重放会把整段历史复制一遍

```text
修复前：
  json 源库（sidecar 行无 version 序号） → sqlite
  import #1: versions=1  links=1
  import #2: versions=1  links=0     ← 应该是 0
  目标库版本行：2 条，内容完全相同，只有序号不同
      m1  version=1  trust=active  reason=gate  created_at=...523244
      m1  version=2  trust=active  reason=gate  created_at=...523244
```

成因：`memory_versions` 的幂等全靠 `UNIQUE(namespace, memory_id, version)`。
SQL 导出的行自带序号，冲突能触发；**JSON sidecar 的行没有序号**，适配器只能推导，
而原来的推导是 `MAX(version) + 1` —— 每次重放都算出一个**不同**的序号，
冲突永远不触发，于是每跑一次迁移就把整段审计历史再抄一遍。

这个方向恰好是唯一一个回滚演练会真跑的方向（生产 SQLite → 回滚 JSON，或 legacy JSON → SQLite），
而“迁移被中断后重跑”又恰好是运维最常做的动作。

修复：新增 `memory/store/rows.py:VersionResolver`，把序号推导从“表内计数”改成“行身份”——
用 sidecar 自己就在用的四元组 `(memory_id, trust, reason, created_at)` 作键，
构造时一次性 `SELECT` 出目标库已有行，命中就复用它的序号，冲突才真的会发生。
显式带序号的行永远优先，所以 SQL→SQL 复制仍落在原行上而不是被重新编号。
同一次导入内两行完全相同也会塌成一行，与 JSON sidecar 的去重语义一致。

```text
修复后（同一 bundle 连跑三次）：
  import #1: versions=2  links=1
  import #2: versions=0  links=0
  import #3: versions=0  links=0
  目标库版本行：2 条，序号 1/2 稳定
```

### J.4 一并锁进测试的读取契约

这两条本轮**没有改代码**，是探针验证行为正确后补的回归测试——契约写在实现里而不在测试里，
等于下一次重写适配器时没人会发现它掉了。

- `episodes.session_id` / `project_id`、`sessions.agent_id` / `user_id` / `project_id` 五个 schema v3 新增列，
  两个适配器的 `load_*` 与 `list_*` 均已回填，测试同时走 by-id 与 list 两条路径
  （SQL 适配器这两条读的是不同列，只测一条会漏）。
- `CuratedMemory.version` / `last_accessed_at` 是 `Field(exclude=True)`，payload 字节级不变，读取时由
  SQL 适配器回填；`touch()` 不改 `version`、不改 `updated_at`（不刷新 freshness），只盖访问时间戳。
  文件适配器**没有** `last_accessed_at` 列，`touch()` 是有意的 no-op：
  与其在回滚路径上谎报“这条记忆刚被用过”，不如老实返回 `None`。

### J.5 测试与回归

新增 6 个用例（`tests/test_memory_store.py`）：

| 用例 | 断言的性质 |
|---|---|
| `test_ownership_columns_survive_a_roundtrip` | 五个归属列在两个适配器上都能写进去也读得回来 |
| `test_governance_metadata_is_read_back_from_the_index` | SQL 回填 `version`/`last_accessed_at`；`touch` 不刷新 freshness、不涨版本号；文件适配器如实返回 `None` |
| `test_audit_trail_travels_with_the_bundle` | bundle 带 `versions`/`links` 且为时间序，目标库读得回签署 reason 与 contradicts 边 |
| `test_replaying_a_bundle_does_not_grow_the_audit_trail` | J.3 那个 bug：JSON→SQL 与 SQL→JSON 双向重放都必须是 0 |
| `test_reimporting_an_unchanged_memory_records_no_version` | `_put_curated` 的重导入识别；同时断言真实变更仍然记录（不是“永不写”） |
| `test_version_resolver_reuses_the_ordinal_it_already_handed_out` | 推导规则本身：复用已有序号、按 memory 各自续号、显式序号优先、容忍 `Decimal`/`NULL` |

Postgres 侧因为需要真实服务器，同一条性质放进 `test_live_server_holds_the_same_contract_as_sqlite`
（`_assert_governance_import_is_idempotent`）；`reason` 带 per-call 后缀，因为 live namespace 跨次运行是持久的，
固定 reason 第二次跑就已经在表里，会以“与被测原因相反”的方式失败。

回归：`tests/` 全量（除 `tests/webui`）**530 passed, 5 skipped, 1 xfailed**，
3 个失败是改动前就存在的无关用例（`test_eval_contracts.py::test_load_case_discovers_golden_case`、
`test_grader_robustness.py` 两个）；`tests/webui` **10 passed**。

---

## 附录 K：三处收尾 —— 迁移期信任降级、PROFILE/PROJECT 两类记忆、召回可观测性

### K.1 为什么是这三处

P0~P4 全部落地之后逐条对表，剩下的缺口有一个共同特征：**契约写了，代码没写，而没有任何一条
现有测试会因为它缺失而变红**。三处都不影响单次 run 的正确性，所以它们能一路活到最后。

| 缺口 | 出处 | 缺失时的表现 |
|---|---|---|
| 迁移不降级历史信任 | §12.4 | 旧语料带着缺陷 A 的 `occurrences` 原样搬进新库，`trust=active` 却答不出凭什么 |
| PROFILE / PROJECT 只有类型没有生产者 | §2.3 | `MemoryType` 六格里两格永远为空；新 run 召回不到“这个项目最近十二次都落在同一台设备同一个 build” |
| `latency_ms` / `truncated` 有字段无值 | §9 | 两个字段早就在模型上、默认值也合法，于是 `MEMORY_RECALLED` 永远报 `0.0 / false`；dashboard 没有“召回延迟”入口 |

第三条最阴险：字段存在且有合法默认值，意味着**读它的人拿到的是一个看起来正常的假数字**，
比字段不存在更糟。

### K.2 §12.4：迁移期把无法解释的信任拉回 `candidate`

新增 `migrate.py:demote_unexplained_trust(memory, earned) -> (记录, 是否降级)`，
在 `_import_curated` 写入目标库之前逐条过。四条豁免，**顺序即优先级**：

1. 本来就不是 trusted 状态 —— 无事可做；
2. `kind in DERIVED_DIGEST_KINDS` —— 派生摘要没有“第二次运行”可以佐证它，在这里降级
   等于把闸门自己判为 `active` 的记录搁浅成永久 `candidate`；
3. `status.value in earned[memory_id]` —— **审计轨迹能证明这一跳**。`earned_trusts()` 只认
   `from_trust` 存在且 `!= trust` 的行：只是复述副本落地状态的行不是证据，认它会让每次迁移
   都自我认证（记录自己的搬运产物成了它保住身份的理由）；
4. `evaluate_trust(CANDIDATE, evidence_for(memory))` 仍能到同一档 —— **自身证据说话**。
   故意传 `CANDIDATE` 而不是记录当前状态，否则闸门“verified 是粘性的”那条短路会用
   正在被审查的状态给它自己作证。

都不满足才降级，动作有三个，缺一不可：

```python
status        = CANDIDATE
status_reason = f"{DEMOTION_REASON}, was {memory.status.value}"   # 留住原档位，否则不可逆
occurrences   = memory.distinct_runs                              # 按 provenance 里的 run:* 去重重算
```

`occurrences` 那一行是重点：缺陷 A 的成因就是这个字段按**写入次数**累加，
搬运时把它一并复制过来等于把谎言也复制过来，而这个谎言是承重的 —— 它决定什么被当作先验注入。

审计与逃生门：降级写的版本行 `reason=DEMOTION_REASON`、`actor=MIGRATE_ACTOR`（与闸门、
冲突检测器、retention 区分开）；`MigrationReport.demoted` 计数，CLI 在非零时打印“信任降级 : N”。
`memory migrate --keep-trust` 关闭这一步，用于“旧语料换底座”这种不改语义的搬迁。

**与设计稿的偏差**：§12.4 原文是“历史数据**默认全部**降级为 `candidate`”。实现是
“降级**无法解释**的信任”，比原文窄。理由：全部降级会把专家签署（附录 I.6 的 `sign_off`）
和闸门刚判过 `active` 的记录一起打回去，迁移完要重新攒两次独立运行才能恢复原状 ——
而这两类记录的信任是有据可查的，抹掉它是把可审计性当成不可信来处理。
原文真正要治的是缺陷 A 那种“`occurrences` 被当证据”的记录，那类恰好落在降级分支里。
规则表锁在 `test_demotion_rule_table`（六行：legacy active 降、earned verified 留、
两次运行 active 留、SUMMARY/PROJECT 摘要留、candidate/archived 留）。

### K.3 §2.3：PROFILE 与 PROJECT 两类记忆的生产者

**PROFILE**（`consolidation.py`）：从 episode 的 `device` / `build` / `target_process` 生成
`mem-profile-<digest>`，`scenario_type=None`，`status=candidate`（**照常走闸门**，它是一条
关于世界的断言，只是断言的对象是环境而不是故障）。关键词**故意**不走 `_keywords()`：
那个函数会折叠 episode 的 scenario 与 symptom，于是一条与场景无关的环境元数据会靠症状文本
匹配进任何查询，把 `device=Xiaomi` 注入到一次没人问设备的分析里。

**PROJECT**（新增 `memory/project.py:ProjectDigestService`）：dreaming beat 的一个 job，
`batch.projects` 报 namespace，dashboard 侧 `JOB_LABEL` / `ACTOR_LABEL` 认 `dreaming:project`。
它是 `SUMMARY` 的同类而非 `FACT` 的同类，因此继承 `summarize.py` 的两条约束：

- **确定性**：无 LLM、无时间戳、集合先排序再渲染，每一行都是计数或截断的存量文本。
  摘要会被当作先验召回，所以“它变了”必须只能因为“语料变了”。
- **幂等**：语料没变就一个字都不写。少了这条，几分钟一次的 beat 会永远刷新 `updated_at`，
  而 freshness 是排序输入 —— **语料的投影会排在语料前面**，这是摘要类记录最典型的自噬。

`PROJECT_CONFIDENCE = 0.5`，与 `SUMMARY_CONFIDENCE` 同理：忠于来源，但不对任何 trace 做断言，
压在闸门置信度地板之下，永远无法自己说到 `verified`。记录 id 由内容决定（`project_memory_id`），
所以重复 digest 得到同一条记录而不是追加。`PROJECT` 与 `SUMMARY` 一起进
`DERIVED_DIGEST_KINDS`：没有第二次独立运行可以佐证“全部运行的投影”，要求一个就等于
让这条记录永久停在 `candidate`，一个只能自我删除的功能。

### K.4 §9：`latency_ms` 与 `truncated` 真的有值了

盖章的位置在 `RecallService.recall()`，**不在调用方**。run、dashboard 探针、`memory stats`、
eval trial 各自量到的都是自己的开销，只有这一处量的是检索本身；放在这里，四个消费者才会
报同一个数字而不是四个数字。它覆盖整次调用（含缓存查找），因为调用方等的就是这段时间 ——
缓存命中时报的是查缓存的耗时，这是诚实的读法。

两条实现细节，都是踩过才写下来的：

- **不进缓存**。`_stamp_latency()` 返回 `model_copy`，缓存里的条目 `latency_ms` 恒为 0。
  直接改原对象会把这次的耗时写成条目的耗时，于是每一次命中都重放那次 miss 的成本，
  报告一个本次调用从未支付过的数字。（`test_recall_stamps_latency_without_persisting_it`）
- **深召回路径单独计时**。`MemoryRuntime.recall` 只在快通道空手时才走深通道，而深通道返回的是
  **新建**的 `RecallResult`；沿用快通道的戳，等于用“什么都没找到的那次检索”的耗时来描述
  这次的答案，比报 0 更具误导性。

`truncated` 由 composer 一路带到调用方，链路是
`compose_memory() -> RecallResult -> _memory_trace() -> MEMORY_RECALLED payload -> run.json`。
`_memory_trace()` 用 `getattr(recall, "truncated", False)` 读，因为那个参数类型是 `Any`：
宁可少报一个标志，也不要让可观测性本身成为 run 失败的原因。
“召回到了”和“注入到了”是两个断言，只有第二个能改变结论，所以这个标志是**带着走**的而不是
从 `len(hits) > len(injected_ids)` 推的 —— composer 还会整段省略连标题都放不下的分区。

dashboard 补上 §9 要的“召回延迟”入口：记忆页签头部一个折叠的**召回探针**
（`#memory-probe`，查询框 + 场景下拉 + 试跑），结果以五枚 chip 呈现 ——
`耗时 N ms` / `命中` / `注入` / `提示词 N 字` / `已截断`（琥珀色，带 title 解释“排序结果超出
提示词预算，末尾条目被整条丢弃”）或 `未截断`。走的是既有 `/api/memory/recall`，
后端无需改动，因为 `model_dump` 本来就会带上新字段 —— 这也反过来验证了 K.1 那句
“字段存在但没值”：接口一直是通的，缺的只是那一端的赋值。

### K.5 测试与回归

新增 6 个用例：

| 用例 | 文件 | 断言的性质 |
|---|---|---|
| `test_demotion_rule_table` | `test_memory_store.py` | K.2 的四条豁免与降级动作逐行成表，含“earned 记录必须原样保留”这一条反向断言 |
| `test_project_digest_is_active_deterministic_and_idempotent` | `test_memory_lifecycle.py` | 摘要落地即 `active`、置信度与 `scenario_type`、重复 digest 完全无操作（内容 / `updated_at` / 版本数都不动）、语料变化才更新、空库返回 `None` |
| `test_recall_stamps_latency_without_persisting_it` | `test_memory_cache.py` | miss 与 hit 都 `> 0`、是不同对象、答案不受盖章影响、**缓存条目仍为 0** |
| `test_budget_pressure_reaches_the_recall_result_as_truncation` | `test_memory_lifecycle.py` | 同一语料下缩小预算：`hits` 不变（排序不读预算）、`truncated` 翻真、注入数 < 命中数；宽松预算下必须为假 |
| `test_recall_event_carries_latency_and_truncation` | `test_memory_inline_governance.py` | 链路最后一跳：`_prepare_memory` 产出的 trace 与 `MEMORY_RECALLED` payload 上的两个值逐字相等 |
| `test_memory_recall_probe_reports_latency_and_truncation` | `tests/webui/test_web_panel.py` | 真实浏览器：面板默认折叠、空查询被拒、试跑出耗时与截断 chip、再点收起（截图存 `reports/webui/`） |

顺带修的两处：`test_memory_semantic.py` 的语料计数改为 `CORPUS_VECTORS = len(COLD_CORPUS) + 1`
（项目摘要是第 4 条被 embedding 的记录，确定性且幂等，所以这个 +1 是常量而不是漂移）；
`test_memory_lifecycle.py` / `test_memory_inline_governance.py` 里“哪些记录该出现在跃迁表里”
的过滤从按 kind 猜改为按 `DERIVED_DIGEST_KINDS` 判 —— 摘要落地即 `active`，
不经历跃迁，拿全量语料去比会把它们算成漏报。

回归：11 个记忆测试文件 **279 passed, 5 skipped**；`tests/` 全量（除 `tests/webui`）
**536 passed, 5 skipped, 1 xfailed**，3 个失败仍是 J.5 就记录在案的无关用例
（`test_eval_contracts.py::test_load_case_discovers_golden_case` 断言 1 条 root cause，
而 `evals/cases/cold_start_001/case.yaml` 里有 RC001/RC002 两条；`test_grader_robustness.py` 两个）；
`tests/webui` **11 passed**（真实 Chrome）。

---

## 附录 L：H 的 live 数字复验、打包缺口与一个序号冲突

附录 H 的 live 结论当年是真跑过的，但 K.5 那轮回归里它们是 **5 个 skip**：
驱动没装、DSN 没设，live 断言一次都没执行。本附录把这两条路补齐，在真实服务器上
重跑，并记录复验逼出来的一个**真实缺陷**——不是测试污染。

### L.1 打包缺口：两个可选适配器没有声明入口

P4 交付了 `PostgresStore` 与 `RedisCache`，但 `pyproject.toml` 的
`[project.optional-dependencies]` 只有 `qoder` / `openai` / `claude` 三个 extra。
也就是说仓库里**没有任何声明入口**告诉运维该装什么驱动，唯一的线索是运行时报错文本
（`no PostgreSQL driver installed (need psycopg or psycopg2)`）。

补上两个 extra，并让文档指回它们：

```toml
postgres = ["psycopg2-binary>=2.9"]
redis    = ["redis>=5"]
```

README 的安装节、常见问题、以及 §10 环境变量表里 `TRACE_AGENT_MEMORY_DSN` /
`TRACE_AGENT_MEMORY_CACHE` 两行都注明了对应的 `--extra`。实测装到的是
psycopg2-binary **2.9.13**（`load_driver()` 返回 `psycopg2`）与 redis **8.1.0**。

### L.2 复验环境

照 H.2 的原方子重建，**不碰**机器上常驻的 `postgresql-x64-17` 服务（5432，
`pg_hba.conf` 全 `scram-sha-256`）：

```powershell
initdb -D "D:\pg-e2e-tmp\data" -U postgres -A trust -E UTF8 --locale=C --no-sync
pg_ctl -D "D:\pg-e2e-tmp\data" -l "D:\pg-e2e-tmp\pg.log" `
       -o "-p 55432 -c listen_addresses=127.0.0.1" -w start
createdb -h 127.0.0.1 -p 55432 -U postgres -T template0 -E UTF8 diting_memory
```

Redis 用的就是 H.6 当时那一个：Memurai Developer 4.1.2（`C:\Program Files\Memurai\memurai.exe`），
`INFO server` 自报 `redis_version:7.2.5` / `redis_mode:standalone`，与 H.6 的
「本地 Redis 7.2.5」逐字对上。live 缓存用例走 **db 15**（`redis://localhost:6379/15`），
避免动 db 0。本轮 L 复验时仍未安装 pgvector，所以 BYTEA 降级路径是当时实际走到的那一条；
pgvector 已在之后从源码编译安装并单独复验（见 L.7），BYTEA 与 vector 两条路径现在都被真跑过。

### L.3 复验逼出来的真实缺陷：审计轨迹可以跑到行版本前面

第二次 live 跑就炸了：

```text
psycopg2.errors.UniqueViolation: duplicate key value violates unique constraint
  "memory_versions_namespace_memory_id_version_key"
DETAIL: Key (namespace, memory_id, version)=(project:pg-test, mem-pg-live, 3) already exists.
  at store.write_curated(memory) → _put_curated → INSERT INTO memory_versions
```

库里的状态是：`memories` 行停在 `version=2`（trust=verified），而 `memory_versions`
已经有 v1 / v2 / **v3**（v3 是治理断言导入的 sidecar 行）。轨迹比行**超前**了。

根因是两条写入路径对「下一个序号」的口径不一致：

* `import_governance` 经 `VersionResolver.ordinal` 给无序号的 sidecar 行推导序号，
  用的是 `MAX(memory_versions.version) + 1`，并且**故意不动** `memories` 行——
  一条搬过来的跃迁是历史，不是记录的新状态。这本身是对的（H/J 的幂等就靠它）。
* 但 `_put_curated` 的「行已存在」分支算的是 `previous.version + 1`。轨迹一旦超前，
  这个数就撞在 `UNIQUE(namespace, memory_id, version)` 上。

值得注意的是「行不存在」分支**早就**从 `MAX(trail)` 推导，注释还写明了理由：
「a memory re-created under the same id must not restart at 1 and collide with the
history it left behind — which would raise inside a consolidation and cost the run
its memory write」。同一个危险从另一个分支照样够得着，只是当时没堵上。

**这不是测试污染。** 生产路径同样够得到：`memory migrate` 把 JSON bundle 的
sidecar 重放到一个已经有记录的 SQL 目标上，之后对那条记忆的任何一次真实跃迁都会
在写入路径里抛异常，代价是这一轮的记忆写入。而且**两个 SQL 适配器都中招**——
SQLite 那条 `INSERT INTO memory_versions` 同样没有 `ON CONFLICT`，离线复现出来的
是一模一样的 `sqlite3.IntegrityError: UNIQUE constraint failed`。

修法是把 `MAX(version)` 查询提到分支外面，两条路径统一成
`max(行版本, 轨迹最大序号) + 1`（`sqlite_store.py` 与 `postgres_store.py` 同步改，
插入路径不多花一次查询）：

```python
last_version = int(highest["version"])          # COALESCE(MAX(version), 0)
if previous is None:
    version = last_version + 1
else:
    version = max(int(previous["version"]), last_version) + 1
```

回归用例 `test_a_write_after_a_sidecar_import_clears_the_trails_ordinal`
（`tests/test_memory_store.py`）**不需要服务器**：写一条 → `set_trust` 到 v2 →
`import_bundle` 一条无序号 sidecar（落到 v3，行仍是 v2）→ 再来一次真实跃迁，
断言序号集合是 `{1, 2, 3, 4}`。修复前它抛 `IntegrityError`，修复后通过。
Postgres 那一半就是 live 用例本身。

live 用例**没有**加清库逻辑，这是有意的：`TRACE_AGENT_MEMORY_DSN` 可能指向一个
真实数据库，测试去 `DELETE` 一个 namespace 就等于替运维做决定。原作者的设计
（固定 `project:pg-test` + 每次调用带唯一 `reason` 后缀以容忍残留）保留原样，
现在它在同一个持久 namespace 上**连跑三遍都绿**——因为撞的是真 bug，修掉就够了。

### L.4 顺带修的 redis live 断言

`test_redis_cache_against_a_live_server` 原本断言从 Redis 读回的 JSON 等于
`first.model_dump_json()`。但按 K.4，`latency_ms` 是**出栈时盖的章、从不回写**，
所以存进去的 payload 里它是 `0.0`，而 `first` 上是真实耗时——这个断言在真服务器上
必然不成立。它以前一直没暴露，只是因为 redis 包没装、用例永远 skip。

改成把契约断言准：`restored.latency_ms == 0.0`、`first.latency_ms > 0.0`、
其余字段 `model_dump_json()` 逐字节相等（拿 `first.model_copy(update={"latency_ms": 0.0})`
比）。这正是「命中不得重放 miss 那一次的耗时」这条性质该有的写法。

### L.5 复验结果

```text
记忆相关 11 个文件，设了 DSN + cache URL（PostgreSQL 17.10 :55432 + Redis 7.2.5 :6379/15）
  → 285 passed, 0 skipped                      # live 断言全部真跑

tests/test_memory_postgres.py 单独连跑三遍（同一持久 namespace）
  → 42 passed / 42 passed / 42 passed          # 含 2 项 live-server

记忆相关 11 个文件，不设 DSN（离线基线）
  → 283 passed, 2 skipped
    2 项 skip 是 test_memory_postgres 的 live-server 用例（未设 DSN 时显式 skip）
    对比 K.5 的 279 passed / 5 skipped：驱动已装（+1）、两个 redis live 用例
    现在能连上本机真服务器（+2）、L.3 新增回归用例（+1），合计 +4

tests/ 全量（含 tests/webui），设了 DSN + cache URL
  → 553 passed, 3 failed, 1 xfailed in 96.43s
    3 项失败仍是 J.5/K.5 记录在案的同一组无关用例，与记忆无关：
      test_eval_contracts.py::test_load_case_discovers_golden_case
        （断言 1 条 root cause，而 case.yaml 里有 RC001/RC002 两条）
      test_grader_robustness.py 两个（打分阈值 0.25>=0.5、0.5>=0.99）
    两个测试文件均为已提交且本轮未改动
```

拆掉临时集群、回到默认环境（不设 DSN）后的最终基线：

```text
tests/ 全量（含 tests/webui）→ 3 failed, 551 passed, 2 skipped, 1 xfailed
  2 项 skip 就是 test_memory_postgres 的两个 live-server 用例，原因写明「DSN is not set」
  3 项失败与上面同一组，未新增
```

### L.6 遗留

1. **临时集群已拆**（`pg_ctl stop` + 删 `D:\pg-e2e-tmp`），H 的数字可按 L.2 的方子
   随时重建复现。5432 上那个常驻服务全程未被触碰。
2. **并发写仍可能算出同一个序号**：两个事务同时 `SELECT MAX(version)` 再 `INSERT`，
   后提交的那个会吃 `UniqueViolation`。这是本改动之前就有的性质（`previous is None`
   分支同理），`UNIQUE` 约束是当前的唯一护栏。H.4 已把分布式锁明确裁剪掉，
   多实例并发写入之前需要先补这一层，而不是指望序号推导。
3. `redis` extra 声明的是 `redis>=5`，实测装到 8.1.0；`RedisCache` 只用到
   `get` / `setex` / `delete` / `scan_iter` / `ping`，跨大版本没有 API 面风险。

### L.7 pgvector：从源码编译安装并跑通 vector 路径

L.2/L.5 复验时机器上没有 pgvector，BYTEA 降级是唯一被真跑到的路径。这一节把缺口补上：
**pgvector 0.8.7 已从源码编译、装进常驻的 PostgreSQL 17.10，并在真服务器上跑通了
`vector` 列 + HNSW 索引 + `<=>` 下推召回**。BYTEA 与 vector 两条路径现在都被实测覆盖。

编译链全部落在 D 盘（按「所有安装都装到 D 盘」的约束），只有 Windows SDK 无法改路径：

```text
VS Build Tools   D:\BuildTools（MSVC 14.44.35207，vcvars64.bat）
pgvector 源码    D:\src\pgvector-0.8.7
构建             nmake /F Makefile.win  PGROOT=C:\Program Files\PostgreSQL\17
                 → D:\src\build.log 末尾 === BUILD_OK ===
Windows SDK      C:\Program Files (x86)\Windows Kits\10（ unavoidable，约 2.5 GB）
```

安装产物拷进 PostgreSQL 17 目录（`nmake install` 的目标位置）：

```text
lib\vector.dll                              281,088 字节
share\extension\vector.control              default_version = 0.8.7
share\extension\vector--*.sql               42 个迁移脚本
include\server\extension\vector\*.h         halfvec.h / sparsevec.h / vector.h
```

在一个**新库** `diting_memory_pgvector` 上复验（不动原来的 `diting_memory`——它的
embedding 列早已是 BYTEA，而 `apply_schema` 有意从不改写已存在列的类型，所以 vector
路径只能在新库上演示）。`CREATE EXTENSION vector` 后，走 DitingAgent 自己的
`PostgresStore`（DSN 指向该库）：

```text
pgvector 探测      store.pgvector == True（pg_schema.apply_schema 探测 pg_extension）
驱动 / 服务器      psycopg2 / PostgreSQL 17.10
embedding 列类型   memories、episodes 均为 USER-DEFINED『vector』（不再是 BYTEA）
vector_index(4)    narrowed=[memories,episodes]  indexes=[memories,episodes]  errors=[]
HNSW 索引          idx_memories_embedding_hnsw / idx_episodes_embedding_hnsw
                   USING hnsw (embedding vector_cosine_ops)
```

召回下推（`vector_search` → `1 - (embedding <=> probe)` + `ORDER BY embedding <=> probe`）
用三条 4 维向量实测，顺序与余弦一致：

```text
写入 m-x=[1,0,0,0]  m-y=[0,1,0,0]  m-z=[0.9,0.1,0,0]，probe=[1,0,0,0]
下推结果  m-x 1.0  →  m-z 0.9939  →  m-y 0.0     # 最近邻正确，正交向量得 0
```

live 测试（DSN 指向 pgvector 库，`--basetemp` 用新路径绕开沙箱临时目录权限）：

```text
TRACE_AGENT_MEMORY_DSN=postgresql://postgres@127.0.0.1:5432/diting_memory_pgvector
pytest tests/test_memory_postgres.py      →  45 passed, 0 skipped（含 2 项 live-server）
                                          （离线：43 passed, 2 skipped）
```

打包口径：`postgres` extra 只声明 Python 驱动（`psycopg2-binary>=2.9`），pgvector 是
**服务器端 C 扩展**，不属于 pip 依赖，需在目标 PostgreSQL 上单独编译/安装（如上）。
代码路径对此是「有则用、无则退」：探测到扩展走 `vector` + HNSW 下推，探测不到自动
退回 BYTEA + 应用层余弦，两者召回结果一致，只是有无 ANN 索引之别（见 §H.4 第 3 条）。

### L.8 装 pgvector 反而暴露的回归：BYTEA 老语料会被误判成 vector

扩展是**按服务器**装的，`CREATE EXTENSION IF NOT EXISTS vector` 从此对每个库都成功；
但 `ADD COLUMN IF NOT EXISTS` 从不改写已存在列的类型。于是「pgvector 装之前建的库」
会出现：`apply_schema` 探测 `pg_extension` 得到 True → 返回 `_pgvector=True`，而
`memories.embedding` / `episodes.embedding` 实际仍是 BYTEA。后果是静默失败，不是报错：

```text
set_embedding(...)      → False（SQL 里的 ::vector 强转对 BYTEA 列不成立）
get_embeddings([...])   → {}
vector_search(probe)    → []      # 召回全空，而日志里 pgvector=True
```

这正是本机 `diting_memory` 的处境（embedding 列 BYTEA，0 条向量），装完扩展立刻踩到。

修法在 `src/trace_agent/memory/store/pg_schema.py`：新增 `_embedding_column_is_vector()`，
经 `to_regclass('memories')` 读 `pg_attribute`/`pg_type` 拿真实 `typname`（走
`search_path`，与 store 自己的非限定查询一致），返回 True/False/None，None 表示
「读不出来」（列不存在、目录查询失败）。`apply_schema` 只在 `pgvector is None`
（即探测路径，也是 `postgres_store.py` 唯一的生产路径）时用它对齐探测结果：

```python
if pgvector is None:
    actual = _embedding_column_is_vector(execute)
    if actual is not None:      # 读不出来就保留探测答案，不猜
        vectors = actual
```

显式传 `pgvector=True/False` 的调用方「已经知道答案」，不付第二次目录查询，
契约由既有用例 `test_an_explicit_pgvector_flag_skips_the_probe` 守住。

回归用例（离线，`tests/test_memory_postgres.py` 的 `_SplitRecorder`：扩展探测与列类型
探测分开应答，正是这个 bug 的形状）：

```text
test_apply_schema_follows_the_bytea_column_over_a_present_extension
test_apply_schema_follows_the_vector_column_when_it_really_is_vector
test_apply_schema_keeps_the_probe_when_the_column_cannot_be_read
```

修完的实测：`diting_memory`（BYTEA + 扩展已装）→ store 报 `pgvector=False`，
BYTEA 写入/读回/应用层余弦召回全部正常；`diting_memory_pgvector`（vector 列）→
仍报 True 并走 HNSW 下推。两个库上 live 各 45 passed；7 个 memory 测试文件离线
202 passed / 2 skipped（skipped 即未设 DSN 的两项 live）。

两个 live 服务同时挂上（DSN 指向 pgvector 库 + Memurai 6379 可 ping），7 个 memory
测试文件一次跑完 **204 passed / 0 skipped**：H.1 表格里当年「live 部分未设 DSN 时显式
skip」的那一栏，现在是真的被跑到了。跑完两个库的测试行都已清空（`TRUNCATE ... CASCADE`，
`store_meta` 的 schema 版本戳保留），扩展、vector 列与 HNSW 索引不受影响。

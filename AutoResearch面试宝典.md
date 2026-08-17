# AutoResearch 面试宝典

> 针对简历项目「AutoResearch：基于 LangGraph 的多 Agent 协同市场调研系统」的全面面试准备材料。
> 内容基于项目**实际代码**（`backend/` 下 graph / nodes / state / tools / manager / main / history / config 等模块），
> 标注了 `文件:行号` 便于你回查原文。建议先读【二、项目全流程】建立主线，再逐节吃透【四、关键设计】，
> 最后用【五、面试深挖问题】自测。

---

## 目录

- [一、两分钟讲清楚这个项目（开场白）](#一两分钟讲清楚这个项目开场白)
- [二、项目全流程（主线）](#二项目全流程主线)
- [三、系统架构图](#三系统架构图)
- [四、关键设计深挖](#四关键设计深挖)
  - [4.1 LangGraph 状态图 + 6 类节点](#41-langgraph-状态图--6-类节点)
  - [4.2 Send 并行扇出 + reducer 自动合并](#42-send-并行扇出--reducer-自动合并)
  - [4.3 interrupt + Command(resume) 人机协同](#43-interrupt--commandresume-人机协同)
  - [4.4 Checkpointer 断点持久化与线程恢复](#44-checkpointer-断点持久化与线程恢复)
  - [4.5 结构化输出容错管线（JSON 提取→字段级清洗→Pydantic 校验→重试）](#45-结构化输出容错管线)
  - [4.6 多源搜索聚合与降级](#46-多源搜索聚合与降级)
  - [4.7 条件路由与反馈分流](#47-条件路由与反馈分流)
  - [4.8 空任务兜底与死循环保护](#48-空任务兜底与死循环保护)
  - [4.9 SSE 实时事件流与 seq 去重](#49-sse-实时事件流与-seq-去重)
  - [4.10 并发防护（双 resume 竞态）](#410-并发防护双-resume-竞态)
  - [4.11 历史持久化设计（独立于 checkpoint）](#411-历史持久化设计独立于-checkpoint)
  - [4.12 python_repl 沙箱安全加固](#412-python_repl-沙箱安全加固)
- [五、面试深挖问题清单（含参考答案）](#五面试深挖问题清单含参考答案)
- [六、简历表述核对与话术优化](#六简历表述核对与话术优化)
- [七、防守反问：如何应对「这不算 Agent」的挑战](#七防守反问如何应对这不算-agent-的挑战)
- [八、你可以反问面试官的问题](#八你可以反问面试官的问题)

---

## 一、两分钟讲清楚这个项目（开场白）

> 面试开场一般会让你介绍项目。用「**问题 → 方案 → 我的角色 → 成果/亮点**」结构，30~60 秒讲完主干，给面试官留追问钩子。

**标准话术（口述版）：**

> 我做了一个叫 AutoResearch 的多 Agent 市场调研系统。背景是：传统人工调研要经历「定主题、查资料、整理数据、写报告」整个链路，耗时且质量依赖个人经验；而直接让大模型一次性生成报告，又存在**幻觉**（数据瞎编）和**不可控**（用户无法中途干预）的问题。
>
> 我的方案是用 **LangGraph 把它编排成一个有状态、可断点恢复、支持人机协同的多 Agent 工作流**：主题输入 → LLM 生成大纲 → **人工确认** → 拆解并行子任务 → **Send 扇出多个 researcher 并行搜索** → 分析师提取结构化数据 → 撰写草稿 → **人工审核** → 定稿导出。人工在「大纲」和「草稿」两个关键节点把关，解决可控性与幻觉问题；并行搜索解决效率问题。
>
> 技术栈上，后端用 **FastAPI + SSE** 做实时交互和流式输出，状态用 LangGraph 的 **Checkpointer** 持久化（SQLite/PostgreSQL 双档），支持后端重启后断点续跑。前端是一个原生 JS 的 demo 页面，有实时日志、确认面板、历史报告回看与 Markdown/Word 导出。
>
> 我主要负责整个项目的架构设计、后端全部代码、LLM 工程化调优和测试。在开发过程中解决了一批比较有代表性的工程问题：比如 **LLM 结构化输出在长 prompt 下的不稳定**（自己写了字段级清洗 + 重试的容错管线）、**子任务拆解失败导致图静默终止**（加了空任务兜底路由）、**并发 feedback 导致双 resume 竞态**（加并发防护）、以及 **SSE 事件重复/遗漏竞态**（seq 去重）等。整个项目有分层测试，单元测试秒级、集成测试用真实 LLM 跑通全流程和异常分支。

**加分钩子（可主动抛出）：**「这个项目里有一个我认为最值得聊的点：把『不可控的 LLM』嵌入到『可控的工程流程』里——通过结构化输出容错、人机确认点、断点持久化这三件事，让大模型应用在真实场景里是可交付、可回滚、不烧钱的。面试官对这个有兴趣的话，我可以展开讲。」

---

## 二、项目全流程（主线）

对应简历那句「主题输入→大纲生成→人工确认→任务拆解→并行搜索→数据提取→报告撰写→人工审核→定稿导出」。**你要能不看代码，一口气把这条链路讲通**，并说出每一步在代码里对应哪个节点/函数。

### 2.1 时序主线

```
用户输入主题 topic
   │
   ▼
POST /research/start
   │  manager.start() 生成 thread_id，后台 asyncio.create_task 执行图  (manager.py:155)
   ▼
图启动：START → supervisor_planner
   │
   ├─ ① supervisor_planner（规划）：LLM 生成大纲 Outline{sections, key_questions}
   │     └─ interrupt({"type":"plan", "outline":...}) → 图暂停，等人工确认  (nodes.py:302)
   │
   ├─【人工确认点1】前端 SSE 收到 interrupt 事件，展示大纲
   │     批准 → feedback(approved=true)   /   打回 → feedback(approved=false, feedback="意见")
   │
   ▼
   Command(resume={approved, feedback}) 恢复图  (manager.py:426)
   │
   ├─ ② route_after_plan 路由 (graph.py:43)
   │      批准 → task_decomposer
   │      打回 → 回 supervisor_planner，带 feedback 重新生成大纲（再循环一次确认）
   │
   ▼
   task_decomposer（拆解）：LLM 基于已确认大纲输出 SubTaskList{tasks:[{id,description,keywords}]}
   │     └─ 拆解失败/为空 → continue_to_researchers 返回 "supervisor_planner" 兜底重规划  (graph.py:81)
   ▼
   continue_to_researchers：Send("researcher", {sub_task:t}) 为每个子任务扇出一个并行分支  (graph.py:84)
   │
   ├─ ③ researcher ×N（并行搜索）：search_web 聚合 Tavily+博查 → 按 URL 去重  (nodes.py:329)
   │     └─ 各分支的 search_results 通过 operator.add reducer 自动合并到一个列表  (state.py:24)
   │
   ▼（superstep 汇合，analyst 只执行一次）
   analyst（分析）：聚合全部搜索结果 → LLM 提取结构化市场数据 JSON  (nodes.py:345)
   │
   ▼
   writer（撰写）：LLM 生成 Markdown 报告草稿  (nodes.py:366)
   │     └─ interrupt({"type":"draft", "report":...}) → 图再次暂停，等人工审核
   │
   ├─【人工确认点2】前端 SSE 收到 interrupt(draft)，展示草稿
   │     批准 / 打回（打回意见含"数据/来源/准确"等关键词 → 回 analyst 重新提取；否则回 writer 重写）
   │
   ▼
   route_after_draft 路由 (graph.py:56)
   │      批准 → finalizer
   │      数据类反馈 → analyst（清空草稿强制重写）  措辞类反馈 → writer
   │
   ▼
   finalizer（定稿）：整合最终反馈，输出 final_report  (nodes.py:406)
   │
   ▼
   END；manager 推送 final 事件，写入历史库 completed  (manager.py:260)
   │
   ▼
GET /research/{thread_id}/report → 拿 Markdown 报告；/export → 导出 Markdown/Word
```

### 2.2 状态流转（ResearchState 每个字段）

`state.py:15` 定义了 `ResearchState(TypedDict)`，你要能说出每个字段**在哪个节点被写**：

| 字段 | 写入节点 | 作用 |
|---|---|---|
| `topic` | 启动时输入 | 调研主题 |
| `outline` | supervisor_planner / feedback 写回 | 大纲（Pydantic Outline） |
| `plan_approved` | supervisor_planner 返回 | 大纲是否通过人工确认 |
| `sub_tasks` | task_decomposer | 拆解出的并行子任务 |
| `search_results` | researcher（`Annotated[list, operator.add]`） | 并行搜索结果自动合并 |
| `sub_task` | Send 传入 | 仅 researcher 分支用，主流程为 None |
| `extracted_data` | analyst | 提炼的结构化市场数据 |
| `draft_report` | writer / feedback 写回 | 报告草稿 |
| `draft_approved` | writer 返回 | 草稿是否通过人工审核 |
| `final_report` | finalizer | 最终报告 |
| `human_feedback` | supervisor_planner / writer 返回 | 最近一次人工反馈，节点消费后置 None |
| `error_log` | 各节点 except | 异常记录（不中断图） |

### 2.3 API 端点一览（main.py）

| 方法 | 路径 | 作用 | 关键实现 |
|---|---|---|---|
| POST | `/research/start` | 启动调研，返回 thread_id | `manager.start()` 后台执行 (main.py:95) |
| GET | `/research/{tid}/stream` | SSE 实时事件流 | `ThreadChannel` + `EventSourceResponse` (main.py:103) |
| POST | `/research/{tid}/feedback` | 人工反馈并恢复执行 | `manager.feedback()`，409 并发防护 (main.py:136) |
| GET | `/research/{tid}/report` | 获取最终报告 | `graph.aget_state()` 查 checkpoint (main.py:164) |
| GET | `/research/history` | 调研历史列表 | `history.list_all()` (main.py:179) |
| GET | `/research/{tid}/detail` | 历史详情（含批改意见） | `history.get()` (main.py:186) |
| GET | `/research/{tid}/history/export` | 导出历史报告 md/docx | 独立于 checkpoint (main.py:196) |
| DELETE | `/research/{tid}` | 删除历史 + checkpoint | `adelete_thread()` (main.py:217) |
| GET | `/research/{tid}/outline/export` | 导出大纲 md/docx | (main.py:255) |
| GET | `/research/{tid}/report/export` | 导出报告 md/docx | (main.py:289) |

---

## 三、系统架构图

```
┌──────────────────────────────────────────────────────────────────┐
│                         前端 (demo.html)                          │
│   EventSource(SSE) 实时日志 / 确认面板 / 历史回看 / 导出按钮          │
│   localStorage 存 thread_id → 刷新自动恢复                          │
└──────────────┬───────────────────────────────┬───────────────────┘
               │ POST /start                   │ POST /feedback
               │ GET /stream(SSE)              │ GET /report 等
               ▼                               ▼
┌──────────────────────────────────────────────────────────────────┐
│                     FastAPI 后端 (main.py)                        │
│  ┌──────────────────────────────────────────────────────────┐    │
│  │  ResearchManager (manager.py)                            │    │
│  │   · threads: dict[thread_id -> ThreadChannel]            │    │
│  │   · _running: set 并发防护                                │    │
│  │   · start / feedback / restore / _execute                │    │
│  │   · NodeStartRecorder 回调捕获 node_start                 │    │
│  └──────────────────────────────────────────────────────────┘    │
│  ┌──────────────────────────────────────────────────────────┐    │
│  │  LangGraph 编译图 (graph.py → build_compiled_graph)      │    │
│  │  START→supervisor_planner→(Send)→researcher×N→analyst    │    │
│  │       →writer→finalizer→END                              │    │
│  └──────────────────────────────────────────────────────────┘    │
└──────────────┬────────────────────────────────┬───────────────────┘
               │ Checkpointer 持久化              │ 独立历史库
               ▼                                ▼
┌──────────────────────────┐     ┌──────────────────────────────┐
│ Checkpoint (sqlite/memory/│     │ ResearchHistory (history.py) │
│  postgres) 保存每次图状态   │     │ · 元数据 / status / 批改意见  │
│  支持断点恢复/线程删除       │     │ · 最终报告全文                 │
└──────────────────────────┘     └──────────────────────────────┘
```

---

## 四、关键设计深挖

> 这一节是面试的核心。每个设计我按「**是什么 / 为什么 / 怎么实现 / 坑与权衡**」展开，都是面试官最可能追的点。

### 4.1 LangGraph 状态图 + 6 类节点

**是什么**：`graph.py:90 build_graph()` 用 `StateGraph(ResearchState)` 组装了 6 类节点（researcher 会被 Send 复制出多个分支，所以是「6 类」而不是固定 6 个执行实例）：

| 节点 | 职责 | LLM 输出类型 |
|---|---|---|
| `supervisor_planner` | 生成调研大纲 | Outline（结构化） |
| `task_decomposer` | 拆解并行子任务 | SubTaskList（结构化） |
| `researcher` | 单个子任务联网搜索 | List[dict]（工具调用） |
| `analyst` | 聚合结果提取市场数据 | dict（JSON） |
| `writer` | 撰写 Markdown 草稿 | str（自由文本） |
| `finalizer` | 整合反馈定稿 | str |

**为什么选 LangGraph**：
- 相比「单 Agent 循环」（LangChain AgentExecutor / ReAct），多 Agent 调研需要**确定性的编排结构**（谁先谁后、哪里要人、哪里要并行），图结构天然表达这种依赖。
- 相比自己用 Python 手写流程编排：图有**状态 schema**、**条件边**、**Send 动态扇出**、**内置 Checkpointer 断点**、**interrupt 人机协同原语**，这些是手写很难做对且容易被你写糊掉的。
- 相比 CrewAI/AutoGen：更底层可控，HITL 和持久化原生支持，可观测性好（每个 superstep 都能拿到）。

**怎么实现（背熟这段）**：
```python
g = StateGraph(ResearchState)
g.add_node("supervisor_planner", supervisor_planner)
g.add_edge(START, "supervisor_planner")
g.add_conditional_edges("supervisor_planner", route_after_plan, {...})
g.add_conditional_edges("task_decomposer", continue_to_researchers, {...})
g.add_edge("researcher", "analyst")
g.add_edge("analyst", "writer")
g.add_conditional_edges("writer", route_after_draft, {...})
g.add_edge("finalizer", END)
g.compile(checkpointer=checkpointer)
```

**坑**：langgraph 1.2.x 把 `add_edge`/`add_conditional_edges` 从顶层函数改成了 `StateGraph` **实例方法**（BUGS.md #1），`AsyncSqliteSaver` 挪到了 `.aio` 子模块，恢复中断必须用 `Command(resume=...)` 而不是 `update_state`。这些 API 变更坑如实讲出来是加分项（说明你在真实踩坑升级）。

**面试追问点**：researcher 节点是同一个函数被 Send 扇出多个实例，LangGraph 的 superstep 会并行跑所有分支，全部完成后**自动汇合**到 `analyst`（因为 `analyst` 是 researcher 出边的下一节点，且没有 Send 再扇出），`analyst` 只执行一次。

---

### 4.2 Send 并行扇出 + reducer 自动合并

**是什么**：`continue_to_researchers`（graph.py:72）返回 `[Send("researcher", {"sub_task": t}) for t in sub_tasks]`，为每个子任务动态创建一个 researcher 并行分支。

**为什么**：子任务数量是 LLM 运行时拆出来的，**不知道数量**，所以不能写成固定 N 个 researcher 节点。Send 允许「运行期按数据动态决定分支数」，这是静态图做不到的。同时注释里明确写了**禁止用 for 循环串行调搜索工具**——那会把并行搜索退化成串行，浪费效率。

**核心机制：reducer 合并**。`state.py:24`：
```python
search_results: Annotated[list, operator.add]
```
LangGraph 约定：`Annotated[T, reducer]` 里第二个参数是 **reducer**。当多个节点（Send 扇出的多个 researcher）同时写 `search_results` 时，LangGraph 不是「后写覆盖」，而是把每个分支的输出用 `operator.add`（list 拼接）**合并**进同一个列表。这就是并行分支结果能自动汇成一份的底层原因。**这是面试必考题，务必讲透**。

**汇合点（superstep）**：所有 researcher 分支完成后，`analyst` 读取合并后的 `search_results` 一次性处理。`analyst` 里 `_MAX_RESULTS_FOR_LLM = 8`（nodes.py:32）控制传给 LLM 的搜索摘要条数，防止 prompt 过长。

**坑与权衡**：
- 结果去重在 `search_web` 里按 URL 做了（tools.py:196），多分支间可能重复。
- 并行分支数量理论上就是子任务数，需要留意 API 并发配额和成本（可作开放题：如何加并发上限？→ 用 `asyncio.Semaphore` 或在 task_decomposer 限制任务数）。
- **空 Send 列表会静默终止图**（详见 4.8）。

---

### 4.3 interrupt + Command(resume) 人机协同

**是什么**：`nodes.py` 的 `supervisor_planner` 和 `writer` 各有一个 `interrupt(payload)` 调用点（两处人工确认），配合 `Command(resume=...)` 实现「图暂停 → 人审 → 恢复」。

**原理（务必讲对）**：
1. 节点执行到 `interrupt({"type":"plan","outline":...})` 时，LangGraph 内部**抛出一个 `GraphInterrupt` 异常**，图的执行被暂停，checkpointer 把当前状态落盘。这就是为什么节点代码里必须：
   ```python
   except GraphInterrupt:
       raise   # nodes.py:311 注释：interrupt 机制依赖异常传播，普通 except 会吞掉中断
   ```
2. 前端收到 SSE 的 `interrupt` 事件后展示大纲/草稿，用户批准或打回。
3. 恢复时，后端把 resume 值作为**图输入**传进去：
   ```python
   graph.astream(Command(resume={"approved": approved, "feedback": feedback}), config)
   ```
   `Command(resume=...)` 会让之前的 `interrupt()` **返回** resume 值，节点拿到 `approved`/`feedback` 后继续往下走。

**resume 值结构**（nodes.py:281 注释）：
```python
{"approved": bool, "feedback": Optional[str]}
# approved=True  → 通过，路由到下一阶段
# approved=False → 携带反馈，条件边路由回本节点（plan）或分流（draft）重做
```

**为什么必须配合 `update_state` 写回已确认内容**：`manager.feedback`（manager.py:411-420）在 `Command(resume)` 之前，会先用 `graph.aupdate_state(config, {"outline": ...})` 把「人工确认过的大纲/草稿」写回 state。原因是：interrupt 恢复时节点会**重跑**（重新进入节点函数），如果不把已确认版本写回，节点会基于旧逻辑**重新生成**一份，导致最终内容与人工看到的**不一致**（manager.py:376 注释）。这是 HITL 一致性最容易踩的坑，主动讲出来是很大的加分项。

**为什么不用「前端直接改 prompt 重跑」**：因为那样没有状态、没有断点、无法审计人审记录；interrupt 的语义是「图暂停在特定位置」，恢复后能精确从该位置继续，而非重头开始。

**坑**：
- `GraphInterrupt` 必须重新抛，`except Exception` 会吞掉（nodes.py:311、400）。
- 早期用 `update_state` + `ainvoke(None)` 恢复会导致图**再次暂停**而不是继续（BUGS.md #1）——只有 `Command(resume)` 会消费中断。
- 打回后 LLM 重写的大纲/草稿可能**与上次内容完全相同**（低温采样稳定），前端若按内容去重会把新中断误判成重连重放而忽略（BUGS.md #21），因此引入 **seq 单调递增**去重（见 4.9）。

---

### 4.4 Checkpointer 断点持久化与线程恢复

**是什么**：`graph.py:149 create_checkpointer()` 按配置返回三种 checkpointer：
- `memory`：`MemorySaver`，进程内，重启丢失（开发/测试用）
- `sqlite`：`AsyncSqliteSaver` + aiosqlite 连接，文件持久化（默认）
- `postgres`：`AsyncPostgresSaver` + psycopg 连接池（生产，Docker 编排）

**为什么需要**：调研流程里有人工确认点，可能暂停几分钟甚至几小时；进程可能重启；用户可能刷新页面。没有断点持久化，一停就全部重来，且白白烧 LLM token。Checkpointer 保存的是**每次执行后的图状态**（含 pending 的 interrupt），恢复后能从暂停位置继续。

**序列化细节（能讲出来很加分）**：state 里放了 Pydantic 模型（Outline/SubTask），langgraph 的 msgpack 序列化器默认不允许反序列化未注册类型（未来版本会从告警变报错）。解决：`JsonPlusSerializer(allowed_msgpack_modules=[("models","Outline"), ...])` 显式声明白名单（graph.py:134，BUGS.md #4）。

**线程恢复 restore**（manager.py:313）：进程重启后 `threads` 字典为空（纯内存），SSE 订阅时若 thread 不在内存，先尝试 `restore(thread_id)`：从 checkpoint 读 `aget_state`，据 `_interrupt_info` 判断——
- 暂停在确认点 → 补发 `node_start + interrupt`（可继续批准/打回）
- 已完成 → 补发 `final`
- 其他 → 补发 `error`
恢复失败（checkpoint 里没有）才返回 404。

**为什么 SQLite 有并发问题**：多进程/进程被强杀导致 `.sqlite-wal/-shm` 残留损坏，`AsyncSqliteSaver` 会无限挂起（BUGS.md #8）。所以测试用独立临时库并清残留，**生产用 PostgreSQL**（docker-compose.yml `CHECKPOINTER: postgres`）。

**资源释放**：`close_checkpointer`（graph.py:188）兼容同步/异步地关闭 saver 自带 close 和底层 conn/连接池，在 FastAPI `lifespan` yield 之后调用（main.py:71），避免 Docker 重启遗留连接（BUGS.md #17）。

**坑**：`AsyncSqliteSaver` 的 aiosqlite 连接需要手动管理，否则事件循环关闭时后台线程清理挂住（BUGS.md #9）；async 上下文查状态要用 `aget_state`，同步 `get_state` 会抛 `InvalidStateError`（BUGS.md #1）。

---

### 4.5 结构化输出容错管线

> 简历上的「JSON 提取→字段级清洗→Pydantic 校验→失败重试」容错管线，对应 `nodes.py:122 _structured_invoke`。**这是你最硬的差异化点之一，面试官一定爱听**。

**背景/问题**：本项目基座是 `deepseek-v4-flash`，它**支持 function_calling（工具调用）和 json_schema 强约束模式**，但本项目未用强约束做结构化输出，而是走 `json_object` 模式——这是架构选择而非能力缺失（详见《为什么不用FunctionCalling》）。更糟的是，**带修改意见的长 prompt 下**（比如打回大纲时把反馈拼进 prompt），模型经常把 `sections`/`keywords` 这类「字符串数组」字段输出成**对象数组**（`[{"title": "...", ...}]`），导致 Pydantic 校验直接失败抛 `OutputParserException`；节点捕获后写 error_log，但 `plan_approved` 仍为 False，条件边路由回自身**无限重试直到 recursion_limit**（BUGS.md #21）。

**解决**：弃用 `with_structured_output`，改为手写容错管线：

```python
def _structured_invoke(model_cls, prompt):        # nodes.py:122
    if "json" not in prompt.lower():
        prompt += "\n请以 JSON 格式输出结果。"       # DeepSeek json_object 要求 prompt 含 "json"
    llm = build_llm().bind(response_format={"type": "json_object"})
    for _ in range(3):                             # 失败重试 3 次
        try:
            resp = llm.invoke(prompt)
            data = _extract_json(_as_text(resp.content))   # ① JSON 提取（容错）
            return _coerce_model(model_cls, data)          # ② 字段级清洗 ③ Pydantic 校验
        except (JSONDecodeError, ValueError, ValidationError) as e:
            last_err = e
    raise ValueError("LLM 结构化输出解析失败（已重试 3 次）")
```

**三个子机制（逐个讲清）**：
1. **`_extract_json`（nodes.py:152）**：先剥离 ```` ```json ```` 代码块包裹整体解析；失败则取「第一个 `{` 到最后一个 `}`」的子串解析（容忍前后杂讯）；再失败抛带原文片段的异常便于定位。
2. **`_coerce_model`（nodes.py:91）+ `_coerce_str_list`（nodes.py:58）**：直接校验失败时，扫描模型字段，对 `List[str]` 类型字段把 dict 元素**取常见文本字段**（title/name/description/content/text/label/tag，nodes.py:71）转回字符串，对嵌套模型列表（如 SubTaskList.tasks）**递归**清洗后重新校验。这直接治好了「字符串数组被输出成对象数组」的病。
3. **`_as_text`（nodes.py:47）**：兼容模型把 content 返回成 list（`[{text:...}]`）的情况，统一转成 str。

**坑与权衡**：
- 这是「工程容错」不是「模型能力提升」：本质是让下游抗住 LLM 输出噪声，而不是让模型变听话。**面试时主动说清这点 = 工程成熟度**。
- 加了「prompt 里出现 json 字样」的兜底（DeepSeek `json_object` 要求），也是踩坑换来的细节（BUGS.md #21）。
- `analyst` 的 `extract_market_data` 不需要模型 schema 约束（返回自由 JSON dict），所以走的是更简单的 `_extract_json`（nodes.py:209）。

---

### 4.6 多源搜索聚合与降级

**是什么**：`tools.py:165 search_web` 聚合「Tavily（国外）+ 博查（国内）」两个搜索源，按 URL 去重后返回统一格式 `[{title, url, content, score, query, source}]`。

**为什么双源**：国内互联网内容（微信公众号、知乎、财经网站）在 Tavily（偏英文索引）里覆盖率差；博查是国产搜索，覆盖中文内容。`language="zh-CN"`（tools.py:108）让英文索引尽量回中文结果，再配合博查补国内内容。

**降级策略（背熟，这是健壮性亮点）**：
- 配置了哪个 key 就启用哪个源；
- 单源失败只跳过该源，不阻断（`errors.append(...)`，tools.py:187）；
- 两源都失败/都未配置 → 返回 `[MOCK]` 模拟结果（tools.py:205），**保证图流程在无 key 环境也能完整跑通**（`_mock_search`，tools.py:81）。
- 按 URL 去重，保留先到达的源（test_search.py 有专门测试）。

**在 researcher 节点里怎么用**（nodes.py:336）：`query = " ".join(sub_task.keywords)`（无 keywords 时用 description 兜底），`max_results` 从配置读。返回的 `List[dict]` 与 `search_results` 的 `operator.add` reducer 天然兼容。

**坑**：`SearchResult` 模型里 `score` 只有 Tavily 会返回，博查是 None——所以分析师 prompt 里对分数要容错。博查响应结构是 `data.webPages.value`（兼容 Bing Search API 结构，tools.py:149），`summary` 优先于 `snippet`（test_search.py 断言过）。

---

### 4.7 条件路由与反馈分流

**是什么**：两处条件边 + 一处反馈分流：
1. `route_after_plan`（graph.py:43）：`plan_approved` True → `task_decomposer`；False → 回 `supervisor_planner` 带反馈重新规划。
2. `continue_to_researchers`（graph.py:72）：见 4.8。
3. `route_after_draft`（graph.py:56）：草稿审核后的三分支路由。

**route_after_draft 的分流逻辑（重点）**：
```python
if state.get("draft_approved"):
    return "finalizer"
if state.get("human_feedback") is None and _last_error_node(state) in ("writer", "analyst"):
    return "finalizer"      # 兜底：失败且无反馈 → 用现有草稿定稿，切断死循环
feedback = state.get("human_feedback") or ""
if any(kw in feedback for kw in _DATA_FEEDBACK_KEYWORDS):
    return "analyst"        # 数据/来源/准确/数字/统计/对比/引用 → 重新提取数据
return "writer"             # 措辞/结构类 → 直接重写
```

**为什么按关键词分流**：草稿被打回，可能是「数据不对」（市场规模的数字、来源、统计）也可能是「写得不好」（措辞、结构、篇幅）。数据类问题要回到 `analyst` **重新提取数据**（并清空 `draft_report` 强制 writer 重写，nodes.py:361），措辞类问题只回 `writer` 重写即可，**省一次 analyst 的 LLM 调用**。`_DATA_FEEDBACK_KEYWORDS`（graph.py:37）覆盖「数据/来源/搜索/准确/事实/数字/统计/对比/引用」。

**坑（可被追问）**：
- 关键词匹配是**启发式**：如果用户的反馈没命中关键词（比如「我觉得第一章太长」），会落到 writer——这其实是合理默认（措辞类）。面试官若问「那如果用户反馈"数据"二字不出现但实际是数据问题呢？」→ 承认是启发式取舍，可改进方向是让 LLM 判断反馈类型（多一次调用）或用带标签的分类模型，工程上选择便宜的启发式。
- `_last_error_node`（graph.py:48）解析 `error_log` 最后一条 `"节点名: 错误"`，用于 4.8 的死循环保护。

---

### 4.8 空任务兜底与死循环保护

**问题 1：空 Send 静默终止**。`task_decomposer` 拆解失败/返回空 `tasks` 时，`continue_to_researchers` 若返回**空的 Send 列表**，LangGraph 会认为「无事可做」直接结束图：没有 interrupt、没有 final_report，`_execute` 的两个判断分支都不命中，前端**永久等待**（BUGS.md #13，严重）。解决：
```python
# graph.py:81
sub_tasks = state.get("sub_tasks") or []
if not sub_tasks:
    return "supervisor_planner"   # 回规划节点重新规划，而不是静默结束
return [Send("researcher", {"sub_task": t}) for t in sub_tasks]
```
再加第二层兜底：`_execute` 里「无中断且无 final_report 就结束」→ 推送 `error` 事件（manager.py:274），兜住所有异常终止路径。

**问题 2：失败自环烧 LLM**。`writer`/`analyst` 连续失败（LLM 不可用）时，`draft_approved` 恒 False、`human_feedback` 恒 None，`route_after_draft` 每次路由回自身重试，每轮重新调 LLM，直到 `recursion_limit`（默认 25）——白烧大量 token（BUGS.md #16，严重）。解决：`route_after_draft` 加兜底——**最近一次错误来自 writer/analyst 且本轮无人工反馈时，直接用现有草稿路由到 finalizer 定稿**，切断失败自环（graph.py:64）。

**这两个兜底是「把不可控 LLM 放进可控工程」最直接的体现，面试讲出来非常加分。**

---

### 4.9 SSE 实时事件流与 seq 去重

**是什么**：`main.py:103` 的 `/stream` 用 `EventSourceResponse`（sse-starlette）推送四类事件：
- `node_start` / `node_end`：节点开始/完成（回调捕获）
- `stream`：LLM 逐 token 增量（打字机效果）
- `interrupt`：人工确认点
- `final` / `error`：完成/失败

**ThreadChannel（manager.py:70）核心设计**：
1. **历史 + 实时广播**：`history` 是 `deque(maxlen=300)`（防无界内存，BUGS.md #14），`publish` 写入历史并广播给所有订阅者。
2. **订阅时同步复制历史快照**（manager.py:84）：`subscribe()` 是**同步方法**，执行期间不让出事件循环，所以「复制历史」和「后续实时事件」之间不会交错——**既不重复也不遗漏**（修复 BUGS.md #18 的补发/实时竞态）。
3. **seq 单调递增去重**（manager.py:82、103）：每条事件带递增 `seq`。前端 `showReview` 用 `seq <= lastInterruptSeq` 判断：断线重连重放的旧中断（seq 小）被忽略，打回后**新产生**的中断（seq 大）即使**内容与上次完全相同**也会重新弹确认面板（修复 BUGS.md #21）。
4. **流式增量缓冲**（manager.py:193）：`stream_mode=["updates","messages"]` 双模式；messages 模式下按「≥80 字符 OR ≥0.3 秒」阈值聚合成一条 stream 事件，避免海量小事件压垮 SSE（`_STREAM_FLUSH_CHARS/_STREAM_FLUSH_INTERVAL`）。
5. **瞬时事件不进历史**：`publish_live`（manager.py:109）只广播给活跃订阅者、不写历史——token 增量高频且无意义，进历史会污染快照和塞满 deque。

**NodeStartRecorder（manager.py:119）**：`on_chain_start` 回调里，用 `kwargs["metadata"]["langgraph_node"]` 拿节点名，并用 `kwargs["name"] == node` **精确过滤「节点本身」的 run**——因为节点内部的 LLM run 也继承了这个 metadata，光看 `langgraph_node` 会误报（BUGS.md #11）。

**坑（踩过的三个）**：
- 早期生成器手动拼 `data:` 前缀，和 `EventSourceResponse` 自带的叠加成 `data: data:` 双重前缀（BUGS.md #5）。
- resume 后 `supervisor_planner` 重跑返回的 `node_end` 里 `outline` 是 **Pydantic 对象**，`json.dumps` 直接抛 `TypeError: Object of type Outline is not JSON serializable`，导致**整个 SSE 响应生成器异常退出**、连接中断、后续事件全丢（BUGS.md #6，严重）。解决：`_to_jsonable`（manager.py:31）递归把 BaseModel → dict。
- 前端 EventSource 断线**自动重连**，重连后服务端补发历史 → 需要按内容/seq 去重避免重复弹面板（BUGS.md #18、#21）。

---

### 4.10 并发防护（双 resume 竞态）

**问题**：对同一 thread_id **并发/重复提交 feedback**（前端双击、start 后立即反馈）时，会同时启动多个后台 `_execute`，对同一 checkpointer 并发执行 `astream(Command(resume=...))` → LangGraph 并发恢复同一线程，抛异常或状态错乱（BUGS.md #12，严重）。

**解决**：`ResearchManager._running` 集合（manager.py:150）。
- `start` / `feedback` 在**首个 `await` 之前同步**检查并标记（manager.py:383-385）：检查 + 标记之间无 await，**事件循环不会切换**，两个并发请求不可能同时通过——这是单线程 asyncio 模型下做「临界区」的正确姿势，一定要讲对。
- `_execute` 的 `finally` 释放标记（manager.py:292）。
- 图仍在运行或无 pending interrupt 时，feedback 端点返回 **409**（未暂停）/ **400**（内容不合法）/ **404**（未知线程）（main.py:144-160）。

**追问点**：为什么「检查+标记之间无 await」就能防并发？因为 asyncio 是**单线程协作式调度**，协程切换只发生在 `await` 处。只要临界区里没有 `await`，就等效于同步临界区。这是对 asyncio 并发模型的理解题，答好很加分。

---

### 4.11 历史持久化设计（独立于 checkpoint）

**是什么**：`history.py` 用独立 SQLite（默认 `data/history.sqlite`）存：元数据、status、`feedbacks`（每次人工批改：stage/approved/feedback/at）、最终报告全文。

**为什么独立于 checkpoint**（history.py:1 模块注释，能讲出来很加分）：
1. checkpoint 每步保留完整状态快照（支持 time travel），但项目只读取**最新**状态；且 `human_feedback` 在最新状态里被节点消费后置 None（nodes.py:291、381）——要回溯每次意见得翻状态快照、依赖 checkpoint 存活，不是可查询的业务记录；
2. SSE 事件历史只在内存（`ThreadChannel.history`），完成后 1 小时清理、进程重启即丢；
3. 最终报告虽然也在 checkpoint 里，但**历史功能不应依赖 checkpoint 存活**（换库/删 checkpoint 后仍可查看）。

**生命周期处理**：
- `connect()` 时把上次进程遗留的 `running` 记录标记为 `interrupted`（history.py:57）——进程重启后那些线程的前端会话（SSE / thread_id）已断，业务上不再自动续跑，如实标成中断而非永远「进行中」。（技术上 checkpoint 仍在，可经 `restore()` 重新接回，见 4.10）
- 写入顺序保证：`_start_and_execute` 先 `history.create` 再执行图（manager.py:170），`finish`/`fail` 在 final/error 事件时写入，避免竞态。
- 写失败静默忽略（`_safe_history`，manager.py:294），不影响调研主流程。
- 删除调研 = 历史记录 + checkpoint 线程（`adelete_thread`）+ 内存 threads **三处一起删**（main.py:217-234），运行中的线程拒绝删除（409）。

---

### 4.12 python_repl 沙箱安全加固

**是什么**：`tools.py:225 python_repl` 提供「在隔离 Python 解释器里执行分析代码」能力（给 analyst 算数用，替代停维护的 langchain-experimental）。**但 exec 沙箱极易逃逸**，这里做了两层防护：

1. **受限 builtins**（tools.py:25）：只暴露 `_SAFE_BUILTINS` 白名单（不含 `__import__`），拦截 `import`。
2. **AST 静态校验**（tools.py:35 `_validate_sandbox_code`）：拦截两类危险代码——
   - 任何 `import` / `from ... import ...` 语句；
   - **任何 dunder 属性访问**（`attr.startswith("__") and endswith("__")`，如 `__class__`、`__subclasses__`、`__globals__`）。

**为什么需要第二层**：受限 builtins 只能拦 `import`，但拦不住**属性链反射逃逸**——经典攻击是 `().__class__.__bases__[0].__subclasses__()` 拿到类列表，再用 `catch_warnings.__init__.__globals__["sys"]` 拿回完整模块，进而 `os.system()` 执行任意命令（RCE）（tools.py:38 注释、BUGS.md #15，严重）。AST 校验在 exec 前就把这类访问挡掉。

**诚实声明（重要）**：代码注释明说这是「安全加固而非完美隔离，仅应在受信任的分析场景使用」（tools.py:45）。面试时主动说出这句 = 有安全意识、不过度承诺。若面试官追问「还有没有更安全方案」→ 提：`PyMiniRacer`/`Deno` 子进程隔离、`RestrictedPython`、`seccomp`、或**干脆不要让 LLM 直接执行任意代码**（改为结构化 tool 调参），并且生产环境绝不对不可信输入开放。

---

## 五、面试深挖问题清单（含参考答案）

> 按主题分组。每一题我都写了「回答要点」，建议你合上文档先自己答一遍，再对照补充。
> 题目后面标注了对应代码位置，方便你回查。

### A. 项目总览与动机

**A1. 为什么做这个项目？解决了什么痛点？**
- 传统人工调研链路长、依赖个人经验；
- 直接让 LLM 一次生成报告 → 幻觉（数据瞎编）+ 不可控（不能中途干预）+ 不可复用；
- 方案：多 Agent 工作流 + 两道人工确认点 + 断点持久化，让 LLM 输出「可控、可审、可回滚」。

**A2. 这个项目是「真实被用」还是「课程/比赛项目」？**
诚实作答：个人项目，但按生产级标准做的——有 Docker 部署、PostgreSQL 持久化、分层测试（单元 + 真实 LLM 集成）、BUGS.md 记录 23 个踩坑点。强调「工程化」而非「Demo」。

**A3. 项目的核心难点是什么？**
可以答 3 个：① LLM 结构化输出不可靠（长 prompt 下输出对象数组 → 自研容错管线）；② 人机协同的一致性（写回已确认内容、Command(resume) 语义）；③ 异常路径处理（空 Send 静默终止、失败自环、并发双 resume、SSE 竞态）。

**A4. 如果让你重新设计，会改什么？（开放题）**
参考答案方向：
- 结构化输出：当前基座已支持 json_schema / function_calling 强约束，可迁移到 `with_structured_output` 的强约束方法；未来换旗舰模型（GPT/Claude 等）强约束更稳，可进一步简化容错管线（容错保留作兜底）；
- 数据层：analyst 结果落库（目前只存 final_report），支持增量更新；
- 搜索质量：加 re-rank 重排（简历里 RAG 项目做了，可迁移）、引用溯源到段落级；
- 可观测性：接入 LangSmith 已预留（`LANGCHAIN_TRACING_V2`），可加每个 superstep 的耗时/token 统计；
- 并发：给 Send 分支加并发上限。

### B. 多 Agent 架构

**B1. 为什么用 LangGraph 而不是 LangChain Agent / CrewAI / AutoGen？**
- 单 Agent（ReAct 循环）不适合确定性多阶段流水线，无法表达「哪里要人、哪里要并行」；
- CrewAI/AutoGen 更高层但封装重，HITL、checkpointer、Send 这类原语不如 LangGraph 透明可控；
- LangGraph 提供：状态 schema、条件边、Send 动态扇出、interrupt、checkpointer，正好契合「确定编排 + 动态并行 + 人机协同」的需求。

**B2. 你的 6 个节点里，哪些是「真正的 Agent」？哪些只是函数？**
诚实且专业的答法：每个节点都由 LLM 驱动做**决策**（生成大纲、拆解任务、提取数据、写报告），但它们是**单轮任务型节点**，不是自主循环型 Agent。researcher 是工具调用型（搜索）。真正「自主」的 Agent 应该有「规划→行动→观察→反思」的循环决策能力。本项目更接近「**可控的 LLM 工作流编排**」——这是工程落地的取舍：自主性高 → 不可控、成本高、难 HITL。**主动区分概念，比吹自己是 Agent 加分**。

**B3. 子任务是怎么被并行执行的？并行度多少？**
Send 动态扇出，分支数 = LLM 拆出的子任务数；LangGraph superstep 并行跑所有 researcher 分支，全部完成自动汇合到 analyst。并行度受子任务数和模型 API 并发限制（可提改进方向：semaphore 限流）。

**B4. 如果两个 researcher 的结果相互依赖，怎么处理？**
当前子任务是**独立**的（按大纲章节/关键问题切分），天然可并行。若出现依赖，LangGraph 支持多层图或先做依赖分析再建图；也可以在一个 researcher 里串行完成依赖步骤。

**B5. 多 Agent 之间如何通信？**
通过**共享的图状态**（ResearchState）——每个节点读 state、返回局部更新，LangGraph 用 reducer 合并。没有 agent 间的直接消息传递，通信是隐式的（通过状态），这是状态图 vs 消息传递式（如 AutoGen）的区别。

### C. 人机协同（HITL）

**C1. interrupt() 的原理是什么？为什么节点里要重新抛 GraphInterrupt？**
interrupt 内部通过抛 `GraphInterrupt` 异常暂停图，checkpointer 落盘。如果节点用 `except Exception` 把它吞掉，图的执行流就被打断且无法恢复（nodes.py:311 注释）。恢复必须用 `Command(resume=...)` 作为图输入，interrupt() 才会返回 resume 值。

**C2. update_state 和 Command(resume) 的区别？**
`update_state` 只更新普通状态字段（如把人工确认的 outline 写回），**不消费中断**；`Command(resume=...)` 作为图输入，才会让 `interrupt()` 返回并继续执行。两者配合：先 `update_state` 写回已确认内容保证一致性，再 `Command(resume)` 恢复（BUGS.md 附录）。

**C3. 为什么打回后要把确认的大纲/草稿写回 state？**
interrupt 恢复时节点会重跑，若不写回已确认版本，节点会重新生成一份，导致最终内容与人工审阅的不一致（manager.py:376）。「人工确认的内容」必须成为流程的**事实来源**。

**C4. 人工打回后，如何决定回 analyst 还是 writer？**
`route_after_draft` 用 `_DATA_FEEDBACK_KEYWORDS` 关键词启发式：命中「数据/来源/准确/数字/统计/对比/引用」→ analyst 重新提取（并清草稿强制重写）；否则 → writer 直接重写。承认是启发式，可改进为 LLM 判断反馈类型。

**C5. 如果用户在大纲确认点直接关闭页面/不反馈怎么办？**
图暂停在 interrupt，checkpointer 已保存状态；前端把 thread_id 存 localStorage，重新打开页面自动恢复（tryRestore），服务端补发 interrupt 事件可继续。长期不反馈也不消耗 token（图是暂停的）。

### D. 状态管理与持久化

**D1. Annotated[list, operator.add] 的 reducer 机制是什么？**
LangGraph 中 `Annotated[T, reducer]` 的第二个参数是合并函数。默认行为是「后写覆盖」；指定 `operator.add` 后，多个节点写入同一字段时，值会**拼接合并**而不是覆盖。这正是 Send 扇出多分支结果能自动汇总的机制（state.py:24）。

**D2. Checkpointer 的三种实现怎么选？**
memory：单进程开发测试，重启丢；sqlite：文件持久化，适合单实例中小规模；postgres：生产多实例，解决 sqlite 并发写问题（BUGS.md #8）。配置 `CHECKPOINTER` 环境变量切换。

**D3. 进程重启后，暂停在确认点的调研如何恢复？**
SSE 订阅时 thread 不在内存 → `restore()` 从 checkpoint `aget_state` 重建 ThreadChannel，据 `_interrupt_info` 补发 interrupt/final/error 事件（manager.py:313）；前端再提交 feedback 即可继续。

**D4. 为什么历史库独立于 checkpoint？**
checkpoint 保留每步状态快照（项目只用最新状态），且 human_feedback 消费后置空——它不是可查询的业务档案；SSE 历史在内存会丢；历史功能不应依赖 checkpoint 存活。所以历史（元数据+批改意见+报告全文）落独立 SQLite（history.py:1 注释）。

### E. LLM 工程化

**E1. 为什么不用 with_structured_output？**
基座 `deepseek-v4-flash` 支持 function_calling（工具调用）和 json_schema 强约束模式，但未用它们做结构化输出是架构选择（workflow 工具直接调用）；且 json_object 在长 prompt 下会输出「对象数组」污染 schema。因此未用 `with_structured_output`，改为手写「invoke → JSON 提取 → 字段级清洗 → Pydantic 校验 → 重试 3 次」管线（nodes.py:122）。

**E2. 字段级清洗是怎么做的？**
`_coerce_model` 扫描模型字段：对 `List[str]` 字段把 dict 元素取 title/name/description/content/text/label/tag 转回字符串；对嵌套模型列表递归清洗（nodes.py:58-119）。

**E3. DeepSeek 输出不稳定具体表现在哪？怎么定位？**
表现：sections/keywords 从字符串数组变成对象数组、输出带 ```json 包裹、content 可能是 list。定位：靠 `_extract_json` 失败时抛出**带原文片段**的异常（nodes.py:170），配合 BUGS.md 记录复盘。工程手段：容错 + 重试 + 兜底，而不是指望模型变听话。

**E4. 为什么每个节点 prompt 都要注入当前时间？**
`_time_hint()`（nodes.py:35）：让模型明确「现在」是哪一天，检索与撰写时优先采用最新信息、避免引用过时数据。

**E5. token 成本怎么控制？**
- 传给 analyst 的搜索摘要限 8 条（`_MAX_RESULTS_FOR_LLM`）；
- writer 的 extracted_data JSON 截断到 6000 字符（nodes.py:249）；
- 打回措辞类只重写 writer、数据类才回 analyst（省一次调用）；
- 失败自环保护避免无限重试烧 token（4.8）；
- 暂停在确认点不消耗 token。

### F. 搜索与工具

**F1. Tavily + 博查双源的价值和取舍？**
国内内容（公众号/知乎/财经）在 Tavily 覆盖率差，博查补中文源；单源失败只跳过该源；全失败回 MOCK 保证流程不断。按 URL 去重。

**F2. 搜索结果是 dict 列表，和 SearchResult Pydantic 模型的关系？**
`search_web` 内部构造 `SearchResult(...).model_dump()` 返回 dict，保证可 JSON 序列化且与 state 的 operator.add reducer 兼容（tools.py:86-97）。

**F3. 搜索结果去重和排序怎么做？**
按 URL 去重（保留先到源）；未做重排（面试官若追问 → 提简历 RAG 项目里的 MMR/双路混合检索可作为迁移方案）。

**F4. python_repl 为什么安全？真的绝对安全吗？**
两层：受限 builtins（拦 import）+ AST 校验（拦 import 语句和 dunder 属性访问）。**不绝对安全**——是安全加固而非完美隔离，仅在受信任场景使用（tools.py:45）。真安全方案：子进程隔离、RestrictedPython、或根本不让 LLM 执行任意代码。

### G. 后端工程

**G1. SSE 和普通 HTTP 轮询的区别？为什么用 SSE？**
SSE 单向实时推送、EventSource 原生断线重连；比 WebSocket 轻（不需要双向）。场景是「服务端 → 前端」单向事件流，SSE 正合适。

**G2. SSE 历史补发与实时推送如何避免重复/遗漏？**
`subscribe()` 是同步方法，执行期间不让出事件循环 → 复制历史快照与后续 publish 之间无交错（manager.py:84-94）。生成器只消费订阅队列，删除手动补发逻辑（main.py:117-132）。

**G3. 流式 token 是怎么推到前端的？**
`stream_mode=["updates","messages"]`；messages 模式拿 LLM 增量，按「≥80 字符 OR ≥0.3 秒」聚合后 `publish_live` 广播（不进历史）；节点切换时先 flush 残留增量（manager.py:193-250）。

**G4. 并发 feedback 怎么防双 resume？**
`_running` 集合 + 检查标记间无 await（asyncio 单线程协作式调度，await 处才切换），start/feedback 首个 await 前同步检查并标记，_execute finally 释放；运行中返回 409（manager.py:383-392）。

**G5. 为什么测试里 httpx 要 trust_env=False？**
系统存在 http_proxy，httpx 默认读代理会把本地请求转发导致 startup 期间 502（BUGS.md #7）。测试直连本机。

**G6. FastAPI lifespan 做了什么？**
启动：create_checkpointer → build_compiled_graph → history.connect → 注入 app.state；退出：close_checkpointer 关闭 sqlite 连接/postgres 连接池 + history.close（main.py:59-72）。

### H. 并发与可靠性

**H1. threads 字典和事件历史如何防内存泄漏？**
`ThreadChannel.history` 用 `deque(maxlen=300)`；图完成后 `_schedule_cleanup` 1 小时后从 threads 移除（留重连窗口）；checkpoint 仍在所以 /report 不依赖内存字典（manager.py:302-308，BUGS.md #14）。

**H2. 节点异常如何不影响整图？**
每个节点 try/except 包住，异常写入 `error_log`（不 raise），图继续往下走；配合路由兜底（失败自环保护）。interrupt 节点的 GraphInterrupt 单独 re-raise。

**H3. 删除调研时做了哪些清理？**
历史记录 + checkpoint 线程（`adelete_thread`）+ 内存 threads 三处一起删；运行中拒绝删除（409）；前端同步关闭 SSE、清 localStorage（main.py:217-234）。

**H4. 如果调研中途 LLM 挂了，会怎样？**
节点捕获异常写 error_log；若卡在 writer/analyst 且无反馈 → 兜底路由到 finalizer 用现有草稿定稿（不烧死循环）；若在搜索阶段 → search_web 单源失败跳过、全失败回 mock。整体原则：**逐级降级，流程不中断**。

### I. 安全与质量

**I1. CORS 全放开有什么风险？**
开发期 `allow_origins=["*"]`（main.py:80）。生产应收紧到指定域名，否则任意站点可跨域调用本服务（消耗你的 API 额度）。面试答出「知道这是开发配置，生产要收」即可。

**I2. 你如何评估/验证报告质量？**
- 集成测试断言结构（有 outline/draft/final、无 error_log、重写后内容不同）；
- prompt 层要求「无法确定标注 null 而不是编造」（nodes.py:228）；
- 人机协同点人工把关（大纲 + 草稿两道）。
诚实：当前没有自动质量评估（LLM-as-Judge），是可改进方向。

**I3. 密钥管理怎么做的？**
全部从环境变量读（config.py），`.env` 不入库，`.env.example` 是占位符；Docker 通过环境变量注入，绝不硬编码。

### J. 综合/开放

**J1. 简历写「准确率 62%→81%」是哪个项目的？能展开讲讲评价指标吗？**
注意区分：这是简历里**校园知识库 RAG 项目**（200 份真实校园资料测试集，端到端准确率，双路混合检索+MMR 重排），不是 AutoResearch。**面试时别串项目**。AutoResearch 侧的证据是集成测试跑通 + 结构化容错验证。

**J2. 如果换 GPT-4o/Claude，你的代码要改什么？**
只需改 `.env` 的 `*_BASE_URL/_MODEL/_API_KEY`（config.py 注释），代码零改动——因为统一走 OpenAI 兼容接口。结构化输出管线可保留（更强的模型输出更稳，容错管线成本更低，属于冗余防御）。

**J3. 这个项目对你最大的成长是什么？**
答：把「LLM 能力边界」和「工程系统性」结合起来——理解了为什么 LLM 应用落地的瓶颈不是写 prompt，而是**状态管理、异常路径、HITL 一致性、可观测性**这些工程问题；也建立了「逐级降级、失败兜底」的系统设计习惯。

**J4. 你觉得自己简历里哪些点会被挑战？提前准备好答案。**
- 「6 节点工作流」——实际是 6 类节点（researcher 扇出 N 个），描述准确；
- 「多 Agent」——要能承认这是「LLM 工作流」而非自主 Agent（见 B2、第七节）；
- 「SQLite/PostgreSQL 双档持久化」——实际是 checkpointer 两种可切换实现，sqlite 有并发缺陷、生产用 postgres（见 D2）。

---

## 六、简历表述核对与话术优化

> 把简历每句话与真实代码对照，找出**容易穿帮**的地方，并给出口径。

| 简历表述 | 真实代码 | 核对结论 / 口径建议 |
|---|---|---|
| 「6 节点工作流」 | `graph.py:94-100` 注册 6 个节点 | ✅ 准确，但建议口述成「6 类节点，researcher 会被 Send 扇出为多个并行实例」 |
| 「Send API 将子任务并行扇出为多个 researcher 分支」 | `graph.py:84` `[Send("researcher", {"sub_task": t}) ...]` | ✅ 准确 |
| 「operator.add reducer 自动合并各分支搜索结果」 | `state.py:24` `Annotated[list, operator.add]` | ✅ 准确，这是必考，要能讲原理 |
| 「空任务兜底路由」 | `graph.py:81` 空 tasks → 回 supervisor_planner | ✅ 准确 |
| 「interrupt()+Command(resume) 实现人工介入」 | `nodes.py:391` `writer` 的 `interrupt` + `manager.py:426` `Command(resume)` | ✅ 准确，但要能讲清 GraphInterrupt 异常传播 |
| 「Checkpointer（SQLite/PostgreSQL 双档持久化）」 | `graph.py:149` memory/sqlite/postgres 三选一 | ⚠️ 建议说成「Checkpointer 支持 SQLite/PostgreSQL 两种持久化（可切换，另有 memory 模式）；生产用 PostgreSQL」 |
| 「后端进程重启后调研状态不丢失、可断点续跑」 | `manager.py:313` `restore()` + checkpoint | ✅ 准确，有集成测试 `test_restore_after_restart` 背书 |
| 「JSON提取→字段级清洗→Pydantic校验→失败重试」 | `nodes.py:122` `_structured_invoke` | ✅ 准确，这是最硬的亮点，详述见 4.5 |
| 「多Provider可切换」 | `config.py` `llm_provider: deepseek\|siliconflow` | ✅ 准确 |
| 「Tavily+博查多源搜索聚合去重」 | `tools.py:165` `search_web` | ✅ 准确 |
| 「深入理解 ReAct/Plan-Execute、工具调用」 | `nodes.py`（节点即 Plan-Execute 思想的图化）、`tools.py` | ⚠️ 项目里**没有**用 LangChain AgentExecutor 跑 ReAct 循环；建议措辞为「用 LangGraph 把 Plan-Execute 思想落成显式图」 |
| 「面向市场调研系统」 | 核心代码已实现 | ✅ 完整度较高，含历史/导出/恢复/删除全功能 |

**简历可微调的话术（让面试官问到你擅长的点）：**
- 把「准确率 62%→81%」只在 RAG 项目出现，AutoResearch 项目强调「**23 个踩坑 Bug 记录 + 分层测试 + Docker/PostgreSQL 生产化**」这类工程证据。
- 把「个人项目」定位成「**按生产级标准做的端到端项目**」，主动讲可观测性（LangSmith 预留）、部署（docker-compose）、测试（集成测试用真实 LLM）。

---

## 七、防守反问：如何应对「这不算 Agent」的挑战

> 面试官可能说：「你这个其实就是个用 LangGraph 串起来的工作流（workflow），不是真正的 multi-agent system，多 Agent 不是应该让 Agent 之间自由对话吗？」——这是**全场最重要的一道压力题**，答好了直接立住「工程派」人设。

**参考答案（三步走）：**

1. **先承认事实，体现诚实**：
   > 您说得对，准确讲这是一个「**由 LLM 驱动的确定性编排工作流 + 人机协同**」，不是让 Agent 在开放空间里自由对话的多智能体系统。我区分得清这两个概念。

2. **给出「为什么不自由对话」的工程理由**（这是核心，要果断、专业）：
   > 因为调研报告这个场景，**正确性和可控性 > 自主性**。自由对话式多 Agent 有 3 个问题：一是结果不可控、容易跑偏甚至互相放大幻觉；二是难加人工确认点——用户要的是「大纲我点头了才往下走」，而不是 Agent 自己绕一圈给我份报告；三是成本不可预期、难调试。而我把「规划→拆解→并行执行→分析→撰写」的**决策过程交给 LLM**（每个节点都是 LLM 在决策），把**执行顺序和边界交给图**，把**关键决策点交给人类**——这恰好是 LangChain 官方文档里 workflow（确定性）和 agent（自主循环）的区分，我选择了 workflow 派，这是对**可交付性**负责。

3. **展示「我也懂 Agent 派」**（证明不是不会，是取舍）：
   > 如果场景需要更高自主性，我也清楚怎么做：比如在 researcher 之后加一个「评估→是否补充搜索→反思」的**反思循环节点**（LangGraph 用 `add_conditional_edges` 自环就能实现），或者在关键节点引入带 ReAct 循环的 AgentExecutor 子图。但当前调研场景下，两轮人工确认已经把质量兜住了，再上自主循环是过度设计。**系统的边界应该由场景决定，不是由技术名词决定。**

**要点**：全程不慌、不争辩、观点清晰。面试官抛出这个问题很多时候是想看你会不会**被概念吓住**或**硬吹自己是 Agent**。你主动、清晰地区分，反而显得比同届生更成熟。

---

## 八、你可以反问面试官的问题

> 结尾反问是展示思考深度和「双向选择」姿态的机会，挑 2~3 个问。

1. 「贵团队现在做 LLM 应用，**Agent 落地卡得最多的点**是模型能力还是工程化（状态管理/HITL/评估）？我想知道真实生产环境里大家的判断。」
2. 「团队在**多 Agent/工作流编排**上，现在更倾向 LangGraph 这类图方案，还是自研框架？为什么？」
3. 「如果我来做这个岗位，第一个月最希望我上手的是**现有系统的某块**，还是**新的实验方向**？」
4. 「对于应届生做 LLM 应用，您最看重的是**能吃透模型底层的**，还是**能把应用工程做扎实的**？」
5. 「你们目前对**评估**（LLM 输出质量怎么评）是怎么做的？这块是我在项目里最想补强的。」

---

## 附：速记卡（面试前 5 分钟扫一眼）

- **图结构**：START→planner→(Send)→researcher×N→analyst→writer→finalizer→END，planner 和 writer 各有一个 interrupt 人工确认点。
- **并行合并**：`Annotated[list, operator.add]` → 多分支结果自动 list 拼接。
- **恢复**：`update_state` 写回已确认内容 + `Command(resume=...)` 才消费中断；GraphInterrupt 必须 re-raise。
- **结构化输出**：json_object → `_extract_json` → `_coerce_model` 字段级清洗 → 校验 → 重试 3 次。
- **两个兜底**：空 Send → 回 planner；writer/analyst 失败且无反馈 → 直接 finalizer。
- **并发防护**：`_running` set，检查+标记之间无 await（asyncio 单线程协作）。
- **SSE**：subscribe 同步复制历史快照防重复/遗漏；seq 单调递增前端去重；流式增量按 80 字符/0.3s 聚合。
- **持久化**：checkpoint（memory/sqlite/postgres）管图状态与断点；history.sqlite 独立存元数据+批改意见+报告全文。
- **降级**：搜索单源失败跳过 → 全失败回 MOCK → 流程不断。
- **安全**：python_repl 受限 builtins + AST 拦 import/dunder，且诚实声明「非完美隔离」。
- **关键坑位（面试常挖）**：① Pydantic 对象 JSON 序列化（`_to_jsonable`）；② SSE `data:` 双重前缀；③ SQLite checkpointer 并发损坏；④ 打回后内容相同导致前端误判（seq 去重）；⑤ asyncio 中 StopIteration 被包装成 RuntimeError。

# AutoResearch 新手教程（零基础版）

> 这份教程假设你**完全没接触过**大模型应用开发、FastAPI、LangGraph 这些技术。
> 我会用大白话和生活比喻，把项目里用到的每一项技术从零讲清楚，然后立刻对照项目里的真实代码。
> 建议按顺序读，一次读 1~2 部分就行，别贪多。读完一部分，试着不看教程复述一遍"它是什么、解决什么问题"。

---

## 第 1 部分：项目全景 —— AutoResearch 到底在干嘛

### 1.1 用"一家调研公司"来理解整个项目

想象你开了一家**私人订制调研公司**，专门帮客户写市场调研报告。客户走进来说：

> "我要一份《2026 年国内咖啡机市场机会》的调研报告。"

你这家公司有一套标准流程，每个环节有专人负责：

| 环节 | 负责人 | 干什么 |
|---|---|---|
| 1. 规划 | **规划师** | 先不急着查资料，而是把报告框架（大纲）想出来：分哪几章、要回答哪些问题 |
| ★ 老板审批 | 你（客户）| 大纲给你看，**你说行才往下走**；不行就打回去让规划师改 |
| 2. 拆解 | **拆解师** | 把大纲拆成几个能独立完成的小任务（每个任务是一次搜索） |
| 3. 并行调查 | **调查员×N** | 每个调查员负责一个小任务，**同时**上网查资料（一个查市场规模、一个查竞品、一个查趋势……） |
| 4. 分析 | **分析师** | 把大家查回来的资料汇总，提炼出关键数据（市场规模、增长率、主要玩家……） |
| 5. 撰写 | **写手** | 根据大纲和数据，写出一份完整的报告**草稿** |
| ★ 老板审批 | 你（客户）| 草稿给你看，**你说行才定稿**；不行就打回去（数据不对→分析师重做；写得不好→写手重写） |
| 6. 定稿 | **定稿师** | 输出最终报告，你可以下载（Markdown 或 Word） |

**AutoResearch 就是这家公司的自动化版本**。区别在于：这公司的"员工"其实都是**同一个大模型（LLM）**，只是每人拿一份不同的"岗位说明书"（Prompt）——规划师看规划师的说明书，写手看写手的说明书。而 **LangGraph 就是这家公司的《管理制度》**，它规定：谁先干、谁后干、哪几个可以同时干、什么时候必须**停下来等老板签字**。

### 1.2 这个项目最核心的三个设计思路

1. **让大模型干活，但流程是确定的**：每一步干什么、顺序是什么，是程序员（你）写死的；但每一步"具体产出什么内容"，是模型自由发挥的。→ 这样既有 AI 的灵活性，又有工程的确定性。
2. **关键节点要人把关**：大纲和草稿两道"老板审批"环节，解决大模型最致命的毛病——**幻觉**（一本正经地编造数据）。
3. **随时能暂停、能恢复**：像游戏存档，哪怕是服务器重启了，调研也能从上次暂停的地方继续，不用从头烧钱重来。

### 1.3 一张图看懂整体结构

```
你打开网页（前端 demo.html）
    │ 输入主题，点"开始调研"
    ▼
POST /research/start ────────→  FastAPI 后端收到请求，启动一个"调研任务"
    │                               │
    ▼                               ▼
网页通过 SSE 实时看到进度 ◀────── LangGraph 图开始跑
    │   "规划师"出大纲 → 网页弹出"请确认大纲"
    │   ┌───────────── 你点【批准】或【打回】
    ▼   │
    ├─ "拆解师"拆任务 → "调查员"们并行搜索 → "分析师"汇总 → "写手"出草稿
    │   → 网页弹出"请审核草稿"
    │   ┌───────────── 你点【批准】或【打回】
    ▼   │
    └─ "定稿师"定稿 → 网页展示最终报告，可下载 md/word
```

**这一部分你需要记住的就一句话：AutoResearch = 让大模型按照一个"带审批点的流程图"自动完成市场调研。**

> **自己检验**：不看上面，说出这个流程有哪 6 个"员工"（节点）？哪 2 个地方要"老板"（用户）审批？

---

## 第 2 部分：基础概念 —— 你得先懂这几个词

这一部分没有项目代码，但它是理解后面所有内容的地基。

### 2.1 什么是 API？什么是后端？什么是前端？

- **前端（Frontend）**：用户直接看到、点击的界面。项目里是 `frontend/demo.html`，一个网页。
- **后端（Backend）**：在服务器上跑的"大脑"，处理逻辑、调用模型、存数据。项目里是 `backend/` 下的 Python 代码。
- **API（接口）**：前后端之间约定的"传话方式"。就像你去餐厅，**菜单上写好的菜名就是 API**——你报菜名（请求），厨房按菜单做菜（处理），服务员端给你（响应）。前后端都遵守这个约定，就能互通。

AutoResearch 的"菜单"（API 列表）长这样：

```
POST /research/start                 # 客户点菜：开始一次调研
GET  /research/{thread_id}/stream    # 实时看进度（SSE 广播）
POST /research/{thread_id}/feedback  # 老板审批：批准或打回
GET  /research/{thread_id}/report    # 取最终报告
...
```

### 2.2 什么是大模型（LLM）？什么是 Prompt？

- **大模型 LLM**：可以理解成一个**读完了海量资料的"超级实习生"**。你给它一段话（Prompt/指令），它给你写一段话。它非常擅长"按照你的要求组织文字"，但有两大特点：
  1. 它的知识是**训练时学到的**，不是实时的（所以问它最新新闻，它可能不知道，**才需要联网搜索工具**）；
  2. 它会**一本正经地胡说八道**（幻觉），所以**关键数据要人去把关**。
- **Prompt（提示词）**：你给模型的指令。相当于你跟实习生说的"这份报告要这样写：……"。项目里每个节点的 Prompt 都在 `backend/nodes.py`，比如给规划师的指令开头是"你是资深市场调研专家。请为以下调研主题生成结构化调研大纲"。

### 2.3 什么是异步（async / await）？

一句话：**"等待的时候别闲着，先去干别的活。"**

- **同步**：你点外卖，站在店门口干等 30 分钟，什么也不干。—— 一个请求没处理完，别的请求都得排队。
- **异步**：你点完外卖，先回家干别的事，外卖到了再去取。—— 一个请求在"等大模型回复"的时候，服务器可以先去处理别的请求。

Python 里用两个关键词：
```python
async def foo():       # async 表示"这是一个可以异步执行的函数"
    await bar()        # await 表示"在这里等 bar() 完成，但等待期间不阻塞别人"
```

AutoResearch 里到处用到：比如后端启动调研后，`asyncio.create_task(...)` 把调研**丢到后台跑**，马上把 `thread_id` 返回给你，而不是让你干等（`backend/manager.py:161`）。

### 2.4 什么是数据结构化输出？

大模型本来只会"说人话"（输出文字）。但有时候我们想要它输出**规整的数据**，方便程序处理。比如：

```
你（想要）：大纲 = {章节: [...], 关键问题: [...]}
模型乱来：有时会输出一大段描述性文字，甚至把格式搞乱
```

让模型输出固定格式（JSON）就叫"结构化输出"。项目里模型要输出三种结构：**大纲 Outline、子任务 SubTask、市场数据**。这部分在第 10 部分详细讲。

> **自己检验**：用自己的话说——同步和异步的区别是什么？为什么大模型需要联网搜索工具？

---

## 第 3 部分：FastAPI —— 后端的"接待前台"

### 3.1 它是什么

**FastAPI** 是 Python 里一个写 Web 后端的框架。它的工作就是：**收到前端发来的请求（HTTP 请求），调用项目里的逻辑，把结果返回给前端（HTTP 响应）**。

它是整个系统的"前台接待"：所有来自网页的请求，第一站都是它。

### 3.2 核心概念：路由（Route）

"路由" = **根据不同的 URL 和请求方法，把请求转发给不同的处理函数**。就像前台根据你填的"找哪个部门"，带你去哪个办公室。

FastAPI 里一个"办公室"长这样（`backend/main.py` 里的真实代码）：

```python
@app.post("/research/start")                 # 有人 POST 这个地址
async def research_start(req: StartRequest) -> dict:
    """启动新调研：立即返回 thread_id，图在后台异步执行。"""
    manager: ResearchManager = app.state.manager
    thread_id = manager.start(req.topic)     # 调 manager 启动调研
    return {"thread_id": thread_id}          # 返回给前端
```

拆开看：
- `@app.post("/research/start")`：这是一个**装饰器**，告诉 FastAPI："当有人通过 POST 方法访问 `/research/start` 这个地址时，就执行下面的函数。"
- `req: StartRequest`：FastAPI 会自动把请求体里的 JSON 转成一个对象（这个 `StartRequest` 就是第 4 部分要讲的 Pydantic 模型）。
- `async def ...`：异步函数（见 2.3）。
- `return {...}`：返回一个字典，FastAPI 会自动把它转成 JSON 发给前端。

### 3.3 请求方法 POST / GET 的区别

| 方法 | 含义 | 项目里的例子 |
|---|---|---|
| GET | **取**东西（只读） | 取报告 `/research/{tid}/report` |
| POST | **做**事情（会改变状态） | 开始调研、提交审批 |

### 3.4 在 AutoResearch 里的整体作用

`backend/main.py` 里定义了一整套接口（"菜单"），前端 `demo.html` 就是按这个菜单点菜的。你点"开始调研"→ 前端发 `POST /research/start` → 后端返回一个 `thread_id`（相当于这次调研的单号）→ 之后所有操作都用这个单号来定位。

> **自己检验**：前端点"批准大纲"按钮，猜猜它会请求哪个接口、传什么参数？（答案：`POST /research/{thread_id}/feedback`，传 `{"approved": true}`）

---

## 第 4 部分：Pydantic —— 数据的"安检门"

### 4.1 它是什么

**Pydantic** 是一个 Python 库，专门做**数据校验和数据模型定义**。它就像机场安检：**每个数据进来都要过安检**——字段齐不齐、类型对不对、是不是合法的，不合格就不放行。

### 4.2 核心概念：BaseModel

在 Pydantic 里，你定义一个"数据结构"，用 `BaseModel`：

```python
from pydantic import BaseModel, Field

class SubTask(BaseModel):
    id: str                        # 字段名 + 类型：id 必须是字符串
    description: str               # 描述必须是字符串
    keywords: List[str] = Field(...)  # 关键词必须是字符串列表
```

只要创建 `SubTask(...)`，Pydantic 就会自动检查：`id` 是不是字符串、`keywords` 是不是列表。**如果不符合，直接抛异常**（ValidationError），不会把脏数据放进来。

### 4.3 在 AutoResearch 里的三种用途

1. **定义数据结构，让数据合法**（`backend/models.py`）：
   - `Outline`：大纲（sections 章节 + key_questions 关键问题）
   - `SubTask` / `SubTaskList`：并行搜索子任务
   - `SearchResult`：一条搜索结果（标题、链接、摘要……）
2. **校验 HTTP 请求**（`backend/main.py`）：前端传来的 `topic` 必须是 1~200 字符、不能是纯空格。
   ```python
   topic: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
   ```
   （注意：这是 Pydantic v2 的写法，老写法 `Field(strip_whitespace=...)` 已废弃——项目踩过这个坑，见 `docs/BUGS.md` #19）
3. **约束大模型的输出**：让模型必须输出符合 `Outline` 结构的 JSON（第 10 部分细讲）。

> **自己检验**：如果前端传 `{"topic": "   "}`（纯空格），后端会返回什么？为什么？（答案：返回 422 校验错误，因为 strip_whitespace 后长度为 0，不满足 min_length=1）

---

## 第 5 部分：LangGraph —— 把流程画成一张图

> **这是整个项目的心脏，也是面试的重点。** 花时间吃透。

### 5.1 它是什么

**LangGraph** 是一个"编排大模型工作流"的框架。核心思想：**把你想要的流程画成一张"流程图"（Graph），然后框架负责按照流程执行**。

想象你在用导航软件画一条路线：有**地点**（节点 Node）、有**路**（边 Edge）、有**岔路口的路牌**（条件边 Conditional Edge）。LangGraph 就是让你把这些画出来，然后它负责开车。

### 5.2 四个基本概念（用类比记住）

| 概念 | 类比 | 作用 |
|---|---|---|
| **Node（节点）** | 地图上的一个地点 | 一个处理步骤。在项目里就是"规划师""写手"这些函数 |
| **State（状态）** | 车上所有人共享的一块小黑板 | 所有节点共享的数据，见第 6 部分 |
| **Edge（边）** | 两个地点之间的路 | 从一个节点走到下一个节点 |
| **Conditional Edge（条件边）** | 岔路口的指示牌 | 根据当前情况（读黑板）决定走哪条路 |

### 5.3 项目里的完整流程图

`backend/graph.py` 里把整个"调研公司"的流程画成了这样（我把它翻译成人话）：

```
START（入口）
  │
  ▼
① supervisor_planner（规划师：出大纲）
  │
  ├─[条件边 route_after_plan] 看"老板批了吗？"
  │    ├─ 批了 → 走 👇
  │    └─ 打回 → 回到 ① 重新规划
  ▼
② task_decomposer（拆解师：拆成子任务）
  │
  ├─[条件边 continue_to_researchers] 看"拆出子任务了吗？"
  │    ├─ 有 → 用 Send 扇出多个 researcher（见第 7 部分）
  │    └─ 没有 → 回到 ① 重新规划（兜底，防止流程卡死）
  ▼
③ researcher ×N（调查员们：并行搜索）
  │   （所有调查员干完，自动汇合）
  ▼
④ analyst（分析师：汇总数据）
  │
  ▼
⑤ writer（写手：出草稿）
  │
  ├─[条件边 route_after_draft] 看"老板批了吗？"
  │    ├─ 批了 → 走 👇
  │    ├─ 反馈提的是"数据/来源/数字"这类 → 回到 ④ 分析师重做
  │    ├─ 反馈提的是"措辞/结构"这类 → 回到 ⑤ 写手重写
  │    └─ 写手/分析师出错且没有老板反馈 → 直接定稿（兜底，防止死循环烧钱）
  ▼
⑥ finalizer（定稿师：出最终报告）
  │
  ▼
END（出口）
```

对应的真实代码（`backend/graph.py:90`）：

```python
g = StateGraph(ResearchState)                          # 创建一张图
g.add_node("supervisor_planner", supervisor_planner)   # 注册 6 个节点
g.add_node("task_decomposer", task_decomposer)
g.add_node("researcher", researcher)
g.add_node("analyst", analyst)
g.add_node("writer", writer)
g.add_node("finalizer", finalizer)

g.add_edge(START, "supervisor_planner")                # 起点 → 规划师
g.add_conditional_edges("supervisor_planner", route_after_plan, {...})  # 条件边
g.add_conditional_edges("task_decomposer", continue_to_researchers, {...})  # 条件边
g.add_edge("researcher", "analyst")                    # 调查员 → 分析师
g.add_edge("analyst", "writer")                        # 分析师 → 写手
g.add_conditional_edges("writer", route_after_draft, {...})  # 条件边
g.add_edge("finalizer", END)                           # 定稿师 → 出口
g.compile(checkpointer=checkpointer)                   # 编译成可执行图
```

### 5.4 条件边怎么判断走哪条路？

条件边需要一个**路由函数**。比如"规划师干完后往哪走"：

```python
# graph.py:43
def route_after_plan(state):
    # 读黑板（state）上的 plan_approved（老板批了吗？）
    if state.get("plan_approved"):
        return "task_decomposer"     # 批了 → 去拆解
    return "supervisor_planner"      # 打回 → 回规划师重来
```

> **自己检验**：`writer` 节点干完后，有哪几条路可以走？分别对应什么情况？（答案：4 条——批准→finalizer；数据类反馈→analyst；措辞类→writer；出错兜底→finalizer）

---

## 第 6 部分：State 状态 + reducer —— 所有员工共享的"小黑板"

### 6.1 它是什么

**State（状态）** 是整个流程中所有节点共享的数据容器。所有"员工"都看着同一块黑板干活：读上面的信息，然后把自己的结果写上去。

在 LangGraph 里，状态用 `TypedDict` 定义（`backend/state.py:15`）：

```python
class ResearchState(TypedDict):
    topic: str                    # 调研主题
    outline: Optional[Outline]    # 大纲
    plan_approved: bool           # 大纲批了吗
    sub_tasks: List[SubTask]      # 拆解出的子任务
    search_results: Annotated[list, operator.add]   # ★ 搜索结果（重点）
    sub_task: Optional[SubTask]   # 单个子任务（调查员用）
    extracted_data: dict          # 分析师提炼的数据
    draft_report: Optional[str]   # 草稿
    draft_approved: bool          # 草稿批了吗
    final_report: Optional[str]   # 最终报告
    human_feedback: Optional[str] # 老板最近一次反馈
    error_log: List[str]          # 异常记录
```

### 6.2 状态是怎么流动的？（这是理解 LangGraph 的关键）

每个节点函数长这样：**输入整个 state，返回"我想更新哪几项"**。LangGraph 负责把这些更新合并回黑板。

```python
# nodes.py:307（真实代码，简化）
def task_decomposer(state):
    outline = state.get("outline")        # ① 读黑板：拿大纲
    tasks = generate_subtasks(...)        # ② 干活：拆任务（调大模型）
    return {"sub_tasks": tasks}           # ③ 写黑板：把子任务写上去
```

所以流程就是：`规划师写 outline → 拆解师读 outline 写 sub_tasks → 调查员读 sub_tasks → ……` 信息在黑板上接力传递。

### 6.3 重点：reducer 是什么？（面试必考）

黑板上有一项 `search_results`（搜索结果），**会有好多个调查员同时往这项上面写**。问题来了：两个人都写，谁覆盖谁？

这就需要一个**合并规则（reducer）**。看这行代码：

```python
search_results: Annotated[list, operator.add]
```

解释：
- `Annotated[list, operator.add]` 的意思是：这个字段的类型是 `list`，合并规则用 `operator.add`。
- `operator.add` 对列表来说就是**拼接**。比如调查员 A 写了 `[结果1, 结果2]`，调查员 B 写了 `[结果3]`，LangGraph 不是让 B 覆盖 A，而是**拼起来**变成 `[结果1, 结果2, 结果3]`。

**这就是为什么多个调查员并行搜索，结果能自动汇总成一份**。如果不用 reducer，后面写的会把前面的盖掉，数据就丢了。

> **自己检验**：如果不写 `Annotated[list, operator.add]`，而只写 `search_results: list`，会发生什么？（答案：多个调查员同时写时，后来的覆盖前面的，搜索结果只剩最后一个人的，其他全丢）

---

## 第 7 部分：Send —— 一个"动态派活"的机制（并行）

### 7.1 为什么要并行？

拆解师拆出来的子任务，可能有 3 个、5 个、8 个……数量是不确定的（由大模型决定）。如果让一个调查员**一个个**去搜，会很慢；更好的办法是**几个调查员同时去搜**。

### 7.2 Send 是什么？

**Send** 是 LangGraph 提供的一个"动态扇出"机制：**在一个节点里，根据数据生成多个"新分支"**，让每个分支从一个指定的节点开始独立执行。

项目里（`backend/graph.py:72`）：

```python
def continue_to_researchers(state):
    sub_tasks = state.get("sub_tasks") or []
    if not sub_tasks:
        return "supervisor_planner"      # 兜底：没拆出子任务就回去重新规划
    return [Send("researcher", {"sub_task": t}) for t in sub_tasks]
```

意思是：假设拆出 3 个子任务，就返回 3 个 `Send`，每个 Send 都"让 researcher 节点跑一次，并给它一个不同的 sub_task"。**3 个 researcher 并行执行。**

### 7.3 关键词是什么？结果怎么汇合？

- **扇出（Fan-out）**：一个变多个。一个 `task_decomposer` 扇出 3 个 researcher。
- **汇合（superstep）**：所有扇出的分支**都跑完**后，才继续往下走。因为 researcher 的下一站是 `analyst`，所以**等所有调查员都搜完了，分析师才拿到全部结果**去汇总。分析师只执行一次。
- 所有 researcher 的结果通过第 6 部分的 **reducer** 自动合并到 `search_results`。

### 7.4 一个重要的坑：空 Send 会怎样？

如果拆解失败、`sub_tasks` 是空的，而你又返回了 `[]`（空列表），LangGraph 会认为"没活了"，**图直接静默结束**——前端就会一直转圈，永远等不到结果。所以代码里加了兜底：空就返回 `"supervisor_planner"`，回去重新规划（`backend/graph.py:81`）。

> **自己检验**：假设拆出 4 个子任务，会扇出几个 researcher？它们的搜索结果是"覆盖"还是"拼接"？（答案：4 个；拼接——靠 operator.add reducer）

---

## 第 8 部分：interrupt —— 让流程停下来问老板

### 8.1 它是什么

**interrupt（中断）** 是 LangGraph 实现"人机协同"（Human-in-the-loop, HITL）的原语。作用：**让图在某个节点处暂停，把当前结果拿给人看，等人给答复，再从暂停处继续**。

类比：业务流程里设的**审批点**。员工干到这一步，必须停下来等领导签字，不能自作主张往下走。

### 8.2 在项目里的两个审批点

项目里有两处 `interrupt`（`backend/nodes.py`）：

**① 规划师出大纲后**（nodes.py:302）：
```python
resp = interrupt({"type": "plan", "outline": outline.model_dump()})
# 图在这里暂停！前端收到 interrupt 事件，弹出大纲让老板确认
approved = bool(resp.get("approved"))    # 拿到老板的答复
```

**② 写手出草稿后**（nodes.py:391）：
```python
resp = interrupt({"type": "draft", "report": draft})
```

### 8.3 老板是怎么"回答"的？—— Command(resume)

当图暂停时，前端会收到 SSE 的 `interrupt` 事件并展示内容。老板点【批准】或填意见点【打回】，前端调用：

```
POST /research/{thread_id}/feedback
Body: {"approved": true/false, "feedback": "修改意见"}
```

后端收到后，用 **`Command(resume=...)`** 作为图的输入去"唤醒"它（`backend/manager.py:426`）：

```python
Command(resume={"approved": approved, "feedback": feedback})
```

这个 resume 值，就是刚才那个 `interrupt(...)` 的**返回值**。图从暂停的地方继续跑，节点拿到 `approved`/`feedback` 决定下一步。

### 8.4 一个必须理解的细节：GraphInterrupt 异常

你可能在代码里看到每个节点都有 `except Exception` 包着，唯独对 interrupt 特别处理：

```python
try:
    ...
    resp = interrupt({...})
    ...
except GraphInterrupt:
    raise          # nodes.py:311 —— 必须重新抛出！
except Exception as e:
    return {"error_log": [f"...: {e}"]}
```

为什么？因为 **interrupt 本质上是靠"抛出一个叫 GraphInterrupt 的异常"来实现暂停的**。如果你把它当成普通异常用 `except Exception` 吞掉了，图就无法正确暂停/恢复。所以代码里特意把它单独拿出来**重新抛出（raise）**。这是 LangGraph 的机制，面试问到要能答上来。

### 8.5 另一个细节：为什么打回后要"写回已确认内容"？

如果老板打回大纲并改了内容，后端在恢复前会先把"老板确认的版本"写回状态（`backend/manager.py:411`）：

```python
await graph.aupdate_state(config, {"outline": Outline(**outline)})
```

为什么？因为 interrupt 恢复后，节点会**重新执行一遍**。如果不把老板确认的版本写回去，节点会**重新生成一份新的**，导致最终内容和老板看到的**不一致**。

一句话：**人工确认过的内容，必须成为流程的"事实来源"。**

> **自己检验**：interrupt 是靠什么机制实现暂停的？恢复时用什么把结果传给图？（答案：GraphInterrupt 异常 + Command(resume=...)）

---

## 第 9 部分：Checkpointer —— 保存游戏进度

### 9.1 它是什么

**Checkpointer（检查点）** 是 LangGraph 的"存档系统"：**每次图执行一步，就把当前状态存下来**。这样即使中途断电、进程崩溃、服务器重启，也能从最近一次"存档"继续，而不是从头再来。

### 9.2 为什么要它？

想想我们的流程：大纲审批那一步，可能要暂停**几十分钟甚至几小时**等老板有空看。如果中间服务器重启了，没有存档，这单调研就废了，前面花的 token 全白费。

### 9.3 项目里的三种"存档方式"

`backend/graph.py:149 create_checkpointer()` 根据配置选一种：

| 方式 | 存哪 | 特点 | 什么时候用 |
|---|---|---|---|
| `memory` | 内存里 | 最快，但**重启就没了** | 开发调试、测试 |
| `sqlite` | 本地一个文件 | 重启不丢，单机够用 | 开发/单机部署（默认） |
| `postgres` | 数据库 | 更可靠，支持多实例并发 | **生产环境**（Docker 部署） |

### 9.4 进程重启后怎么恢复？

假设服务器重启了，之前暂停在大纲审批点的调研怎么继续？

后端有个 `restore()` 方法（`backend/manager.py:313`）：SSE 订阅时发现线程不在内存里，就去**存档（checkpoint）里查**这个调研的状态——
- 如果它停在大纲审批点 → 重新发出 `interrupt` 事件，前端继续显示确认面板；
- 如果它已经完成了 → 重放最终报告。

这样老板刷新页面后，调研还能继续，不会丢。

> **自己检验**：用 memory 方式存档，服务器重启后调研还能恢复吗？为什么？（答案：不能，因为存内存里，重启即失。生产要用 sqlite 或 postgres）

---

## 第 10 部分：LLM 工程化 —— 结构化输出与容错（最硬核的亮点）

> 这一部分是你简历上「JSON提取→字段级清洗→Pydantic校验→失败重试」的来历，**面试官最喜欢深挖这里**。先理解问题，再看方案。

### 10.1 问题：大模型输出不可靠

我们想让模型输出**规整的结构**（比如大纲的章节列表）。一般的做法是用 `with_structured_output` 让模型按 schema 输出。但项目用的模型是 `deepseek-v4-flash`：它**支持 function_calling（工具调用）和 json_schema 强约束结构化输出模式**，但项目最终选了 `json_object` 模式（只保证是合法 JSON、不保证结构符合要求）——未用 function_calling / json_schema 强约束做结构化输出是**架构选择**（详见《为什么不用FunctionCalling》）。

更麻烦的是：**当 prompt 很长（比如带上了老板的修改意见）时，模型经常把格式搞乱**。比如明明要求输出字符串数组：

```
正确：sections = ["行业概况", "竞争格局"]
抽风：sections = [{"title": "行业概况", ...}, {"title": "竞争格局", ...}]   ← 变成了对象数组！
```

一旦格式乱了，直接让 Pydantic 校验就会**报错**，流程就断了。

### 10.2 方案：自己写一个"容错管线"

项目没有傻等模型变听话，而是自己写了套**容错管线**（`backend/nodes.py:122`）：

```python
for _ in range(3):                                 # ③ 失败就重试，最多 3 次
    try:
        resp = llm.invoke(prompt)                  # 1. 调大模型
        data = _extract_json(解析文本)              # 2. 从文本里"抠"出 JSON
        return _coerce_model(Model, data)          # 3. 清洗 + 校验成 Model
    except (解析失败, 校验失败) as e:
        last_err = e
raise ValueError("解析失败，已重试 3 次")
```

一步步讲：

**① 从文本里抠 JSON（`_extract_json`，nodes.py:152）**
模型输出可能是 `{"sections": [...]}`，也可能前面带一堆废话，甚至用 ```json 包着。这个函数：先尝试整体解析 → 不行就取"第一个 `{` 到最后一个 `}` 之间的部分"再解析 → 还不行就抛带原文的异常方便排查。

**② 字段级清洗（`_coerce_model` + `_coerce_str_list`，nodes.py:58, 91）**
就是治"字符串数组变成对象数组"这个病。核心逻辑：

```python
for it in items:
    if isinstance(it, str):
        直接收下这个字符串
    elif isinstance(it, dict):          # 是个对象！比如 {"title": "行业概况"}
        从里面挑一个文本字段拿出来（title / name / description / ...）
```

这样 `[{"title": "行业概况"}, ...]` 就被清洗回 `["行业概况", ...]`。

**③ Pydantic 校验 + 重试**
清洗完再用 Pydantic 校验。如果还是失败，重试一次（换个生成的输出）。3 次都不行才放弃。

### 10.3 这个设计说明了什么（面试怎么讲）

你要能说出："大模型输出本质上是有噪声的。工程上与其指望模型永远格式正确，不如**在下游做容错**——清洗、校验、重试。这套容错管线让我在长 prompt 场景下，把结构化输出的失败率降到了可接受水平。"

### 10.4 顺带一提：给 prompt 加"当前时间"

每个节点生成前，都往 prompt 里塞一句"当前时间是 XX"（`backend/nodes.py:35 _time_hint`）。作用是让模型知道"现在"是哪天，**检索和写报告时优先用最新信息，别引用过时数据**。这是个不起眼但很实用的小细节。

> **自己检验**：模型把 sections 输出成 `[{"title":"行业概况"}]` 这种对象数组时，容错管线的哪一步把它救回来？（答案：字段级清洗 `_coerce_str_list`）

---

## 第 11 部分：工具与搜索 —— 让 Agent 联网查资料

### 11.1 它是什么

大模型**不知道实时信息**（见 2.2）。所以要让调查员"上网搜"。搜索就是一个**工具（Tool）**——模型可以用它来获取真实世界的资料。

项目里定义了几个工具（`backend/tools.py`）：
- `search_web`：聚合多个搜索引擎（主工具）
- `get_current_datetime`：获取当前时间（给 prompt 用）
- `python_repl`：执行一段 Python 代码（给分析师算数用）

### 11.2 为什么搜"两次"？—— 多源聚合

国内外的搜索引擎各有优劣：Tavily 偏国外、博查偏国内中文内容。所以 `search_web` 把**两个源的结果合并**，再按 URL 去重（同一个网页可能两个引擎都搜到了），返回统一格式。这样覆盖更全。

### 11.3 降级策略（稳健性的体现）

真实世界充满意外：某源 API key 没配、某源挂了、网络断了……项目做了**逐级降级**：

```
两个源都正常 → 合并结果
一个源挂了   → 只用另一个源（跳过坏的那个）
两个都挂了   → 返回 [MOCK] 模拟结果（带个 [MOCK] 前缀标记）
```

这样**无论外部环境多糟，流程都能走完**，不会卡死。这就是工程里的"降级"思想。

### 11.4 一个小小的安全细节：python_repl 沙箱

`python_repl` 能让模型执行任意 Python 代码，这很危险（代码里写 `os.system("rm -rf /")` 咋办？）。项目做了两层防护：
1. 只给模型一部分"安全的内置函数"（没有 `import`）；
2. 用 AST（代码结构分析）在**执行前**检查代码：禁止 `import`、禁止访问 `__class__` 这类危险属性。

而且代码注释里诚实写了：这只是**安全加固，不是完美隔离**，只在受信任的场景用。你能说出来"这不是绝对安全"，本身就是安全意识。

> **自己检验**：两个搜索源都挂了，调研流程会卡死吗？（答案：不会，回退到 [MOCK] 模拟结果，流程继续走）

---

## 第 12 部分：SSE —— 服务器向网页实时广播

### 12.1 它是什么

**SSE（Server-Sent Events，服务器推送事件）** 是一种让**服务器主动向网页持续推送消息**的技术。类比：**电台广播**——服务器不停地播，网页打开就能听（单向）。

对比一下：
| | 普通 HTTP | SSE | WebSocket |
|---|---|---|---|
| 方向 | 请求→响应（一次） | 服务器→网页（持续单向） | 双向实时 |
| 类比 | 寄快递 | 听广播 | 打电话 |

项目用 SSE 是因为场景就是**单向的**：服务器要把调研进度、模型逐字生成的内容、审批请求，实时推给网页看。网页不需要给服务器发消息（发消息走普通 POST 接口）。

### 12.2 项目里推送了哪几类事件？

`backend/main.py:103` 的 `/stream` 接口，通过 SSE 推这些事件给前端：

| 事件 | 含义 | 前端怎么反应 |
|---|---|---|
| `node_start` | 某节点开始执行 | 日志显示"▶ 规划师 开始执行" |
| `node_end` | 某节点完成 | 日志显示"✔ 规划师 完成" |
| `stream` | 模型逐字生成的内容 | 日志里"打字机"式显示 |
| `interrupt` | 需要老板确认 | 弹出确认面板 |
| `final` | 调研完成 | 展示最终报告 |
| `error` | 出错 | 日志显示错误 |

### 12.3 两个工程细节（讲出来很加分）

**细节 1：断线重连不丢事件**
用户刷新页面 / 网络断了，SSE 会自动重连。重连后，服务器要把**之前发生过的所有事件**补发给新连接，否则页面就是"空白进度"。项目里每个线程都有一个事件历史（`ThreadChannel.history`），新连接订阅时**一次性复制历史**给它（`backend/manager.py:84`）。因为复制是同步操作、不会和实时事件交错，所以**既不重复也不遗漏**。

**细节 2：每个事件带一个"序号"（seq）**
前端要用序号去重。为什么？举个例子：老板打回大纲，模型重新生成了大纲，但**内容和上次一模一样**（大模型低温时输出很稳定）。前端如果不看序号，就会以为"这是断线重连重放的历史事件"，直接忽略——结果老板打回了，页面却不弹新确认框。加上单调递增的 `seq` 后，前端只认"序号更大的事件"，就不会误伤了。

**细节 3：流式内容要"攒一攒"再推**
模型是一个字一个字生成的。如果每生成一个字就推一条 SSE 消息，消息量太大。项目里**攒够 80 个字符，或超过 0.3 秒，才推一条**（`backend/manager.py:193`）。既实时，又不刷屏。

> **自己检验**：SSE 适合什么场景？为什么不适合做"聊天室"这种双向沟通？（答案：适合服务器单向推送给网页；聊天室需要双向，用 WebSocket 更合适）

---

## 第 13 部分：历史库、部署、测试 —— 让它跑得稳、看得见

### 13.1 为什么调研记录要单独存一个库？

LangGraph 的 checkpoint 虽然存了状态，但项目只读取它的**最新状态**（checkpoint 本身保留每步历史快照、支持 time travel，但那是运行时状态）；而且人工反馈被消费后就会从当前状态里清空，**没法直接追溯每次老板的批改意见**。所以项目用一个独立的 `history.sqlite` 库（`backend/history.py`）存：
- 每次调研的元数据（主题、状态、时间）
- **全部人工批改记录**（哪一步、批了还是打回、意见内容）
- 最终报告全文

这就是为什么网页上能回看历史报告，还能看到"生成过程中老板的每次批改意见"。

### 13.2 怎么部署？—— Docker

项目用 **Docker**（把整个后端打包成"一个盒子"，到哪都能跑）+ **docker-compose**（一键启动多个盒子）部署。`docker-compose.yml` 里定义了两个"盒子"：
- `postgres`：数据库（生产用 PostgreSQL 存 checkpoint）
- `backend`：FastAPI 后端

跑一条命令 `docker compose up --build` 就全部起来了。密钥通过环境变量注入，**不写死在代码里**。

### 13.3 怎么保证代码没写坏？—— 测试

项目用 **pytest** 写了两类测试：
- **单元测试**：测某个小功能（比如"python_repl 是否拦截了 import"、"多源搜索是否去重"），秒级跑完，不依赖真实大模型。
- **集成测试**：启动真实服务器、调用真实大模型，把完整流程（开始调研 → 批准大纲 → 批准草稿 → 出报告）跑一遍，验证全链路没坏。

这保证了：你每改一次代码，都有"机器人"帮你把整个调研流程跑一遍，不会改着改着把功能改坏了。

> **自己检验**：为什么批改意见要存在独立历史库，而不是靠 checkpoint？（答案：checkpoint 的历史快照是运行时状态，项目只读最新状态、且反馈消费后不在当前状态里；独立历史库存结构化、可查询、长期持久的每次批改记录和报告全文）

---

## 结尾：一张"技术 → 作用"速查表

读完 13 个部分后，你应该能不看文档说出下面每行：

| 技术 | 一句话作用 | 在项目里解决什么问题 |
|---|---|---|
| **大模型 LLM + Prompt** | 每个节点的大脑 | 生成大纲、拆任务、写报告 |
| **FastAPI** | 后端框架 | 提供前端调用的 API 接口 |
| **Pydantic** | 数据校验 | 保证数据、请求、模型输出格式合法 |
| **asyncio** | 异步编程 | 调研在后台跑，前端不用干等 |
| **LangGraph** | 工作流编排 | 把流程画成图并执行 |
| **State + reducer** | 共享状态 | 节点间传数据，并行结果自动合并 |
| **Send** | 动态并行 | 并行扇出多个搜索分支 |
| **interrupt + Command** | 人机协同 | 大纲/草稿等老板审批 |
| **Checkpointer** | 断点持久化 | 重启后能续跑 |
| **结构化输出容错管线** | 治模型抽风 | 长 prompt 下输出格式乱也不断流 |
| **搜索工具 + 多源降级** | 联网能力 | 让模型拿到实时资料，且不因单源故障卡死 |
| **SSE** | 实时推送 | 前端实时看到进度和逐字生成 |
| **历史库 + Docker + pytest** | 工程保障 | 可追溯、可部署、可测试 |

---

## 接下来怎么学？

1. **先读第 1、2 部分**，用自己话复述一遍"AutoResearch 在干嘛"。
2. 然后按顺序读第 3→12 部分，每读完一部分做一下文末的"自己检验"。
3. 读完不理解的地方，**随时来问我**，我可以针对某一节再展开、再举例，或者带你一行行读代码。
4. 全部读完、感觉通了之后，再回去看《AutoResearch面试宝典.md》，你会发现那些问题你都能答上来了。
```

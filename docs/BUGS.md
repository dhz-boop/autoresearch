# AutoResearch 开发过程 Bug 记录

本文档记录 AutoResearch 开发（阶段 1~3）中遇到的所有问题及其解决方案，
按「现象 → 根因 → 解决」组织，供后续维护与排查参考。

---

## 1. langgraph 1.2.x 的 API 变更（版本迁移，非逻辑 Bug）

**现象**：按旧文档/示例导入 `from langgraph.graph import add_edge` 报
`ImportError: cannot import name 'add_edge'`；`from langgraph.checkpoint.sqlite import AsyncSqliteSaver`
同样导入失败；用 `update_state` + `ainvoke(None)` 恢复 interrupt 时图**再次暂停**而非继续。

**根因**：langgraph 1.2.10 相比旧版本发生了多处 API 变化：
- `add_edge` / `add_conditional_edges` 不再是模块顶层函数，而是 `StateGraph` 的**实例方法**；
- `AsyncSqliteSaver` 迁移到子模块 `langgraph.checkpoint.sqlite.aio`；
- `interrupt()` 的恢复语义变更：**必须用 `Command(resume=...)` 作为图输入**才能消费中断；
  `update_state` 只更新普通状态字段，不会让 `interrupt()` 返回；
- async 上下文中查询状态要用 `aget_state`（同步 `get_state` 会抛
  `InvalidStateError: Synchronous calls to AsyncSqliteSaver are only allowed from a different thread`）。

**解决**：
- 全部改用 `StateGraph` 实例方法 `add_node` / `add_edge` / `add_conditional_edges`；
- 从 `langgraph.checkpoint.sqlite.aio` 导入 `AsyncSqliteSaver`；
- 恢复执行统一用 `graph.ainvoke(Command(resume=...), config)` / `astream(Command(resume=...), ...)`；
- async 上下文统一用 `await graph.aget_state(...)`。

**涉及文件**：`backend/graph.py`、`backend/nodes.py`、`backend/manager.py`

---

## 2. `InteractiveInterpreter.runsource` 不支持多行独立语句

**现象**：`python_repl` 执行
```python
nums=[1,2,3,4,5]
print('sum=', sum(nums))
```
报 `SyntaxError: multiple statements found while compiling a single statement`。

**根因**：`code.InteractiveInterpreter.runsource` 默认以 `symbol="single"` 编译，
多行独立语句（非缩进块）会被判为「单个语句里出现多个语句」而报错。

**解决**：改用标准库 `exec(compile(code, "<repl>", "exec"))`，以 `exec` 模式编译，
天然支持任意多行/多语句代码，异常用 `traceback.print_exc` 捕获输出。

**涉及文件**：`backend/tools.py`

---

## 3. 受限内置命名空间构建时 `__builtins__` 不可下标

**现象**：`python_repl` 执行 `import os` 未按预期拦截，或构建受限环境时报错。

**根因**：在函数体内 `__builtins__` 是 `builtins` **模块对象**，不支持
`__builtins__[name]` 下标访问（旧代码曾这样取安全内置）。

**解决**：`import builtins` 后用 `getattr(builtins, name)` 逐个取白名单内置，
并将不含 `__import__` 的 dict 作为 `__builtins__` 传入解释器，从而拦截 `import`。

**涉及文件**：`backend/tools.py`

---

## 4. Pydantic 模型在 checkpoint 中反序列化告警

**现象**：运行图时日志出现
`Deserializing unregistered type models.Outline from checkpoint. This will be blocked in a future version`。

**根因**：state 中存放了 `models` 模块的 Pydantic 模型（`Outline` / `SubTask` 等），
langgraph 的 msgpack 序列化器（`JsonPlusSerializer`）默认不允许反序列化未注册模块的类型，
当前版本仅告警，未来版本会直接报错。

**解决**：在 `JsonPlusSerializer` 构造时显式声明允许的模块白名单，
并通过 `checkpointer` 的 `serde` 参数注入：
```python
JsonPlusSerializer(allowed_msgpack_modules=[("models", "Outline"), ("models", "SubTask"), ("models", "SubTaskList")])
```

**涉及文件**：`backend/graph.py`（`_build_serde`）

---

## 5. SSE 事件出现双重 `data:` 前缀

**现象**：SSE 客户端收到的行是 `data: data: {"event": ...}`，解析失败。

**根因**：`sse-starlette` 的 `EventSourceResponse` 会自动为每次 yield 添加 `data: ` 前缀，
而自定义生成器里又手动拼了 `f"data: {json}\n\n"`，导致前缀重复。

**解决**：生成器只 yield **纯 JSON 字符串**，前缀与 `\n\n` 分隔交给 `EventSourceResponse`；
同时删除手动 keep-alive（框架自带 ping 心跳）。

**涉及文件**：`backend/main.py`（`research_stream`）

---

## 6. Pydantic 对象无法 JSON 序列化导致 SSE 连接崩溃（关键 Bug）

**现象**：完整流程中，第二次 SSE 连接读到 `node_start(supervisor_planner)` 后连接中断，
后续节点事件全部丢失，客户端报 `StopIteration` / `httpx.RemoteProtocolError`。

**根因**：resume 后 `supervisor_planner` 重跑返回的 `node_end` 事件里 `outline` 字段是
Pydantic `Outline` **对象**。SSE 生成器中 `json.dumps(evt)` 抛
`TypeError: Object of type Outline is not JSON serializable`，
导致整个 SSE 响应生成器异常退出、连接关闭，后续事件无法推送。

**解决**：在 `ResearchManager._execute` 推送事件前，用递归转换函数
`_to_jsonable()` 把所有 Pydantic 模型递归转成 dict（`BaseModel` → `.model_dump()`，
dict/list 递归），确保 `node_end` / `interrupt` 事件均可 JSON 序列化。

**涉及文件**：`backend/manager.py`

---

## 7. uvicorn 启动阶段 /health 返回 502 与测试服务启动失败

**现象**：测试 fixture 中 `httpx.get(/health)` 在启动初期持续返回 502，
导致「uvicorn 服务启动失败」；而手动 `curl` 却正常。

**根因**（两层）：
1. uvicorn 的 lifespan 启动较慢（需 import 并初始化 langchain），数秒后才开始处理请求；
2. 系统环境存在 `http_proxy` / `https_proxy`，`httpx` 默认 `trust_env=True` 会读取代理。
   startup 期间经代理转发到本服务时，代理侧出现 502。

**解决**：
- 测试中所有 `httpx` 客户端显式 `trust_env=False`，绕过系统代理直连本机；
- fixture 等待逻辑对「非 200」一律 sleep 重试，容忍数秒的启动耗时；
- 启动失败时读取子进程 stderr 帮助定位。

**涉及文件**：`tests/test_stage3.py`（server fixture）

---

## 8. SQLite 检查点文件残留/损坏导致 AsyncSqliteSaver 卡死

**现象**：`AsyncSqliteSaver` 的首次运行/恢复操作偶发无限挂起（无异常、无输出），
删除数据库文件后立即恢复正常。

**根因**：多个进程并发写同一个 `checkpoints.sqlite`，或进程被强杀，
导致 `.sqlite-wal` / `.sqlite-shm` 残留损坏，`AsyncSqliteSaver` 操作阻塞。

**解决**：
- 测试改用**独立临时库** `CHECKPOINT_DB=/tmp/autoresearch_test_checkpoints.sqlite`，
  每次启动前清理 `-wal` / `-shm` 残留，避免污染开发库；
- 生产环境（阶段 4）改用 PostgreSQL，规避 SQLite 并发写问题。

**涉及文件**：`tests/test_stage3.py`、`backend/config.py`（`CHECKPOINT_DB`）

---

## 9. asyncio.run 退出时 aiosqlite 后台线程清理挂住

**现象**：开发调试脚本（非应用）在 `asyncio.run(main())` 结束后不退出，或打印
`RuntimeError: Event loop is closed`。

**根因**：`AsyncSqliteSaver` 手动创建的 aiosqlite 连接在事件循环关闭时未显式关闭，
其后台 worker 线程尝试 `call_soon_threadsafe` 失败。属于脚本退出时的资源清理问题，
**在 FastAPI 应用生命周期内（事件循环持续运行）不会出现**。

**解决**：生产代码不受影响；调试脚本结束后显式关闭连接（或接受该无害告警）。

**涉及文件**：`backend/graph.py`（`create_checkpointer` 的连接管理）

---

## 10. `next(generator)` 在协程内抛 StopIteration 被 asyncio 包装成 RuntimeError

**现象**：测试中 `next(e for e in events2 if ...)` 在列表为空时抛
`StopIteration`，被 `asyncio.run` 包装为
`RuntimeError: coroutine raised StopIteration`。

**根因**：`next()` 对生成器表达式取不到元素时抛 `StopIteration`；在协程栈上
`StopIteration` 会被 asyncio 视为异常泄漏并转换，掩盖了真实问题（事件没等到）。

**解决**：先判断列表是否包含目标事件再取；或使用 `next(..., None)` + 显式断言，
避免裸 `next()` 抛 `StopIteration`。同时补全「没等到事件」时的调试输出。

**涉及文件**：`tests/test_stage3.py`

---

## 11. langgraph 回调无法直接获取节点名

**现象**：希望通过 `AsyncCallbackHandler.on_chain_start` 捕获节点开始事件，
但 `serialized.get("name")` 拿到的是 `None`，且内部 LLM 的 run 也会触发回调。

**根因**：langgraph 节点的回调 `serialized` 参数可能为 `None`；节点名需从
`kwargs["metadata"]["langgraph_node"]` 获取；而节点内部的 LLM run 也继承了该 metadata，
无法仅凭 `langgraph_node` 区分「节点本身」与「内部 LLM」。

**解决**：当 `kwargs["name"] == kwargs["metadata"]["langgraph_node"]` 时才是节点本身的 run
（节点 run 的 name 即节点名，内部 LLM run 的 name 是模型名），据此过滤并推送 `node_start`。

**涉及文件**：`backend/manager.py`（`NodeStartRecorder`）

---

## 12. 并发 feedback 导致双 resume 竞态（严重）

**现象**：对同一 thread_id 并发/重复提交 feedback（如前端快速双击、start 后立即反馈）
时，`ResearchManager.feedback` 会同时启动多个后台 `_execute`，对同一 checkpointer
并发执行 `astream(Command(resume=...))`，导致 LangGraph 并发恢复同一线程：
抛出异常（被捕获后推 `error` 事件）或状态错乱。

**根因**：feedback 端点无任何「正在执行」防护；start 后后台图仍在跑，此时提交反馈
同样会与首次运行并发。

**解决**：
- `ResearchManager` 增加 `_running` 集合记录正在执行/恢复的 thread_id；
- `start`/`feedback` 在**首个 await 之前同步**检查并标记（检查+标记间无 await，
  事件循环不会切换，杜绝多请求竞态），`_execute` 的 `finally` 中释放标记；
- 图仍在运行或无 pending interrupt 时，feedback 端点返回 **409**（未暂停）/ **400**
  （内容不合法），而非静默双跑。

**涉及文件**：`backend/manager.py`、`backend/main.py`

---

## 13. 子任务为空导致图静默终止、前端永久等待（严重）

**现象**：`task_decomposer` 拆解失败（LLM 异常/返回空 `tasks`）时，图既不暂停在
interrupt、也不产出 `final_report`，SSE 无任何事件推送，前端永远停在「开始调研」。

**根因**：`continue_to_researchers` 对空 `sub_tasks` 返回**空的 Send 列表**。LangGraph
对空 Send 的处理是图在此直接结束（`st.next == ()`），`_execute` 的「有中断」与
「有 final_report」两个分支都不命中，于是无事件可推。

**解决**（两层兜底）：
- `continue_to_researchers` 在子任务为空时返回 `"supervisor_planner"`，条件边
  注册 `{"researcher": "researcher", "supervisor_planner": "supervisor_planner"}`，
  回规划节点重新规划（而非静默结束）；
- `_execute` 在图结束后「无中断且无 final_report」时推送 `error` 事件
  （「调研流程意外终止」），兜底所有异常终止路径。

**涉及文件**：`backend/graph.py`、`backend/manager.py`

---

## 14. threads 字典与事件历史无界累积导致内存泄漏

**现象**：长时间运行后进程内存持续上涨，最终 OOM。每次 `/research/start` 都会在
`self.threads` 永久新增 `ThreadChannel`，其 `history` 无界追加所有事件。

**根因**：线程完成后无清理入口，历史列表无上限。

**解决**：
- `ThreadChannel.history` 改用 `deque(maxlen=300)`，仅保留最近 300 条事件；
- 图完成（`final` / `error`）后 `_execute` 调用 `_schedule_cleanup`，1 小时后从
  `threads` 移除该线程，给迟到的 SSE 订阅者留出重连窗口（checkpoint 仍在，
  `/report` 端点不依赖 `threads` 字典）。

**涉及文件**：`backend/manager.py`

---

## 15. python_repl 沙箱可被 dunder 反射逃逸（严重）

**现象**：`python_repl` 号称「隔离解释器」，但通过经典 class 链可拿到完整模块并
执行任意代码：
```python
w = [c for c in ().__class__.__bases__[0].__subclasses__() if c.__name__ == "catch_warnings"]
sys_mod = w[0].__init__.__globals__["sys"]   # 完整 sys 模块
print(sys_mod.modules["os"].getcwd())        # 实际执行成功
```
可进一步 `os.system()` 执行任意命令（若暴露给不受信输入即为 RCE）。

**根因**：`exec` 环境的受限 builtins 只能拦截 `import`，无法阻止对对象**属性链**的
反射访问（`__class__` / `__subclasses__` / `__globals__` 等 dunder 属性）。

**解决**：在 `exec` 前增加 `_validate_sandbox_code` 做 AST 静态校验：
- 禁止任何 `import` 语句；
- 禁止访问任何 dunder 属性（以 `__` 开头且结尾的属性名）。
同时补充单元测试 `test_python_repl_blocks_dunder_escape`。
> 注意：这是安全加固而非完美隔离，仍仅在受信任的分析场景使用。

**涉及文件**：`backend/tools.py`、`tests/test_stage1.py`

---

## 16. 节点失败后条件路由自环持续调用 LLM

**现象**：`writer`/`analyst` 连续失败（如 LLM 不可用）时，`draft_approved` 恒为
False、`human_feedback` 恒为 None，`route_after_draft` 每次把图路由回自身重试，
每轮都重新调用 LLM，直到 `recursion_limit`（默认 25）才停止，白白消耗大量 API 额度。

**根因**：节点异常只写 `error_log`，路由不感知「最近一次执行是否失败」，无反馈地
反复重试。

**解决**：`route_after_draft` 增加兜底——当最近一条错误来自 `writer`/`analyst` 且
本轮无人工反馈时，直接路由到 `finalizer`（用现有草稿定稿），切断失败自环。

**涉及文件**：`backend/graph.py`

---

## 17. 应用退出不释放 checkpointer 连接

**现象**：FastAPI lifespan 结束时不关闭 SQLite 的 aiosqlite 连接 / PostgreSQL 的
连接池，Docker 每次重启都遗留连接。

**根因**：`lifespan` 只有 `yield` 前（创建），没有 `yield` 后（销毁）；且
`AsyncSqliteSaver`/`AsyncPostgresSaver` 本身没有 `close()` 方法，需关闭其底层
`conn`（aiosqlite 连接 / psycopg 池）。

**解决**：`graph.py` 新增 `close_checkpointer`——先尝试 saver 自带的 `close()`，
再关闭 `saver.conn`（兼容同步/异步），`main.py` 的 lifespan 在 `yield` 后调用。

**涉及文件**：`backend/graph.py`、`backend/main.py`

---

## 18. SSE 历史补发与实时推送存在重复/遗漏竞态

**现象**：订阅者 `subscribe` 注册队列后、`history()` 补发完成前，新发布的事件会
**同时**进入 `history`（被补发）和订阅者队列（被实时消费），同一连接收到重复事件；
前端断线重连时尤为明显。

**根因**：历史补发与实时推送走两条路径，边界没有串行化。

**解决**：`ThreadChannel.subscribe` 改为**同步**把当前历史一次性复制进订阅者队列
（同步方法执行期间不会让出事件循环，与 publish 不交错）；`main.py` 的 stream 生成器
只消费队列，删除手动补发历史逻辑。

**涉及文件**：`backend/manager.py`、`backend/main.py`

---

## 19. Pydantic v2 中 `Field(strip_whitespace=...)` 失效导致空主题通过校验

**现象**：`POST /research/start` 传 `{"topic": "   "}` 返回 200 而非 422，纯空白
主题被当作合法调研主题。

**根因**：Pydantic v2 中 `strip_whitespace` 作为 `Field` 的额外关键字参数已废弃、
不生效（运行时告警 `PydanticDeprecatedSince20`）。

**解决**：改用 `Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]`。

**涉及文件**：`backend/main.py`

---

## 20. 若干小问题修复（合并记录）

- **`Outline(**outline)` 缺字段时 500**：`manager.feedback` 捕获 `ValidationError`，
  转为 400「大纲内容不合法」。
- **interrupt 事件节点定位**：不再简单取 `st.tasks[-1]`，改为遍历 `st.tasks` 找
  第一个带 interrupts 的任务，node 优先用 `task.name`（PregelTask 自带节点名）。
- **`_extract_json` 容错**：先整体解析，失败再取首个 `{` 到末个 `}` 子串，再失败
  抛出带原文片段的 `ValueError`（原实现会静默切错或抛晦涩异常）。
- **`finalize_report` 截断草稿**：移除 `draft_report[:12000]` 截断，避免超长草稿
  尾部丢失后再喂 LLM。
- **前端 demo.html**：批准/打回按钮加防抖（配合 #12 的服务端 409）；
  EventSource 断线重连时对已处理的中断/final 事件去重（配合 #18 的历史重放）。

**涉及文件**：`backend/manager.py`、`backend/nodes.py`、`frontend/demo.html`

---

## 21. 提交修改意见（打回）后前端无响应、不出现新的确认面板（严重）

**现象**：批准可以正常走到下一个确认环节；但只要打回（approved=false，带修改意见），
前端就一直「加载不出来」——既不显示新的确认面板，也没有明确的错误提示。

**根因**（两层叠加）：

1. **LLM 结构化解析脆弱**：真实 DeepSeek 在带修改意见的较长 prompt 下，经常把
   `sections` / `keywords` 等「字符串数组」字段输出成**对象数组**
   （如 `[{"title": "市场概况", ...}, ...]`）。原实现用
   `with_structured_output(method="json_mode")`，解析失败直接抛
   `OutputParserException`；`supervisor_planner` 捕获后写入 error_log，但
   `plan_approved` 仍为 False，条件边路由回自身无限重试，直到 recursion_limit
   抛 `GraphRecursionError`。前端此时只收到 error 日志，没有确认面板。
   （首次生成 prompt 较短通常格式正确，所以「批准」路径能过；打回路径带反馈的
   prompt 更长，模型更易偏离格式。）
2. **前端按内容去重误伤**：打回后 LLM 重写的草稿/大纲可能与旧内容**完全相同**
   （DeepSeek 低温下对相似 prompt 输出稳定）。前端原去重逻辑基于
   `type + JSON.stringify(data)`，把「内容相同的打回后新中断」误判为
   「断线重连重放的历史」，直接忽略，面板不重新显示。

**解决**：
1. **`_structured_invoke` 重写为容错 + 重试**：不再使用 `with_structured_output`，
   改为「普通 invoke（response_format=json_object）→ `_extract_json` 提取 →
   `_coerce_model` 字段级清洗 → Pydantic 校验」，失败重试 3 次。新增辅助函数：
   - `_as_text`：把 LLM 的 content（可能为 list）统一转文本；
   - `_coerce_str_list`：把「对象数组」里的 dict 元素取 title/name/tag 等常见字段
     转回字符串；
   - `_coerce_model`：直接校验失败时，对 `List[str]` 字段清洗、对嵌套模型列表递归
     清洗后重新校验。
   同时给 `generate_outline` / `generate_subtasks` 的 prompt 补充字段类型说明
   （sections/keywords 为字符串数组、不要输出对象）。
2. **事件增加单调递增 `seq`，前端改按 seq 去重**：
   - `ThreadChannel.publish` 为每条事件附加递增的 `seq`；
   - 前端 `showReview` 用 `seq <= lastInterruptSeq` 判断是否重复：断线重连重放的
     旧事件（seq 较小）被忽略，打回后新产生的中断（seq 更大）**即使内容与上次
     完全相同也会重新弹出确认面板**。

**验证**：真实 LLM 下打回大纲 → 重新生成新大纲并暂停；打回草稿 → analyst 重提取、
writer 重写、再次中断；批准重写草稿 → final。全程 0 error 事件，seq 单调递增。

**涉及文件**：`backend/nodes.py`、`backend/manager.py`、`frontend/demo.html`

---

## 22. 调研中断后无法恢复（刷新页面 / 后端重启后调研丢失）（严重）

**现象**：调研进行中刷新页面或关闭浏览器，前端丢失 `thread_id`，无法继续观看/确认，
只能从头重跑；后端进程重启后，线程从内存的 `threads` 字典消失，即使 checkpoint 中状态
仍在，SSE 订阅也直接 404，停在确认点的调研无法继续生成。

**根因**（两层叠加）：
1. **前端不持久化 thread_id**：`currentThread` 只存在页面内存变量，刷新即丢失，SSE
   「新连接补发历史」的机制无从触发（服务端能力早已具备，缺的是恢复入口）。
2. **后端不回归内存线程**：`research_stream` 直接要求 `thread_id in manager.threads`，
   而 `threads` 是纯内存字典（线程完成 1 小时后还会被清理）；进程重启后线程不会回到
   内存，checkpoint 中的状态成了「看得见但够不着」。

**解决**：
1. **前端 localStorage 持久化 thread_id**：`research/start` 成功后写入 `localStorage`；
   页面加载时 `tryRestore()` 自动恢复「未完成」的调研（已完成的不打扰），重新连接 SSE，
   由服务端补发历史事件并继续实时推送。
2. **后端 `ResearchManager.restore()` 从 checkpoint 恢复线程**：SSE 订阅遇到不在内存的
   `thread_id` 时，先从 checkpoint 读取状态重建 `ThreadChannel` 并补发对应事件——
   停在确认点 → `node_start + interrupt`（可继续批准/打回）；已完成 → `final` 重放；
   其他 → `error`。恢复失败才 404。
3. **删除/重置时关闭 SSE**：删除当前连接的调研或发起新调研时主动关闭旧连接，避免
   悬挂连接残留。

**验证**：集成测试 `test_restore_after_restart`（真实 LLM）：跑到大纲确认点后模拟进程
重启（新建 manager、threads 为空），`restore` 补发 plan 中断 → 批准大纲 → 推进到草稿
确认点。

**涉及文件**：`backend/manager.py`、`backend/main.py`、`frontend/demo.html`

---

## 23. 历史报告无删除入口，且删除需彻底清除 checkpoint

**现象**：历史报告功能只能查看/导出，无法删除；若只删历史表记录，checkpoint 中的线程
状态仍残留，无法彻底清理。

**根因**：历史功能缺少删除 API；checkpoint 与历史库是两套独立持久化，删除需同时清理
两者（历史记录 + checkpoint 线程 + 内存 threads）。

**解决**：新增 `DELETE /research/{thread_id}`：先校验历史记录存在且线程未在执行（运行中
返回 409，避免误删进行中的调研），再调用 `graph.adelete_thread()` 删除 checkpoint 线程
状态，最后删除历史库记录并从内存 `threads` 移除；前端历史列表每项加 🗑 删除按钮
（`confirm` 确认），删除当前连接的调研时同时关闭 SSE。

**验证**：集成测试断言删除后 `/detail` 404、历史列表不再包含、重复删除 404。

**涉及文件**：`backend/main.py`、`backend/history.py`、`backend/manager.py`、`frontend/demo.html`

---

## 附：langgraph 1.2.10 恢复 interrupt 的正确姿势（防再踩坑）

```python
from langgraph.types import Command

# 1. 首次运行：节点内 interrupt(payload) 后图暂停
await graph.ainvoke({"topic": "..."}, config)

# 2. 恢复：先 update_state 写回「已确认内容」保持一致性，再 Command(resume) 驱动
await graph.aupdate_state(config, {"outline": outline_obj})
await graph.ainvoke(Command(resume={"approved": True, "feedback": None}), config)
```

> 注意：仅 `update_state` 更新普通字段**不会**消费中断；必须配合
> `Command(resume=...)` 作为图输入，`interrupt()` 才会返回 resume 值。

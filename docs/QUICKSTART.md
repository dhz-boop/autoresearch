# AutoResearch 启动测试步骤

## 一、环境准备

- Python 3.14+
- API 密钥：DeepSeek（`DEEPSEEK_API_KEY`）、可选 Tavily（`TAVILY_API_KEY`）
- Docker（可选，用于容器化部署）

```bash
cd autoresearch
python3 -m venv .venv && source .venv/bin/activate
pip install -r backend/requirements.txt -r requirements-dev.txt
cp .env.example .env        # 填入 DEEPSEEK_API_KEY（Tavily 可选，缺省走 mock）
```

> `.env` 中 `LLM_PROVIDER=deepseek`（模型 `deepseek-v4-flash`），可切换 `siliconflow`。

---

## 二、运行测试

```bash
# 单元测试（不调用 LLM，tavily 无 key 时走 mock）—— 秒级
pytest tests/ -m "not integration"

# 集成测试（真实 LLM，较慢，约 5~10 分钟）
pytest tests/ -m integration
```

- 单元测试：7 项（配置/模型/状态 reducer/工具）
- 集成测试：
  - `tests/test_stage2.py`：图流程 3 条路径（happy path / 大纲修改 / 草稿数据类反馈）
  - `tests/test_stage3.py`：FastAPI + SSE 全流程 + 404 分支

---

## 三、本地启动后端

```bash
cd backend
uvicorn main:app --reload --port 8000
# 或 ../.venv/bin/uvicorn main:app --port 8000
```

验证健康检查：

```bash
curl http://localhost:8000/health
# {"status":"ok"}
```

---

## 四、API 手动测试（curl）

以「2026年国内咖啡机市场机会」为例：

```bash
BASE=http://localhost:8000

# 1. 启动调研，获得 thread_id
curl -s -X POST $BASE/research/start \
  -H "Content-Type: application/json" \
  -d '{"topic":"2026年国内咖啡机市场机会"}'
# => {"thread_id":"<thread_id>"}

# 2. 订阅 SSE 实时事件（会持续输出直到图暂停或完成）
#    看到 event=interrupt 且 data.type=plan 时，即大纲待确认
curl -N $BASE/research/<thread_id>/stream
# => data: {"event":"node_start","node":"supervisor_planner",...}
# => data: {"event":"interrupt","node":"supervisor_planner","data":{"type":"plan","outline":{...}}}

# 3. 批准大纲（重新开一个终端）
curl -s -X POST $BASE/research/<thread_id>/feedback \
  -H "Content-Type: application/json" \
  -d '{"approved":true}'

# 4. 回到 SSE 终端继续观察，直到出现 data.type=draft 的 interrupt
# 5. 批准草稿
curl -s -X POST $BASE/research/<thread_id>/feedback \
  -H "Content-Type: application/json" \
  -d '{"approved":true}'

# 6. SSE 出现 event=final 后，获取最终报告
curl -s $BASE/research/<thread_id>/report
# => {"thread_id":"...","final_report":"# ..."}
```

> 打回修改：`-d '{"approved":false,"feedback":"请补充海外市场对比章节"}'`
> 大纲/草稿修改意见会触发重新规划/重写，并再次进入确认环节。

---

## 五、前端演示

```bash
# 1. 后端运行在 8000
# 2. 前端页面用静态服务器开在 8080（不能与后端同端口）
python -m http.server 8080 --directory frontend
# 3. 访问 http://localhost:8080/demo.html
```

> `demo.html` 通过顶部 `API_BASE = 'http://localhost:8000'` 连接后端。
> 若把页面开在 8000 会与后端冲突，且 `http.server` 不支持 POST（会返回 501）。

或参考 `frontend/README.md` 中的 React 集成示例。

---

## 六、Docker 部署（生产：PostgreSQL 持久化）

```bash
# 确保 .env 已配置 DEEPSEEK_API_KEY 等
docker compose up --build

# 验证
curl http://localhost:8000/health          # {"status":"ok"}
docker compose ps                          # postgres healthy / backend running
```

- `backend` 服务默认 `CHECKPOINTER=postgres`，状态持久化到 PostgreSQL；
- 密钥从项目根 `.env` 自动注入容器；
- 前端页面 `frontend/demo.html` 浏览器直接访问 `http://localhost:8000/demo.html` 不可用
  （FastAPI 不托管静态文件），请用 `python -m http.server` 或接入自有前端。

---

## 七、常见问题

| 现象 | 处理 |
|---|---|
| SSE 连接很快断开、事件不全 | 确认后端版本与 `docs/BUGS.md` 一致（Pydantic 序列化、`data:` 前缀等已修复） |
| `AsyncSqliteSaver` 卡死 | 删除 `data/checkpoints.sqlite*` 残留文件后重启（见 BUGS #8） |
| 无 Tavily Key | `tavily_search` 自动返回 `[MOCK]` 结果，流程可完整跑通 |
| 想切到硅基流动模型 | `.env` 设 `LLM_PROVIDER=siliconflow` 并填 `SILICONFLOW_API_KEY` |

---

## 八、中断恢复与历史管理

### 中断恢复（刷新页面 / 后端重启后继续生成）

中断不会丢失调研，可以从上次暂停的位置继续：

- **刷新页面 / 关闭浏览器再打开**：前端把最近一次调研的 `thread_id` 保存在浏览器
  `localStorage`，重新打开页面时自动尝试恢复「未完成」的调研：
  - 线程仍在运行 → 恢复实时日志与确认面板，继续观看与人工确认；
  - 暂停在大纲/草稿确认点 → 后端补发中断事件，可直接批准或打回继续；
  - 已完成 → 不自动恢复（从侧边栏「历史报告」查看）。
- **后端进程重启**：线程状态持久化在 LangGraph checkpoint（`sqlite` / `postgres`）。
  前端恢复连接时，后端会从 checkpoint 自动把线程恢复到内存管理——暂停在确认点的
  调研可继续批准/打回，已完成的重放最终报告。因此进程重启后调研不会丢。
- 调研历史记录写入独立的历史库（`HISTORY_DB`，默认 `data/history.sqlite`），
  与 checkpoint 分离，互不影响。

> 说明：历史列表中的「进行中」调研若在进程重启后未再被前端恢复连接，
> 会被标记为「中断」（`interrupted`）。再次打开页面会自动恢复并继续。

### 历史报告管理

- 侧边栏「历史报告」列出全部调研，状态徽章：进行中 / 已完成 / 失败 / 中断；
- 点击条目查看报告全文与**生成过程中的全部批改意见**（大纲/草稿的批准或打回记录）；
- 导出：`GET /research/{id}/history/export?format=markdown|docx`（前端按钮一键下载）；
- 删除：`DELETE /research/{id}`（前端 🗑 按钮，确认后删除历史记录 + checkpoint）。
  **正在执行的调研不可删除**（返回 409）。

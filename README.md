# AutoResearch · 自动化调研系统

一个「AI 自动完成深度调研」的后端服务 + 前端演示页。输入一个主题，系统自动规划大纲 → 分步搜索 → 撰写报告，并在关键节点暂停等待**人工确认**（大纲、草稿），确认或打回修改后继续生成，最终输出可导出的 Markdown / Word 报告。

- 后端：FastAPI + LangGraph，SSE 实时推送事件流
- 前端：`frontend/demo.html` 单文件原生 JS，无需构建，双击或静态服务器即可打开
- LLM：DeepSeek（默认）/ SiliconFlow（OpenAI 兼容），`.env` 一键切换
- 搜索：Tavily / 博查，未配置 key 时自动降级为 mock 结果，本地即可跑通全流程

## ✨ 特性

| 能力 | 说明 |
|---|---|
| 🧠 智能调研 | 规划 → 搜索 → 撰写 → 审核 → 精修的全自动流程，内置人工介入点 |
| 👤 人工反馈 | 大纲 / 草稿两级确认，打回修改意见会触发重新规划 / 重写 |
| 🔄 中断恢复 | 刷新页面、后端重启都能从上次暂停点继续，调研不丢失 |
| 📦 历史管理 | 历史报告 + 全部批改意见留痕，支持 Markdown / Word 导出与删除 |
| 🔌 多 LLM | DeepSeek 官方 API 与硅基流动一键切换 |
| 🧪 可测试 | 单元测试不调用 LLM（秒级），集成测试覆盖全流程（含 SSE） |
| 🐳 生产部署 | `docker compose up` 一键起 PostgreSQL + 后端 |

## 🚀 快速开始

### 环境准备

```bash
cd autoresearch
python3 -m venv .venv && source .venv/bin/activate
pip install -r backend/requirements.txt -r requirements-dev.txt
cp .env.example .env        # 填入 DEEPSEEK_API_KEY（搜索 key 可选）
```

### 启动后端

```bash
cd backend
uvicorn main:app --port 8000
```

验证健康检查：`curl http://localhost:8000/health` → `{"status":"ok"}`

### 打开前端演示页（任选其一）

```bash
# 方案 a：直接双击 frontend/demo.html
# 方案 b：静态服务器（需在项目根目录执行，且不能占用 8000 端口）
python -m http.server 8080 --directory frontend
# 访问 http://localhost:8080/demo.html
```

> 页面默认连接 `http://localhost:8000`，改了后端端口就到 `demo.html` 顶部改 `API_BASE`。

### 跑一遍调研（curl 版）

```bash
BASE=http://localhost:8000

# 1. 启动调研，获得 thread_id
curl -s -X POST $BASE/research/start -H "Content-Type: application/json" \
  -d '{"topic":"2026年国内咖啡机市场机会"}'

# 2. 订阅 SSE 实时事件（看到 event=interrupt 且 type=plan 时，大纲待确认）
curl -N $BASE/research/<thread_id>/stream

# 3. 另一个终端批准大纲
curl -s -X POST $BASE/research/<thread_id>/feedback \
  -H "Content-Type: application/json" -d '{"approved":true}'
```

打回修改：`{"approved":false,"feedback":"请补充海外市场对比章节"}`

完整步骤见 [docs/QUICKSTART.md](docs/QUICKSTART.md)。

## 🔌 API 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/research/start` | 启动调研，返回 `{"thread_id": "..."}` |
| GET | `/research/{id}/stream` | SSE 事件流：`node_start` / `node_end` / `interrupt` / `final` / `error` |
| POST | `/research/{id}/feedback` | 提交人工反馈 `{approved, feedback?, outline?, draft?}` |
| GET | `/research/{id}/report` | 获取最终 Markdown 报告 |
| GET | `/research/history` | 调研历史列表 |
| GET | `/research/{id}/detail` | 单次调研详情（报告全文 + 批改意见） |
| GET | `/research/{id}/report/export` | 导出最终报告（Markdown / Word） |
| GET | `/research/{id}/outline/export` | 导出大纲（Markdown / Word） |
| GET | `/research/{id}/history/export` | 从历史库导出报告 |
| DELETE | `/research/{id}` | 删除调研（含 checkpoint），执行中返回 409 |
| GET | `/health` | 健康检查 |

SSE 事件格式：`{"event": "node_start", "node": "节点名", "data": {...}}`

前端接入示例（React hook + 页面）见 [frontend/README.md](frontend/README.md)。

## ⚙️ 配置（`.env`）

复制 `.env.example` 为 `.env` 后按需填写：

| 配置项 | 说明 | 默认 |
|---|---|---|
| `LLM_PROVIDER` | `deepseek` / `siliconflow` | `deepseek` |
| `DEEPSEEK_API_KEY` | DeepSeek 官方 API Key | - |
| `SILICONFLOW_API_KEY` | 硅基流动 API Key | - |
| `TAVILY_API_KEY` | Tavily 搜索（可选） | - |
| `BOCHA_API_KEY` | 博查搜索（可选） | - |
| `CHECKPOINTER` | `memory` / `sqlite` / `postgres` | `sqlite` |
| `HISTORY_DB` | 调研历史库路径 | `./data/history.sqlite` |
| `LANGCHAIN_API_KEY` | LangSmith 追踪（可选） | - |

> 所有密钥只从环境变量读取，绝不硬编码；`.env` 已被 `.gitignore` 排除。

## 🧪 测试

```bash
# 单元测试（不调用 LLM，秒级）
pytest tests/ -m "not integration"

# 集成测试（真实 LLM，约 5~10 分钟，覆盖 SSE 全流程）
pytest tests/ -m integration
```

## 🐳 Docker 部署（PostgreSQL 持久化）

```bash
docker compose up --build
# 验证：curl http://localhost:8000/health
```

- `backend` 服务默认 `CHECKPOINTER=postgres`，状态持久化到 PostgreSQL；
- 密钥从项目根 `.env` 自动注入容器；
- 详情见 [docs/QUICKSTART.md](docs/QUICKSTART.md)。

## 📁 项目结构

```
.
├── backend/            # FastAPI 后端
│   ├── main.py         # 应用入口 + 全部 REST/SSE 端点
│   ├── graph.py        # LangGraph 图编排（规划/搜索/撰写/审核）
│   ├── nodes.py        # 各阶段节点实现
│   ├── manager.py      # 线程管理（并发防护 / 中断恢复）
│   ├── history.py      # 调研历史持久化
│   ├── exporters.py    # Markdown / Word 导出
│   └── config.py       # 环境变量配置
├── frontend/
│   ├── demo.html       # 单文件演示页（原生 JS + SSE）
│   └── README.md       # 前端接入文档（含 React 示例）
├── docs/
│   ├── QUICKSTART.md   # 详细启动与测试步骤
│   └── BUGS.md         # 已修复问题记录
├── tests/              # 单元 + 集成测试
└── docker-compose.yml  # PostgreSQL + 后端编排
```

## 📄 文档

- [快速开始 / 完整 API 调试](docs/QUICKSTART.md)
- [前端接入指南](frontend/README.md)
- [问题排查记录](docs/BUGS.md)

# AutoResearch 前端

后端提供 4 个接口，前端只需两件事：**SSE 消费实时事件** + **提交人工反馈**。

| 接口 | 方法 | 说明 |
|---|---|---|
| `/research/start` | POST `{"topic": "..."}` | 启动调研，返回 `{"thread_id": "..."}` |
| `/research/{thread_id}/stream` | GET (SSE) | 实时事件流：`node_start` / `node_end` / `interrupt` / `final` / `error` |
| `/research/{thread_id}/feedback` | POST | 提交 `{approved, feedback?, outline?, draft?}` |
| `/research/{thread_id}/report` | GET | 获取最终 Markdown 报告 |

> 事件格式：`{"event": "node_start" | "node_end" | "interrupt" | "final" | "error", "node": "节点名", "data": {...}}`
> - `interrupt.data.type === "plan"`：待确认大纲，`data.outline` 为大纲对象
> - `interrupt.data.type === "draft"`：待审核草稿，`data.report` 为 Markdown 文本

---

## 一、最快跑通：`demo.html`（无需构建）

`demo.html` 是一个单文件原生 JS 页面，直接用浏览器打开即可使用（后端需允许 CORS，`main.py` 已放开）。

```bash
# 1. 启动后端
cd backend && ../.venv/bin/uvicorn main:app --port 8000

# 2. 打开演示页（任选其一）
#    a) 直接双击 frontend/demo.html
#    b) 本地静态服务：python3 -m http.server 8080 --directory frontend
#       然后访问 http://localhost:8080/demo.html
```

> **端口关系**：页面默认在 8080，后端在 8000。demo.html 顶部有
> `const API_BASE = 'http://localhost:8000'`，所有请求都指向它。
> 若后端换了端口，改这一处即可。
> 注意：若把页面通过 `python -m http.server` 开在 8000 会与后端冲突，
> 且静态服务器不支持 POST（会返回 501），务必让前端与后端分端口。

页面包含：主题输入 → 开始调研 → SSE 实时日志 → 大纲/草稿确认面板（批准/打回修改）→ 最终报告展示。

---

## 二、React 集成（复用已有项目）

核心是两个能力：**SSE 订阅** 与 **反馈提交**。可参考下面的 hook 接入任意 React 组件。

```tsx
// useResearch.ts
import { useEffect, useState } from "react";

export type SSEEvent =
  | { event: "node_start" | "node_end"; node: string; data: Record<string, unknown> }
  | { event: "interrupt"; node: string; data: { type: "plan" | "draft"; outline?: any; report?: string } }
  | { event: "final"; node: string; data: { final_report: string } }
  | { event: "error"; node: string; data: { error: string } };

export function useResearch() {
  const [threadId, setThreadId] = useState<string | null>(null);
  const [logs, setLogs] = useState<string[]>([]);
  const [pending, setPending] = useState<SSEEvent | null>(null); // 待人工确认的内容
  const [report, setReport] = useState<string | null>(null);

  const start = async (topic: string) => {
    const res = await fetch("/research/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ topic }),
    });
    const { thread_id } = await res.json();
    setThreadId(thread_id);

    const es = new EventSource(`/research/${thread_id}/stream`);
    es.onmessage = (e) => {
      const evt: SSEEvent = JSON.parse(e.data);
      if (evt.event === "node_start") setLogs((l) => [...l, `▶ ${evt.node} 开始`]);
      if (evt.event === "node_end") setLogs((l) => [...l, `✔ ${evt.node} 完成`]);
      if (evt.event === "interrupt") setPending(evt);              // 触发审核 UI
      if (evt.event === "final") { setReport(evt.data.final_report); es.close(); }
      if (evt.event === "error") setLogs((l) => [...l, `✖ ${evt.data.error}`]);
    };
  };

  const submitFeedback = async (approved: boolean, feedback?: string) => {
    if (!threadId || !pending) return;
    const body: Record<string, unknown> = { approved, feedback };
    // 关键：把当前确认的大纲/草稿写回，保证后端恢复后内容一致
    if (pending.data.type === "plan") body.outline = pending.data.outline;
    if (pending.data.type === "draft") body.draft = pending.data.report;
    await fetch(`/research/${threadId}/feedback`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    setPending(null);
  };

  return { threadId, logs, pending, report, start, submitFeedback };
}
```

```tsx
// 组件中使用
function App() {
  const { logs, pending, report, start, submitFeedback } = useResearch();
  const [topic, setTopic] = useState("");

  return (
    <div>
      <input value={topic} onChange={(e) => setTopic(e.target.value)} placeholder="调研主题" />
      <button onClick={() => start(topic)}>开始调研</button>
      <ul>{logs.map((l, i) => <li key={i}>{l}</li>)}</ul>

      {/* 人工确认面板 */}
      {pending?.event === "interrupt" && (
        <div>
          <h3>{pending.data.type === "plan" ? "请确认大纲" : "请审核草稿"}</h3>
          <pre>
            {pending.data.type === "plan"
              ? JSON.stringify(pending.data.outline, null, 2)
              : pending.data.report}
          </pre>
          <textarea id="fb" placeholder="修改意见（选填）" />
          <button onClick={() => submitFeedback(true)}>批准</button>
          <button onClick={() => submitFeedback(false)}>打回修改</button>
        </div>
      )}

      {report && (
        <div>
          <h3>最终报告</h3>
          <pre>{report}</pre>
        </div>
      )}
    </div>
  );
}
```

> Markdown 渲染可用任意库（如 `react-markdown`）或直接展示 `report` 文本。

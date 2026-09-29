# 🛰️ 交期哨兵 Delivery Sentinel（V2）

> 📖 **想了解这个项目为什么做、解决什么痛点、业务流程如何优化？先读 [项目说明.md](项目说明.md)**——完整的项目背景、痛点分析、流程梳理（As-Is / To-Be）与方案详解。
> 本 README 是技术手册：快速开始、功能总览、架构、对接与测试。

面向制造业的**交期风险预警智能体**：真实数据经感知层（事件 API / 文件收件箱 / API 轮询 / IMAP 邮箱）汇入「订单-工序-物料」状态图，**规则 → 评分 → LLM 归因 → Agent 自主调查**四级引擎预警交期风险，AI 起草催办动作，**人审后**发送。

仿 Dify 形态：LangGraph 十节点运行画布 + 业务工作台 + **感知中心** + ReAct 智能助手 + 人工确认闭环。

> 本项目是《制造业Agent发力方向与场景案例设计.md》案例 2「交期哨兵 Agent」的工程实现。V1 完整快照存于 `../delivery-sentinel-v1/`；V2 新增：感知层真实采集、ReAct 自主助手、预警调查员。

---

## 快速开始

```bash
# 1) 依赖（本机已验证：Python 3.11 + fastapi/uvicorn/langgraph/langchain + pandas/openpyxl）
pip install -r requirements.txt

# 2) 启动（二选一）
run.bat                       # Windows，自动打开浏览器
python -m uvicorn backend.main:app --host 127.0.0.1 --port 8765

# 3) 打开 http://127.0.0.1:8765
```

**3 分钟全链路演示**：感知中心 → [生成示例表格] → [立即扫描]（Xclaw 场景的本地替身）→ 工作台 ▶ 运行一轮扫描 → 预警中心看**调查报告** → 智能助手追问（有 Key 时走 ReAct，回复带"via ReAct·N步"）。

## 感知层：真实数据怎么进来

| 通道 | 用法 | 配置 |
| --- | --- | --- |
| **事件 API**（推荐给 Xclaw） | `POST /api/events`（Bearer token），支持 `/events/batch` 批量；自由文本走 `/api/events/extract`（LLM/正则抽取，抽不出→未解析队列） | `SENTINEL_API_TOKEN`（默认 sentinel-dev-token） |
| **文件收件箱** | Xclaw/人工把 ERP/MES 导出表格放进 `data/inbox/`，连接器解析→事件→归档 processed/；表头差异用 `data/inbox/mapping.json` 映射 | `SENTINEL_INBOX_DIR`、`SENTINEL_INBOX_INTERVAL` |
| **外部 API 轮询** | 拉取有接口系统的 JSON 行数组（每行=一条事件） | `SENTINEL_POLL_URL/TOKEN/INTERVAL` |
| **IMAP 邮箱** | 拉未读邮件 → LLM/正则抽取交期与变更 → 事件 | `SENTINEL_IMAP_HOST/PORT/USER/PASS/FOLDER/SSL` |

所有通道汇入同一事件收件箱，由 LangGraph「事件接入/状态更新」节点消费（支持订单登记事件注册新订单、报工完工数量、物料新 ETA 等）；订单不存在或抽取失败的内容进**未解析队列**，感知中心可人工挂靠订单转正或忽略——宁可漏一条，不可错一条。

## 功能总览

| 页面 | 能力 |
| --- | --- |
| 🏭 工作台 | KPI、风险分布环图、TOP 风险订单、实时事件流、模拟数据源注入 |
| 📋 订单风险 | 12 张订单的实际 vs 计划进度、物料齐套、点击行打开详情抽屉（工序/物料/事件/预警） |
| 🔌 感知中心 | 连接器启停/扫描、事件 API token 与 curl 示例（Xclaw 对接）、摄入统计、未解析队列人工处理 |
| 🔔 预警中心 | 黄/橙/红分级预警：**证据链 + Agent 调查报告**、建议动作、AI 催办草稿，人工确认发送 |
| 🤖 智能体运行 | LangGraph 十节点画布：事件接入→状态更新→规则扫描→风险评分→预警去重→归因生成→**风险调查**→动作起草→人工闸门→汇总；逐节点点亮 + 日志明细 |
| 💬 智能助手 | LLM 开启时走 **ReAct 自主查证**（自己决定查订单/拉事件线/对照同类单，显示查询步数）；无 Key 降级规则引擎 |
| ⚙️ 设置 | LLM Provider 配置（可选）+ 连接测试；风险阈值展示 |

## 架构

```
frontend/  零构建 SPA（原生 HTML/CSS/JS，FastAPI 托管）
backend/
  ├─ main.py           FastAPI 入口（api + api_perception 双路由）
  ├─ api.py            业务接口（dashboard/orders/alerts/agent/simulator/copilot/settings）
  ├─ api_perception.py 感知层接口（events 推送+抽取 / 连接器控制 / 未解析队列 / hub 状态）
  ├─ domain.py         Order / ProcessStep / Material / Event / RiskAlert（含调查报告）
  ├─ store.py          内存状态 + JSON 持久化 + 未解析队列 + 摄入统计
  ├─ seed_data.py      12 张种子订单 + 历史事件 + 「订单登记」构造器
  ├─ simulator.py      演示事件模拟器（4 类场景）
  ├─ engine/           L1 规则 + L2 评分（纯函数可单测）
  ├─ perception/
  │    └─ extractor.py 非结构化文本 → 结构化事件（LLM 主路 / 正则兜底）
  ├─ connectors/
  │    ├─ base.py      连接器基类（scan / 后台循环 / 状态）
  │    ├─ inbox_file.py 文件收件箱（.csv/.xlsx → 事件，pandas 列名映射）
  │    ├─ feeds.py     API 轮询 + IMAP 邮件（MailSource 可注入测试桩）
  │    └─ manager.py   连接器注册/启停/状态
  ├─ agents/
  │    ├─ graph.py     LangGraph 十节点工作流（stream 逐节点产出）
  │    ├─ react.py     自研 ReAct 引擎 + 6 个只读领域工具
  │    ├─ attribution.py  L3 归因：模板引擎 + LLM 双路
  │    ├─ investigator.py 预警调查员：报告四节结构，LLM/模板双路
  │    └─ copilot.py   助手：ReAct（LLM 开）/ 规则意图（兜底）
  └─ llm/provider.py   OpenAI 兼容协议（openai/dashscope/zhipu/deepseek/custom）
tests/                 pytest 47 例：引擎阈值 / E2E / 感知层三连接器 / ReAct / 调查员 / 事件 API 鉴权
scripts/
  ├─ simulate.py       终端仿真（不依赖浏览器）
  └─ api_smoke.py      全接口冒烟 16 项（含感知层全链路）
```

## 接入真实 LLM

**LLM 为可选增强，不配 Key 全功能可用**：未配置 Key 时，归因、调查报告、助手自动降级为确定性模板引擎，行为完全可预期。配好 Key 后，系统实测接通智谱 **GLM-4.5-Air**（"思考模式"默认关闭——实测同一任务 3.5s/233 tokens → 0.7s/32 tokens，质量不降）。

- 配置（二选一）：环境变量 `SENTINEL_LLM_PROVIDER / BASE_URL / MODEL / API_KEY`，或前端「设置」页（OpenAI 兼容协议，支持 openai/dashscope/zhipu/deepseek/custom）；
- 关闭 LLM：provider 设为 `none`，全系统自动降级为确定性模板引擎，功能不受影响；
- 健壮性：429/5xx/超时自动退避重试；`response_format` 不被支持时自动回退文本模式解析。

**防幻觉三道防线（实测驱动）**：① 事件归一化——抽取的类型与字段形状必须匹配（"只有交期没有数量"自动改判为供应商反馈）；② strict ReAct——模型不查工具直接作答会被系统退回，拒不悔改则整体回落确定性路径；③ 接地校验——调查报告/助手回复中出现的订单号、部件号必须真实存在，否则回落模板。

### 拨真测试（真实调用 LLM）

```bash
# 日常测试（默认）：LLM 自动禁用，确定性、零成本，不外呼
python -m pytest tests/ -v           # 51 passed + 4 skipped

# 拨真测试：真实调用 GLM-4.5-Air 验证端到端质量（连通/抽取归一化/报告接地/ReAct）
SENTINEL_LIVE=1 python -m pytest tests/test_live_llm.py -v    # 4 passed
```

## 真实数据演示（data/inbox/ 内置三张真实规格导入表）

| 文件 | 模拟来源 | 内容与测试点 |
| --- | --- | --- |
| `A_ERP订单主数据导入_*.csv` | ERP 订单主数据 | 3 张新订单（含千分位数量"1,500"、无物料行）→ 订单登记 → 立即参与风险计算 |
| `B_采购部_供应商到货承诺更新_*.csv` | 采购部 Excel | 表头变体（"最新承诺到货/变化说明"）考验模糊匹配；既有恶化（缺口 3→5 天）也有改善 |
| `C_MES工单报工导出_*.csv` | MES 报工 | 9 道工单跨 8 张订单，千分位、空备注、含工单号等额外列 |

演示路径：感知中心 → [立即扫描]（16 行 → 16 事件，新订单即时注册）→ 工作台 ▶ 运行一轮扫描（15 单、真 LLM 并行调查，约 40 秒）→ 预警中心看 **LLM 调查报告**。重复导入同内容文件会被行指纹自动去重。

## 测试

```bash
python -m pytest tests/ -v          # 51 例通过（LLM 自动禁用，确定性零成本）+ 4 例拨真跳过
SENTINEL_LIVE=1 python -m pytest tests/test_live_llm.py -v   # 拨真：真实调用 GLM-4.5-Air
python scripts/simulate.py          # 终端仿真（无需起服务）
python scripts/api_smoke.py         # 起服务后跑全接口冒烟（16 项，真 LLM 在环约 3 分钟）
```

## 设计决策（为什么这么做）

| 决策 | 理由 |
| --- | --- |
| 预警必须带证据链 | 白皮书对制造业 AI 的"可解释、可追溯"要求；跟单员要拿去和车间对质 |
| 人工闸门（human-in-the-loop） | Gartner 预测 2027 年末 40%+ 代理型 AI 项目被取消；高风险动作人审是存活策略 |
| 同级抑制 + 恶化升级 | 抑制"预警疲劳"——误报太多跟单员会关掉通知 |
| LLM 可插拔可降级 | 制造业内网环境常态无外网 Key；模板引擎保证可用性，LLM 是增强而非依赖 |
| L1 规则 + L2 评分取更差 | 规则保证可解释下限，评分捕捉多因子叠加的组合风险 |
| 事件只入 inbox、由工作流消费 | 模拟"感知-决策-执行"闭环：数据源与智能体解耦，未来接真实 ERP/MES 只换采集层 |

## 数据口径

- **进度偏差**：`(计划应完成 − 实际完成) / 计划应完成`，计划按线性排程估算
- **物料缺口**：`承诺到货日 − 需求日`（天），>0 即缺口；被推迟次数单独累计作证据
- **评分**：`100 × (0.40×偏差 + 0.25×物料 + 0.20×紧迫 + 0.15×波动)`，≥70 红 / ≥50 橙 / ≥35 黄
- 种子数据基于"今天"动态生成，每天首次启动自动重播种

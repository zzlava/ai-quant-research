# 量化研究仪表盘

本地只读界面，消费仓库已有 FastAPI。**研究系统，不交易，不构成投资建议。**

不会改 Python 策略、数据/PIT 契约或后端路由。开发时由 Vite 把 `/api` 代理到 `http://127.0.0.1:8000`，不依赖后端 CORS。

## 运行

先在仓库根目录启动 API（需已有本地快照；本前端不会下载、导入数据或读取 Token）：

```bash
cd ..
source .venv/bin/activate
uvicorn app.api.main:app --reload --port 8000
```

再启动仪表盘：

```bash
cd frontend
npm install
npm run dev
```

浏览器打开 Vite 提示的地址（默认 `http://127.0.0.1:5173`）。

```bash
npm test
npm run build
```

## 页面行为

- 常驻说明：研究系统，不交易 / 不构成投资建议。
- 显示 `GET /health`。
- 从 `GET /strategies` 选择策略，按日期和 `top` 请求 `GET /ranking`，展示排名与分项分数。
- `POST /backtests` 提交研究回测，展示状态、核心指标和 `equity_curve`；无曲线时显示空状态。
- 网络失败、空排名、预检不足等只展示后端 `detail`，不生成假排名、假收益或“策略有效”文案。

## 后端缺口

当前 API **没有**返回严格点时状态。`signal_ready_start`、universe 模式、成员完整性和来源 provenance 必须用 CLI 复核：

```bash
python -m app.cli preflight-research \
  --strategy baseline_csi300_pit_v1 \
  --start 2022-01-01 \
  --end 2024-12-31
```

本页不会把“API 能打分/能回测”解释成已经通过 PIT 预检。

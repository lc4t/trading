# Trading

量化交易工具集：**行情数据同步** + **策略回测**，配有一个独立部署的 Next.js 前端。

> 完全开源，代码内不包含 API key / 密码 / 个人信息——可配置项见 `.env.example`。

## 内置策略

- **双均线 + 吊灯/ADR 止损**（1.0 版即有，单标的）—— 见 [`trading/strategies/dual_ma.py`](trading/strategies/dual_ma.py)
- **动量轮动**（2.0 新增，多标的）—— 两个变体：
  - `MomentumBasket`：用户为每个候选标的预设目标权重，动量过阈值的留，其余转现金。
  - `MomentumTopN`：每期取动量最强的 N 个，等权持有。

## 仓库结构

```
.
├── trading/                # Python 包
│   ├── api.py                  # 用户态门面 run_backtest / MomentumBasket / ...
│   ├── data/                   # 数据仓库（MySQL）+ fetcher（tt-fetcher）
│   ├── market/                 # 行情缓存层：抓取 / 缓存 / 元数据，tt-fetcher、回测、MCP 共用
│   ├── strategies/             # base / signals / dual_ma / momentum / momentum_rotation
│   ├── engine/                 # single / multi / sweep
│   ├── analysis/               # metrics / report
│   ├── io/                     # notify / json_export / csv_export / rotation_export / digest
│   ├── cli/                    # argparse 入口
│   └── templates/              # Jinja2 报告模板
├── notebooks/quickstart.ipynb  # Jupyter 示例
├── frontend/                   # Next.js 静态站点（独立部署）
├── tests/                      # pytest
├── scripts/                    # 一次性脚本（csvfilter 等）
├── Dockerfile                  # 后端镜像（GHCR）
├── pyproject.toml              # uv-managed Python 项目
└── .github/workflows/          # CI: 构建并推送 Docker 镜像
```

> 根目录的 `backtest.py` / `fetcher.py` 是 **向后兼容 shim**——
> 老的工作流命令仍能直接使用，内部转发到 `trading.cli.*`。

## 安装

依赖：[`uv`](https://docs.astral.sh/uv/) 与 Python 3.13+。

```bash
uv sync
cp .env.example .env   # 按需填入 DB / SMTP / PushGo
```

## CLI 用法（与 1.0 兼容）

```bash
# 同步历史行情
uv run python -m trading.cli.fetcher --start-date 2024-12-01

# 单标的回测（双均线 + 吊灯）
uv run python -m trading.cli.backtest 159915.SZ \
  --initial-capital 50000 \
  --start-date 2022-01-01 \
  --use-ma --ma-short 5 --ma-long 8 \
  --use-chandelier --chandelier-multiplier 1.5 --chandelier-period 15 \
  --output-json data/159915.SZ.json
```

## 交易信号日报

当 `data/*.json`（及 `data/rotation/*.json`）已生成后，运行独立汇总命令，把当天所有标的/策略的状态汇总后推送到 PushGo。

在 `.env` 中配置：

```bash
PUSHGO_CHANNEL_ID=your-channel-id
PUSHGO_PASSWORD=your-channel-password
# 可选，默认使用官方网关
PUSHGO_URL=https://gateway.pushgo.dev/push
```

```bash
uv run python -m trading.cli.digest --data-dir frontend/data
```

可选参数：

- `--date 2026-03-10`（默认 Asia/Shanghai 当天）
- `--pushgo-url https://gateway.pushgo.dev/push`
- `--max-retries 3` / `--retry-delay 5`
- `--dry-run`（只输出标题和正文，不推送，本地验证格式用）

行为说明：

- 只汇总 `reportDate` 等于目标日期的 JSON（单标的在 `data-dir` 下，轮动策略在 `data-dir/rotation` 下）
- 标题只统计 `买入`/`卖出`，`持有`/`观望` 仍会展示详情
- 没有任何当天 JSON 时，会发送一条"无任何数据可用"的通知
- PushGo 推送失败会按参数重试；超过重试次数后命令非零退出

## 交互式 API（2.0 新增）

```python
from trading import run_backtest, MomentumBasket, MomentumTopN, Schedule

# 加权篮子
result = run_backtest(
    strategy=MomentumBasket(
        weights={
            "159915.SZ": 0.4,
            "513100.SH": 0.4,
            "518880.SH": 0.2,
        },
        momentum_fn="simple_return",
        lookback=63,
        threshold=0.0,                              # 动量为负转现金
        schedule=Schedule.monthly_nth_trading_day(1),
    ),
    start="2022-01-01",
    initial_capital=50_000,
)

print(result.summary())
result.trades         # → DataFrame
result.metrics        # → dict
result.next_signal    # → dict
result.plot()         # → matplotlib figure
```

详见 [`notebooks/quickstart.ipynb`](notebooks/quickstart.ipynb)。

## 动量轮动策略参数

### `MomentumBasket(weights, momentum_fn, lookback, threshold, schedule, cooldown_days)`

| 参数              | 类型              | 默认值                  | 说明 |
| ----------------- | ----------------- | ----------------------- | ---- |
| `weights`         | `dict[str,float]` | —                       | `{symbol: 目标权重}`。所有权重之和必须 ≤ 1.0；未占满的部分自动为现金底仓。允许负权重值会被拒绝。 |
| `momentum_fn`     | `str` 或 callable | `"simple_return"`       | 动量计算函数，见下表。也可传入自定义 `(closes, lookback) -> float`。 |
| `lookback`        | `int`             | `63`                    | 动量回望窗口（交易日）。3 个月 ≈ 63 ，6 个月 ≈ 120，1 年 ≈ 252。 |
| `threshold`       | `float`           | `-0.99`                 | 动量准入门槛。`momentum < threshold` 的标的转现金。默认 -0.99 ≈ 不过滤；改成 `0.0` 就成了 "不买入下跌标的"。 |
| `schedule`        | `Schedule`        | `every_n_trading_days(1)` | 调仓日历，见下方表格。 |
| `cooldown_days`   | `int`             | `0`                     | 一次 rebalance 后强制等待 N 个交易日。0 = 不限制。 |

### `MomentumTopN(universe, top_n, cash_buffer, momentum_fn, lookback, threshold, schedule, cooldown_days)`

| 参数             | 类型           | 默认值                    | 说明 |
| ---------------- | -------------- | ------------------------- | ---- |
| `universe`       | `list[str]`    | —                         | 候选标的列表。 |
| `top_n`          | `int`          | `1`                       | 每期持有几个标的（等权）。 |
| `cash_buffer`    | `float`        | `0.0`                     | 永久保留的现金比例（0 ≤ x < 1）。 |
| 其余参数同上     |                |                           |      |

### 动量算法（5 种）

| `momentum_fn` key | 算法                                      | 适用场景 |
| ----------------- | ----------------------------------------- | -------- |
| `simple_return`   | `close[t] / close[t-N] - 1`               | 最经典、最普遍。|
| `log_return`      | `ln(close[t] / close[t-N])`               | 对极端涨跌不敏感，适合横截面比较。|
| `sharpe`          | `年化日均对数收益 / 年化日波动`           | 偏好"稳定上涨"而不是"暴涨"。|
| `weighted`        | 指数衰减加权对数收益（默认 `half_life=21` 交易日） | 强调近期表现，老数据权重快速衰减。|
| `dual_12_1`       | `12 个月对数收益 − 最近 1 个月对数收益`   | Asness 等经典论文，抑制短期反转噪声。默认 `lookback=252, short_skip=21`。|

### 调仓日历 (`Schedule`)

| 工厂                                       | 含义 |
| ------------------------------------------ | ---- |
| `Schedule.every_n_trading_days(n)`         | 每 N 个交易日触发。首日必触发。**默认 `n=1`**。 |
| `Schedule.weekly(weekday=1)`               | 每周特定工作日（ISO：周一=1...周五=5）。 |
| `Schedule.weekly_nth_trading_day(n=1)`     | 每周第 N 个交易日。 |
| `Schedule.monthly(day=1)`                  | 每月某个**自然日**当天首个交易日（含或之后）。 |
| `Schedule.monthly_nth_trading_day(n=1)`    | 每月第 N 个交易日。 |

### 执行细节

- **回测引擎**：基于 [backtrader](https://www.backtrader.com/)，每根 K 线代表一个交易日。
- **执行时机**：rebalance 信号在当日收盘后产生，订单默认在 **次日开盘** 成交（backtrader 默认行为）。
- **手续费**：通过 `commission_rate` 参数设置，默认 `0.0001`（万分之一）。

## 行情缓存 + MCP 服务（`tt-serve`）

同一个镜像里的另一个入口：把行情缓存在 MySQL 里，通过带鉴权的 **MCP（Streamable HTTP）** 提供给 agent；
并把 Trading 已有的回测能力以薄封装暴露为工具。它只做行情读取、缓存和回测，不含任何持仓或投资纪律相关能力。

### 配置（全部环境变量，参见 [`.env.example`](.env.example)）

| 变量 | 必填 | 说明 |
| ---- | ---- | ---- |
| `DB_HOST` `DB_USER` `DB_PASSWORD` `DB_NAME` / `DB_PORT` | 是 | MySQL 连接，沿用现有变量；账号需要建表和读写权限 |
| `MCP_TOKENS` 或 `MCP_TOKENS_FILE` | 是 | token 配置（见下），两者可同时用；都没有则启动失败 |
| `MCP_HOST` / `MCP_PORT` | 否 | 监听地址 / 端口，默认 `127.0.0.1:8765`（镜像内默认 `0.0.0.0`） |
| `MCP_RATE_LIMIT_{READ,REFRESH,BACKTEST}_PER_MIN` | 否 | 每个 token 每分钟调用上限，默认 120 / 10 / 6 |
| `MCP_BACKTEST_{CONCURRENCY,MAX_SPAN_DAYS,MAX_SYMBOLS}` | 否 | 回测并发 / 单次区间天数 / 标的数上限，默认 2 / 3660 / 20 |
| `MCP_ALLOWED_HOSTS` | 否 | 逗号分隔的 Host 白名单；设置后启用 DNS-rebinding 防护 |

缺 MySQL 配置、缺 token 配置、token 配置非法或数据库连不上时，服务**启动即失败**并打印原因（只写变量名，不回显值）。
启动时自动建表（含元数据旁表）。

### 生成 token

服务端只保存 token 的 SHA-256；明文只在生成时出现一次，交给客户端保管：

```bash
uv run tt-serve gen-token --name agent-a --scopes read
# 输出 token 以及形如 {"name":"agent-a","scopes":["read"],"sha256":"…"} 的配置条目
export MCP_TOKENS='[{"name":"agent-a","scopes":["read"],"sha256":"…"}, {"name":"ops","scopes":["refresh","backtest"],"sha256":"…"}]'
```

| scope | 允许 |
| ----- | ---- |
| `read` | 行情读取，`refresh=missing / never` |
| `refresh` | 在 `read` 之外，允许 `refresh=always` 强制回源和 `refresh_fund_nav_cache`（按 token 限流，防止刷爆上游） |
| `backtest` | 回测工具（独立 scope，不隐含 `read`；通常和 `read` 一起发） |

缺少或错误的 token → HTTP 401；权限不足 → 403；超过限流 → 429。比较使用常量时间，日志不会出现 token 或 DB 密码。

### 启动

```bash
uv run tt-serve                      # 本机
# 容器（同一个镜像；请显式用非 root 用户）
docker run -d --name trading-market --user 10001:10001 -p 8765:8765 \
  -e DB_HOST=… -e DB_USER=… -e DB_PASSWORD=… -e DB_NAME=trading \
  -e MCP_TOKENS="$MCP_TOKENS" \
  ghcr.io/lc4t/trading-2.0:latest /app/.venv/bin/tt-serve
```

- 健康检查：`GET /healthz`，**不要求 token**，只返回 `{"status":"ok"}` / `{"status":"unavailable"}`（503），不带任何内部信息。镜像里的 `HEALTHCHECK` 只对 PID 1 是 `tt-serve` 的容器生效，每日流水线等一次性容器不受影响。
- 镜像默认仍以 root 运行（每日流水线要往挂载的 `data/` 写文件）；服务请加 `--user 10001:10001`（镜像里已建好该用户）。
- 服务本身不做 TLS，需要对外暴露时放在反向代理后面。

### 接入 MCP 客户端

端点：`http(s)://<host>:8765/mcp`，请求头 `Authorization: Bearer <token>`。

```bash
# Claude Code
claude mcp add --transport http trading-market http://localhost:8765/mcp \
  --header "Authorization: Bearer $TT_MCP_TOKEN"
```

```json
{
  "mcpServers": {
    "trading-market": {
      "type": "http",
      "url": "http://localhost:8765/mcp",
      "headers": { "Authorization": "Bearer ${TT_MCP_TOKEN}" }
    }
  }
}
```

### 工具

行情工具（`read`）：

| 工具 | 说明 |
| ---- | ---- |
| `get_price_history(symbols, start_date, end_date, refresh, adjust)` | 日线 OHLCV，多标的 |
| `get_latest_price(symbol, refresh, adjust)` | 缓存里最新一根日线（不是实时报价） |
| `get_fund_nav_history(fund_code, start_date, end_date, refresh)` | 场外基金官方单位净值（六位代码） |
| `get_latest_fund_nav(fund_code, refresh)` | 最新单位净值 |
| `refresh_fund_nav_cache(fund_code, start_date, end_date)` | 强制回源（`refresh` scope） |

`refresh`：`missing`（默认，缺什么补什么）/ `always`（强制回源，需 `refresh` scope）/ `never`（只读缓存）。
返回沿用既有行情 MCP 契约的结构（`cache`、`data_fingerprint`、`count`、`data`），只做加法：每行多了
`as_of`（数据所属时点）、`fetched_at`、`source`、`is_final`、`price_basis`；`cache[symbol]` 里多了 `coverage`、`price_basis`、`nonfinal_count` 等。

回测工具（`backtest`，只读缓存里的日线、不回源、不画图；先用行情工具把数据备好）：
`run_dual_ma_backtest`、`run_momentum_topn_backtest`、`run_momentum_basket_backtest`，入参对应 CLI / 前端表单，返回
`metrics`、`trades`（最近 `trade_limit` 条）、`next_signal` 和每个标的的 `data_quality`（价格口径、末行是否已定稿）。

### 缓存语义

- **已定稿的历史日线只读，不再回源**。缓存没覆盖的区间不会静默补抓，而是在 `coverage`（`gaps` 列表）里暴露；要补就显式 `refresh=always`。
- **当日数据允许缓存，但必须带时点**：只有抓取时刻晚于该交易日收盘 + 30 分钟才标 `is_final=true`，`as_of` = 当日收盘时刻；
  盘中抓到的行 `is_final=false`、`as_of` = 抓取时刻。`is_final=false` 的行每次读取都按 TTL（默认 5 分钟）判断是否重抓，
  收盘后必重抓——**即使请求的 `end` 恰好等于缓存里最新的一天**。时钟以 Asia/Shanghai 业务日为准，收盘时间按标的所在交易所（A 股 15:00、港股 16:10、美股 16:00 ET）。
- **价格口径 `price_basis`**：新标的写入不复权价（A 股场内 → 东财 `fqt=0`，`raw`；港股美股指数 → Yahoo `auto_adjust=False`，`split_adjusted`，仅拆分调整；场外基金 → 官方单位净值 `raw`）。
  分红/拆分事件另存 `market_corporate_actions`：检测到新的拆分时，受影响的历史行自动失效并重抓；`adjust="qfq"` 用已记录的分红即时计算前复权视图（不落库，返回里 `price_view=adjusted_qfq`）。
  **不再用分红额扣减历史价**。
- **遗留数据**：2.0 之前 `trading_data` 里的行没有口径记录，返回里标 `price_basis=legacy_unknown`（其中 A 股 ETF 实际来自同花顺前复权全量、
  Yahoo 标的是 `auto_adjust` 价）。为避免同一序列混入两种口径，**已有历史行的标的继续沿用历史抓取方式**（`ths_qfq` / `yahoo_adjusted`），
  只有全新的标的才用不复权口径；一条序列里出现多种口径时 `price_basis="mixed"`，`price_bases` 列出全部。
- 上游失败时返回缓存并在 `tail_refresh_error` 里说明；请求路径上只做最多 2 次、间隔 ≤ 0.5s 的短重试，失败后 60 秒静默期内不再打上游。
  （`tt-fetcher` 批量同步保留历史行为：Yahoo 限流最多 10 次、30+60n 秒退避。）

## Docker 镜像

后端镜像由 [.github/workflows/docker-build.yml](.github/workflows/docker-build.yml) 自动构建并推送到 GHCR：

```
ghcr.io/lc4t/trading-<branch>:<short-sha>
ghcr.io/lc4t/trading-<branch>:latest
```

- `main` / `1.0` / `2.0`  三个分支都会自动构建。
- `2.0` 是当前默认/稳定分支；`1.0` 是旧版，只做归档。

### 镜像内布局

| 路径 | 内容 |
| ---- | ---- |
| `/app/trading/` | 全部 Python 源码（包） |
| `/app/.venv/` | 构建时安装好的虚拟环境 |
| `/app/{backtest,fetcher}.py` | 向后兼容的 CLI shim |
| `/app/.venv/bin/tt-serve` | 行情缓存 + MCP 服务入口（见上一节），与回测/同步共用同一镜像 |
| ~~`/app/frontend/`~~ | **不打入镜像**——前端独立部署 |

## 测试

```bash
uv run pytest
uv run pytest --cov=trading --cov-report=term-missing
```

## 路线图

- [x] 1.0：双均线策略 + 参数寻优
- [x] 2.0：动量轮动（MomentumBasket / MomentumTopN）+ 用户态 API + Jupyter，已成为默认分支
- [ ] 后续：更多策略（趋势 / 反转 / 配对交易），更丰富的资金管理

## License

MIT

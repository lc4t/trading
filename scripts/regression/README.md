# 回归对比（基线 vs 当前分支）

在同一份**虚构**夹具上，用基线 git ref 和当前工作区分别运行每日流水线里的同款命令，逐项比较输出：

| 检查 | 内容 |
| ---- | ---- |
| 单标的双均线 | `trading.cli.backtest 159915.SZ …` 的 JSON 输出 |
| 港股单标的 | `trading.cli.backtest 0700.HK …` 的 JSON 输出 |
| 动量轮动 Top-1 | `trading.cli.rotation_backtest …` 的 JSON 输出 |
| 同步写库 | `trading.data.fetcher` 用 mock 的同花顺 / Yahoo 数据写库后，`trading_data` 全表 md5 |

```bash
uv run python scripts/regression/run.py                    # 基线默认 2.0
uv run python scripts/regression/run.py --baseline-ref v2.0.0 --keep   # 指定 ref，保留临时目录排查
```

结尾输出 `结果：PASS` 或列出差异（退出码 0 / 1）。

- 需要 docker、git、uv。基线用 `git archive` 导出到临时目录并 `uv sync`（需要能装依赖）；
  MySQL 是本地临时容器（`mysql:8.4`，随机临时凭据，结束即删）。
- 不访问行情网络、不连任何真实库；上游数据全部是固定种子的虚构数据（`seed.py`、`sync_mock.py`）。
- 不属于默认 `pytest`，也不会进入镜像（`.dockerignore` 已排除）。
- 回测 JSON 里带「报告日期」，所以别跨午夜运行。

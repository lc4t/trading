# 港股 / 美股日线上游探测

VPS 的出口 IP 被 Yahoo 限流时，评估能不能换别的上游。`upstream_probe.py` 对每个候选源取**同一只港股（0700.HK）和同一只美股（AAPL）近 30 天日线**，
输出能不能用、口径是什么、多快。只用标准库（`yahoo_yfinance` / `yahoo_chart_curl` 两项在镜像里有 yfinance / curl_cffi 时才会跑），不需要数据库和任何密钥。

```bash
# 在 VPS 的容器里运行（没有挂载代码时可以把脚本 cat 进去；镜像里已有 python 与 yfinance/curl_cffi）
docker exec -i <容器> /app/.venv/bin/python - < scripts/probe/upstream_probe.py
# 或在能访问仓库的环境里
python scripts/probe/upstream_probe.py [--only eastmoney_fqt0,tencent_qfq] [--symbols 0700.HK,AAPL] [--json out.json]
```

约束：每个源对每只标的**最多 2 次请求**（第 2 次只在网络异常 / 5xx 时重试，**遇到 429 绝不重试**），请求之间间隔 ≥ 1 秒，不并发、不压测；完整一次大约 15 次请求。

## 输出怎么看

| 列 | 含义 |
| --- | --- |
| `source` | 候选源（见下表） |
| `http` | HTTP 状态；`429` 表示被限流/反爬拦住；`-` 表示该源没有这个市场的端点（没发请求）；`ERR` 是网络/解析异常 |
| `rows` / `last_date` | 近 30 天返回的日线行数 / 最近一个交易日（美股比港股晚一天是正常的：北京时间还没到美股收盘） |
| `ms` / `req` | 耗时（毫秒）/ 实际发出的请求数 |
| 复权口径 / 备注 | 该源给的价格口径，以及非官方接口的使用风险 |

末尾的「同一日期的收盘价对照」把所有成功的源放在一起：**最近日期各源应当几乎相等**；**窗口最早日期上出现固定比例差异 = 该源做了复权**（窗口内发生过分红）。
这决定了一个源的行能不能和库里已有的行拼在同一个序列里——**不同口径绝不能静默拼接**。

## 候选源

| 源 | 港股 | 美股 | 复权口径 |
| --- | --- | --- | --- |
| `yahoo_yfinance` | ✓ | ✓ | `auto_adjust`（= 库里的 `yahoo_adjusted`）；当前实现，cookie/crumb 阶段会被 IP 限流 |
| `yahoo_chart_q1` / `_q2` | ✓ | ✓ | 标准库直连 chart 接口，**不带浏览器 TLS 指纹**，通常会被 429 |
| `yahoo_chart_curl` | ✓ | ✓ | curl_cffi 带浏览器 TLS 指纹直连 chart，**跳过 cookie/crumb**（yfinance 失败的那一步）；响应里同时有不复权收盘和 `adjclose` |
| `eastmoney_fqt0` / `eastmoney_fqt1` | ✓（`116.00700`） | ✓（`105.AAPL`，前缀随交易所变） | 不复权 / 前复权 |
| `tencent_qfq` | ✓（`hk00700`） | ✓（`usAAPL.OQ`，后缀随交易所变） | 前复权，行里带分红说明 |
| `sina_us` | ✗ | ✓ | 疑似不复权；一次返回全部历史 |
| `stooq` | ✓ | ✓ | 近期要 JS 工作量证明，脚本基本不可用 |

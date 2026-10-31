"""行情缓存层：统一的抓取 / 缓存 / 元数据逻辑，供 tt-fetcher、回测引擎与 MCP 服务共用.

本包 ``__init__`` 刻意不导入子模块（repository 会反向依赖 ``market.rows``）。
"""

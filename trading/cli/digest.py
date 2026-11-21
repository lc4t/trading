"""digest CLI：汇总当日交易信号（单标的 + 动量轮动）并推送到 PushGo / Resend 邮件 / iGotify。

示例:

    uv run python -m trading.cli.digest --data-dir frontend/data
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime
from pathlib import Path

from dotenv import load_dotenv

from ..io.channels import Message, send_all
from ..io.channels.markup import markdown_to_html
from ..io.digest import (
    build_digest,
    build_email_subject,
    load_reports,
    load_rotation_reports,
    today_in_timezone,
)
from ..io.digest_markdown import build_full_markdown
from ..io.digest_push import build_push_parts


def parse_date(value: str) -> date:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"无效日期: {value}") from exc


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="汇总当日交易信号并推送到 PushGo / Resend 邮件 / iGotify（按环境变量自动启用）"
    )
    parser.add_argument(
        "--data-dir",
        default="frontend/data",
        help="JSON 数据目录，默认 frontend/data",
    )
    parser.add_argument(
        "--date",
        dest="target_date",
        type=parse_date,
        help="目标日期，格式 YYYY-MM-DD，默认 Asia/Shanghai 当天",
    )
    parser.add_argument(
        "--pushgo-url",
        default=None,
        help="PushGo 推送地址，默认读取 PUSHGO_URL 或使用官方网关",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=3,
        help="PushGo 最大重试次数，默认 3",
    )
    parser.add_argument(
        "--retry-delay",
        type=int,
        default=5,
        help="PushGo 重试间隔秒数，默认 5",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只输出标题和正文，不执行推送",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    load_dotenv()
    target_date = args.target_date or today_in_timezone()
    data_dir = Path(args.data_dir)
    reports, warnings, reason = load_reports(data_dir, target_date)
    rotation_reports, rotation_warnings = load_rotation_reports(data_dir, target_date)
    warnings.extend(rotation_warnings)

    for warning in warnings:
        print(f"[WARN] {warning}", file=sys.stderr)

    title, body = build_digest(reports, target_date, reason, rotation_reports)

    if args.dry_run:
        print(title)
        print(body)
        return 0

    subject = build_email_subject(title, reports, target_date, rotation_reports)
    # PushGo 沿用精简正文；邮件用完整版 Markdown（另带 HTML）；iGotify 用手机精简版（结论/总览/需关注分条发送）
    full_markdown = build_full_markdown(reports, target_date, rotation_reports, warnings)
    parts = build_push_parts(reports, target_date, rotation_reports, warnings)
    message = Message(
        title=title,
        body=body,
        subject=subject,
        markdown=full_markdown,
        html=markdown_to_html(full_markdown, subject),
        parts=parts,
    )
    result = send_all(
        message,
        os.environ,
        pushgo_url=args.pushgo_url,
        max_retries=args.max_retries,
        retry_delay=args.retry_delay,
    )

    if not result.configured:
        print(
            "[ERROR] 没有任何已配置的通知渠道（PushGo / Resend / iGotify 的环境变量均不完整）",
            file=sys.stderr,
        )
        return 1
    if not result.ok:
        print("[ERROR] 所有已配置的通知渠道都发送失败", file=sys.stderr)
        return 1

    print(f"[INFO] 推送成功渠道: {', '.join(result.sent)}")
    if result.failed:
        print(f"[WARN] 失败渠道: {', '.join(name for name, _ in result.failed)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

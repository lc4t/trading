#!/usr/bin/env python3
"""回归对比：基线 git ref vs 当前工作区，在同一份虚构夹具上跑同样的命令并逐项比较.

    uv run python scripts/regression/run.py [--baseline-ref 2.0] [--keep]

需要 docker（起一个临时 MySQL 容器）、git、uv。不访问行情网络、不连任何真实库；
容器凭据是每次随机生成的临时值，结束后容器和临时目录都会清理。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = Path(subprocess.check_output(["git", "rev-parse", "--show-toplevel"], cwd=HERE, text=True).strip())

# 与 deploy/github-action.trading-v2.yml 里同款的命令（基准统一用夹具里有的 000300.SS）
COMMANDS = {
    "单标的双均线 159915.SZ": (
        "159915.SZ.json",
        ["-m", "trading.cli.backtest", "159915.SZ", "--benchmark", "000300.SS", "--initial-capital", "50000",
         "--start-date", "2022-01-01", "--use-ma", "--ma-short", "19", "--ma-long", "26", "--use-chandelier",
         "--chandelier-multiplier", "3.6", "--chandelier-period", "20", "--output-json", "{out}/159915.SZ.json"],
    ),
    "港股单标的 0700.HK": (
        "0700.HK.json",
        ["-m", "trading.cli.backtest", "0700.HK", "--benchmark", "000300.SS", "--initial-capital", "50000",
         "--start-date", "2022-01-01", "--use-ma", "--ma-short", "6", "--ma-long", "30", "--use-chandelier",
         "--chandelier-multiplier", "2.5", "--chandelier-period", "38", "--output-json", "{out}/0700.HK.json"],
    ),
    "动量轮动 Top-1": (
        "rotation/momentum-top1.json",
        ["-m", "trading.cli.rotation_backtest", "--strategy-id", "momentum-top1", "--strategy-name", "Top-1",
         "--universe", "159915.SZ,513100.SS,518880.SS", "--top-n", "1", "--cash-buffer", "0.05",
         "--momentum-fn", "simple_return", "--lookback", "23", "--schedule", "daily",
         "--start-date", "2022-01-01", "--initial-capital", "50000",
         "--output-json", "{out}/rotation/momentum-top1.json"],
    ),
}
SYNC_SYMBOLS = {"同步写库 159915.SZ（同花顺格式）": "159915.SZ", "同步写库 000300.SS（Yahoo 格式）": "000300.SS"}
ROWS_SQL = (
    "select symbol,date,open_price,close_price,high,low,volume,amount,`change`,change_pct "
    "from {db}.trading_data order by symbol,date"
)


def sh(cmd, **kw):
    return subprocess.run(cmd, check=True, text=True, capture_output=True, **kw)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Mysql:
    def __init__(self):
        self.name = f"trading-regression-{secrets.token_hex(4)}"
        self.root_pw = secrets.token_urlsafe(12)
        self.user_pw = secrets.token_urlsafe(12)
        self.port = free_port()

    def start(self):
        sh(["docker", "run", "-d", "--rm", "--name", self.name, "-p", f"127.0.0.1:{self.port}:3306",
            "-e", f"MYSQL_ROOT_PASSWORD={self.root_pw}", "-e", "MYSQL_USER=ttuser",
            "-e", f"MYSQL_PASSWORD={self.user_pw}", "mysql:8.4"])
        for _ in range(60):
            if subprocess.run(self._cli("select 1", root=True), capture_output=True).returncode == 0:
                return
            time.sleep(2)
        raise SystemExit("临时 MySQL 启动超时")

    def _cli(self, sql: str, root: bool = False):
        user, pw = ("root", self.root_pw) if root else ("ttuser", self.user_pw)
        return ["docker", "exec", self.name, "mysql", f"-u{user}", f"-p{pw}", "-N", "-e", sql]

    def query(self, sql: str) -> str:
        return subprocess.run(self._cli(sql, root=True), check=True, capture_output=True, text=True).stdout

    def create_db(self, name: str):
        self.query(f"create database {name}; grant all on {name}.* to 'ttuser'@'%'")

    def env(self, db: str) -> dict:
        return {**os.environ, "DB_HOST": "127.0.0.1", "DB_PORT": str(self.port), "DB_USER": "ttuser",
                "DB_PASSWORD": self.user_pw, "DB_NAME": db, "TZ": "Asia/Shanghai"}

    def stop(self):
        subprocess.run(["docker", "rm", "-f", self.name], capture_output=True)


def run_py(cwd: Path, env: dict, args: list[str], log: Path) -> int:
    with log.open("w") as fh:
        return subprocess.run(["uv", "run", "python", "-W", "ignore", *args], cwd=cwd, env=env,
                              stdout=fh, stderr=subprocess.STDOUT).returncode


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--baseline-ref", default="2.0", help="作为基线的 git ref，默认 2.0")
    ap.add_argument("--keep", action="store_true", help="结束后保留临时目录（排查差异用）")
    args = ap.parse_args()

    work = Path(tempfile.mkdtemp(prefix="trading-regression-"))
    base_dir = work / "baseline"
    base_dir.mkdir()
    mysql = Mysql()
    failures: list[str] = []
    try:
        print(f"[1/5] 导出基线 {args.baseline_ref} 并安装依赖 …")
        archive = subprocess.Popen(["git", "archive", args.baseline_ref], cwd=REPO, stdout=subprocess.PIPE)
        subprocess.run(["tar", "-x", "-C", str(base_dir)], stdin=archive.stdout, check=True)
        if archive.wait() != 0:
            raise SystemExit(f"git archive {args.baseline_ref} 失败")
        shutil.rmtree(base_dir / "frontend", ignore_errors=True)
        sh(["uv", "sync", "--no-dev", "--frozen"], cwd=base_dir)

        print("[2/5] 启动临时 MySQL（随机临时凭据）…")
        mysql.start()
        sides = {"baseline": (base_dir, "trading_base"), "current": (REPO, "trading_head")}
        for _, db in sides.values():
            mysql.create_db(db)

        print("[3/5] 用基线代码灌同一份虚构夹具 …")
        for _, db in sides.values():
            r = run_py(base_dir, mysql.env(db), [str(HERE / "seed.py")], work / f"seed_{db}.log")
            if r:
                raise SystemExit(f"灌夹具失败，见 {work}/seed_{db}.log")

        print("[4/5] 回测命令 …")
        outputs: dict[str, Path] = {}
        for side, (code_dir, db) in sides.items():
            out = work / f"out_{side}"
            (out / "rotation").mkdir(parents=True)
            outputs[side] = out
            for label, (_, cmd) in COMMANDS.items():
                cmd = [a.replace("{out}", str(out)) for a in cmd]
                rc = run_py(code_dir, mysql.env(db), cmd, work / f"{side}_{label[:4]}.log")
                if rc:
                    failures.append(f"{label}: {side} 运行失败（exit {rc}）")
        for label, (rel, _) in COMMANDS.items():
            try:
                a = json.loads((outputs["baseline"] / rel).read_text())
                b = json.loads((outputs["current"] / rel).read_text())
            except (OSError, ValueError):
                failures.append(f"{label}: 缺少输出文件")
                continue
            ok = a == b
            print(f"  {'PASS' if ok else 'FAIL'}  {label}")
            if not ok:
                diff = sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))
                failures.append(f"{label}: 顶层字段有差异 {diff}")

        print("[5/5] 同步写库（mock 上游）全表 md5 …")
        for label, symbol in SYNC_SYMBOLS.items():
            for side, (code_dir, db) in sides.items():
                rc = run_py(code_dir, mysql.env(db), [str(HERE / "sync_mock.py"), symbol], work / f"sync_{side}_{symbol}.log")
                if rc:
                    failures.append(f"{label}: {side} 运行失败（exit {rc}）")
            digests = {
                side: hashlib.md5(mysql.query(ROWS_SQL.format(db=db)).encode()).hexdigest()
                for side, (_, db) in sides.items()
            }
            ok = digests["baseline"] == digests["current"]
            print(f"  {'PASS' if ok else 'FAIL'}  {label}  md5={digests['current'][:12]}")
            if not ok:
                failures.append(f"{label}: trading_data 全表 md5 不一致 {digests}")
    finally:
        mysql.stop()
        if args.keep:
            print(f"临时目录保留在 {work}")
        else:
            shutil.rmtree(work, ignore_errors=True)

    if failures:
        print("\n结果：FAIL")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\n结果：PASS（回测输出与同步写库均与基线逐项一致）")
    return 0


if __name__ == "__main__":
    sys.exit(main())

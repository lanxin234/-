# -*- coding: utf-8 -*-
"""
运行时数据备份

备份什么：
  data/chain.sqlite3    链本体（区块、交易、事件日志、合约存储）
  data/app_state.json   账号哈希、昵称、用户↔地址映射、合约地址、令牌黑名单
  uploads/              商品图片（可选，内容寻址，文件多时才需要）

为什么不能直接 cp：
  链库是 WAL 模式，运行中直接复制文件有可能拷到"写了一半"的状态。
  这里用 sqlite3 官方的 backup API，会在数据库内部取一致性快照 ——
  服务不用停，拷出来的库可以直接打开。

用法：
  python scripts/backup_data.py                 # 备份到 backups/<时间戳>/
  python scripts/backup_data.py --keep 5        # 只保留最近 5 份
  python scripts/backup_data.py --with-uploads  # 连图片一起备份
  python scripts/backup_data.py --dest D:/bk    # 备份到别的盘（推荐！）
"""

import argparse
import json
import os
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("MARKET_DATA_DIR") or (ROOT / "data"))
UPLOAD_DIR = ROOT / "uploads"
DEFAULT_DEST = ROOT / "backups"


def human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024
    return f"{n:.1f} TB"


def backup_sqlite(src: Path, dst: Path) -> None:
    """用 backup API 取一致性快照（服务运行中也能用）"""
    src_conn = sqlite3.connect(str(src), timeout=30)
    try:
        dst_conn = sqlite3.connect(str(dst))
        try:
            with dst_conn:
                src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
    finally:
        src_conn.close()


def prune(dest_root: Path, keep: int) -> list:
    """只清理本脚本产生的备份目录，不动别的文件"""
    if keep <= 0:
        return []
    runs = sorted(
        [p for p in dest_root.iterdir()
         if p.is_dir() and p.name[:8].isdigit() and "_" in p.name],
        key=lambda p: p.name,
    )
    stale = runs[:-keep] if len(runs) > keep else []
    for p in stale:
        shutil.rmtree(p, ignore_errors=True)
    return [p.name for p in stale]


def main() -> int:
    ap = argparse.ArgumentParser(description="备份链数据与账号快照")
    ap.add_argument("--dest", default=str(DEFAULT_DEST),
                    help=f"备份根目录（默认 {DEFAULT_DEST}）")
    ap.add_argument("--keep", type=int, default=10,
                    help="保留最近 N 份（默认 10，0 表示不清理）")
    ap.add_argument("--with-uploads", action="store_true",
                    help="连 uploads/ 图片一起备份")
    args = ap.parse_args()

    if not DATA_DIR.exists():
        print(f"[X] 数据目录不存在：{DATA_DIR}")
        return 1

    chain_db = DATA_DIR / "chain.sqlite3"
    state_file = DATA_DIR / "app_state.json"
    if not chain_db.exists() and not state_file.exists():
        print(f"[X] {DATA_DIR} 里没有可备份的数据（chain.sqlite3 / app_state.json 都不在）")
        return 1

    dest_root = Path(args.dest)
    dest_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    target = dest_root / stamp
    target.mkdir(parents=True, exist_ok=True)

    total = 0

    # ---- 1. 链数据库（一致性快照） ----
    if chain_db.exists():
        out = target / "chain.sqlite3"
        try:
            backup_sqlite(chain_db, out)
            size = out.stat().st_size
            total += size
            print(f"  [OK] chain.sqlite3        {human(size)}")
        except Exception as e:
            print(f"  [X]  chain.sqlite3 备份失败：{e}")
            return 2

    # ---- 2. 业务快照 ----
    if state_file.exists():
        out = target / "app_state.json"
        shutil.copy2(state_file, out)
        total += out.stat().st_size
        print(f"  [OK] app_state.json       {human(out.stat().st_size)}")

        # 顺手报告一下备份里有多少账号，便于确认没拷到空库
        try:
            meta = json.loads(state_file.read_text(encoding="utf-8"))
            print(f"       账号 {len(meta.get('credentials') or {})} 位 / "
                  f"会话 {len(meta.get('user_sessions') or {})} 条")
        except Exception:
            pass

    # ---- 3. 图片（可选） ----
    if args.with_uploads and UPLOAD_DIR.exists():
        files = [p for p in UPLOAD_DIR.iterdir() if p.is_file()]
        if files:
            shutil.copytree(UPLOAD_DIR, target / "uploads", dirs_exist_ok=True)
            size = sum(p.stat().st_size for p in files)
            total += size
            print(f"  [OK] uploads/             {len(files)} 个文件，{human(size)}")

    # ---- 4. 写一个说明文件，避免以后看不懂这是什么 ----
    (target / "README.txt").write_text(
        "区块链二手交易平台 数据备份\n"
        f"时间：{stamp}\n"
        f"来源：{DATA_DIR}\n\n"
        "恢复方法：停止服务，把 chain.sqlite3 与 app_state.json\n"
        "覆盖回 data/ 目录，再启动即可。两者必须来自同一份备份，\n"
        "否则会出现'链上有商品但账号映射对不上'的情况。\n",
        encoding="utf-8",
    )

    print(f"\n  备份完成：{target}   合计 {human(total)}")

    removed = prune(dest_root, args.keep)
    if removed:
        print(f"  已清理旧备份 {len(removed)} 份：{', '.join(removed)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

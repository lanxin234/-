# -*- coding: utf-8 -*-
"""
把 xiangmu 项目的源代码同步到归档目录（默认 C:/Users/25191/Desktop/项目源代码）。

用途：以后每次修改完项目，跑一次本脚本，归档就会与源项目保持一致。

用法：
    # 用默认归档路径
    python scripts/sync_source.py

    # 指定归档路径
    python scripts/sync_source.py --dest "D:/某个目录"

    # 只看差异、不实际复制
    python scripts/sync_source.py --dry-run

    # 同步后删掉归档里多余的旧文件（默认只提示不删除）
    python scripts/sync_source.py --prune

设计要点：
  - 排除项与 .gitignore 保持一致：.git / __pycache__ / build / uploads /
    日志 / 密钥 / 本地临时文件 —— 归档里只放"源代码"，不放运行产物和敏感文件
  - 复制后**逐个校验 md5**；有任何不一致就非零退出，绝不"假装成功"
  - 不删除任何文件（除非显式 --prune），避免误伤用户数据
"""
import argparse
import hashlib
import os
import shutil
import sys

# 默认归档目录。若换机器或不想用这个位置，运行时加 --dest 覆盖即可。
DEFAULT_DEST = r"C:/Users/25191/Desktop/项目源代码"

# 与 .gitignore 对应：这些不进归档
# data/ 和 backups/ 一定要排除 —— 里面是链数据库与账号哈希，
# 归档目录是"纯源代码副本"，不该带任何运行时数据。
SKIP_DIRS = {".git", "__pycache__", "build", "uploads", "data", "backups",
             ".idea", ".vscode", "venv", ".venv", "node_modules"}
SKIP_FILES = {".secret_key", "backend.log", ".server.log",
              "config.env", ".env",
              ".pscheck.txt", ".run_server.bat", ".launch.vbs"}
SKIP_SUFFIX = (".pyc", ".log", ".map")


def should_skip_file(name: str) -> bool:
    return name in SKIP_FILES or name.endswith(SKIP_SUFFIX)


def md5(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def collect(root: str) -> dict:
    """返回 {相对路径: md5}，路径统一用 / 分隔。"""
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            if should_skip_file(fn):
                continue
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            out[rel] = md5(full)
    return out


def main():
    ap = argparse.ArgumentParser(description="同步项目源代码到归档目录")
    ap.add_argument("--dest", default=DEFAULT_DEST, help="归档目录")
    ap.add_argument("--src", default=None, help="源项目目录（默认取脚本所在项目的根）")
    ap.add_argument("--dry-run", action="store_true", help="只显示差异，不复制")
    ap.add_argument("--prune", action="store_true", help="删除归档中多余的旧文件")
    args = ap.parse_args()

    # 脚本在 <项目>/scripts/ 下，项目根 = 上一级
    src = args.src or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    dest = os.path.abspath(args.dest)

    if not os.path.isdir(src):
        print(f"✗ 源目录不存在：{src}")
        return 1
    if not os.path.isdir(dest):
        print(f"✗ 归档目录不存在：{dest}")
        print("  请先创建该目录，或改用 --dest 指定其他路径。")
        return 1

    print("=" * 64)
    print("  源代码同步")
    print("=" * 64)
    print(f"  源   ：{src}")
    print(f"  归档 ：{dest}")
    print()

    s = collect(src)
    d = collect(dest)

    to_copy = sorted(k for k in s if k not in d or s[k] != d[k])
    only_dest = sorted(set(d) - set(s))

    print(f"源 {len(s)} 个文件 / 归档 {len(d)} 个文件")
    print(f"需要复制/更新：{len(to_copy)} 个")
    for k in to_copy:
        tag = "新增" if k not in d else "更新"
        print(f"    [{tag}] {k}")
    if only_dest:
        print(f"归档中多余（源里已不存在）：{len(only_dest)} 个")
        for k in only_dest:
            print(f"    [多余] {k}")
    print()

    if args.dry_run:
        print("（--dry-run：未做任何修改）")
        return 0

    if not to_copy and not (only_dest and args.prune):
        print("✓ 归档已是最新，无需操作")
        return 0

    # ---- 复制 ----
    for rel in to_copy:
        s_full = os.path.join(src, rel.replace("/", os.sep))
        t_full = os.path.join(dest, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(t_full), exist_ok=True)
        shutil.copy2(s_full, t_full)
    print(f"✓ 已复制 {len(to_copy)} 个文件")

    # ---- 清理多余文件（仅在显式 --prune 时）----
    if only_dest and args.prune:
        for rel in only_dest:
            t_full = os.path.join(dest, rel.replace("/", os.sep))
            os.remove(t_full)
            print(f"  已删除多余文件：{rel}")
        # 顺手清掉空的目录
        for dirpath, dirnames, filenames in os.walk(dest, topdown=False):
            if dirpath == dest:
                continue
            if not os.listdir(dirpath):
                os.rmdir(dirpath)
    elif only_dest:
        print(f"  注意：归档里有 {len(only_dest)} 个多余文件未删除（如需清理加 --prune）")

    # ---- 校验 ----
    s2, d2 = collect(src), collect(dest)
    bad = []
    for k in s2:
        if k not in d2:
            bad.append(f"缺失 {k}")
        elif s2[k] != d2[k]:
            bad.append(f"不一致 {k}")
    leftover = sorted(set(d2) - set(s2))

    print()
    print("-" * 64)
    if bad:
        print(f"✗ 校验失败：{len(bad)} 项")
        for b in bad:
            print(f"    {b}")
        return 1
    if leftover:
        print(f"⚠ 归档仍有 {len(leftover)} 个多余文件（内容本身已全部一致）")
        for k in leftover:
            print(f"    {k}")
        return 0
    print(f"✓ 归档 {len(d2)} 个文件，全部与源项目逐字节一致")
    print("=" * 64)
    return 0


if __name__ == "__main__":
    sys.exit(main())

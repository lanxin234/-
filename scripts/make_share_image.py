# -*- coding: utf-8 -*-
"""
把「当前公网地址」写进同学使用说明，并重新导出 PNG。

为什么需要它：
  免费隧道每次重启都会换域名，而说明图里印的是固定文字。
  如果图里留着旧地址，转发出去的人点开就是超时 —— 很难排查。

用法：
  python scripts/make_share_image.py https://xxxx.trycloudflare.com
  python scripts/make_share_image.py https://xxxx.trycloudflare.com --no-png

依赖：Pillow（已装）+ 本机 Edge（无头模式截图）。不需要装别的包。
"""

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
HTML = ROOT / "docs" / "同学使用说明.html"
PNG = ROOT / "docs" / "同学使用说明.png"

EDGE_CANDIDATES = [
    Path(r"C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"),
    Path(r"C:/Program Files/Microsoft/Edge/Application/msedge.exe"),
    Path(r"C:/Program Files/Google/Chrome/Application/chrome.exe"),
]

# 说明卡里的地址块：把域名拆成两行，避免长 URL 断在词中间
URL_BLOCK = re.compile(r'(<div class="url">)(.*?)(</div>)', re.S)


def split_host(host: str):
    """xxx.yyy.zzz -> (xxx-yyy, zzz) 这种两行显示"""
    parts = host.split(".")
    if len(parts) >= 3:
        return ".".join(parts[:-2]), ".".join(parts[-2:])
    return host, ""


def update_html(url: str) -> None:
    host = re.sub(r"^https?://", "", url.strip().rstrip("/"))
    first, rest = split_host(host)
    inner = "https://" + first + ("<br>." + rest if rest else "")

    text = HTML.read_text(encoding="utf-8")
    new_text, n = URL_BLOCK.subn(lambda m: m.group(1) + inner + m.group(3), text, count=1)
    if n == 0:
        raise SystemExit("没找到地址块（<div class=\"url\">），说明 HTML 结构变了")
    HTML.write_text(new_text, encoding="utf-8")
    print(f"  [OK] 已更新 HTML 地址 -> https://{host}")


def find_browser() -> Path:
    for p in EDGE_CANDIDATES:
        if p.exists():
            return p
    raise SystemExit("找不到 Edge/Chrome，无法导出图片")


def render_png() -> None:
    browser = find_browser()
    tmpdir = Path(tempfile.mkdtemp(prefix="share_"))
    raw = tmpdir / "raw.png"
    try:
        # 375 CSS 像素 × 2 倍 = 750 像素宽，适合直接发微信
        subprocess.run([
            str(browser), "--headless=new", "--disable-gpu", "--hide-scrollbars",
            "--force-device-scale-factor=2", "--window-size=375,2000",
            f"--screenshot={raw}", HTML.as_uri(),
        ], check=True, capture_output=True)
        if not raw.exists():
            raise SystemExit("浏览器没有生成截图")

        im = Image.open(raw).convert("RGB")
        w, h = im.size
        px = im.load()
        bg = (238, 242, 249)          # 页面背景色
        last = 0
        for y in range(h - 1, -1, -1):
            if any(px[x, y] != bg for x in range(0, w, 7)):
                last = y
                break
        im.crop((0, 0, w, min(h, last + 52))).save(PNG, optimize=True)
        size = Image.open(PNG).size
        print(f"  [OK] 已导出 {PNG.name}  {size[0]}×{size[1]}")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser(description="更新说明卡里的公网地址并重新出图")
    ap.add_argument("url", help="当前公网地址，如 https://xxxx.trycloudflare.com")
    ap.add_argument("--no-png", action="store_true", help="只改 HTML，不出图")
    args = ap.parse_args()

    if not re.match(r"^https?://[^\s/]+", args.url.strip()):
        print("地址格式不对，应该以 http(s):// 开头")
        return 1

    update_html(args.url)
    if not args.no_png:
        render_png()
    return 0


if __name__ == "__main__":
    sys.exit(main())

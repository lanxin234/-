"""
试运行：把真实业务流程从头到尾走一遍，并打印一份可读的结果。

跟自动化测试的区别：
  自动化测试关心"断言是否通过"，输出一堆 [OK]；
  这个脚本关心"实际用起来是什么样"，输出的是人能看懂的业务结果 ——
  谁上架了什么、谁买了、钱怎么流转、时间线长什么样。

跑法（需要后端在 5000 端口跑着）：

  C:/Users/25191/.venvs/base/Scripts/python.exe -X utf8 scripts/smoke_run.py

跑完会留下一批演示数据，方便直接打开页面看效果。
想清空：页面右上角「区块链浏览 → 重置整条链」，
或者 python -c "import requests; requests.post('http://127.0.0.1:5000/api/reset', json={'wipe_credentials': True})"
"""

import json
import os
import struct
import sys
import zlib

# 环境里的 http_proxy 会拦本机请求，先绕开
os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")
os.environ.setdefault("no_proxy", "127.0.0.1,localhost")

import requests  # noqa: E402

BASE = "http://127.0.0.1:5000"

PASS = 0
FAIL = 0


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"    ✓ {label}")
    else:
        FAIL += 1
        print(f"    ✗ {label}  {extra}")


def hdr(token):
    return {"Authorization": f"Bearer {token}"}


def make_png(size=16, rgb=(90, 140, 220)):
    """内存里生成一张最小的合法 PNG，用来测试图片上架"""
    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    raw = b""
    for _ in range(size):
        raw += b"\x00" + bytes(rgb) * size
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw))
            + chunk(b"IEND", b""))


def main():
    print("=" * 64)
    print("  试运行：端到端走一遍真实业务")
    print("=" * 64)

    # ---------- 0. 连通性 ----------
    print("\n【0】连通性")
    try:
        st = requests.get(f"{BASE}/api/status", timeout=10).json()
    except Exception as e:
        print(f"    ✗ 连不上后端：{type(e).__name__}: {e}")
        print("\n    请先启动服务：双击 start_server.bat")
        return 1
    check("后端可访问", st.get("success") or "contract_address" in st)
    print(f"      账户 {st.get('accounts_count')} 个 | "
          f"区块高度 {st.get('block_number')} | "
          f"合约 {st.get('contract_address')}")

    # ---------- 1. 重置（干净起点） ----------
    print("\n【1】重置链，从干净状态开始")
    r = requests.post(f"{BASE}/api/reset", json={"wipe_credentials": True}, timeout=60)
    check("重置成功", r.status_code == 200, f"HTTP {r.status_code}")

    # ---------- 2. 注册用户 ----------
    print("\n【2】注册 3 位用户（勾选「记住我」）")
    users = {}
    for name, nick, pwd in [("小明", "小明", "Xiaoming123"),
                            ("小红", "小红", "Xiaohong123"),
                            ("小刚", "小刚", "Xiaogang123")]:
        r = requests.post(f"{BASE}/api/register",
                          json={"username": name, "password": pwd,
                                "nickname": nick, "remember": True},
                          timeout=30).json()
        ok = r.get("success")
        users[name] = r.get("token")
        check(f"{name} 注册成功", ok, str(r)[:120])
        if ok:
            print(f"      链上地址 {r.get('address')} | "
                  f"令牌 {r.get('expires_in') // 86400} 天")

    # ---------- 3. 上架商品 ----------
    print("\n【3】上架商品（一件带图片）")
    png = make_png()

    # 3a. 带图片
    r = requests.post(
        f"{BASE}/api/items",
        headers=hdr(users["小明"]),
        data={"name": "九成新 iPad Air", "description": "2024 年购入，无磕碰，配件齐全",
              "category": "数码电子", "price_eth": "1.5"},
        files={"image": ("ipad.png", png, "image/png")},
        timeout=30,
    ).json()
    check("带图上架成功", r.get("success"), str(r)[:160])
    img_hash = r.get("image_hash", "")
    print(f"      图片内容哈希 {img_hash[:24]}…")

    # 3b. 不带图片
    for name, title, cat, price in [("小红", "高等数学 第七版", "图书教材", "0.08"),
                                    ("小刚", "山地自行车", "运动户外", "0.6")]:
        r = requests.post(f"{BASE}/api/items", headers=hdr(users[name]),
                          json={"name": title, "description": "自用闲置，诚心出",
                                "category": cat, "price_eth": price},
                          timeout=30).json()
        check(f"{name} 上架「{title}」", r.get("success"), str(r)[:120])

    # 找到 iPad 的 id
    items = requests.get(f"{BASE}/api/items", params={"q": "iPad"}, timeout=10).json()
    ipad = items["items"][0]
    ipad_id = ipad["id"]
    print(f"\n      商品列表共 {items.get('total')} 件，iPad 的 ID = {ipad_id}")

    # ---------- 4. 购买 ----------
    print("\n【4】小红购买 iPad（资金进入合约托管）")
    before = requests.get(f"{BASE}/api/me", headers=hdr(users["小红"]), timeout=10).json()
    bal_before = before.get("balance_eth")

    r = requests.post(f"{BASE}/api/buy", headers=hdr(users["小红"]),
                      json={"item_id": ipad_id}, timeout=30).json()
    check("下单成功", r.get("success"), str(r)[:160])
    if r.get("success"):
        print(f"      {r.get('message')}")

    escrow = requests.get(f"{BASE}/api/status", timeout=10).json().get("escrow_balance")
    check("资金确实锁在合约里", escrow and abs(escrow - 1.5) < 1e-6, f"托管 {escrow}")

    # ---------- 5. 确认收货 ----------
    print("\n【5】小红确认收货（货款放行给卖家，扣 1% 平台费）")
    r = requests.post(f"{BASE}/api/confirm", headers=hdr(users["小红"]),
                      json={"item_id": ipad_id}, timeout=30).json()
    check("确认收货成功", r.get("success"), str(r)[:160])
    if r.get("success"):
        print(f"      {r.get('message')}")

    after = requests.get(f"{BASE}/api/me", headers=hdr(users["小红"]), timeout=10).json()
    print(f"      小红余额 {bal_before} → {after.get('balance_eth')} ETH")

    # ---------- 6. 查看时间线 ----------
    print("\n【6】交易时间线（从链上事件还原）")
    tl = requests.get(f"{BASE}/api/timeline/{ipad_id}", timeout=10).json()
    steps = tl.get("steps", [])
    check("时间线有节点", len(steps) >= 3, f"只有 {len(steps)} 步")
    for s in steps:
        print(f"      · {s.get('title', '?')}"
              + (f"  ({s.get('time', '')})" if s.get("time") else ""))

    # ---------- 7. 互相评价 ----------
    print("\n【7】交易双方互评")
    r = requests.post(f"{BASE}/api/rate", headers=hdr(users["小红"]),
                      json={"item_id": ipad_id, "target": "小明", "score": 5},
                      timeout=30).json()
    check("小红给小明 5 星", r.get("success"), str(r)[:120])
    r = requests.post(f"{BASE}/api/rate", headers=hdr(users["小明"]),
                      json={"item_id": ipad_id, "target": "小红", "score": 4},
                      timeout=30).json()
    check("小明给小红 4 星", r.get("success"), str(r)[:120])

    # ---------- 8. 数据看板 ----------
    print("\n【8】数据看板")
    dash = requests.get(f"{BASE}/api/dashboard", timeout=15).json()
    # 注意：看板的统计字段叫 metrics，不是 stats
    stats = dash.get("metrics", {})
    for k, label in [("total_items", "商品总数"), ("onsale", "在售"),
                     ("sold", "已售出"), ("gmv_eth", "累计成交额(ETH)"),
                     ("platform_fee_eth", "平台手续费(ETH)")]:
        print(f"      {label}: {stats.get(k)}")
    check("看板有成交额", (stats.get("gmv_eth") or 0) > 0, str(stats)[:160])
    check("成交额 = 1.5 ETH", abs((stats.get("gmv_eth") or 0) - 1.5) < 1e-6,
          stats.get("gmv_eth"))
    check("平台费 = 成交额 1%",
          abs((stats.get("platform_fee_eth") or 0) - 0.015) < 1e-9,
          stats.get("platform_fee_eth"))

    # ---------- 9. 分类接口 ----------
    print("\n【9】分类筛选（这里曾一直 500）")
    cats = requests.get(f"{BASE}/api/categories", timeout=10)
    check("分类接口返回 200", cats.status_code == 200, f"HTTP {cats.status_code}")
    if cats.status_code == 200:
        counts = cats.json().get("counts", {})
        print("      " + " | ".join(f"{k} {v}" for k, v in counts.items() if v))

    # ---------- 10. 汇总 ----------
    print("\n" + "=" * 64)
    print(f"  试运行结果：{PASS} 通过 / {FAIL} 失败")
    print("=" * 64)
    print(f"\n  演示数据已留在链上，账号密码：")
    for n, p in [("小明", "Xiaoming123"), ("小红", "Xiaohong123"), ("小刚", "Xiaogang123")]:
        print(f"    {n} / {p}")
    print(f"\n  打开浏览器就能看到效果：http://127.0.0.1:5000")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())

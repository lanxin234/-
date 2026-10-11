# -*- coding: utf-8 -*-
"""
填充演示数据 —— 让数据看板有内容可看

生成多种分类、多种价格区间的商品，并完成若干笔交易。

注意：后端已启用登录鉴权（token），因此本脚本流程为
  注册（带密码）→ 拿到 token → 后续所有写操作都带 Bearer token。
身份完全由 token 决定，请求体里不再传 username。

运行前请确保后端已在 5000 端口运行，且链是干净的（或账户池还有余额）。
"""
import io
import struct
import sys
import zlib
import requests

BASE = "http://127.0.0.1:5000"

# 演示账号统一密码（仅用于本地演示数据，不用于任何真实场景）
DEMO_PASSWORD = "Demo1234"

# 本演示链共 10 个账户（1 个平台 + 9 个用户），所以最多注册 9 个用户
MAX_USERS = 9


def make_png(r, g, b, size=8):
    def chunk(tag, data):
        c = tag + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)
    raw = b""
    for _ in range(size):
        raw += b"\x00" + bytes([r, g, b]) * size
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw))
            + chunk(b"IEND", b""))


COLORS = {
    "数码电子": (29, 93, 184),
    "图书教材": (14, 124, 90),
    "生活用品": (183, 121, 31),
    "运动户外": (107, 78, 230),
    "服饰鞋包": (192, 57, 43),
    "其他":     (110, 116, 128),
}

SELLERS = ["张三", "李四", "王五"]
BUYERS  = ["赵六", "钱七"]

# 商品清单：(卖家, 名称, 分类, 价格, 是否成交)
LISTINGS = [
    ("张三", "索尼 WH-1000XM4 降噪耳机", "数码电子", 0.85, True),
    ("张三", "罗技 MX Master 3 鼠标",    "数码电子", 0.32, True),
    ("李四", "《数据结构与算法》教材",    "图书教材", 0.05, True),
    ("李四", "考研英语真题集（未拆封）",  "图书教材", 0.08, False),
    ("王五", "小米智能台灯",             "生活用品", 0.12, True),
    ("王五", "折叠收纳箱 3 个装",        "生活用品", 0.06, False),
    ("张三", "尤尼克斯羽毛球拍",         "运动户外", 0.65, True),
    ("李四", "亚瑟士跑鞋 42 码",         "运动户外", 1.20, False),
    ("王五", "优衣库羽绒服 L 码",        "服饰鞋包", 1.80, True),
    ("张三", "复古机械键盘 87 键",       "数码电子", 0.48, False),
    ("李四", "iPad Air 5 256G",          "数码电子", 3.20, False),
    ("王五", "宿舍小冰箱 45L",           "生活用品", 2.50, True),
]

TOKENS = {}      # username -> token
USERNAMES = {}   # 用户名 -> 链上地址（仅用于打印）


def auth_hdr(name):
    return {"Authorization": "Bearer " + TOKENS[name]}


def register(name, nickname):
    """注册（带密码）。已存在则改为登录，两条路都能拿到 token。"""
    r = requests.post(f"{BASE}/api/register", json={
        "username": name, "nickname": nickname, "password": DEMO_PASSWORD,
    })
    j = r.json()

    if j.get("success"):
        TOKENS[name] = j["token"]
        USERNAMES[name] = j.get("address", "")
        return "注册"

    err = j.get("error", "")
    code = j.get("code", "")

    # 用户已存在 → 直接登录
    if r.status_code == 409:
        r2 = requests.post(f"{BASE}/api/login", json={
            "username": name, "password": DEMO_PASSWORD,
        })
        j2 = r2.json()
        if j2.get("success"):
            TOKENS[name] = j2["token"]
            USERNAMES[name] = j2.get("address", "")
            return "登录"
        return f"登录失败: {j2.get('error')}"

    if code == "ACCOUNT_POOL_FULL":
        return f"账户池已满: {err}"

    return f"失败: {err}"


def main():
    print("=" * 56)
    print("  填充演示数据")
    print("=" * 56)

    # ---- 1. 注册/登录 ----
    print("\n[1/4] 注册演示账户（用户名 + 密码）...")
    if len(SELLERS + BUYERS) > MAX_USERS:
        print(f"  警告：本次需要 {len(SELLERS + BUYERS)} 个账户，"
              f"但演示链最多支持 {MAX_USERS} 个用户")

    ok_names = []
    for name in SELLERS + BUYERS:
        action = register(name, name)
        good = name in TOKENS
        print(f"  {'OK ' if good else 'ERR'} {name}  [{action}]"
              + (f"  {USERNAMES.get(name, '')[:12]}..." if good else ""))
        if good:
            ok_names.append(name)

    if not ok_names:
        print("\n  没有任何账号可用，无法继续。")
        print("  提示：请先重置整条链再重新运行本脚本。")
        sys.exit(1)

    live_sellers = [s for s in SELLERS if s in TOKENS]
    live_buyers = [b for b in BUYERS if b in TOKENS]
    if not live_sellers or not live_buyers:
        print(f"\n  卖家 {live_sellers} / 买家 {live_buyers} 不足，无法模拟成交。")
        sys.exit(1)

    # ---- 2. 上架商品 ----
    print("\n[2/4] 上架商品（身份取自 token）...")
    placed = []
    for i, (seller, name, cat, price, sold) in enumerate(LISTINGS):
        if seller not in TOKENS:
            print(f"  SKIP {name}  (卖家 {seller} 不可用)")
            continue
        png = make_png(*COLORS[cat], size=10 + (i * 3) % 14)
        r = requests.post(
            f"{BASE}/api/items",
            headers=auth_hdr(seller),
            data={
                # 注意：不再传 username，服务端从 token 取身份
                "name": name,
                "description": f"{cat}·自用闲置，成色良好，可当面交易。",
                "category": cat, "price_eth": str(price),
            },
            files={"image": (f"item{i}.png", png, "image/png")},
        )
        j = r.json()
        ok = j.get("success")
        print(f"  {'OK ' if ok else 'ERR'} {name}  ({price} ETH)  {'' if ok else j}")
        if ok:
            placed.append((name, price, sold))

    # ---- 3. 模拟成交 ----
    print("\n[3/4] 模拟成交（下单 + 确认收货 + 评价）...")
    all_items = requests.get(f"{BASE}/api/items", params={"page_size": 48}).json()["items"]
    by_name = {it["name"]: it for it in all_items}

    dealt = 0
    for name, price, should_sell in placed:
        if not should_sell:
            continue
        it = by_name.get(name)
        if not it or it["status"] != 0:
            continue
        buyer = live_buyers[dealt % len(live_buyers)]
        seller = it["seller_name"]

        r1 = requests.post(f"{BASE}/api/buy",
                           headers=auth_hdr(buyer), json={"item_id": it["id"]}).json()
        r2 = requests.post(f"{BASE}/api/confirm",
                           headers=auth_hdr(buyer), json={"item_id": it["id"]}).json()
        r3 = requests.post(f"{BASE}/api/rate",
                           headers=auth_hdr(buyer),
                           json={"item_id": it["id"], "score": 4 + (dealt % 2),
                                 "target": seller}).json()

        good = r1.get("success") and r2.get("success")
        print(f"  {'OK ' if good else 'ERR'} {name}: 买家 {buyer} 成交 {price} ETH"
              + ("" if good else f"  buy={r1.get('error')} confirm={r2.get('error')}"))
        if good:
            dealt += 1

    # ---- 4. 汇总 ----
    db = requests.get(f"{BASE}/api/dashboard").json()
    m = db["metrics"]
    print("\n" + "=" * 56)
    print("  演示数据就绪")
    print("=" * 56)
    print(f"  可用账号   {len(TOKENS)} 个  （演示密码：{DEMO_PASSWORD}）")
    print(f"  商品总数   {m['total_items']} 件")
    print(f"  在售 / 已售 {m['onsale']} / {m['sold']}")
    print(f"  累计成交额 {m['gmv_eth']} ETH")
    print(f"  平台手续费 {m['platform_fee_eth']} ETH")
    print(f"  分类占比   {db['donut']['labels']}")
    print(f"  价格分布   {db['price_buckets']['values']}")
    print("=" * 56)
    print(f"\n  登录任意演示账号即可查看：用户名 = 张三/李四/王五/赵六/钱七，密码 = {DEMO_PASSWORD}")


if __name__ == "__main__":
    main()

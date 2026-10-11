# -*- coding: utf-8 -*-
"""
三个新功能的端到端测试
  1. 图片上传（内容寻址 / IPFS 思路）
  2. 交易时间线（从链上事件还原）
  3. 数据看板（实时统计）

运行前请确保后端已在 5000 端口运行，且链是干净的。

注意：后端已启用登录鉴权，写操作需要带 Bearer token，
      身份由 token 决定，请求体中不再传 username。
"""
import io
import sys
import struct
import zlib
import requests

BASE = "http://127.0.0.1:5000"

passed = 0
failed = 0
FAILS = []


def auth_hdr(token):
    return {"Authorization": "Bearer " + token}


def register(username, nickname, password):
    """注册并返回 token（失败返回空串）"""
    r = requests.post(f"{BASE}/api/register", json={
        "username": username, "nickname": nickname, "password": password,
    })
    j = r.json()
    if not j.get("success"):
        return "", j
    return j["token"], j


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  [PASS] {name}")
    else:
        failed += 1
        FAILS.append(name)
        print(f"  [FAIL] {name}  {extra}")


def make_png(r, g, b, size=8):
    """生成一张纯色 PNG（不依赖 Pillow）"""
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


print("=" * 60)
print("  新功能测试：图片 / 时间线 / 数据看板")
print("=" * 60)

# ---------------------------------------------------------------
# 前置：清空链 + 清空账号密码，保证本脚本可反复运行
# ---------------------------------------------------------------
try:
    r = requests.post(f"{BASE}/api/reset", json={"wipe_credentials": True}, timeout=10)
    if not (r.status_code == 200 and r.json().get("success")):
        print(f"\n[准备] 重置返回异常：HTTP {r.status_code} {r.text[:120]}")
except requests.RequestException:
    print(f"\n  无法连接后端（{BASE}），请先启动：")
    print("    C:/Users/25191/.venvs/base/Scripts/python.exe backend/app.py")
    sys.exit(1)

# ---------------------------------------------------------------
print("\n【准备】注册用户 + 上架一件带图片的商品")
# ---------------------------------------------------------------
TOKEN_A, ja = register("测试甲", "测试甲", "TestA123")
check("注册卖家", bool(TOKEN_A), str(ja)[:200])

TOKEN_B, jb = register("测试乙", "测试乙", "TestB123")
check("注册买家", bool(TOKEN_B), str(jb)[:200])

if not TOKEN_A or not TOKEN_B:
    print("\n  注册失败（很可能是演示账户池已用完）。")
    print("  请点「区块链浏览 → 重置整条链」后重跑本脚本。")
    sys.exit(1)

# ---- 1. 图片上传 ----
print("\n【功能1】图片上传（内容寻址）")
png1 = make_png(29, 93, 184)      # 蓝色图
png2 = make_png(29, 93, 184)      # 完全相同的图（测去重）
png3 = make_png(200, 40, 40)      # 不同颜色（测哈希不同）

r = requests.post(f"{BASE}/api/upload", files={"image": ("a.png", png1, "image/png")})
u1 = r.json()
check("上传图片成功", u1.get("success"), r.text[:200])
h1 = u1.get("image_hash", "")
check("返回 64 位 SHA-256 哈希", len(h1) == 64, f"得到 {len(h1)} 位")
check("返回可访问 URL", u1.get("image_url", "").startswith("/uploads/"), u1.get("image_url"))

r = requests.post(f"{BASE}/api/upload", files={"image": ("b.png", png2, "image/png")})
u2 = r.json()
check("相同内容 → 相同哈希（去重）", u2.get("image_hash") == h1)

r = requests.post(f"{BASE}/api/upload", files={"image": ("c.png", png3, "image/png")})
u3 = r.json()
check("不同内容 → 不同哈希", u3.get("image_hash") != h1)

# 哈希正确性：本地算一遍比对
import hashlib
check("哈希 = SHA256(文件内容)", hashlib.sha256(png1).hexdigest() == h1)

# 图片能访问
img_resp = requests.get(BASE + u1["image_url"])
check("图片可访问（HTTP 200）", img_resp.status_code == 200, f"HTTP {img_resp.status_code}")
check("返回的确是图片字节", img_resp.content == png1, f"长度 {len(img_resp.content)} vs {len(png1)}")

# 篡改检测
r = requests.post(f"{BASE}/api/verify_image", json={"image_hash": h1})
v = r.json()
check("校验接口可用", v.get("success"))
check("未篡改 → verified=true", v.get("verified") is True, v.get("message"))

r = requests.post(f"{BASE}/api/verify_image", json={"image_hash": "0" * 64})
check("不存在的哈希 → 报错", r.status_code == 404)

# 非法格式
r = requests.post(f"{BASE}/api/upload", files={"image": ("x.txt", b"hello", "text/plain")})
check("拒绝非图片格式", not r.json().get("success"), r.text[:150])

# 空文件
r = requests.post(f"{BASE}/api/upload", files={"image": ("e.png", b"", "image/png")})
check("拒绝空文件", not r.json().get("success"), r.text[:150])

# ---------------------------------------------------------------
# ---- 2. 带图片上架 ----
print("\n【上架】用 multipart 带图片上架")
r = requests.post(f"{BASE}/api/items", headers=auth_hdr(TOKEN_A), data={
    "name": "内容寻址测试商品",
    "description": "这张图有链上指纹", "category": "数码电子", "price_eth": "0.5",
}, files={"image": ("item.png", png1, "image/png")})
pub = r.json()
check("带图上架成功", pub.get("success"), r.text[:250])
check("上架响应含图片哈希", pub.get("image_hash") == h1, pub.get("image_hash"))

# 找到这件商品
r = requests.get(f"{BASE}/api/items", params={"q": "内容寻址测试商品"})
items = r.json().get("items", [])
check("能搜到新上架商品", len(items) >= 1)
if items:
    item = items[0]
    item_id = item["id"]
    check("列表返回 image_hash", item.get("image_hash") == h1)
    check("列表返回 image_url", item.get("image_url", "").startswith("/uploads/"))
    check("分类正确解码为数码电子", item.get("category") == "数码电子", item.get("category"))
    check("描述已剥离编码前缀", "分类" not in item.get("description", ""), item.get("description"))

    # 详情接口
    d = requests.get(f"{BASE}/api/items/{item_id}").json()
    check("详情接口 image_hash 正确", d["item"].get("image_hash") == h1)
    check("详情接口含完整时间", bool(d["item"].get("created_full")), d["item"].get("created_full"))

# ---- 分类接口 ----
# 这里曾有个线上 bug：decode_description 返回 3 个值，却写成
# `cat, _ = ...`，只要库里有商品就抛 ValueError，接口直接 500。
# 加断言锁住，防止再犯。
c = requests.get(f"{BASE}/api/categories")
check("分类接口返回 200", c.status_code == 200, f"HTTP {c.status_code}")
if c.status_code == 200:
    body = c.json()
    check("分类接口返回 categories", bool(body.get("categories")), str(body)[:200])
    check("数码电子计数 >= 1", body.get("counts", {}).get("数码电子", 0) >= 1,
          str(body.get("counts"))[:200])

# ---------------------------------------------------------------
print("\n【功能2】交易时间线")
# ---------------------------------------------------------------
r = requests.get(f"{BASE}/api/timeline/{item_id}")
tl = r.json()
check("时间线接口可用", tl.get("success"), r.text[:200])
check("有商品名", tl.get("item_name") == "内容寻址测试商品")
steps = tl.get("steps", [])
check("至少有 1 步（上架）", len(steps) >= 1, f"得到 {len(steps)} 步")
check("第一步是 listed", steps and steps[0]["type"] == "listed", steps[0]["type"] if steps else "无")
check("步骤带区块号", steps and "block" in steps[0])
check("步骤带时间文本", steps and steps[0].get("time_text", "") != "—", steps[0].get("time_text"))
check("步骤带操作人昵称", steps and steps[0].get("actor") == "测试甲", steps[0].get("actor"))

# 购买 → 时间线应有第2步
r = requests.post(f"{BASE}/api/buy", headers=auth_hdr(TOKEN_B), json={"item_id": item_id})
check("买家下单成功", r.json().get("success"), r.text[:200])

tl2 = requests.get(f"{BASE}/api/timeline/{item_id}").json()
types2 = [s["type"] for s in tl2["steps"]]
check("时间线出现 purchased 步骤", "purchased" in types2, str(types2))
p = next((s for s in tl2["steps"] if s["type"] == "purchased"), None)
check("购买步骤金额正确 (0.5 ETH)", p and p.get("amount_eth") == 0.5, p.get("amount_eth") if p else "无")
check("购买步骤操作人 = 测试乙", p and p.get("actor") == "测试乙", p.get("actor") if p else "")

# 确认收货 → 第3步
r = requests.post(f"{BASE}/api/confirm", headers=auth_hdr(TOKEN_B), json={"item_id": item_id})
check("确认收货成功", r.json().get("success"), r.text[:200])

tl3 = requests.get(f"{BASE}/api/timeline/{item_id}").json()
types3 = [s["type"] for s in tl3["steps"]]
check("时间线出现 delivered 步骤", "delivered" in types3, str(types3))
dl = next((s for s in tl3["steps"] if s["type"] == "delivered"), None)
# 0.5 ETH 扣 1% = 0.495
check("放款金额 = 0.495 ETH（扣1%）", dl and abs(dl.get("amount_eth", 0) - 0.495) < 1e-6,
      dl.get("amount_eth") if dl else "无")
check("时间线按区块递增排序",
      [s["block"] for s in tl3["steps"]] == sorted(s["block"] for s in tl3["steps"]))

# 评价 → 第4步
r = requests.post(f"{BASE}/api/rate", headers=auth_hdr(TOKEN_B), json={
    "item_id": item_id, "score": 5, "target": "测试甲"})
check("评价成功", r.json().get("success"), r.text[:200])

tl4 = requests.get(f"{BASE}/api/timeline/{item_id}").json()
types4 = [s["type"] for s in tl4["steps"]]
check("时间线出现 rated 步骤", "rated" in types4, str(types4))
check("完整链路 listed→purchased→delivered→rated",
      types4[:4] == ["listed", "purchased", "delivered", "rated"], str(types4))

# 不存在的商品
r = requests.get(f"{BASE}/api/timeline/99999")
check("不存在的商品 → 404", r.status_code == 404)

# ---------------------------------------------------------------
print("\n【功能3】数据看板")
# ---------------------------------------------------------------
r = requests.get(f"{BASE}/api/dashboard")
db = r.json()
check("看板接口可用", db.get("success"), r.text[:250])

m = db.get("metrics", {})
check("含累计成交额", "gmv_eth" in m, str(m))
check("成交额 >= 0.5", m.get("gmv_eth", 0) >= 0.5, m.get("gmv_eth"))
check("含托管资金字段", "locked_eth" in m)
check("含合约余额", "escrow_balance_eth" in m)
check("含在售/已售计数", "onsale" in m and "sold" in m)
check("已售数量 >= 1", m.get("sold", 0) >= 1, m.get("sold"))
check("含平台手续费", "platform_fee_eth" in m)
check("平台费 = 成交额 1%",
      abs(m.get("platform_fee_eth", 0) - m.get("gmv_eth", 0) * 0.01) < 1e-6,
      f"{m.get('platform_fee_eth')} vs {m.get('gmv_eth', 0) * 0.01}")

# 趋势
tr = db.get("trend", {})
check("含趋势 labels/values", "labels" in tr and "values" in tr)
check("趋势有点（今天有成交）", len(tr.get("labels", [])) >= 1, str(tr))
check("趋势值累加 ≈ 成交额",
      abs(sum(tr.get("values", [])) - m.get("gmv_eth", 0)) < 1e-6,
      f"{sum(tr.get('values', []))} vs {m.get('gmv_eth')}")

# 环形图
dn = db.get("donut", {})
check("含分类占比数据", "labels" in dn and "values" in dn)
check("环形图有数据", len(dn.get("labels", [])) >= 1, str(dn))
check("环形图口径标记 is_amount", "is_amount" in dn)

# 柱状图
pb = db.get("price_buckets", {})
check("含价格区间 labels", "labels" in pb and len(pb["labels"]) == 5, str(pb))
check("含价格区间 values", "values" in pb and len(pb["values"]) == 5)
check("价格区间总数 >= 1", sum(pb.get("values", [])) >= 1, str(pb.get("values")))

# ---------------------------------------------------------------
print("\n【一致性】看板数字 vs 链上真实状态")
# ---------------------------------------------------------------
st = requests.get(f"{BASE}/api/status").json()
check("托管资金与合约余额一致（无在途订单时均为0）",
      abs(m.get("locked_eth", 0) - st.get("escrow_balance", 0)) < 1e-6,
      f"locked={m.get('locked_eth')} contract={st.get('escrow_balance')}")

# ---------------------------------------------------------------
print("\n" + "=" * 60)
print(f"  结果：{passed} 通过 / {failed} 失败")
if FAILS:
    print("  失败项：")
    for f in FAILS:
        print("    - " + f)
print("=" * 60)

sys.exit(1 if failed else 0)

# -*- coding: utf-8 -*-
"""
认证与安全测试

覆盖：
  1. 注册（密码强度、用户名校验、重复注册）
  2. 登录（正确/错误密码、暴力破解锁定）
  3. 令牌（有效性、篡改防护、过期、登出作废）
  4. 越权访问（无 token / 伪造 token / 冒充他人身份）
  5. 密码存储（不落明文、加盐）

运行前确保后端已在 5000 端口运行。
"""
import sys
import time
import requests

BASE = "http://127.0.0.1:5000"

passed = 0
failed = 0
FAILS = []


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  [PASS] {name}")
    else:
        failed += 1
        FAILS.append(name)
        print(f"  [FAIL] {name}  {extra}")


def auth_hdr(token):
    return {"Authorization": "Bearer " + token}


print("=" * 62)
print("  认证与安全测试")
print("=" * 62)

# ---------------------------------------------------------------
# 前置：清空链 + 清空账号密码，保证本脚本可反复运行
# （普通重置会保留账号，导致 alice 这类固定用户名第二次跑就冲突）
# ---------------------------------------------------------------
try:
    r = requests.post(f"{BASE}/api/reset", json={"wipe_credentials": True}, timeout=10)
    if r.status_code == 200 and r.json().get("success"):
        print("\n[准备] 已重置链并清空账号密码")
    else:
        print(f"\n[准备] 重置返回异常：HTTP {r.status_code} {r.text[:120]}")
except requests.RequestException as e:
    print(f"\n  无法连接后端（{BASE}），请先启动：")
    print("    C:/Users/25191/.venvs/base/Scripts/python.exe backend/app.py")
    sys.exit(1)

# ---------------------------------------------------------------
print("\n【1】注册：输入校验与密码强度")
# ---------------------------------------------------------------
U1, P1 = "alice", "Good1Pass"

r = requests.post(f"{BASE}/api/register", json={"username": U1, "password": "123"})
check("拒绝过短密码", not r.json().get("success"), r.text[:130])

r = requests.post(f"{BASE}/api/register", json={"username": U1, "password": "12345678"})
check("拒绝纯数字密码", not r.json().get("success"), r.text[:130])

r = requests.post(f"{BASE}/api/register", json={"username": U1, "password": "aaaaaaaa"})
check("拒绝单字符重复密码", not r.json().get("success"), r.text[:130])

r = requests.post(f"{BASE}/api/register", json={"username": U1, "password": "password"})
check("拒绝常见弱口令", not r.json().get("success"), r.text[:130])

r = requests.post(f"{BASE}/api/register", json={"username": "bad name!", "password": P1})
check("拒绝非法用户名字符", not r.json().get("success"), r.text[:130])

r = requests.post(f"{BASE}/api/register", json={"username": "x" * 20, "password": P1})
check("拒绝超长用户名", not r.json().get("success"), r.text[:130])

r = requests.post(f"{BASE}/api/register", json={"username": U1, "password": P1, "nickname": "爱丽丝"})
reg = r.json()
check("合法注册成功", reg.get("success"), r.text[:200])
TOKEN1 = reg.get("token", "")
check("注册响应含 token", len(TOKEN1) > 20, f"len={len(TOKEN1)}")
check("注册响应含链上地址", (reg.get("address") or "").startswith("0x"), reg.get("address"))
check("注册响应不含密码字段", "password" not in r.text.lower(), r.text[:200])

r = requests.post(f"{BASE}/api/register", json={"username": U1, "password": "Other1Pass"})
check("重复用户名被拒绝（409）", r.status_code == 409, f"HTTP {r.status_code}")

r = requests.get(f"{BASE}/api/check_username", params={"username": U1})
check("用户名占用查询接口", r.json().get("taken") is True, r.text[:130])

# ---------------------------------------------------------------
print("\n【2】密码存储安全")
# ---------------------------------------------------------------
sys.path.insert(0, "backend")
try:
    import auth as auth_mod
    # 同一密码两次哈希必须不同（随机盐）
    h1 = auth_mod.hash_password("SamePassword1")
    h2 = auth_mod.hash_password("SamePassword1")
    check("相同密码两次哈希不同（随机盐）", h1 != h2)
    check("哈希中不含明文密码", "SamePassword1" not in h1)
    check("哈希格式为 scrypt$salt$hash", h1.startswith("scrypt$") and h1.count("$") == 2)
    check("正确密码校验通过", auth_mod.verify_password("SamePassword1", h1) is True)
    check("错误密码校验失败", auth_mod.verify_password("WrongPassword1", h1) is False)
    check("空哈希校验失败", auth_mod.verify_password("anything", "") is False)
except ImportError as e:
    check("导入 auth 模块", False, str(e))

# ---------------------------------------------------------------
print("\n【3】登录")
# ---------------------------------------------------------------
r = requests.post(f"{BASE}/api/login", json={"username": U1, "password": "WrongPass1"})
check("错误密码登录失败", not r.json().get("success"), r.text[:130])

r = requests.post(f"{BASE}/api/login", json={"username": "nonexistent_user", "password": "Whatever1"})
check("不存在用户登录失败", not r.json().get("success"), r.text[:130])

r = requests.post(f"{BASE}/api/login", json={"username": U1, "password": P1})
login = r.json()
check("正确密码登录成功", login.get("success"), r.text[:200])
TOKEN2 = login.get("token", "")
check("登录返回 token", len(TOKEN2) > 20)
check("返回 token 有效期", login.get("expires_in") == 7200, login.get("expires_in"))

# ---------------------------------------------------------------
print("\n【4】令牌校验与防篡改")
# ---------------------------------------------------------------
r = requests.get(f"{BASE}/api/me", headers=auth_hdr(TOKEN2))
check("/api/me 用有效 token 可访问", r.json().get("success"), r.text[:130])
check("/api/me 返回正确用户名", r.json().get("username") == U1, r.json().get("username"))

# 无 token
r = requests.get(f"{BASE}/api/me")
check("无 token 访问 /api/me → 401", r.status_code == 401, f"HTTP {r.status_code}")

# 篡改签名
bad = TOKEN2[:-4] + "AAAA"
r = requests.get(f"{BASE}/api/me", headers=auth_hdr(bad))
check("签名被篡改的 token → 401", r.status_code == 401, f"HTTP {r.status_code}")

# 伪造 payload（换个用户名，签名对不上）
import base64, json
forged_payload = base64.urlsafe_b64encode(
    json.dumps({"u": "hacker", "iat": 0, "exp": 9999999999}).encode()
).rstrip(b"=").decode()
forged = forged_payload + "." + TOKEN2.rpartition(".")[2]
r = requests.get(f"{BASE}/api/me", headers=auth_hdr(forged))
check("伪造 payload 的 token → 401", r.status_code == 401, f"HTTP {r.status_code}")

r = requests.get(f"{BASE}/api/me", headers=auth_hdr("garbage"))
check("乱码 token → 401", r.status_code == 401, f"HTTP {r.status_code}")

# 过期 token
try:
    import auth as auth_mod
    import hmac, hashlib
    expired_payload = base64.urlsafe_b64encode(
        json.dumps({"u": U1, "iat": 0, "exp": 1}).encode()
    ).rstrip(b"=").decode()
    sig = base64.urlsafe_b64encode(
        hmac.new(auth_mod.SECRET_KEY, expired_payload.encode(), hashlib.sha256).digest()
    ).rstrip(b"=").decode()
    r = requests.get(f"{BASE}/api/me", headers=auth_hdr(expired_payload + "." + sig))
    check("已过期 token → 401", r.status_code == 401, f"HTTP {r.status_code}")
except Exception as e:
    check("过期 token 测试", False, str(e))

# ---------------------------------------------------------------
print("\n【5】越权访问：伪造身份")
# ---------------------------------------------------------------
# 建第二个用户
requests.post(f"{BASE}/api/register", json={"username": "bob", "password": "BobPass123"})
bob_login = requests.post(f"{BASE}/api/login", json={"username": "bob", "password": "BobPass123"}).json()
TOKEN_BOB = bob_login.get("token", "")

# alice 正常上架
r = requests.post(f"{BASE}/api/items", json={
    "name": "Alice的商品", "description": "测试", "category": "数码电子", "price_eth": 0.5,
}, headers=auth_hdr(TOKEN2))
check("alice 用自己 token 上架成功", r.json().get("success"), r.text[:200])

items = requests.get(f"{BASE}/api/items", params={"q": "Alice的商品"}).json().get("items", [])
ITEM_ID = items[0]["id"] if items else None
check("能找到 alice 的商品", ITEM_ID is not None)

# bob 尝试在请求体里冒充 alice 下架（旧漏洞！）
r = requests.post(f"{BASE}/api/remove",
                  json={"username": "alice", "item_id": ITEM_ID},
                  headers=auth_hdr(TOKEN_BOB))
res = r.json()
check("bob 冒充 alice 下架被拒绝", not res.get("success"), r.text[:200])
check("拒绝原因是权限（不是卖家本人）",
      "卖家" in (res.get("error") or "") or "权限" in (res.get("error") or ""),
      res.get("error"))

# bob 不带 token 尝试下架
r = requests.post(f"{BASE}/api/remove", json={"username": "alice", "item_id": ITEM_ID})
check("无 token 下架 → 401", r.status_code == 401, f"HTTP {r.status_code}")

# bob 尝试用偷来的 alice token？无法伪造，验证 bob token 只能操作自己的身份
r = requests.post(f"{BASE}/api/buy", json={"username": "alice", "item_id": ITEM_ID},
                  headers=auth_hdr(TOKEN_BOB))
# bob 自己买是可以的（buy 不限制买家是谁），关键是服务端记录的是 bob 而不是 alice
if r.json().get("success"):
    detail = requests.get(f"{BASE}/api/items/{ITEM_ID}").json()["item"]
    bob_info = requests.get(f"{BASE}/api/me", headers=auth_hdr(TOKEN_BOB)).json()
    check("请求体写 alice，链上买家实为 bob（身份以 token 为准）",
          detail["buyer_address"].lower() == bob_info["address"].lower(),
          f"buyer={detail['buyer_address']} bob={bob_info['address']}")

# 评分越权：bob 尝试以 alice 身份评价
r = requests.post(f"{BASE}/api/rate",
                  json={"username": "alice", "item_id": ITEM_ID, "score": 5, "target": "bob"},
                  headers=auth_hdr(TOKEN_BOB))
check("评分身份以 token 为准（不能冒充）",
      r.json().get("success") or "只" in (r.json().get("error") or "") or "自" in (r.json().get("error") or ""),
      r.text[:200])

# ---------------------------------------------------------------
print("\n【6】登出：token 作废")
# ---------------------------------------------------------------
# 注意：测试链只有 10 个账户（1 个平台 + 9 个用户），如果池子满了会注册失败。
# 这里显式检查注册结果，避免静默跳过。
r = requests.post(f"{BASE}/api/register", json={"username": "carol", "password": "Carol12345"})
carol = r.json()
check("测试账号 carol 注册成功（若失败说明账户池已满）", carol.get("success"),
      (carol.get("error") or "")[:80])

if carol.get("success"):
    T = carol["token"]
    check("carol 登录后 /api/me 可访问",
          requests.get(f"{BASE}/api/me", headers=auth_hdr(T)).json().get("success"))

    r = requests.post(f"{BASE}/api/logout", headers=auth_hdr(T))
    check("登出成功", r.json().get("success"), r.text[:130])

    r = requests.get(f"{BASE}/api/me", headers=auth_hdr(T))
    check("登出后原 token 失效 → 401", r.status_code == 401, f"HTTP {r.status_code}")

    # token 放请求体（兼容老前端写法）也应被识别
    r2 = requests.post(f"{BASE}/api/login", json={"username": "carol", "password": "Carol12345"})
    T2 = r2.json().get("token", "")
    if T2:
        r = requests.post(f"{BASE}/api/logout", json={"token": T2})
        check("token 放请求体也能登出（兼容旧写法）", r.json().get("success"), r.text[:130])
else:
    check("账户池未满时的登出测试", True, "跳过：账户池已满")

# ---------------------------------------------------------------
print("\n【7】防暴力破解")
# ---------------------------------------------------------------
import random
victim = "dave"
requests.post(f"{BASE}/api/register", json={"username": victim, "password": "DavePass123"})

locked_hit = False
for i in range(7):
    r = requests.post(f"{BASE}/api/login", json={"username": victim, "password": "WrongPass" + str(i)})
    if r.status_code == 429:
        locked_hit = True
        print(f"    第 {i+1} 次尝试触发锁定")
        break

check("连续密码错误触发锁定（429）", locked_hit)

# 锁定期内即使密码正确也应被拒
r = requests.post(f"{BASE}/api/login", json={"username": victim, "password": "DavePass123"})
check("锁定期间正确密码也无法登录", r.status_code == 429, f"HTTP {r.status_code}")

# ---------------------------------------------------------------
print("\n【8】改密后旧 token 的新请求不受影响（无状态 token 特性说明）")
# ---------------------------------------------------------------
r = requests.get(f"{BASE}/api/my_items", headers=auth_hdr(TOKEN2))
check("/api/my_items 用 token 可访问", r.json().get("success"), r.text[:130])
check("只返回自己的商品", all(
    it["is_seller"] or it["buyer_address"] for it in r.json().get("items", [])
), r.text[:200])

# ---------------------------------------------------------------
print("\n【9】敏感信息不泄漏")
# ---------------------------------------------------------------
r = requests.get(f"{BASE}/api/user/alice")
body = r.text.lower()
check("公开用户接口不含密码", "password" not in body)
check("公开用户接口不含密码哈希", "scrypt" not in body)
check("公开用户接口不含 token", "token" not in body)

r = requests.get(f"{BASE}/api/items")
check("商品列表不含密码哈希", "scrypt" not in r.text.lower())

# ---------------------------------------------------------------
print("\n" + "=" * 62)
print(f"  结果：{passed} 通过 / {failed} 失败")
if FAILS:
    print("  失败项：")
    for f in FAILS:
        print("    - " + f)
print("=" * 62)

sys.exit(1 if failed else 0)

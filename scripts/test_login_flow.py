# -*- coding: utf-8 -*-
"""
登录态健壮性测试 —— 专门盯"总出现登录已失效"这个问题。

之前的三条病灶：
  1. 点"重置整条链"后 user_sessions 被清空但账号还在 → 令牌有效却查不到链上地址 → 全站报"登录已失效"
  2. 前端被踢下线后只剩一个只读的用户条，没有登录入口
  3. 错误码不统一（有的 400 有的 404，都不带 AUTH_REQUIRED），前端只能瞎猜

本脚本覆盖前端的后端契约部分：
  - 重置链后，令牌依然有效，且接口能自愈（不该报"登录已失效"）
  - 演示账户用尽时返回 503 + ACCOUNT_POOL_FULL，而不是把用户踢下线
  - 真正失效的令牌返回 401 + AUTH_REQUIRED
"""
import sys
import time
import requests

BASE = "http://127.0.0.1:5000"
PASS = 0
FAIL = 0
FAILED = []


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK] {name}")
    else:
        FAIL += 1
        FAILED.append(name)
        print(f"  [XX] {name}  {extra}")


def hdr(token):
    return {"Authorization": "Bearer " + token}


def safe_json(r):
    """只有 content-type 是 JSON 时才解析，避免把 HTML 错误页炸成异常。"""
    if r.headers.get("content-type", "").startswith("application/json"):
        try:
            return r.json()
        except Exception:
            return {}
    return {}


def jpost(path, body, token=None, **kw):
    h = dict(kw.pop("headers", {}) or {})
    if token:
        h.update(hdr(token))
    return requests.post(f"{BASE}{path}", json=body, headers=h, **kw)


def jget(path, token=None):
    return requests.get(f"{BASE}{path}", headers=hdr(token) if token else {})


print("=" * 62)
print("  登录态健壮性测试")
print("=" * 62)

# ---------- 0. 干净起步 ----------
r = jpost("/api/reset", {"wipe_credentials": True})
check("重置整条链（清空账号密码）", r.status_code == 200 and r.json().get("success"), r.text[:120])
time.sleep(0.5)

# ---------- 1. 注册 + 登录 ----------
name = "flowuser"
print(f"\n【1】注册并登录 {name}")
r = jpost("/api/register", {"username": name, "password": "Flow9999", "nickname": "流程用户"})
ok = r.status_code == 200 and r.json().get("success")
check("注册成功", ok, r.text[:160])
token = r.json().get("token") if ok else None
if not ok:
    print("\n注册失败，后续测试无意义，中止。")
    sys.exit(1)

r = jget("/api/me", token)
check("登录后 /api/me 返回用户名", r.status_code == 200 and r.json().get("username") == name, r.text[:160])
bal = r.json().get("balance_eth")
# 注册交易本身要付一点 gas，所以略低于 100 是正常的
check("余额接近 100 ETH（扣除注册 gas）", 99.9 <= float(bal) <= 100, f"实际 {bal}")

# ---------- 2. 核心场景：重置链但不删账号 ----------
print("\n【2】模拟用户点「重置整条链」（保留账号，不清密码）")
r = jpost("/api/reset", {"wipe_credentials": False})
check("重置返回成功", r.status_code == 200 and r.json().get("success"), r.text[:120])
time.sleep(0.5)

# 关键断言：令牌还在，接口不该把它当成"登录已失效"
r = jget("/api/me", token)
body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
check("重置后旧令牌仍然可查 /api/me（地址已自愈）",
      r.status_code == 200 and body.get("username") == name,
      f"HTTP {r.status_code} {r.text[:160]}")
check("自愈后没有返回 AUTH_REQUIRED",
      body.get("code") != "AUTH_REQUIRED",
      f"错误码 {body.get('code')}")

# 各个需要登录的接口都不该报"登录已失效"
for path in ["/api/my_items", "/api/transactions"]:
    r = jget(path, token)
    body = safe_json(r)
    check(f"重置后 {path} 不报登录失效",
          body.get("code") != "AUTH_REQUIRED",
          f"HTTP {r.status_code} {r.text[:140]}")

# ---------- 3. 真正失效的令牌 ----------
print("\n【3】真正失效的令牌 → 必须是 401 + AUTH_REQUIRED")
r = jget("/api/me", "this.is.garbage")
body = safe_json(r)
check("乱码令牌返回 401", r.status_code == 401, f"HTTP {r.status_code}")
check("错误码为 AUTH_REQUIRED", body.get("code") == "AUTH_REQUIRED", str(body)[:140])

# 登出后旧令牌作废
jpost("/api/logout", {}, token=token)
r = jget("/api/me", token)
body = safe_json(r)
check("登出后旧令牌返回 401", r.status_code == 401, f"HTTP {r.status_code}")
check("登出后错误码为 AUTH_REQUIRED", body.get("code") == "AUTH_REQUIRED", str(body)[:140])

# ---------- 4. 演示账户用尽 ----------
print("\n【4】演示账户用尽 → 503 + ACCOUNT_POOL_FULL（不能把用户踢下线）")
jpost("/api/reset", {"wipe_credentials": True})
time.sleep(0.3)

tokens = []
pool_full_seen = False

# 先问清楚这条链到底能注册多少人，再决定试多少次。
# 之前写死"9 个用户"，后来把账户数提到 200 就永远触发不了池满，
# 所以改成动态：注册到返回 ACCOUNT_POOL_FULL 为止，上限留足余量。
try:
    _st = requests.get(f"{BASE}/api/status", timeout=10).json()
    capacity = int(_st.get("accounts_count", 10)) - 1
except Exception:
    capacity = 9
max_tries = max(12, capacity + 5)

for i in range(1, max_tries + 1):
    uname = f"pool{i:03d}"
    r = jpost("/api/register", {"username": uname, "password": "Pool9999"})
    d = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if d.get("success"):
        tokens.append((uname, d.get("token")))
        continue
    if d.get("code") == "ACCOUNT_POOL_FULL":
        pool_full_seen = True
        check("账户池用尽返回 503", r.status_code == 503, f"HTTP {r.status_code}")
        break

check("能够触发 ACCOUNT_POOL_FULL", pool_full_seen,
      f"共成功注册 {len(tokens)} 个用户仍未触发（池子比预期大？）")

# 已登录用户的令牌在池子满之后仍应可用（关键：不能被连累踢下线）
if tokens:
    last_name, last_token = tokens[-1]
    r = jget("/api/me", last_token)
    body = r.json() if r.status_code == 200 else {}
    check(f"池子满后已登录用户 {last_name} 仍可访问 /api/me",
          r.status_code == 200 and body.get("username") == last_name,
          f"HTTP {r.status_code} {r.text[:140]}")

# ---------- 5. 未登录访问 ----------
print("\n【5】未登录访问受保护接口")
r = jget("/api/me")
body = safe_json(r)
check("无令牌访问 /api/me 返回 401", r.status_code == 401, f"HTTP {r.status_code}")
check("无令牌错误码为 AUTH_REQUIRED", body.get("code") == "AUTH_REQUIRED", str(body)[:140])

# ---------- 汇总 ----------
print("\n" + "=" * 62)
print(f"  断言：{PASS} 通过 / {FAIL} 失败  （共 {PASS + FAIL} 条）")
if FAILED:
    print("  失败项：")
    for f in FAILED:
        print(f"    - {f}")
print("=" * 62)
sys.exit(1 if FAIL else 0)

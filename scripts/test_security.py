# -*- coding: utf-8 -*-
"""
安全加固回归测试

覆盖这次为"对外暴露"补的几个口子：
  【1】/api/reset 的权限边界（本机免密钥 / 外部必须带 X-Admin-Key）
  【2】隧道场景：cloudflared 是本地进程，外部请求的 REMOTE_ADDR 是 127.0.0.1，
       只能靠代理头识别 —— 这条是本次最容易写错、也最致命的地方
  【3】/api/upload 必须登录
  【4】邀请码（配了 INVITE_CODE 才启用）
  【5】登录/注册按 IP 限流

全部用 Flask 的 test_client 在进程内跑，数据目录隔离到临时目录，
不会碰 data/ 里的真实数据。

运行：
  C:/Users/25191/.venvs/base/Scripts/python.exe -X utf8 scripts/test_security.py
"""

import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

TMP = Path(tempfile.mkdtemp(prefix="sec_test_"))

# 必须在 import app 之前设置，否则会读到真实 data/
os.environ["MARKET_DATA_DIR"] = str(TMP)
os.environ["ADMIN_KEY"] = "test-admin-key"
os.environ["INVITE_CODE"] = "test-invite"
os.environ["PYTHONIOENCODING"] = "utf-8"
os.environ["PYTHONUTF8"] = "1"

sys.path.insert(0, str(ROOT / "backend"))

import app as appmod          # noqa: E402

PASS = 0
FAIL = 0
FAILS = []


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK] {name}")
    else:
        FAIL += 1
        FAILS.append(name)
        print(f"  [XX] {name}  {extra}")


client = appmod.app.test_client()

# 模拟"从外网来"：REMOTE_ADDR 不是环回地址
EXT = {"REMOTE_ADDR": "203.0.113.9"}
# 模拟"经过 Cloudflare 隧道"：REMOTE_ADDR 是环回，但带了 CF 头 —— 最危险的情形
TUNNEL = {"REMOTE_ADDR": "127.0.0.1", "HTTP_CF_CONNECTING_IP": "203.0.113.9"}
# 模拟"经过普通反代"
PROXIED = {"REMOTE_ADDR": "127.0.0.1", "HTTP_X_FORWARDED_FOR": "203.0.113.9"}

print("=" * 64)
print("  安全加固回归测试")
print("=" * 64)

# ------------------------------------------------------------
print("\n【1】/api/reset 权限边界")
# ------------------------------------------------------------
r = client.post("/api/reset", json={"wipe_credentials": False})
check("本机直连可重置（自动化测试依赖这条）", r.status_code == 200, f"HTTP {r.status_code}")

r = client.post("/api/reset", json={"wipe_credentials": False}, environ_base=EXT)
check("外网来源无密钥 → 403", r.status_code == 403, f"HTTP {r.status_code} {r.get_json()}")
check("错误码为 FORBIDDEN", (r.get_json() or {}).get("code") == "FORBIDDEN",
      str(r.get_json()))

r = client.post("/api/reset", json={"wipe_credentials": False},
                headers={"X-Admin-Key": "wrong"}, environ_base=EXT)
check("外网来源密钥错误 → 403", r.status_code == 403, f"HTTP {r.status_code}")

r = client.post("/api/reset", json={"wipe_credentials": False},
                headers={"X-Admin-Key": "test-admin-key"}, environ_base=EXT)
check("外网来源密钥正确 → 200", r.status_code == 200, f"HTTP {r.status_code}")

# ------------------------------------------------------------
print("\n【2】隧道/反代场景（REMOTE_ADDR 是环回，但带了转发头）")
# ------------------------------------------------------------
r = client.post("/api/reset", json={"wipe_credentials": False}, environ_base=TUNNEL)
check("带 CF-Connecting-IP 时不再当成本机 → 403", r.status_code == 403,
      f"HTTP {r.status_code}")

r = client.post("/api/reset", json={"wipe_credentials": False}, environ_base=PROXIED)
check("带 X-Forwarded-For 时不再当成本机 → 403", r.status_code == 403,
      f"HTTP {r.status_code}")

r = client.post("/api/reset", json={"wipe_credentials": False},
                headers={"X-Admin-Key": "test-admin-key"}, environ_base=TUNNEL)
check("隧道 + 正确密钥 → 200", r.status_code == 200, f"HTTP {r.status_code}")

# ------------------------------------------------------------
print("\n【3】/api/upload 必须登录")
# ------------------------------------------------------------
r = client.post("/api/upload", environ_base=EXT)
check("未登录上传 → 401", r.status_code == 401, f"HTTP {r.status_code}")

r = client.post("/api/upload", headers={"Authorization": "Bearer broken"},
                environ_base=EXT)
check("伪造令牌上传 → 401", r.status_code == 401, f"HTTP {r.status_code}")

# ------------------------------------------------------------
print("\n【4】邀请码")
# ------------------------------------------------------------
r = client.post("/api/register", json={"username": "邀请甲", "password": "Test123456"},
                environ_base=EXT)
check("外网无邀请码注册 → 403", r.status_code == 403, f"HTTP {r.status_code} {r.get_json()}")
check("错误码 BAD_INVITE", (r.get_json() or {}).get("code") == "BAD_INVITE", str(r.get_json()))

r = client.post("/api/register",
                json={"username": "邀请乙", "password": "Test123456", "invite_code": "nope"},
                environ_base=EXT)
check("外网错误邀请码 → 403", r.status_code == 403, f"HTTP {r.status_code}")

r = client.post("/api/register",
                json={"username": "邀请丙", "password": "Test123456",
                      "invite_code": "test-invite"},
                environ_base=EXT)
j = r.get_json() or {}
check("外网正确邀请码 → 注册成功", r.status_code == 200 and j.get("success"),
      f"HTTP {r.status_code} {j}")

TOKEN = j.get("token")
check("返回 token", bool(TOKEN), str(j)[:150])

r = client.post("/api/register", json={"username": "本机甲", "password": "Test123456"})
check("本机直连不需邀请码（测试脚本兼容）", r.status_code == 200, f"HTTP {r.status_code}")

# ------------------------------------------------------------
print("\n【5】/api/upload 登录后可用")
# ------------------------------------------------------------
import hashlib
import struct
import zlib


def make_png(size=8):
    def chunk(tag, data):
        c = tag + data
        return (struct.pack(">I", len(data)) + c
                + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF))
    raw = b""
    for _ in range(size):
        raw += b"\x00" + bytes([10, 20, 30]) * size
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw))
            + chunk(b"IEND", b""))


r = client.post("/api/upload",
                headers={"Authorization": "Bearer " + (TOKEN or "")},
                data={"image": (__import__("io").BytesIO(make_png()), "a.png")},
                content_type="multipart/form-data",
                environ_base=EXT)
j = r.get_json() or {}
check("登录后可上传", r.status_code == 200 and j.get("success"),
      f"HTTP {r.status_code} {j}")
check("返回内容哈希", len(j.get("image_hash") or "") == 64, str(j)[:150])

# ------------------------------------------------------------
print("\n【6】限流（把阈值调低来验证逻辑）")
# ------------------------------------------------------------
appmod.RATE_LIMITS["register"] = (3, 60)
appmod._rate_buckets.clear()
codes = []
for i in range(5):
    rr = client.post("/api/register",
                     json={"username": f"限流{i}", "password": "Test123456",
                           "invite_code": "test-invite"},
                     environ_base={"REMOTE_ADDR": "198.51.100.7"})
    codes.append(rr.status_code)

check("超过阈值后返回 429", 429 in codes, str(codes))
check("前 3 次未被限流", codes[:3] == [200, 200, 200], str(codes))

# 本机直连不受限流影响
local_codes = []
for i in range(5):
    rr = client.post("/api/register",
                     json={"username": f"本机限流{i}", "password": "Test123456"})
    local_codes.append(rr.status_code)
check("本机直连不受限流影响", all(c == 200 for c in local_codes), str(local_codes))

# ------------------------------------------------------------
print("\n【7】管理员接口（用户列表 / 充值 / 下架）")
# ------------------------------------------------------------
r = client.get("/api/admin/session")
j = r.get_json() or {}
check("本机直连自动是管理员", r.status_code == 200 and j.get("admin") is True, str(j))
check("模式为 local", j.get("mode") == "local", str(j))

r = client.get("/api/admin/session", environ_base=EXT)
check("外部来源不是管理员", (r.get_json() or {}).get("admin") is False, r.get_data(as_text=True))

r = client.get("/api/admin/users", environ_base=EXT)
check("外部无密钥看用户列表 → 403", r.status_code == 403, f"HTTP {r.status_code}")
check("错误码 ADMIN_REQUIRED", (r.get_json() or {}).get("code") == "ADMIN_REQUIRED",
      str(r.get_json()))

r = client.get("/api/admin/users", headers={"X-Admin-Key": "test-admin-key"},
               environ_base=EXT)
j = r.get_json() or {}
check("外部带密钥可看用户列表", r.status_code == 200 and j.get("success"), f"HTTP {r.status_code}")
check("列表含平台账户信息", "platform" in j, list(j.keys()))

# 用之前注册的 邀请丙 发布一件商品，供管理员下架
r = client.post("/api/items",
                headers={"Authorization": "Bearer " + (TOKEN or "")},
                data={"name": "待下架商品", "price_eth": "2", "category": "其他",
                      "description": "测试用"},
                content_type="multipart/form-data")
check("测试用户成功上架", r.status_code == 200 and (r.get_json() or {}).get("success"),
      r.get_data(as_text=True)[:150])

r = client.get("/api/admin/items")
items = (r.get_json() or {}).get("items") or []
check("管理员能看到商品列表", len(items) >= 1, str(len(items)))
target = items[0] if items else {}
check("商品带状态字段", "status" in target, str(target)[:120])

# 充值
before = None
r = client.get("/api/admin/users")
for u in (r.get_json() or {}).get("users", []):
    if u["username"] == "邀请丙":
        before = u["balance_eth"]
check("查到充值目标用户", before is not None, str(before))

r = client.post("/api/admin/recharge",
                json={"username": "邀请丙", "amount_eth": "7.5"},
                headers={"X-Admin-Key": "test-admin-key"}, environ_base=EXT)
j = r.get_json() or {}
check("管理员充值成功", r.status_code == 200 and j.get("success"), f"HTTP {r.status_code} {j}")
check("余额确实增加 7.5",
      before is not None and abs((j.get("balance_eth") or 0) - (before + 7.5)) < 0.01,
      f"{before} -> {j.get('balance_eth')}")

r = client.post("/api/admin/recharge",
                json={"username": "邀请丙", "amount_eth": "0"},
                headers={"X-Admin-Key": "test-admin-key"}, environ_base=EXT)
check("充值 0 → 400", r.status_code == 400, f"HTTP {r.status_code}")

r = client.post("/api/admin/recharge",
                json={"username": "不存在的人", "amount_eth": "1"},
                headers={"X-Admin-Key": "test-admin-key"}, environ_base=EXT)
check("给不存在的用户充值 → 404", r.status_code == 404, f"HTTP {r.status_code}")

r = client.post("/api/admin/recharge", json={"username": "邀请丙", "amount_eth": "1"},
                environ_base=EXT)
check("外部无密钥充值 → 403", r.status_code == 403, f"HTTP {r.status_code}")

# 下架
r = client.post("/api/admin/remove_item", json={"item_id": target.get("item_id")},
                headers={"X-Admin-Key": "test-admin-key"}, environ_base=EXT)
j = r.get_json() or {}
check("管理员下架成功", r.status_code == 200 and j.get("success"), f"HTTP {r.status_code} {j}")

r = client.post("/api/admin/remove_item", json={"item_id": target.get("item_id")},
                headers={"X-Admin-Key": "test-admin-key"}, environ_base=EXT)
check("重复下架 → 409", r.status_code == 409, f"HTTP {r.status_code}")
check("错误码 ITEM_NOT_ON_SALE", (r.get_json() or {}).get("code") == "ITEM_NOT_ON_SALE",
      str(r.get_json()))

r = client.get("/api/admin/items")
after = [x for x in ((r.get_json() or {}).get("items") or [])
         if x.get("item_id") == target.get("item_id")]
check("下架后状态变为 3", after and after[0].get("status") == 3, str(after)[:150])

# 审计日志
r = client.get("/api/admin/logs")
logs = (r.get_json() or {}).get("logs") or []
check("写入了审计日志", len(logs) >= 2, str(len(logs)))
check("日志含 recharge 与 remove_item",
      {"recharge", "remove_item"} <= {x.get("action") for x in logs},
      str([x.get("action") for x in logs]))

# ------------------------------------------------------------
print("\n【8】未配置 ADMIN_KEY 时，对外必须一律拒绝（fail closed）")
# ------------------------------------------------------------
appmod.ADMIN_KEY = ""
r = client.post("/api/reset", json={"wipe_credentials": False}, environ_base=EXT)
check("无 ADMIN_KEY + 外网 → 403", r.status_code == 403, f"HTTP {r.status_code}")
check("错误码 RESET_DISABLED", (r.get_json() or {}).get("code") == "RESET_DISABLED",
      str(r.get_json()))

r = client.post("/api/reset", json={"wipe_credentials": False})
check("无 ADMIN_KEY 时本机仍可重置", r.status_code == 200, f"HTTP {r.status_code}")

# ------------------------------------------------------------
print("\n" + "=" * 64)
print(f"  断言：{PASS} 通过 / {FAIL} 失败  （共 {PASS + FAIL} 条）")
if FAILS:
    print("  失败项：")
    for f in FAILS:
        print("    - " + f)
print("=" * 64)

shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if FAIL else 0)

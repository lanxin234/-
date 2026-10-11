"""
持久化回归测试：证明"关掉服务再打开，数据还在"。

为什么必须开子进程
------------------
在同一个进程里重建 AppState 也能读到 SQLite，但那不能证明数据真的
落到了磁盘 —— 万一是内存缓存没清呢？所以每个阶段都开一个全新的
Python 进程，前一个进程彻底退出后再启动下一个，这才是真的"重启"。

覆盖：
  【1】首次启动：建链 + 注册 + 上架 + 购买 + 确认收货 + 评价
  【2】重启（新进程）：以上数据全部还在
  【3】再重启一次：连续重启不会累积错乱
  【4】重置链：数据清空，但账号保留（wipe_credentials=False）
  【5】并发：多线程同时下单不会把链改乱

运行：
  C:/Users/25191/.venvs/base/Scripts/python.exe -X utf8 scripts/test_persistence.py
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable

PASS = 0
FAIL = 0


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {label}")
    else:
        FAIL += 1
        print(f"  ✗ {label}  {extra}")


def run_phase(code: str, data_dir: Path) -> dict:
    """
    在一个全新的 Python 进程里跑一段代码，返回它打印的 JSON 结果。

    每个阶段都是干净进程 —— 前一个进程退出后 OS 会把它的内存全部回收，
    能读回来的东西只可能是从磁盘读的。
    """
    env = dict(os.environ)
    env["MARKET_DATA_DIR"] = str(data_dir)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"

    proc = subprocess.run(
        [PY, "-X", "utf8", "-c", code],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        print("  --- 子进程 stdout ---")
        print(proc.stdout[-3000:])
        print("  --- 子进程 stderr ---")
        print(proc.stderr[-3000:])
        raise RuntimeError(f"子进程失败（exit {proc.returncode}）")

    # 从输出里找 JSON 那一行
    for line in proc.stdout.splitlines():
        line = line.strip()
        if line.startswith("__RESULT__"):
            return json.loads(line[len("__RESULT__"):])
    raise RuntimeError("子进程没输出 __RESULT__")


# ------------------------------------------------------------
#  阶段 1：制造数据
# ------------------------------------------------------------

PHASE1 = r'''
import sys, json
sys.path.insert(0, "backend")
import app

st = app.state
W3 = st.w3

def wei(e): return W3.to_wei(e, "ether")
def eth(w): return float(W3.from_wei(w, "ether"))

# --- 注册两个用户 ---
r1 = st.get_account("alice"); app.chain_tx(st.market.functions.register("阿丽"), r1)
st.set_credentials("alice", "Alice1234"); st.nicknames["alice"] = "阿丽"

r2 = st.get_account("bob");   app.chain_tx(st.market.functions.register("鲍勃"), r2)
st.set_credentials("bob", "Bob12345");   st.nicknames["bob"] = "鲍勃"
st.save_meta()

# --- alice 上架一件商品 ---
desc = app.encode_description("数码电子", "九成新 iPad", "")
app.chain_tx(st.market.functions.listItem("iPad 2025", desc, wei(1.5)), r1)

# --- bob 买下 ---
# 注意：合约里商品 ID 从 1 开始（先 itemCounter++ 再赋值），不是 0
item = st.market.functions.getItem(1).call()
price = item[3]
app.chain_tx(st.market.functions.buyItem(1), r2, value=price)

# --- bob 确认收货 ---
app.chain_tx(st.market.functions.confirmReceived(1), r2)

# --- 互相评价 ---
app.chain_tx(st.market.functions.rate(1, r2, 5), r1)
app.chain_tx(st.market.functions.rate(1, r1, 4), r2)

out = {
    "contract": st.contract_address,
    "accounts": len(st.accounts),
    "blocks": W3.eth.block_number,
    "users": sorted(st.credentials.keys()),
    "nicknames": st.nicknames,
    "alice_addr": r1,
    "bob_addr": r2,
    # getItem 返回顺序：id, name, description, price, seller, buyer, status, createdAt
    "item_name": st.market.functions.getItem(1).call()[1],
    "item_status": int(st.market.functions.getItem(1).call()[6]),
    "alice_bal": eth(W3.eth.get_balance(r1)),
    "bob_bal": eth(W3.eth.get_balance(r2)),
    # getUserInfo 返回 (昵称, 卖出数, 买入数, 平均评分*100)
    "alice_info": list(st.market.functions.getUserInfo(r1).call()),
    "bob_info": list(st.market.functions.getUserInfo(r2).call()),
}
print("__RESULT__" + json.dumps(out))
'''


# ------------------------------------------------------------
#  阶段 2/3：重启后校验
# ------------------------------------------------------------

PHASE_VERIFY = r'''
import sys, json
sys.path.insert(0, "backend")
import app

st = app.state
W3 = st.w3

def eth(w): return float(W3.from_wei(w, "ether"))

item = st.market.functions.getItem(1).call()   # 商品 ID 从 1 开始
r1 = st.addr_of("alice")
r2 = st.addr_of("bob")

out = {
    "contract": st.contract_address,
    "accounts": len(st.accounts),
    "blocks": W3.eth.block_number,
    "users": sorted(st.credentials.keys()),
    "nicknames": st.nicknames,
    "alice_addr": r1,
    "bob_addr": r2,
    "item_name": item[1],
    "item_status": int(item[6]),
    "alice_bal": eth(W3.eth.get_balance(r1)),
    "bob_bal": eth(W3.eth.get_balance(r2)),
    "alice_info": list(st.market.functions.getUserInfo(r1).call()),
    "bob_info": list(st.market.functions.getUserInfo(r2).call()),
    "alice_pwd_ok": st.check_credentials("alice", "Alice1234"),
    "bob_pwd_ok": st.check_credentials("bob", "Bob12345"),
}
print("__RESULT__" + json.dumps(out))
'''


# ------------------------------------------------------------
#  阶段 5：并发下单
# ------------------------------------------------------------

PHASE_CONCURRENT = r'''
import sys, json, threading
sys.path.insert(0, "backend")
import app

st = app.state
W3 = st.w3

# 先准备 8 件商品（串行）
sellers = []
for i in range(8):
    u = f"u{i}"
    a = st.get_account(u)
    app.chain_tx(st.market.functions.register(u), a)
    st.set_credentials(u, "Pass12345")
    sellers.append((u, a))

# 上架 8 件（ID 从 1 开始，所以是 1..8）
for i, (u, a) in enumerate(sellers):
    desc = app.encode_description("图书教材", f"书{i}", "")
    app.chain_tx(st.market.functions.listItem(f"book{i}", desc, W3.to_wei(0.5, "ether")), a)

# 让"另一个用户"并发买下这 8 件，模拟多人同时下单
buyer = st.get_account("buyer")
app.chain_tx(st.market.functions.register("buyer"), buyer)
st.set_credentials("buyer", "Pass12345")

errors = []
def buy(i):
    try:
        p = st.market.functions.getItem(i).call()[3]
        app.chain_tx(st.market.functions.buyItem(i), buyer, value=p)
    except Exception as e:
        errors.append(f"{i}: {type(e).__name__}: {e}")

threads = [threading.Thread(target=buy, args=(i,)) for i in range(1, 9)]
for t in threads: t.start()
for t in threads: t.join()

escrow = 0
for i in range(1, 9):
    if int(st.market.functions.getItem(i).call()[6]) == 1:
        escrow += 1

out = {
    "errors": errors,
    "blocks": W3.eth.block_number,
    "in_escrow": escrow,
    "buyer_bal": float(W3.from_wei(W3.eth.get_balance(buyer), "ether")),
}
print("__RESULT__" + json.dumps(out))
'''


def main():
    tmp = Path(tempfile.mkdtemp(prefix="persist_test_"))
    data_dir = tmp / "data"

    print("=" * 64)
    print("  持久化回归测试（每个阶段都是全新进程）")
    print("=" * 64)
    print(f"\n  临时数据目录：{data_dir}\n")

    try:
        # ---------- 【1】首次启动，制造数据 ----------
        print("【1】首次启动：建链 → 注册 → 上架 → 购买 → 确认 → 评价")
        a = run_phase(PHASE1, data_dir)
        check("合约已部署", bool(a["contract"]), a)
        check("账户数 = 200", a["accounts"] == 200, a["accounts"])
        check("注册了 2 个用户", a["users"] == ["alice", "bob"], a["users"])
        check("商品已上架", a["item_name"] == "iPad 2025", a["item_name"])
        check("交易已完成（status=2）", a["item_status"] == 2, a["item_status"])
        check("alice 余额增加", a["alice_bal"] > 100, a["alice_bal"])
        check("bob 余额减少", a["bob_bal"] < 100, a["bob_bal"])
        print(f"      alice {a['alice_bal']:.4f} ETH / bob {a['bob_bal']:.4f} ETH")
        print(f"      用户信息 alice={a['alice_info']}  bob={a['bob_info']}")
        check("评价已生效（alice 均分 400=4星）", a["alice_info"][3] == 400, a["alice_info"])
        check("评价已生效（bob 均分 500=5星）", a["bob_info"][3] == 500, a["bob_info"])

        snapshot = a

        # ---------- 【2】重启 ----------
        print("\n【2】重启进程：数据应原样还在")
        b = run_phase(PHASE_VERIFY, data_dir)
        check("合约地址不变", b["contract"] == snapshot["contract"],
              f'{b["contract"]} vs {snapshot["contract"]}')
        check("区块高度不变", b["blocks"] == snapshot["blocks"],
              f'{b["blocks"]} vs {snapshot["blocks"]}')
        check("用户账号还在", b["users"] == ["alice", "bob"], b["users"])
        check("昵称还在", b["nicknames"].get("alice") == "阿丽", b["nicknames"])
        check("地址映射还在", b["alice_addr"] == snapshot["alice_addr"], b["alice_addr"])
        check("商品还在", b["item_name"] == "iPad 2025", b["item_name"])
        check("交易状态还是已完成", b["item_status"] == 2, b["item_status"])
        check("余额完全一致", abs(b["alice_bal"] - snapshot["alice_bal"]) < 1e-9,
              f'{b["alice_bal"]} vs {snapshot["alice_bal"]}')
        check("信誉记录还在", b["alice_info"] == snapshot["alice_info"],
              f'{b["alice_info"]} vs {snapshot["alice_info"]}')
        check("密码仍能验证通过", b["alice_pwd_ok"] and b["bob_pwd_ok"], b)

        # ---------- 【3】再重启一次 ----------
        print("\n【3】再重启一次：不应累积错乱")
        c = run_phase(PHASE_VERIFY, data_dir)
        check("合约地址仍不变", c["contract"] == snapshot["contract"])
        check("区块高度仍不变", c["blocks"] == snapshot["blocks"])
        check("余额仍一致", abs(c["bob_bal"] - snapshot["bob_bal"]) < 1e-9,
              f'{c["bob_bal"]} vs {snapshot["bob_bal"]}')

        # ---------- 【4】重置链 ----------
        print("\n【4】重置链（保留账号）")
        code = r'''
import sys, json
sys.path.insert(0, "backend")
import app
st = app.state
before_users = sorted(st.credentials.keys())
before_blocks = st.w3.eth.block_number
st.reset(wipe_credentials=False)
# reset 会清空"用户名→地址"的映射，所以要重新分配一个账户
alice = st.ensure_account("alice")
out = {
    "users_before": before_users,
    "users_after": sorted(st.credentials.keys()),
    "blocks_before": before_blocks,
    "blocks": st.w3.eth.block_number,
    "item_count": st.market.functions.getItemCount().call(),
    "alice_reassigned": alice is not None,
    "alice_bal": float(st.w3.from_wei(st.w3.eth.get_balance(alice), "ether")),
}
print("__RESULT__" + json.dumps(out))
'''
        d = run_phase(code, data_dir)
        check("账号被保留", d["users_after"] == ["alice", "bob"], d["users_after"])
        check("商品已清空", d["item_count"] == 0, d["item_count"])
        check("链回到新状态（区块高度很低）", d["blocks"] < d["blocks_before"],
              f'{d["blocks"]} vs {d["blocks_before"]}')
        check("老用户能自动补回账户", d["alice_reassigned"], d)
        check("余额回到 100", abs(d["alice_bal"] - 100) < 1e-6, d["alice_bal"])

        # ---------- 【5】并发 ----------
        print("\n【5】并发：8 个线程同时下单")
        e = run_phase(PHASE_CONCURRENT, data_dir)
        check("并发下单无异常", e["errors"] == [], e["errors"])
        check("8 件全部进入托管", e["in_escrow"] == 8, e["in_escrow"])
        check("买家余额正确扣减", abs(e["buyer_bal"] - (100 - 4.0)) < 0.01,
              e["buyer_bal"])
        print(f"      买家余额 {e['buyer_bal']:.4f} ETH（期望约 96）")

    except Exception as exc:
        global FAIL
        FAIL += 1
        print(f"\n  ✗ 测试中断：{type(exc).__name__}: {exc}")
        import traceback
        traceback.print_exc()

    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    print("=" * 64)
    print(f"  结果：{PASS} 通过，{FAIL} 失败")
    print("=" * 64)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())

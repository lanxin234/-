"""
探针：验证"重新打开已有 SQLite 链库"会不会把链头重置回创世。

这一步决定整个持久化方案是否成立：
  如果 from_genesis 会把 canonical head 拉回 0，那链虽然落盘了，
  重启后却读不到任何区块，持久化等于白做。

同时验证：创世状态里直接写 100 ETH 是否生效（可借此删掉
app.py 里那段 py-evm 底层状态改写的 hack）。
"""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

import chaindb  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="chaindb_probe_"))
DB = TMP / "chain.sqlite3"

NUM_ACCOUNTS = 20
INITIAL_ETH = 100

chaindb.set_db_path(DB)
chaindb.install()


def make_w3():
    """用 SQLite 后端建一条链（创世即 100 ETH）"""
    from eth_tester import EthereumTester, PyEVMBackend
    from eth_tester.backends.pyevm.main import (
        generate_genesis_state_for_keys,
        get_default_account_keys,
    )
    from web3 import Web3
    from web3.providers.eth_tester import EthereumTesterProvider

    keys = get_default_account_keys(quantity=NUM_ACCOUNTS)
    gs = generate_genesis_state_for_keys(
        keys, overrides={"balance": 100 * 10**18}
    )
    backend = PyEVMBackend(genesis_state=gs)
    return Web3(EthereumTesterProvider(EthereumTester(backend)))


def main():
    print("=" * 64)
    print("【1】首次建链")
    print("=" * 64)
    w3 = make_w3()
    accts = w3.eth.accounts
    print(f"  账户数 = {len(accts)}（期望 {NUM_ACCOUNTS}）")
    bal0 = float(w3.from_wei(w3.eth.get_balance(accts[0]), "ether"))
    print(f"  账户0 余额 = {bal0} ETH（期望 {INITIAL_ETH}）")
    print(f"  区块高度 = {w3.eth.block_number}")

    print("\n" + "=" * 64)
    print("【2】制造状态：转账 + 出块")
    print("=" * 64)
    a, b = accts[0], accts[1]
    tx = w3.eth.send_transaction({"from": a, "to": b, "value": w3.to_wei(7, "ether")})
    w3.eth.wait_for_transaction_receipt(tx)
    bal_b = float(w3.from_wei(w3.eth.get_balance(b), "ether"))
    blk = w3.eth.block_number
    print(f"  b 余额 = {bal_b} ETH（期望 {INITIAL_ETH + 7}）")
    print(f"  区块高度 = {blk}")

    # 再部署一个合约，制造合约存储 + 事件
    print("\n  （跳过合约部署，先验证基础状态）")

    # 主动释放连接，模拟进程退出
    print("\n  模拟进程退出（丢弃 w3 对象）...")
    del w3

    print("\n" + "=" * 64)
    print("【3】重新打开同一个 SQLite 库")
    print("=" * 64)
    w3b = make_w3()
    accts_b = w3b.eth.accounts
    blk_b = w3b.eth.block_number
    bal_b2 = float(w3b.from_wei(w3b.eth.get_balance(accts_b[1]), "ether"))
    print(f"  账户数 = {len(accts_b)}（期望 {NUM_ACCOUNTS}）")
    print(f"  区块高度 = {blk_b}（期望 {blk}，若为 0 则链头被重置）")
    print(f"  b 余额 = {bal_b2} ETH（期望 {bal_b}）")

    print("\n" + "=" * 64)
    print("结论")
    print("=" * 64)
    ok_head = blk_b == blk
    ok_bal = abs(bal_b2 - bal_b) < 1e-9
    ok_acct = len(accts_b) == NUM_ACCOUNTS
    print(f"  链头保住        : {'✓' if ok_head else '✗ 链头被重置回创世'}")
    print(f"  余额保住        : {'✓' if ok_bal else '✗'}")
    print(f"  账户数正确      : {'✓' if ok_acct else '✗'}")

    if ok_head and ok_bal and ok_acct:
        print("\n  → 持久化方案成立：重新打开库即可原样续用")
    else:
        print("\n  → 需要换思路：不能用 PyEVMBackend(genesis_state=...) 重开")

    print(f"\n  临时目录：{TMP}")


if __name__ == "__main__":
    main()

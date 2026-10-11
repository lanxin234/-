"""
合约编译与部署脚本

功能：
  1. 读取 contracts/SecondHandMarket.sol
  2. 用 solc 0.8.19 编译
  3. 部署到一个本地测试链
  4. 把合约地址和 ABI 保存下来，供后端和前端使用

运行：
  C:/Users/25191/.venvs/base/Scripts/python.exe scripts/deploy.py
"""

import json
import sys
from pathlib import Path

from solcx import compile_standard, install_solc, get_installed_solc_versions

# ============ 路径 ============
ROOT = Path(__file__).resolve().parent.parent
CONTRACT_FILE = ROOT / "contracts" / "SecondHandMarket.sol"
BUILD_DIR = ROOT / "build"
BUILD_DIR.mkdir(exist_ok=True)

SOLC_VERSION = "0.8.19"


def ensure_solc():
    """确保编译器已安装"""
    installed = [str(v) for v in get_installed_solc_versions()]
    if SOLC_VERSION not in installed:
        print(f"  正在安装 solc {SOLC_VERSION} ...")
        install_solc(SOLC_VERSION)
    print(f"  编译器就绪：solc {SOLC_VERSION}")


def compile_contract():
    """编译合约，返回 abi 和 bytecode"""
    print()
    print("=" * 60)
    print("第 1 步：编译合约")
    print("=" * 60)

    ensure_solc()

    source = CONTRACT_FILE.read_text(encoding="utf-8")

    compiled = compile_standard(
        {
            "language": "Solidity",
            "sources": {"SecondHandMarket.sol": {"content": source}},
            "settings": {
                "outputSelection": {
                    "*": {"*": ["abi", "evm.bytecode.object"]}
                },
                "optimizer": {"enabled": True, "runs": 200},
            },
        },
        solc_version=SOLC_VERSION,
    )

    contract_data = compiled["contracts"]["SecondHandMarket.sol"]["SecondHandMarket"]
    abi = contract_data["abi"]
    bytecode = contract_data["evm"]["bytecode"]["object"]

    print(f"  ✓ 编译成功")
    print(f"    ABI 条目数：{len(abi)}")
    print(f"    字节码长度：{len(bytecode)} 字符")

    # 保存编译产物
    artifact = {"abi": abi, "bytecode": bytecode, "solc": SOLC_VERSION}
    out = BUILD_DIR / "SecondHandMarket.json"
    out.write_text(json.dumps(artifact, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"    产物已保存：{out}")

    return abi, bytecode


def main():
    print("=" * 60)
    print("  区块链二手交易合约 —— 编译与部署")
    print("=" * 60)

    if not CONTRACT_FILE.exists():
        print(f"【错误】找不到合约文件：{CONTRACT_FILE}")
        sys.exit(1)

    abi, bytecode = compile_contract()

    print()
    print("=" * 60)
    print("第 2 步：部署")
    print("=" * 60)
    print()
    print("  使用本地测试链部署（不依赖 Ganache，无需联网）")
    print()

    # 用 eth-tester 内存链部署
    try:
        from web3 import Web3
        from web3.providers.eth_tester import EthereumTesterProvider
    except ImportError as e:
        print(f"【错误】缺少 eth-tester：{e}")
        print("请运行：")
        print('  export VIRTUAL_ENV="C:/Users/25191/.venvs/base"')
        print('  "C:/Users/25191/.local/bin/uv.exe" pip install "web3[tester]"')
        sys.exit(1)

    w3 = Web3(EthereumTesterProvider())

    if not w3.is_connected():
        print("【错误】测试链连接失败")
        sys.exit(1)

    print(f"  ✓ 测试链已启动")

    accounts = w3.eth.accounts
    print(f"    账户数量：{len(accounts)}")
    print(f"    测试账户示例：{accounts[0]}")

    balance = w3.eth.get_balance(accounts[0])
    print(f"    账户余额：{w3.from_wei(balance, 'ether')} ETH")

    # 部署
    print()
    print("  正在部署合约...")
    contract = w3.eth.contract(abi=abi, bytecode=bytecode)

    tx_hash = contract.constructor().transact({"from": accounts[0]})
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash)

    contract_address = receipt.contractAddress
    print(f"  ✓ 部署成功")
    print(f"    合约地址：{contract_address}")
    print(f"    交易哈希：{tx_hash.hex()}")
    print(f"    消耗 Gas：{receipt.gasUsed}")

    # 保存部署信息
    deploy_info = {
        "contract_address": contract_address,
        "deployer": accounts[0],
        "tx_hash": tx_hash.hex(),
        "gas_used": receipt.gasUsed,
        "solc_version": SOLC_VERSION,
    }
    out = BUILD_DIR / "deployment.json"
    out.write_text(json.dumps(deploy_info, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"    部署信息已保存：{out}")

    print()
    print("=" * 60)
    print("  ✓ 全部完成")
    print("=" * 60)
    print()
    print("说明：这是「内存测试链」，进程结束数据就没了。")
    print("      目的是验证合约能编译、能部署、逻辑正确。")
    print("      要持久化运行请看 README 的「启动完整系统」章节。")


if __name__ == "__main__":
    main()

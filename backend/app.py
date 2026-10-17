"""
区块链二手交易平台 —— 后端服务

技术：Flask + web3.py + py-evm（链数据落盘到 SQLite）
职责：
  1. 启动时编译部署合约到本地链（已有链数据则直接续用）
  2. 提供 REST API 供前端调用
  3. 管理链上账户（每个注册用户分配一个）

运行：
  C:/Users/25191/.venvs/base/Scripts/python.exe backend/app.py

然后浏览器打开 http://127.0.0.1:5000
同一局域网内的其他设备用「本机局域网 IP:5000」访问。

持久化：
  链（区块/交易/事件日志/合约存储）→ data/chain.sqlite3
  业务数据（账号、昵称、用户↔地址映射）→ data/app_state.json
  两者都在，重启后数据原样续用；删掉它们等于恢复出厂。
"""

import json
import os
import sys
import time
import hmac
import socket
import hashlib
import threading
import re
from collections import deque
from decimal import Decimal, InvalidOperation
from functools import wraps
from pathlib import Path
from datetime import datetime

from flask import Flask, jsonify, request, send_from_directory
from web3 import Web3
from web3.providers.eth_tester import EthereumTesterProvider
from solcx import compile_standard, install_solc, get_installed_solc_versions

import auth
import chaindb

# ============================================================
#  路径配置
# ============================================================

ROOT = Path(__file__).resolve().parent.parent
CONTRACT_FILE = ROOT / "contracts" / "SecondHandMarket.sol"
UPLOAD_DIR = ROOT / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)
FRONTEND_DIR = ROOT / "frontend"
BUILD_DIR = ROOT / "build"
SOLC_VERSION = "0.8.19"

# 运行时数据目录（链数据库 + 业务快照）
# 可用环境变量 MARKET_DATA_DIR 整体挪走（自动化测试需要隔离）
DATA_DIR = Path(os.environ.get("MARKET_DATA_DIR") or (ROOT / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE = DATA_DIR / "app_state.json"

# ============================================================
#  对外暴露时的安全开关（局域网/公网部署才需要）
# ============================================================
# 为什么要有这一层：
#   本机开发时一切从 127.0.0.1 来，方便；一旦通过 Cloudflare Tunnel /
#   反向代理暴露出去，**所有外部请求在服务看来都来自 127.0.0.1**
#   （cloudflared 是本地进程，转发的连接源地址就是环回地址）。
#   如果只看 remote_addr，就等于把外网当成本机，重置接口会裸奔。
#   所以必须先看代理头：只要带了转发头，就一律视为"外部请求"。

ADMIN_KEY = (os.environ.get("ADMIN_KEY") or "").strip()
INVITE_CODE = (os.environ.get("INVITE_CODE") or "").strip()

# 出现任意一个，就说明请求不是"直连本机"，而是经过了代理/隧道
PROXY_HEADERS = (
    "CF-Connecting-IP", "CF-Ray", "X-Forwarded-For", "X-Real-IP", "Forwarded",
)

# 限流：滑动窗口，按「客户端 IP + 动作」计数
RATE_LIMITS = {
    "register": (30, 60),   # 30 次 / 分钟
    "login":    (60, 60),   # 60 次 / 分钟（整班同时登录也能过）
}
_rate_lock = threading.Lock()
_rate_buckets = {}


def client_ip():
    """取真实客户端 IP。经过隧道时用 Cloudflare 提供的头，否则用连接地址。"""
    for h in ("CF-Connecting-IP", "X-Real-IP"):
        v = request.headers.get(h)
        if v:
            return v.strip()
    xff = request.headers.get("X-Forwarded-For")
    if xff:
        return xff.split(",")[0].strip()
    return request.remote_addr or ""


def is_local_direct():
    """
    是否"就在本机、且没经过任何代理"。

    只有这种请求才享受免密钥/免限流/免邀请码的待遇 —— 它等价于
    "操作者已经坐在这台电脑前"，本来就能改 data/ 目录，谈不上越权。
    自动化测试全部走 127.0.0.1 直连，所以不受下面这些限制影响。
    """
    for h in PROXY_HEADERS:
        if request.headers.get(h):
            return False
    return (request.remote_addr or "") in ("127.0.0.1", "::1")


def rate_limited(scope: str):
    """返回需要等待的秒数；未被限流返回 None。本机直连不限。"""
    if is_local_direct():
        return None
    limit, window = RATE_LIMITS.get(scope, (60, 60))
    key = f"{scope}:{client_ip() or 'unknown'}"
    now = time.time()
    with _rate_lock:
        # 桶太多时整体清一次，避免被大量伪造 IP 撑爆内存
        if len(_rate_buckets) > 10000:
            _rate_buckets.clear()
        q = _rate_buckets.setdefault(key, deque())
        while q and now - q[0] > window:
            q.popleft()
        if len(q) >= limit:
            return max(1, int(window - (now - q[0])) + 1)
        q.append(now)
    return None


# ============================================================
#  中文错误映射
# ============================================================

ERROR_MAP = {
    "Please register first": "请先注册账号",
    "Item does not exist": "商品不存在",
    "Already registered": "你已经注册过了",
    "Nickname cannot be empty": "昵称不能为空",
    "Nickname too long (max 20)": "昵称最长20个字符",
    "Item name cannot be empty": "商品名称不能为空",
    "Item name too long (max 50)": "商品名称最长50个字符",
    "Price must be greater than 0": "价格必须大于0",
    "Item is not on sale": "该商品不在售",
    "Cannot buy your own item": "不能购买自己发布的商品",
    "Payment amount does not match price": "付款金额与标价不符",
    "Item is not in escrow": "该商品不在托管状态",
    "Only the buyer can confirm receipt": "只有买家本人能确认收货",
    "Only the seller can remove the item": "只有卖家本人能下架",
    "Only items on sale can be removed": "只有在售商品能下架",
    "Can only rate after the deal is completed": "交易完成后才能评价",
    "Score must be between 1 and 5": "评分必须在1-5之间",
    "Only the two parties can rate": "只有交易双方能评价",
    "Can only rate the counterparty": "只能评价交易对手",
    "Cannot rate yourself": "不能评价自己",
    "You have already rated": "你已经评价过了",
    "Only the platform owner can modify": "只有平台方可以修改",
    "Platform fee cannot exceed 5%": "手续费不能超过5%",
}


def translate(msg: str) -> str:
    """合约英文报错 → 中文"""
    text = str(msg)
    for en, zh in ERROR_MAP.items():
        if en in text:
            return zh
    # 没匹配到的，截取 revert 原因
    if "execution reverted" in text:
        return "交易被合约拒绝"
    return text[:200]


def friendly_error(e: Exception) -> str:
    """提取人类可读的错误信息"""
    return translate(e)


# ============================================================
#  商品分类 + 图片哈希
# ============================================================
# 链上合约存的是纯文本，为了不修改合约，把附加信息编码在描述里。
# 格式：[分类]分类名|图片哈希|实际描述
# 读取时解析出来。这是"在不改协议的前提下扩展字段"的常见技巧。

CATEGORIES = ["数码电子", "图书教材", "生活用品", "运动户外", "服饰鞋包", "其他"]
CATEGORY_PREFIX = "[分类]"

# 每个链上账户的初始余额，直接写进创世状态。
# eth-tester 默认给 100 万 ETH，读数太长不便演示；商品价格区间是
# 0.05 ~ 3.2 ETH，100 ETH 足够买卖几十次。
INITIAL_BALANCE_ETH = 100

# 链上账户总数：第 1 个留给平台方，其余分配给注册用户。
# 之前固定 10 个（只能注册 9 人），局域网给同学用根本不够，
# 提到 200（199 位用户）。可用环境变量 CHAIN_ACCOUNTS 覆盖。
#
# 注意：改这个数字只对新链生效。已有链要改的话，删掉 data/ 目录即可。
NUM_CHAIN_ACCOUNTS = int(os.environ.get("CHAIN_ACCOUNTS") or 200)

# 链本身不是线程安全的（chain.header、mine_block 都在改共享状态）。
# waitress 是多线程的，两个人同时下单就可能把链改乱，
# 所以所有上链操作统一走这把锁串行化。
CHAIN_LOCK = threading.RLock()


def encode_description(category: str, description: str, image_hash: str = "") -> str:
    """把分类和图片哈希编码进描述"""
    cat = category if category in CATEGORIES else "其他"
    return f"{CATEGORY_PREFIX}{cat}|{image_hash}|{description}"


def decode_description(raw: str):
    """
    从描述里解析出 (分类, 图片哈希, 纯描述)
    兼容三种格式：
      [分类]分类名|哈希|描述     ← 新版
      [分类]分类名|描述          ← 上一版
      描述                       ← 最早版本
    """
    if raw and raw.startswith(CATEGORY_PREFIX):
        rest = raw[len(CATEGORY_PREFIX):]
        parts = rest.split("|", 2)
        if len(parts) == 3:
            return parts[0], parts[1], parts[2]
        if len(parts) == 2:
            return parts[0], "", parts[1]
        return rest, "", ""
    return guess_category(raw or ""), "", (raw or "")


def guess_category(text: str) -> str:
    """给没有分类的旧商品猜分类"""
    rules = [
        ("数码电子", ["手机", "电脑", "耳机", "键盘", "鼠标", "iPad", "相机", "充电", "音箱", "显示器", "平板"]),
        ("图书教材", ["书", "教材", "笔记", "试卷", "复习", "题", "英文", "词典"]),
        ("生活用品", ["台灯", "水杯", "床", "桌", "椅", "收纳", "雨伞", "箱"]),
        ("运动户外", ["球", "拍", "跑鞋", "健身", "瑜伽", "单车", "帐篷"]),
        ("服饰鞋包", ["衣", "裤", "鞋", "包", "帽", "外套", "T恤"]),
    ]
    for cat, kws in rules:
        if any(k in text for k in kws):
            return cat
    return "其他"


# ============================================================
#  图片存储（内容寻址，模拟 IPFS）
# ============================================================
# 真正的 IPFS 用文件内容的哈希作为地址，保证内容不可篡改。
# 这里用同样的原理：SHA-256(文件内容) 作为文件名。
# 好处：
#   1. 同一张图重复上传只存一份（天然去重）
#   2. 哈希写进区块链后，任何人改动图片都会导致哈希对不上
#   3. 原理与 IPFS 一致，只是不做分布式网络那部分

ALLOWED_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
MAX_IMAGE_SIZE = 5 * 1024 * 1024   # 5MB


def save_image(file_storage):
    """
    保存上传的图片，返回 (哈希, 文件名, 错误信息)
    哈希 = 文件内容的 SHA-256，作为内容指纹
    """
    filename = file_storage.filename or ""
    ext = Path(filename).suffix.lower()

    if ext not in ALLOWED_EXT:
        return None, None, f"不支持的图片格式（{ext or '未知'}），请上传 jpg/png/gif/webp"

    data = file_storage.read()
    if not data:
        return None, None, "图片文件为空"
    if len(data) > MAX_IMAGE_SIZE:
        return None, None, f"图片过大（{len(data)/1024/1024:.1f}MB），请压缩到 5MB 以内"

    # 内容寻址：哈希由文件内容算出
    digest = hashlib.sha256(data).hexdigest()
    stored_name = digest + ext
    target = UPLOAD_DIR / stored_name

    if not target.exists():
        target.write_bytes(data)

    return digest, stored_name, None


def image_url(image_hash: str):
    """哈希 → 可访问的 URL"""
    if not image_hash:
        return ""
    for ext in ALLOWED_EXT:
        if (UPLOAD_DIR / (image_hash + ext)).exists():
            return f"/uploads/{image_hash}{ext}"
    return ""


def relative_time(ts: int) -> str:
    """时间戳 → 相对时间描述"""
    import time as _time
    diff = int(_time.time()) - int(ts)
    if diff < 60:
        return "刚刚"
    if diff < 3600:
        return f"{diff // 60} 分钟前"
    if diff < 86400:
        return f"{diff // 3600} 小时前"
    if diff < 86400 * 30:
        return f"{diff // 86400} 天前"
    return f"{diff // 86400 // 30} 个月前"


def full_time(ts: int) -> str:
    """时间戳 → 完整时间"""
    return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M:%S")


# ============================================================
#  编译与部署
# ============================================================

def ensure_solc():
    installed = [str(v) for v in get_installed_solc_versions()]
    if SOLC_VERSION not in installed:
        print(f"  正在安装 solc {SOLC_VERSION} ...")
        install_solc(SOLC_VERSION)


def compile_contract():
    """编译合约，返回 abi 和 bytecode"""
    ensure_solc()
    source = CONTRACT_FILE.read_text(encoding="utf-8")
    compiled = compile_standard(
        {
            "language": "Solidity",
            "sources": {"SecondHandMarket.sol": {"content": source}},
            "settings": {
                "outputSelection": {"*": {"*": ["abi", "evm.bytecode.object"]}},
                "optimizer": {"enabled": True, "runs": 200},
            },
        },
        solc_version=SOLC_VERSION,
    )
    data = compiled["contracts"]["SecondHandMarket.sol"]["SecondHandMarket"]
    return data["abi"], data["evm"]["bytecode"]["object"]


# ============================================================
#  全局状态（内存链，进程结束即重置）
# ============================================================

class AppState:
    def __init__(self):
        self.w3 = None
        self.abi = None
        self.market = None
        self.accounts = []
        self.contract_address = None
        # 用户名 → 账户地址的映射（模拟登录）
        self.user_sessions = {}
        # 用户名 → 昵称
        self.nicknames = {}
        # 用户名 → 密码哈希（scrypt，绝不存明文）
        self.credentials = {}
        # 已登出、但还没过期的 token 黑名单：{token: 过期时间戳}
        # 记过期时间是为了能自动清理 —— 过了期自然就无效了，没必要一直存着。
        self.revoked_tokens = {}
        self.boot()

    # ============================================================
    #  启动：有数据就续用，没有就新建
    # ============================================================

    def boot(self):
        """
        启动时决定"续用旧链"还是"新建一条链"。

        链数据落盘后，重启不该从头开始 —— 否则"持久化"就没意义了。
        判断依据有两个，缺一不可：
          1. data/chain.sqlite3 存在（链本体在）
          2. data/app_state.json 里记着合约地址（知道该连哪个合约）
        """
        chaindb.install()          # 必须在建链之前把后端切成 SQLite

        if not chaindb.db_exists():
            print("  未发现链数据，正在创建一条新链...")
            self.init_chain()
            return

        meta = self._load_meta()
        if not meta:
            print("  ! 链数据存在但缺少元信息，重新初始化")
            self.init_chain()
            return

        try:
            self._attach(meta)
        except Exception as e:
            print(f"  ! 加载已有链失败（{e}），回退为重新初始化")
            self.init_chain()

    def _attach(self, meta: dict):
        """重开已有链：复用保存下来的合约地址，不重新部署"""
        self.abi, _ = compile_contract()
        self.w3 = self._build_w3()
        self.accounts = self.w3.eth.accounts

        self.contract_address = meta["contract_address"]
        self.market = self.w3.eth.contract(address=self.contract_address, abi=self.abi)

        self.credentials = dict(meta.get("credentials") or {})
        self.nicknames = dict(meta.get("nicknames") or {})
        self.user_sessions = dict(meta.get("user_sessions") or {})

        # 已登出令牌的黑名单。顺手丢掉已经自然过期的 —— 到点了本来就无效，
        # 留着只会让文件越来越大。
        now = int(__import__("time").time())
        self.revoked_tokens = {
            t: exp for t, exp in (meta.get("revoked_tokens") or {}).items()
            if not exp or exp > now
        }

        # 快照里的地址必须在链上确实存在，否则是脏数据
        valid = set(self.accounts)
        for u in list(self.user_sessions):
            if self.user_sessions[u] not in valid:
                del self.user_sessions[u]

        # 确认合约还在（链库和元信息对不上时直接报错，让 boot 回退重建）
        code = self.w3.eth.get_code(self.contract_address)
        if not code or len(code) == 0:
            raise RuntimeError(f"合约地址 {self.contract_address} 上没有代码")

        print(f"  ✓ 已加载已有链：{len(self.accounts)} 个账户，"
              f"区块高度 {self.w3.eth.block_number}")
        print(f"  ✓ 合约地址：{self.contract_address}")
        print(f"  ✓ 已注册用户 {len(self.credentials)} 位")

    def init_chain(self, wipe_credentials: bool = False):
        """新建一条链（会删掉旧的链数据，回到出厂状态）。

        参数:
          wipe_credentials -- 是否同时清空账号密码。
            - False（默认）：保留 credentials。链重置后老账号能直接登录，
              不用重新注册。代价是用户名仍被占用。
            - True：连账号一起清空，回到完全干净的状态。自动化测试脚本
              需要反复用同一批用户名注册时用这个。
        """
        chaindb.clear_db()

        self.user_sessions = {}
        self.nicknames = {}
        self.revoked_tokens = {}
        if wipe_credentials:
            self.credentials = {}
            auth.clear_all_failures()
            # 签名密钥也换掉，让所有旧 token 立即失效
            auth.rotate_secret()
            print("  ✓ 已清空全部账号密码（wipe_credentials=True）")
        # 默认不动 credentials，否则链重置后所有人都登不上了

        print("  正在编译合约...")
        self.abi, bytecode = compile_contract()
        print("  ✓ 编译完成")

        print(f"  正在创建链（{NUM_CHAIN_ACCOUNTS} 个账户，数据落盘）...")
        self.w3 = self._build_w3()
        self.accounts = self.w3.eth.accounts
        print(f"  ✓ 链已创建：{len(self.accounts)} 个账户，"
              f"每个初始 {INITIAL_BALANCE_ETH:g} ETH")

        print("  正在部署合约...")
        contract = self.w3.eth.contract(abi=self.abi, bytecode=bytecode)
        tx = contract.constructor().transact({"from": self.accounts[0]})
        receipt = self.w3.eth.wait_for_transaction_receipt(tx)
        self.contract_address = receipt.contractAddress
        self.market = self.w3.eth.contract(address=self.contract_address, abi=self.abi)
        print(f"  ✓ 合约已部署：{self.contract_address}")

        self.save_meta()

    def _build_w3(self):
        """
        建（或重开）一条 SQLite 落盘的链。

        初始余额直接写进创世状态 —— 这样就不必像早期版本那样，
        建完链再去改 py-evm 的底层账户 trie（那套 hack 依赖内部实现，
        换个 py-evm 版本就可能失效）。

        重开时传入同样的 genesis_state 是安全的：创世块是确定性生成的，
        重复写入不会把链头拉回 0（已实测验证）。
        """
        from eth_tester import EthereumTester, PyEVMBackend
        from eth_tester.backends.pyevm.main import (
            generate_genesis_state_for_keys,
            get_default_account_keys,
        )

        keys = get_default_account_keys(quantity=NUM_CHAIN_ACCOUNTS)
        genesis_state = generate_genesis_state_for_keys(
            keys, overrides={"balance": INITIAL_BALANCE_ETH * 10**18}
        )
        backend = PyEVMBackend(genesis_state=genesis_state)
        return Web3(EthereumTesterProvider(EthereumTester(backend)))

    def reset(self, wipe_credentials: bool = False):
        """重置整条链，回到初始状态（= 删数据 + 新建）"""
        self.init_chain(wipe_credentials=wipe_credentials)

    # ============================================================
    #  业务数据落盘
    # ============================================================

    def _load_meta(self):
        """读取业务快照；不存在或损坏则返回 None"""
        try:
            if not STATE_FILE.exists():
                return None
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                meta = json.load(f)
            if not meta.get("contract_address"):
                return None
            return meta
        except Exception as e:
            print(f"  ! 读取 {STATE_FILE.name} 失败（{e}），将重新初始化")
            return None

    def save_meta(self):
        """
        把业务侧数据落盘：账号哈希、昵称、用户↔地址映射、合约地址。

        写临时文件再原子替换 —— 避免写一半崩掉留下半个 JSON，
        那样下次启动就读不出来、整条链白建了。
        """
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            meta = {
                "contract_address": self.contract_address,
                "credentials": self.credentials,
                "nicknames": self.nicknames,
                "user_sessions": self.user_sessions,
                # 已登出的令牌黑名单（含过期时间，加载时会自动清理过期项）
                "revoked_tokens": self.revoked_tokens,
                "num_accounts": len(self.accounts),
                "saved_at": datetime.now().isoformat(timespec="seconds"),
            }
            tmp = STATE_FILE.with_suffix(".json.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False, indent=2)
            tmp.replace(STATE_FILE)
        except Exception as e:
            print(f"  ! 保存 {STATE_FILE.name} 失败：{e}")

    # ============================================================
    #  账户分配
    # ============================================================

    @property
    def max_users(self) -> int:
        """最多能注册多少用户（第 1 个账户留给平台方）"""
        return max(0, len(self.accounts) - 1)

    def get_account(self, username: str) -> str:
        """根据用户名获取账户地址（用户名 → 分配一个链上账户）"""
        if username not in self.user_sessions:
            # 按注册顺序分配账户，跳过 accounts[0]（平台账户）
            idx = len(self.user_sessions) + 1
            if idx >= len(self.accounts):
                raise ValueError(f"链上账户已用完（本链最多 {self.max_users} 位用户）")
            self.user_sessions[username] = self.accounts[idx]
        return self.user_sessions[username]

    def addr_of(self, username: str):
        return self.user_sessions.get(username)

    def ensure_account(self, username: str):
        """
        确保用户名有一个可用的链上账户，返回地址；分配不到返回 None。

        存在的意义：链重置会清空 user_sessions（用户名 → 地址的映射），
        但故意保留 credentials（账号密码）。这就造成一种尴尬状态 ——
        用户令牌有效、账号也还在，却因为没有链上地址而处处报"登录已失效"。

        调用本方法会自动补发账户，让"重置链之后刷新页面即可继续用"成立，
        而不是逼用户重新登录一次。
        """
        addr = self.user_sessions.get(username)
        if addr:
            return addr
        try:
            return self.get_account(username)
        except ValueError:
            # 账户池满了
            return None

    # ---- 认证相关 ----

    def is_registered(self, username: str) -> bool:
        """是否已注册（有密码凭据即视为已注册）"""
        return username in self.credentials

    def revoke_token(self, token: str):
        """
        把 token 加进黑名单（登出时调用）。

        连带记下它的过期时间：到点了就没必要再留着，
        否则 30 天期的「记住我」令牌会让这个名单无限增长。
        """
        self.revoked_tokens[token] = auth.token_expiry(token)
        self.save_meta()
        self._prune_revoked()

    def _prune_revoked(self):
        """清掉黑名单里已经自然过期的条目"""
        now = int(__import__("time").time())
        stale = [t for t, exp in self.revoked_tokens.items() if exp and exp <= now]
        for t in stale:
            del self.revoked_tokens[t]

    def set_credentials(self, username: str, password: str):
        self.credentials[username] = auth.hash_password(password)
        self.save_meta()      # 立刻落盘，避免重启后账号"消失"

    def check_credentials(self, username: str, password: str) -> bool:
        stored = self.credentials.get(username)
        if not stored:
            return False
        return auth.verify_password(password, stored)

    def display_name(self, username: str) -> str:
        return self.nicknames.get(username, username)


state = AppState()


def chain_tx(contract_call, from_addr: str, value: int = None) -> dict:
    """
    发送一笔链上交易并等回执。

    所有写操作都必须走这里，原因有两个：
      1. 链不是线程安全的。waitress 多线程，两个人同时下单会让
         chain.header 和 mine_block 打架，状态直接错乱。
      2. 统一在这里等回执，调用方拿到的就是已确认的结果。

    value -- 随交易发送的 ETH（买家付款时用）
    """
    with CHAIN_LOCK:
        params = {"from": from_addr}
        if value is not None:
            params["value"] = value
        tx = contract_call.transact(params)
        return state.w3.eth.wait_for_transaction_receipt(tx)


# ============================================================
#  Flask 应用
# ============================================================

app = Flask(__name__, static_folder=None)


# ============================================================
#  认证：装饰器 & 辅助函数
# ============================================================

def extract_token():
    """
    从请求里取出 token。支持两种方式：
      1. Authorization: Bearer <token>   ← 推荐
      2. 请求体/表单里的 token 字段        ← 兼容老前端
    """
    hdr = request.headers.get("Authorization") or ""
    if hdr.lower().startswith("bearer "):
        return hdr[7:].strip()

    if request.method == "POST":
        if request.content_type and request.content_type.startswith("multipart/form-data"):
            return (request.form.get("token") or "").strip()
        data = request.get_json(silent=True) or {}
        return (data.get("token") or "").strip()

    return (request.args.get("token") or "").strip()


def current_user():
    """返回当前登录用户名，未登录返回 None"""
    token = extract_token()
    username, _ = auth.verify_token(token)
    if not username:
        return None
    if token in state.revoked_tokens:
        return None
    return username


def require_auth(view):
    """
    需要登录才能访问的接口。
    校验通过后把用户名放进 request，业务代码用 get_auth_user() 取。

    两种失败情形要分开处理（前端提示语不一样）：
      - 令牌本身无效（签名错/过期/已登出）→ AUTH_REQUIRED，必须重新登录
      - 令牌有效但账号没了（比如令牌签发后被清空了账号）→ AUTH_REQUIRED
      - 令牌有效、账号也在，只是链重置后丢了地址映射 → 自动补发，不打扰用户
    """
    @wraps(view)
    def wrapper(*args, **kwargs):
        token = extract_token()
        username, remain = auth.verify_token(token)

        if not username or token in state.revoked_tokens:
            return jsonify({
                "success": False,
                "error": "登录已失效，请重新登录",
                "code": "AUTH_REQUIRED",
            }), 401

        if not state.is_registered(username):
            return jsonify({
                "success": False,
                "error": "账号不存在，请重新注册",
                "code": "AUTH_REQUIRED",
            }), 401

        # 令牌没问题，但可能因为链重置丢了地址映射 —— 自动补一个
        if state.addr_of(username) is None and state.ensure_account(username) is None:
            return jsonify({
                "success": False,
                "error": f"链上账户已用完（本链最多 {state.max_users} 位用户）。请重置整条链后再试",
                "code": "ACCOUNT_POOL_FULL",
            }), 503

        request.auth_user = username
        resp = view(*args, **kwargs)

        # 快过期时顺便续期：在响应头里带一个新 token
        if auth.should_renew(remain) and hasattr(resp, "headers"):
            resp.headers["X-Renewed-Token"] = auth.create_token(username)
        return resp
    return wrapper


def get_auth_user():
    """取当前请求的登录用户（由 require_auth 注入）"""
    return getattr(request, "auth_user", None)


@app.route("/")
def index():
    return send_from_directory(FRONTEND_DIR, "index.html")


@app.route("/<path:filename>")
def static_files(filename):
    return send_from_directory(FRONTEND_DIR, filename)


# ------------------------------------------------------------
#  系统状态
# ------------------------------------------------------------

@app.route("/api/status")
def api_status():
    return jsonify({
        "contract_address": state.contract_address,
        "chain_id": state.w3.eth.chain_id,
        "block_number": state.w3.eth.block_number,
        "accounts_count": len(state.accounts),
        "escrow_balance": float(state.w3.from_wei(
            state.market.functions.getEscrowBalance().call(), "ether")),
    })


@app.route("/api/reset", methods=["POST"])
def api_reset():
    """
    重置整条链（演示时用来恢复初始状态）。

    默认保留账号密码（老账号直接登录即可）。
    传 {"wipe_credentials": true} 会连账号一起清空，
    让注册过的用户名重新可用（自动化测试脚本用）。

    权限：本机直连（127.0.0.1）免密钥；其他来源必须带请求头
    X-Admin-Key，且与服务端的 ADMIN_KEY 环境变量一致。
    未配置 ADMIN_KEY 时，非本机请求一律拒绝。
    """
    data = request.get_json(silent=True) or {}
    wipe = bool(data.get("wipe_credentials"))

    # ---- 权限校验 ----
    # 本机直连免密钥（自己人 + 自动化测试）；
    # 其余来源（局域网同学、隧道/公网）必须带正确的 X-Admin-Key。
    # 没配 ADMIN_KEY 时对外一律拒绝（fail closed），不给"忘了配就等于没有"的机会。
    if not is_local_direct():
        provided = (request.headers.get("X-Admin-Key") or "").strip()
        if not ADMIN_KEY:
            return jsonify({
                "success": False,
                "error": "重置功能已关闭（服务端未配置 ADMIN_KEY）",
                "code": "RESET_DISABLED",
            }), 403
        if not hmac.compare_digest(provided, ADMIN_KEY):
            return jsonify({
                "success": False,
                "error": "无权重置：管理员密钥缺失或不正确",
                "code": "FORBIDDEN",
            }), 403

    state.reset(wipe_credentials=wipe)
    msg = "链已重置，请重新登录" if not wipe else "链已重置，账号密码已清空"
    return jsonify({"success": True, "message": msg, "contract_address": state.contract_address})


# ------------------------------------------------------------
#  用户认证
# ------------------------------------------------------------

@app.route("/api/check_username")
def api_check_username():
    """检查用户名是否已被注册（注册页实时提示用）"""
    username = (request.args.get("username") or "").strip()
    if not username:
        return jsonify({"success": False, "error": "请提供用户名"}), 400
    return jsonify({
        "success": True,
        "username": username,
        "taken": state.is_registered(username),
    })


@app.route("/api/register", methods=["POST"])
def api_register():
    """
    注册：创建账号 + 设置密码。

    安全要点：
      - 密码强度校验
      - scrypt 加盐哈希后存储，明文不落盘
      - 用户名校验，防止奇怪字符
      - 注册成功后直接签发 token（免去二次登录）
    """
    data = request.get_json() or {}

    # ---- 限流：防止有人脚本刷注册，把 199 个账户占满 ----
    wait = rate_limited("register")
    if wait is not None:
        return jsonify({
            "success": False,
            "error": f"注册太频繁，请 {wait} 秒后再试",
            "code": "RATE_LIMITED",
            "retry_after": wait,
        }), 429

    # ---- 邀请码：配置了 INVITE_CODE 才启用（本机直连不需要） ----
    if INVITE_CODE and not is_local_direct():
        provided = (data.get("invite_code") or "").strip()
        if not hmac.compare_digest(provided, INVITE_CODE):
            return jsonify({
                "success": False,
                "error": "邀请码不正确",
                "code": "BAD_INVITE",
            }), 403

    username = (data.get("username") or "").strip()
    nickname = (data.get("nickname") or "").strip() or username
    password = data.get("password") or ""
    # 「记住我」同样适用于注册（注册后直接进入，理应保持登录态）
    remember = bool(data.get("remember"))

    # ---- 输入校验 ----
    if not username:
        return jsonify({"success": False, "error": "用户名不能为空"}), 400
    if len(username) > 12:
        return jsonify({"success": False, "error": "用户名最长 12 位"}), 400
    if not re.match(r"^[\w\u4e00-\u9fa5]+$", username):
        return jsonify({"success": False, "error": "用户名只能包含中文、字母、数字、下划线"}), 400

    ok, msg = auth.check_password_strength(password)
    if not ok:
        return jsonify({"success": False, "error": msg}), 400

    if state.is_registered(username):
        return jsonify({"success": False, "error": "这个用户名已经被注册了，换一个或直接登录"}), 409

    pool_msg = (f"链上账户已用完（本链最多 {state.max_users} 位用户）。"
                f"点「区块链浏览 → 重置整条链」可清空重置")

    # ---- 链上注册 ----
    try:
        addr = state.get_account(username)
    except ValueError:
        return jsonify({
            "success": False,
            "error": pool_msg,
            "code": "ACCOUNT_POOL_FULL",
        }), 503

    try:
        chain_tx(state.market.functions.register(nickname), addr)
    except Exception:
        # 如果链上已注册（比如链重置前注册过），尝试换一个账户
        try:
            idx = len(state.user_sessions)
            if idx >= len(state.accounts):
                raise ValueError("pool full")
            addr = state.accounts[idx]
            state.user_sessions[username] = addr
            chain_tx(state.market.functions.register(nickname), addr)
        except ValueError:
            return jsonify({
                "success": False,
                "error": pool_msg,
                "code": "ACCOUNT_POOL_FULL",
            }), 503
        except Exception as e2:
            return jsonify({"success": False, "error": friendly_error(e2)}), 400

    # ---- 保存凭据 + 签发 token ----
    state.set_credentials(username, password)
    state.nicknames[username] = nickname
    auth.clear_failures(username)
    ttl = auth.REMEMBER_TTL if remember else auth.TOKEN_TTL
    token = auth.create_token(username, ttl=ttl)
    state.save_meta()      # 昵称 + 地址映射落盘

    return jsonify({
        "success": True,
        "username": username,
        "nickname": nickname,
        "address": addr,
        "token": token,
        "expires_in": ttl,
        "remember": remember,
    })


@app.route("/api/login", methods=["POST"])
def api_login():
    """
    登录：校验密码 + 签发 token。

    安全要点：
      - 防暴力破解：连续失败会临时锁定
      - 用户名不存在和密码错误返回同样的提示，避免枚举账号
      - 校验通过才签发 token
    """
    data = request.get_json() or {}

    # ---- 按 IP 限流：账号级锁定只能防单账号爆破，防不了"换着用户名试" ----
    wait = rate_limited("login")
    if wait is not None:
        return jsonify({
            "success": False,
            "error": f"请求过于频繁，请 {wait} 秒后再试",
            "code": "RATE_LIMITED",
            "retry_after": wait,
        }), 429

    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    # 「记住我」：签发一个 30 天的长令牌，关掉浏览器第二天打开仍然是登录状态
    remember = bool(data.get("remember"))

    if not username or not password:
        return jsonify({"success": False, "error": "请输入用户名和密码"}), 400

    # ---- 检查是否被锁定 ----
    locked, remain = auth.is_locked(username)
    if locked:
        return jsonify({
            "success": False,
            "error": f"密码错误次数过多，请 {remain} 秒后再试",
            "code": "LOCKED",
            "retry_after": remain,
        }), 429

    # ---- 校验凭据 ----
    if not state.is_registered(username) or not state.check_credentials(username, password):
        auth.record_failure(username)
        left = auth.MAX_FAILURES - auth.failure_stats(username)
        tip = f"用户名或密码错误" + (f"，还可尝试 {left} 次" if left > 0 else "，账号已锁定")
        return jsonify({"success": False, "error": tip, "code": "BAD_CREDENTIALS"}), 401

    # ---- 成功 ----
    auth.clear_failures(username)
    ttl = auth.REMEMBER_TTL if remember else auth.TOKEN_TTL
    token = auth.create_token(username, ttl=ttl)
    # 链重置会清掉地址映射，这里顺手补发，避免"登录成功却没有链上身份"
    addr = state.ensure_account(username)

    return jsonify({
        "success": True,
        "username": username,
        "nickname": state.display_name(username),
        "address": addr,
        "token": token,
        "expires_in": ttl,
        "remember": remember,
    })


@app.route("/api/logout", methods=["POST"])
@require_auth
def api_logout():
    """
    登出：把当前 token 加入黑名单。

    黑名单现在会落盘（见 save_meta）。以前它只在内存里，
    服务一重启就忘了谁登出过 —— 2 小时的短令牌问题不大，
    但「记住我」是 30 天，不落盘的话登出等于没登出。
    """
    token = extract_token()
    if token:
        state.revoke_token(token)
    return jsonify({"success": True, "message": "已安全退出"})


@app.route("/api/me")
@require_auth
def api_me():
    """
    获取当前登录用户信息。
    这是前端的「我是谁」接口 —— 刷新页面后用它恢复登录态。
    """
    username = get_auth_user()
    addr = state.ensure_account(username)
    if not addr:
        return jsonify({
            "success": False,
            "error": "演示账户已用完，请重置整条链",
            "code": "ACCOUNT_POOL_FULL",
        }), 503

    nick, sold, bought, avg = state.market.functions.getUserInfo(addr).call()
    balance = state.w3.eth.get_balance(addr)

    return jsonify({
        "success": True,
        "username": username,
        "nickname": nick or state.display_name(username),
        "address": addr,
        "sold_count": sold,
        "bought_count": bought,
        "avg_rating": round(avg / 100, 1) if avg else 0,
        "balance_eth": round(float(state.w3.from_wei(balance, "ether")), 4),
    })


@app.route("/api/user/<username>")
def api_user(username):
    """公开的用户信息（不含任何敏感数据）"""
    addr = state.addr_of(username)
    if not addr:
        return jsonify({"success": False, "error": "用户不存在"}), 404

    nick, sold, bought, avg = state.market.functions.getUserInfo(addr).call()
    balance = state.w3.eth.get_balance(addr)

    return jsonify({
        "success": True,
        "username": username,
        "nickname": nick or state.display_name(username),
        "address": addr,
        "sold_count": sold,
        "bought_count": bought,
        "avg_rating": round(avg / 100, 1) if avg else 0,
        "balance_eth": round(float(state.w3.from_wei(balance, "ether")), 4),
    })


# ------------------------------------------------------------
#  商品
# ------------------------------------------------------------

@app.route("/api/items")
def api_items():
    """
    商品列表

    支持的查询参数：
      q         关键词搜索（匹配名称和描述）
      category  按分类筛选
      status    按状态筛选（0在售 1交易中 2已售出 3已下架）
      sort      排序：newest(默认) / price_asc / price_desc / hot
      page      页码，从 1 开始
      page_size 每页数量，默认 12
    """
    q = (request.args.get("q") or "").strip().lower()
    category = (request.args.get("category") or "").strip()
    status_filter = request.args.get("status")
    sort = request.args.get("sort") or "newest"
    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1
    try:
        page_size = min(48, max(1, int(request.args.get("page_size", 12))))
    except ValueError:
        page_size = 12

    count = state.market.functions.getItemCount().call()
    status_names = {0: "在售", 1: "交易中", 2: "已售出", 3: "已下架"}
    all_items = []
    # 分类统计
    category_counts = {c: 0 for c in CATEGORIES}

    for i in range(1, count + 1):
        raw = state.market.functions.getItem(i).call()

        seller_addr = raw[4]
        nick, sold, bought, avg = state.market.functions.getUserInfo(seller_addr).call()
        seller_name = nick or seller_addr[:10] + "..."

        cat, img_hash, pure_desc = decode_description(raw[2])
        category_counts[cat] = category_counts.get(cat, 0) + 1

        # 买家昵称
        buyer_name = ""
        if raw[5] and raw[5] != "0x0000000000000000000000000000000000000000":
            bnick, _, _, _ = state.market.functions.getUserInfo(raw[5]).call()
            buyer_name = bnick or raw[5][:10] + "..."

        item = {
            "id": raw[0],
            "name": raw[1],
            "description": pure_desc,
            "category": cat,
            "image_hash": img_hash,
            "image_url": image_url(img_hash),
            "price_eth": round(float(state.w3.from_wei(raw[3], "ether")), 4),
            "price_wei": str(raw[3]),
            "seller_address": seller_addr,
            "seller_name": seller_name,
            "seller_rating": round(avg / 100, 1) if avg else 0,
            "seller_sold": sold,
            "buyer_address": raw[5],
            "buyer_name": buyer_name,
            "status": raw[6],
            "status_text": status_names.get(raw[6], "未知"),
            "created_at": raw[7],
            "created_text": relative_time(raw[7]),
            "created_full": full_time(raw[7]),
        }

        # ---- 筛选 ----
        if q:
            haystack = (item["name"] + " " + item["description"] + " " + item["category"]).lower()
            if q not in haystack:
                continue
        if category and category != "全部":
            if item["category"] != category:
                continue
        if status_filter is not None and status_filter != "":
            try:
                if item["status"] != int(status_filter):
                    continue
            except ValueError:
                pass

        all_items.append(item)

    # ---- 排序 ----
    if sort == "price_asc":
        all_items.sort(key=lambda x: x["price_eth"])
    elif sort == "price_desc":
        all_items.sort(key=lambda x: x["price_eth"], reverse=True)
    elif sort == "hot":
        # 在售优先 + 卖家信誉高的优先
        all_items.sort(key=lambda x: (x["status"] != 0, -x["seller_rating"]))
    else:  # newest
        all_items.sort(key=lambda x: x["id"], reverse=True)

    # ---- 分页 ----
    total_filtered = len(all_items)
    start = (page - 1) * page_size
    page_items = all_items[start:start + page_size]

    # ---- 全局统计 ----
    stats = {
        "total": count,
        "onsale": sum(1 for x in all_items if x["status"] == 0) if not q and not category else None,
        "categories": category_counts,
    }
    # 统计在售数量（不受筛选影响）
    onsale_all = 0
    for i in range(1, count + 1):
        raw = state.market.functions.getItem(i).call()
        if raw[6] == 0:
            onsale_all += 1
    stats["onsale"] = onsale_all

    return jsonify({
        "success": True,
        "items": page_items,
        "total": total_filtered,
        "all_total": count,
        "page": page,
        "page_size": page_size,
        "total_pages": max(1, (total_filtered + page_size - 1) // page_size),
        "stats": stats,
    })


@app.route("/api/items", methods=["POST"])
@require_auth
def api_new_item():
    """上架商品。支持两种提交方式：
      1. JSON  —— 不带图片时
      2. multipart/form-data —— 带图片文件时（字段名 image）

    安全：卖家身份取自登录 token，不接受前端传入的 username。
    """
    if request.content_type and request.content_type.startswith("multipart/form-data"):
        data = request.form
        image_file = request.files.get("image")
    else:
        data = request.get_json() or {}
        image_file = None

    username = get_auth_user()
    name = (data.get("name") or "").strip()
    description = (data.get("description") or "").strip()
    category = (data.get("category") or "其他").strip()
    price_eth = data.get("price_eth")

    if not name:
        return jsonify({"success": False, "error": "商品名称不能为空"}), 400

    if len(name) > 50:
        return jsonify({"success": False, "error": "商品名称最长 50 个字符"}), 400

    try:
        price_float = float(price_eth)
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "请填写有效价格"}), 400
    if price_float <= 0:
        return jsonify({"success": False, "error": "价格必须大于0"}), 400
    if price_float > 10000:
        return jsonify({"success": False, "error": "价格超出合理范围"}), 400

    addr = state.ensure_account(username)
    if not addr:
        return jsonify({
            "success": False,
            "error": "演示账户已用完，请重置整条链",
            "code": "ACCOUNT_POOL_FULL",
        }), 503

    # ---- 处理图片（可选）----
    image_hash = ""
    if image_file and image_file.filename:
        digest, stored_name, err = save_image(image_file)
        if err:
            return jsonify({"success": False, "error": err}), 400
        image_hash = digest

    try:
        price_wei = state.w3.to_wei(price_float, "ether")
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "价格格式不正确"}), 400

    # 把分类和图片哈希编码进描述，这样不用改合约就能支持扩展字段
    encoded_desc = encode_description(category, description, image_hash)

    try:
        chain_tx(state.market.functions.listItem(name, encoded_desc, price_wei), addr)
    except Exception as e:
        return jsonify({"success": False, "error": friendly_error(e)}), 400

    return jsonify({
        "success": True,
        "message": f"商品「{name}」已上架到区块链",
        "category": category,
        "image_hash": image_hash,
        "image_url": image_url(image_hash),
    })


# ------------------------------------------------------------
#  图片上传
# ------------------------------------------------------------

@app.route("/uploads/<path:filename>")
def uploaded_file(filename):
    """提供已上传图片的访问"""
    return send_from_directory(UPLOAD_DIR, filename)


@app.route("/api/upload", methods=["POST"])
@require_auth
def api_upload():
    """
    单独上传图片（上架前预览用）。
    返回内容哈希 —— 这就是"内容寻址"：地址由内容决定。
    真正的 IPFS 也是这个原理，只是它还会把文件分发到全球节点。

    需要登录：否则任何人都能往服务器磁盘灌文件（5MB/张、张数不限）。
    """
    if "image" not in request.files:
        return jsonify({"success": False, "error": "没有收到图片文件"}), 400

    digest, stored_name, err = save_image(request.files["image"])
    if err:
        return jsonify({"success": False, "error": err}), 400

    return jsonify({
        "success": True,
        "image_hash": digest,
        "image_url": f"/uploads/{stored_name}",
        "size_note": "已按内容哈希去重存储",
    })


@app.route("/api/verify_image", methods=["POST"])
def api_verify_image():
    """
    校验图片是否被篡改。
    前端传入哈希，后端重新对磁盘上文件算一遍哈希比对。
    —— 这就是内容寻址的防篡改特性：改一个字节，哈希就完全不同。
    """
    data = request.get_json() or {}
    claimed = (data.get("image_hash") or "").strip()
    if not claimed:
        return jsonify({"success": False, "error": "缺少 image_hash"}), 400

    url = image_url(claimed)
    if not url:
        return jsonify({"success": False, "verified": False, "error": "链上哈希对应的文件不存在"}), 404

    path = UPLOAD_DIR / url.split("/")[-1]
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    verified = (actual == claimed)

    return jsonify({
        "success": True,
        "verified": verified,
        "on_chain_hash": claimed,
        "actual_hash": actual,
        "message": "内容完整，与链上哈希一致" if verified else "警告：文件内容与链上哈希不符，可能已被篡改",
    })


@app.route("/api/categories")
def api_categories():
    """返回分类列表和每个分类的商品数"""
    count = state.market.functions.getItemCount().call()
    counts = {c: 0 for c in CATEGORIES}
    for i in range(1, count + 1):
        raw = state.market.functions.getItem(i).call()
        # 注意 decode_description 返回 3 个值（分类、图片哈希、纯描述）。
        # 这里曾写成 `cat, _ = ...`，一旦有商品就抛 ValueError，
        # 整个 /api/categories 直接 500（分类筛选功能等于废了）。
        cat, _, _ = decode_description(raw[2])
        counts[cat] = counts.get(cat, 0) + 1
    return jsonify({"success": True, "categories": CATEGORIES, "counts": counts})


@app.route("/api/items/<int:item_id>")
def api_item_detail(item_id):
    """单个商品详情"""
    try:
        raw = state.market.functions.getItem(item_id).call()
    except Exception:
        return jsonify({"success": False, "error": "商品不存在"}), 404

    status_names = {0: "在售", 1: "交易中", 2: "已售出", 3: "已下架"}
    seller_addr = raw[4]
    nick, sold, bought, avg = state.market.functions.getUserInfo(seller_addr).call()
    cat, img_hash, pure_desc = decode_description(raw[2])

    buyer_name = ""
    if raw[5] and raw[5] != "0x0000000000000000000000000000000000000000":
        bnick, _, _, _ = state.market.functions.getUserInfo(raw[5]).call()
        buyer_name = bnick or raw[5][:10] + "..."

    return jsonify({
        "success": True,
        "item": {
            "id": raw[0],
            "name": raw[1],
            "description": pure_desc,
            "category": cat,
            "image_hash": img_hash,
            "image_url": image_url(img_hash),
            "price_eth": round(float(state.w3.from_wei(raw[3], "ether")), 4),
            "price_wei": str(raw[3]),
            "seller_address": seller_addr,
            "seller_name": nick or seller_addr[:10] + "...",
            "seller_rating": round(avg / 100, 1) if avg else 0,
            "seller_sold": sold,
            "buyer_address": raw[5],
            "buyer_name": buyer_name,
            "status": raw[6],
            "status_text": status_names.get(raw[6], "未知"),
            "created_at": raw[7],
            "created_text": relative_time(raw[7]),
            "created_full": full_time(raw[7]),
        }
    })


@app.route("/api/buy", methods=["POST"])
@require_auth
def api_buy():
    data = request.get_json() or {}
    username = get_auth_user()          # 身份以 token 为准，不信前端
    item_id = data.get("item_id")

    addr = state.ensure_account(username)
    if not addr:
        return jsonify({
            "success": False,
            "error": "演示账户已用完，请重置整条链",
            "code": "ACCOUNT_POOL_FULL",
        }), 503

    try:
        item = state.market.functions.getItem(int(item_id)).call()
        price = item[3]
        chain_tx(state.market.functions.buyItem(int(item_id)), addr, value=price)
    except Exception as e:
        return jsonify({"success": False, "error": friendly_error(e)}), 400

    return jsonify({
        "success": True,
        "message": f"下单成功，{state.w3.from_wei(price, 'ether')} ETH 已进入合约托管",
        "tx_hash": "链上已确认",
    })


@app.route("/api/confirm", methods=["POST"])
@require_auth
def api_confirm():
    data = request.get_json() or {}
    username = get_auth_user()
    item_id = data.get("item_id")

    addr = state.ensure_account(username)
    if not addr:
        return jsonify({
            "success": False,
            "error": "演示账户已用完，请重置整条链",
            "code": "ACCOUNT_POOL_FULL",
        }), 503

    try:
        chain_tx(state.market.functions.confirmReceived(int(item_id)), addr)
    except Exception as e:
        return jsonify({"success": False, "error": friendly_error(e)}), 400

    return jsonify({"success": True, "message": "确认收货成功，货款已放给卖家（扣除1%平台费）"})


@app.route("/api/remove", methods=["POST"])
@require_auth
def api_remove():
    data = request.get_json() or {}
    username = get_auth_user()
    item_id = data.get("item_id")

    addr = state.ensure_account(username)
    if not addr:
        return jsonify({
            "success": False,
            "error": "演示账户已用完，请重置整条链",
            "code": "ACCOUNT_POOL_FULL",
        }), 503

    try:
        chain_tx(state.market.functions.removeItem(int(item_id)), addr)
    except Exception as e:
        return jsonify({"success": False, "error": friendly_error(e)}), 400

    return jsonify({"success": True, "message": "商品已下架"})


@app.route("/api/rate", methods=["POST"])
@require_auth
def api_rate():
    data = request.get_json() or {}
    username = get_auth_user()
    item_id = data.get("item_id")
    score = data.get("score")
    target = (data.get("target") or "").strip()   # 被评价人的用户名

    addr = state.ensure_account(username)
    if not addr:
        return jsonify({
            "success": False,
            "error": "演示账户已用完，请重置整条链",
            "code": "ACCOUNT_POOL_FULL",
        }), 503

    # 评分范围校验（合约里也有，这里提前拦一道，省一次上链）
    try:
        score_int = int(score)
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "评分必须是 1-5 的整数"}), 400
    if not 1 <= score_int <= 5:
        return jsonify({"success": False, "error": "评分必须在 1-5 之间"}), 400

    if target == username:
        return jsonify({"success": False, "error": "不能评价自己"}), 400

    target_addr = state.addr_of(target)
    if not target_addr:
        return jsonify({"success": False, "error": f"用户 {target} 不存在"}), 400

    try:
        chain_tx(state.market.functions.rate(int(item_id), target_addr, score_int), addr)
    except Exception as e:
        return jsonify({"success": False, "error": friendly_error(e)}), 400

    return jsonify({"success": True, "message": f"已给 {target} 评 {score_int} 星"})


@app.route("/api/my_items")
@require_auth
def api_my_items():
    """
    当前登录用户的交易记录。
    服务端按 token 里的身份过滤 —— 前端传什么 username 都没用。
    """
    username = get_auth_user()
    my_addr = (state.ensure_account(username) or "").lower()
    count = state.market.functions.getItemCount().call()
    status_names = {0: "在售", 1: "交易中", 2: "已售出", 3: "已下架"}
    mine = []

    for i in range(1, count + 1):
        raw = state.market.functions.getItem(i).call()
        seller = raw[4].lower()
        buyer = (raw[5] or "").lower()
        if my_addr != seller and my_addr != buyer:
            continue

        cat, img_hash, pure_desc = decode_description(raw[2])

        seller_nick, _, _, _ = state.market.functions.getUserInfo(raw[4]).call()
        buyer_name = ""
        if raw[5] and raw[5] != "0x0000000000000000000000000000000000000000":
            buyer_nick, _, _, _ = state.market.functions.getUserInfo(raw[5]).call()
            buyer_name = buyer_nick or raw[5][:10] + "..."

        mine.append({
            "id": raw[0],
            "name": raw[1],
            "description": pure_desc,
            "category": cat,
            "image_hash": img_hash,
            "image_url": image_url(img_hash),
            "price_eth": round(float(state.w3.from_wei(raw[3], "ether")), 4),
            "seller_address": raw[4],
            "seller_name": seller_nick or raw[4][:10] + "...",
            "buyer_address": raw[5],
            "buyer_name": buyer_name,
            "is_seller": my_addr == seller,
            "status": raw[6],
            "status_text": status_names.get(raw[6], "未知"),
            "created_at": raw[7],
            "created_text": relative_time(raw[7]),
        })

    mine.sort(key=lambda x: x["id"], reverse=True)
    return jsonify({"success": True, "items": mine, "total": len(mine)})


@app.route("/api/transactions")
def api_transactions():
    """返回链上最近的交易记录（模拟区块链浏览器）"""
    block_number = state.w3.eth.block_number
    txs = []

    for i in range(max(0, block_number - 20), block_number + 1):
        try:
            block = state.w3.eth.get_block(i, full_transactions=True)
        except Exception:
            continue
        for tx in block.transactions:
            txs.append({
                "block": i,
                "hash": tx.hash.hex(),
                "from": tx["from"],
                "to": tx["to"],
                "value_eth": round(float(state.w3.from_wei(tx["value"], "ether")), 6),
                "gas": tx.gas,
            })

    txs.reverse()
    return jsonify({"success": True, "transactions": txs[:30], "block_number": block_number})


# ============================================================
#  交易时间线（从链上事件日志还原）
# ============================================================
# 关键点：这些数据不是后端自己"记"的，而是从区块链的事件日志里读出来的。
# 合约在执行每一步操作时都会 emit 一个事件，事件永久保存在链上。
# 所以时间线就是"区块历史上真实发生过什么"的忠实还原。

def nick_or_addr(addr: str) -> str:
    """地址 → 昵称（查不到就截断地址）"""
    if not addr or addr == "0x0000000000000000000000000000000000000000":
        return "—"
    try:
        nick, _, _, _ = state.market.functions.getUserInfo(addr).call()
    except Exception:
        nick = ""
    return nick or (addr[:10] + "...")


def event_time(block_number: int) -> int:
    """取某个区块的时间戳"""
    try:
        return state.w3.eth.get_block(block_number)["timestamp"]
    except Exception:
        return 0


@app.route("/api/timeline/<int:item_id>")
def api_timeline(item_id):
    """
    某件商品的完整流转时间线。
    按区块顺序，从 ItemListed → ItemPurchased → ItemDelivered 逐步还原。
    """
    try:
        item = state.market.functions.getItem(item_id).call()
    except Exception:
        return jsonify({"success": False, "error": "商品不存在"}), 404

    data = json.dumps({"item_id": item_id})
    steps = []
    # ---- 1. 上架 ----
    try:
        listed = state.market.events.ItemListed.get_logs(
            from_block=0, argument_filters={"itemId": item_id})
    except Exception:
        listed = []
    for ev in listed:
        a = ev["args"]
        steps.append({
            "block": ev["blockNumber"],
            "type": "listed",
            "icon": "📦",
            "title": "商品上架",
            "actor": nick_or_addr(a["seller"]),
            "actor_addr": a["seller"],
            "detail": f"以 {round(float(state.w3.from_wei(a['price'], 'ether')), 4)} ETH 挂出",
            "amount_eth": round(float(state.w3.from_wei(a["price"], "ether")), 4),
            "time": event_time(ev["blockNumber"]),
        })

    # ---- 2. 购买（资金进托管）----
    try:
        purchased = state.market.events.ItemPurchased.get_logs(
            from_block=0, argument_filters={"itemId": item_id})
    except Exception:
        purchased = []
    for ev in purchased:
        a = ev["args"]
        steps.append({
            "block": ev["blockNumber"],
            "type": "purchased",
            "icon": "🔒",
            "title": "买家下单 · 资金进入合约托管",
            "actor": nick_or_addr(a["buyer"]),
            "actor_addr": a["buyer"],
            "detail": f"{round(float(state.w3.from_wei(a['amount'], 'ether')), 4)} ETH 已锁定在合约中，卖家暂时拿不到",
            "amount_eth": round(float(state.w3.from_wei(a["amount"], "ether")), 4),
            "time": event_time(ev["blockNumber"]),
        })

    # ---- 3. 确认收货（放款）----
    try:
        delivered = state.market.events.ItemDelivered.get_logs(
            from_block=0, argument_filters={"itemId": item_id})
    except Exception:
        delivered = []
    for ev in delivered:
        a = ev["args"]
        gross = item[3]
        fee_bps = state.market.functions.platformFeeRate().call()
        fee = gross * fee_bps // 10000
        net = gross - fee
        steps.append({
            "block": ev["blockNumber"],
            "type": "delivered",
            "icon": "✅",
            "title": "买家确认收货 · 货款放给卖家",
            "actor": nick_or_addr(a["buyer"]),
            "actor_addr": a["buyer"],
            "detail": f"卖家到账 {round(float(state.w3.from_wei(net, 'ether')), 4)} ETH（扣平台费 {round(float(state.w3.from_wei(fee, 'ether')), 4)} ETH）",
            "amount_eth": round(float(state.w3.from_wei(net, "ether")), 4),
            "time": event_time(ev["blockNumber"]),
        })

    # ---- 4. 下架 ----
    try:
        removed = state.market.events.ItemRemoved.get_logs(
            from_block=0, argument_filters={"itemId": item_id})
    except Exception:
        removed = []
    for ev in removed:
        a = ev["args"]
        steps.append({
            "block": ev["blockNumber"],
            "type": "removed",
            "icon": "🚫",
            "title": "卖家下架商品",
            "actor": nick_or_addr(a["seller"]),
            "actor_addr": a["seller"],
            "detail": "商品不再对外出售",
            "amount_eth": 0,
            "time": event_time(ev["blockNumber"]),
        })

    # ---- 5. 评价（买卖双方都可评）----
    try:
        rated = state.market.events.Rated.get_logs(from_block=0)
    except Exception:
        rated = []
    for ev in rated:
        a = ev["args"]
        if a["itemId"] != item_id:
            continue
        steps.append({
            "block": ev["blockNumber"],
            "type": "rated",
            "icon": "⭐",
            "title": f"{nick_or_addr(a['from'])} 评价了 {nick_or_addr(a['to'])}",
            "actor": nick_or_addr(a["from"]),
            "actor_addr": a["from"],
            "detail": "★" * a["score"] + "☆" * (5 - a["score"]),
            "amount_eth": 0,
            "score": a["score"],
            "time": event_time(ev["blockNumber"]),
        })

    # ---- 按区块排序 ----
    steps.sort(key=lambda s: s["block"])

    # 补全时间文本
    for s in steps:
        s["time_text"] = full_time(s["time"]) if s["time"] else "—"

    return jsonify({
        "success": True,
        "item_id": item_id,
        "item_name": item[1],
        "status": item[6],
        "steps": steps,
        "step_count": len(steps),
    })


# ============================================================
#  数据看板（全部由链上数据实时统计得出）
# ============================================================

@app.route("/api/dashboard")
def api_dashboard():
    """
    数据看板：成交额趋势、分类占比、价格区间分布、核心指标。
    所有数字都是从链上逐条读取后现算的，不是预先存好的。
    """
    count = state.market.functions.getItemCount().call()
    fee_bps = state.market.functions.platformFeeRate().call()

    total_gmv = 0.0          # 已完成成交总额
    total_locked = 0.0       # 当前托管中金额
    onsale = 0
    sold = 0
    in_escrow = 0
    removed = 0
    cat_sold_amount = {c: 0.0 for c in CATEGORIES}
    cat_sold_count = {c: 0 for c in CATEGORIES}
    cat_list_count = {c: 0 for c in CATEGORIES}
    price_buckets = {"0-0.1": 0, "0.1-0.5": 0, "0.5-1": 0, "1-5": 0, "5+": 0}
    daily_gmv = {}           # 日期 → 成交额
    all_records = []

    for i in range(1, count + 1):
        raw = state.market.functions.getItem(i).call()
        status = raw[6]
        price_eth = float(state.w3.from_wei(raw[3], "ether"))
        cat, _, _ = decode_description(raw[2])

        cat_list_count[cat] = cat_list_count.get(cat, 0) + 1

        # 价格区间分布（只统计在售 + 已售出的真实成交价）
        if status in (0, 2):
            if price_eth < 0.1:
                price_buckets["0-0.1"] += 1
            elif price_eth < 0.5:
                price_buckets["0.1-0.5"] += 1
            elif price_eth < 1:
                price_buckets["0.5-1"] += 1
            elif price_eth < 5:
                price_buckets["1-5"] += 1
            else:
                price_buckets["5+"] += 1

        if status == 0:
            onsale += 1
        elif status == 1:
            in_escrow += 1
            total_locked += price_eth
        elif status == 2:
            sold += 1
            total_gmv += price_eth
            cat_sold_amount[cat] = cat_sold_amount.get(cat, 0) + price_eth
            cat_sold_count[cat] = cat_sold_count.get(cat, 0) + 1
        elif status == 3:
            removed += 1

    # ---- 成交额趋势（从 ItemDelivered 事件还原每日成交）----
    try:
        delivered_logs = state.market.events.ItemDelivered.get_logs(from_block=0)
    except Exception:
        delivered_logs = []

    for ev in delivered_logs:
        iid = ev["args"]["itemId"]
        try:
            raw = state.market.functions.getItem(iid).call()
            price_eth = float(state.w3.from_wei(raw[3], "ether"))
        except Exception:
            continue
        ts = event_time(ev["blockNumber"])
        day = datetime.fromtimestamp(ts).strftime("%m-%d")
        daily_gmv[day] = round(daily_gmv.get(day, 0) + price_eth, 4)

    # 按时间排序
    trend_labels = sorted(daily_gmv.keys())
    trend_values = [daily_gmv[d] for d in trend_labels]

    # ---- 分类占比（按成交金额）----
    donut_labels = [c for c in CATEGORIES if cat_sold_amount.get(c, 0) > 0]
    donut_values = [round(cat_sold_amount[c], 4) for c in donut_labels]
    if not donut_labels:
        # 没有成交时，退化成"上架数量"占比，保证图表不空白
        donut_labels = [c for c in CATEGORIES if cat_list_count.get(c, 0) > 0]
        donut_values = [cat_list_count[c] for c in donut_labels]

    avg_price = round(total_gmv / sold, 4) if sold else 0.0
    platform_fee = round(total_gmv * fee_bps / 10000, 6)

    return jsonify({
        "success": True,
        "metrics": {
            "total_items": count,
            "onsale": onsale,
            "in_escrow": in_escrow,
            "sold": sold,
            "removed": removed,
            "gmv_eth": round(total_gmv, 4),
            "locked_eth": round(total_locked, 4),
            "avg_price_eth": avg_price,
            "platform_fee_eth": platform_fee,
            "escrow_balance_eth": round(float(state.w3.from_wei(
                state.market.functions.getEscrowBalance().call(), "ether")), 4),
        },
        "trend": {"labels": trend_labels, "values": trend_values},
        "donut": {
            "labels": donut_labels,
            "values": donut_values,
            "is_amount": bool(cat_sold_amount and any(cat_sold_amount.values())),
        },
        "price_buckets": {
            "labels": list(price_buckets.keys()),
            "values": list(price_buckets.values()),
        },
    })


# ============================================================
#  管理员接口
# ============================================================
# 为什么管理员能"下架别人的商品"：
#   合约的 removeItem 只认 item.seller == msg.sender。
#   本平台的链账户由后端托管（私钥在服务端），所以管理员可以
#   **用卖家的身份**发这笔交易 —— 合约看到的就是卖家本人操作。
#   这是真上链的下架（会留下 ItemRemoved 事件），不是前端隐藏。
#
# 为什么充值能生效：
#   平台账户（accounts[0]，即合约部署者）持有 ETH，直接转账给用户。
#   同样是真链上转账，区块高度会 +1。

ADMIN_LOG = DATA_DIR / "admin_log.jsonl"


def log_admin(action: str, **fields):
    """审计日志：谁在什么时候对谁做了什么。写失败不影响业务。"""
    rec = {
        "ts": int(time.time()),
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "action": action,
    }
    rec.update(fields)
    try:
        with open(ADMIN_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


def admin_mode():
    """
    返回 'local' / 'key' / None。

    local —— 就在这台机器上直连（管理员本人坐在电脑前），等价于
             已经能改 data/ 目录，谈不上越权
    key   —— 带了正确的 X-Admin-Key（手机/校外管理时用）
    """
    if is_local_direct():
        return "local"
    supplied = (request.headers.get("X-Admin-Key") or "").strip()
    if ADMIN_KEY and supplied and hmac.compare_digest(supplied, ADMIN_KEY):
        return "key"
    return None


def require_admin(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        mode = admin_mode()
        if not mode:
            return jsonify({
                "success": False,
                "error": "需要管理员权限（管理员密钥缺失或不正确）",
                "code": "ADMIN_REQUIRED",
            }), 403
        request.admin_mode = mode
        return view(*args, **kwargs)
    return wrapper


def platform_account():
    """平台账户 = 合约部署者 = accounts[0]"""
    return state.accounts[0]


@app.route("/api/admin/session")
def api_admin_session():
    """
    前端用它决定要不要显示「管理」入口。
    未登录也返回 200，只是 admin=false —— 不做成 403 是为了避免控制台报错。
    """
    mode = admin_mode()
    payload = {"success": True, "admin": bool(mode), "mode": mode}
    if mode:
        payload["platform_balance_eth"] = float(state.w3.from_wei(
            state.w3.eth.get_balance(platform_account()), "ether"))
        payload["user_count"] = len(state.credentials)
    return jsonify(payload)


@app.route("/api/admin/users")
@require_admin
def api_admin_users():
    """
    全部注册用户 + 余额 + 信誉 + 在售数量。

    商品统计只遍历一次链上商品，避免对每个用户重复调合约。
    """
    total_items = state.market.functions.getItemCount().call()
    listed = {}
    on_sale = {}
    for i in range(1, total_items + 1):
        try:
            it = state.market.functions.getItem(i).call()
        except Exception:
            continue
        seller = (it[4] or "").lower()
        listed[seller] = listed.get(seller, 0) + 1
        if it[6] == 0:                      # 0 = OnSale
            on_sale[seller] = on_sale.get(seller, 0) + 1

    rows = []
    for username in list(state.credentials.keys()):
        addr = state.addr_of(username)
        row = {
            "username": username,
            "nickname": state.display_name(username),
            "address": addr or "",
            "has_account": bool(addr),
            "balance_eth": 0.0,
            "sold_count": 0,
            "bought_count": 0,
            "avg_rating": 0.0,
            "listed_count": 0,
            "on_sale_count": 0,
        }
        if addr:
            try:
                nickname, sold, bought, avg = state.market.functions.getUserInfo(addr).call()
                row["sold_count"] = int(sold)
                row["bought_count"] = int(bought)
                row["avg_rating"] = round(int(avg) / 100.0, 2)
                if nickname:
                    row["nickname"] = nickname
            except Exception:
                pass
            try:
                row["balance_eth"] = float(state.w3.from_wei(
                    state.w3.eth.get_balance(addr), "ether"))
            except Exception:
                pass
            row["listed_count"] = listed.get(addr.lower(), 0)
            row["on_sale_count"] = on_sale.get(addr.lower(), 0)
        rows.append(row)

    platform = platform_account()
    return jsonify({
        "success": True,
        "count": len(rows),
        "users": rows,
        "platform": {
            "address": platform,
            "balance_eth": float(state.w3.from_wei(
                state.w3.eth.get_balance(platform), "ether")),
        },
    })


@app.route("/api/admin/items")
@require_admin
def api_admin_items():
    """全部商品（含已下架），供管理页审核用"""
    addr_to_user = {a.lower(): u for u, a in state.user_sessions.items()}
    total = state.market.functions.getItemCount().call()
    rows = []
    for i in range(1, total + 1):
        try:
            it = state.market.functions.getItem(i).call()
        except Exception:
            continue
        seller_addr = it[4] or ""
        seller_user = addr_to_user.get(seller_addr.lower(), "")
        rows.append({
            "item_id": int(it[0]),
            "name": it[1],
            "price_eth": float(state.w3.from_wei(int(it[3]), "ether")),
            "seller_address": seller_addr,
            "seller_username": seller_user,
            "seller_name": state.display_name(seller_user) if seller_user else "",
            "status": int(it[6]),
            "created_at": int(it[7]) if len(it) > 7 else 0,
        })
    rows.reverse()          # 新上架的在前，方便先看最近的
    return jsonify({"success": True, "count": len(rows), "items": rows})


@app.route("/api/admin/recharge", methods=["POST"])
@require_admin
def api_admin_recharge():
    """给某个用户充值（平台账户转账，真上链）"""
    data = request.get_json() or {}
    username = (data.get("username") or "").strip()
    if not username:
        return jsonify({"success": False, "error": "请指定用户名"}), 400

    try:
        amount = Decimal(str(data.get("amount_eth")))
    except (InvalidOperation, TypeError, ValueError):
        return jsonify({"success": False, "error": "充值金额格式不正确"}), 400
    if not amount.is_finite() or amount <= 0:
        return jsonify({"success": False, "error": "充值金额必须大于 0"}), 400
    if amount > Decimal("1000"):
        return jsonify({"success": False, "error": "单次充值不能超过 1000 ETH"}), 400
    if -amount.as_tuple().exponent > 6:
        return jsonify({"success": False, "error": "最多支持 6 位小数"}), 400

    addr = state.addr_of(username)
    if not addr:
        return jsonify({
            "success": False,
            "error": "该用户不存在，或还没有分配链上账户",
        }), 404

    wei = state.w3.to_wei(amount, "ether")
    platform = platform_account()
    platform_balance = state.w3.eth.get_balance(platform)
    if platform_balance < wei:
        return jsonify({
            "success": False,
            "error": f"平台账户余额不足（当前 "
                     f"{float(state.w3.from_wei(platform_balance, 'ether')):.4f} ETH）",
        }), 400

    try:
        with CHAIN_LOCK:
            tx_hash = state.w3.eth.send_transaction({
                "from": platform, "to": addr, "value": wei,
            })
            state.w3.eth.wait_for_transaction_receipt(tx_hash)
    except Exception as e:
        return jsonify({"success": False, "error": f"转账失败：{friendly_error(e)}"}), 400

    new_balance = float(state.w3.from_wei(state.w3.eth.get_balance(addr), "ether"))
    log_admin("recharge", target=username, address=addr,
              amount=str(amount), balance_after=new_balance,
              by=request.admin_mode)

    return jsonify({
        "success": True,
        "message": f"已给「{state.display_name(username)}」充值 {amount} ETH",
        "username": username,
        "amount_eth": float(amount),
        "balance_eth": new_balance,
        "block_number": state.w3.eth.block_number,
    })


@app.route("/api/admin/remove_item", methods=["POST"])
@require_admin
def api_admin_remove_item():
    """
    管理员下架违规商品。

    以**卖家身份**上链发 removeItem —— 合约只校验 msg.sender 是不是卖家，
    而本平台的账户私钥都在服务端，所以这是合法的真下架。
    只在售状态可下架（合约自身限制）；托管中的商品需要买家确认收货或退款，
    这里如实告知，不假装成功。
    """
    data = request.get_json() or {}
    try:
        item_id = int(data.get("item_id"))
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "商品 ID 不正确"}), 400

    try:
        item = state.market.functions.getItem(item_id).call()
    except Exception:
        return jsonify({"success": False, "error": "商品不存在"}), 404

    name, seller, status = item[1], item[4], item[6]
    if status != 0:
        return jsonify({
            "success": False,
            "code": "ITEM_NOT_ON_SALE",
            "error": "该商品不在「在售」状态（交易中或已成交），合约不允许下架。"
                     "托管中的订单需要买家确认收货或走退款。",
        }), 409

    try:
        chain_tx(state.market.functions.removeItem(item_id), seller)
    except Exception as e:
        return jsonify({"success": False, "error": friendly_error(e)}), 400

    log_admin("remove_item", item_id=item_id, item_name=name,
              seller=seller, by=request.admin_mode)

    return jsonify({
        "success": True,
        "message": f"商品「{name}」已下架",
        "item_id": item_id,
        "block_number": state.w3.eth.block_number,
    })


@app.route("/api/admin/logs")
@require_admin
def api_admin_logs():
    """最近的管理操作（默认 50 条，最新的在前）"""
    try:
        limit = max(1, min(200, int(request.args.get("limit") or 50)))
    except ValueError:
        limit = 50
    if not ADMIN_LOG.exists():
        return jsonify({"success": True, "logs": []})
    lines = ADMIN_LOG.read_text(encoding="utf-8").splitlines()[-limit:]
    out = []
    for line in reversed(lines):
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return jsonify({"success": True, "logs": out})


# ============================================================
#  启动
# ============================================================


def lan_addresses(port: int):
    """
    列出本机在局域网里的 IP，供其他设备访问。

    用两个办法取，互为补充：
      1. 连一下外部地址（不真发包），看本机用的是哪块网卡 —— 最准
      2. 解析主机名 —— 有多块网卡（比如装了虚拟机）时能补全
    """
    found = []

    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        if ip:
            found.append(ip)
    except Exception:
        pass

    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in found and not ip.startswith("127."):
                found.append(ip)
    except Exception:
        pass

    return [f"http://{ip}:{port}" for ip in found]


def print_banner(port: int):
    print()
    print("=" * 64)
    print("  区块链二手交易平台 —— 后端服务")
    print("=" * 64)
    print()
    print(f"  合约地址  {state.contract_address}")
    print(f"  链账户数  {len(state.accounts)}（可注册用户 {state.max_users} 位）")
    print(f"  已注册    {len(state.credentials)} 位")
    print(f"  区块高度  {state.w3.eth.block_number}")
    print(f"  数据目录  {DATA_DIR}")
    print()
    print("  安全开关：")
    print(f"    重置接口  {'已启用（需 X-Admin-Key）' if ADMIN_KEY else '仅限本机（对外已关闭）'}")
    print(f"    邀请码    {'已启用' if INVITE_CODE else '未启用（任何人可注册）'}")
    print(f"    访问限流  登录 {RATE_LIMITS['login'][0]}/分，注册 {RATE_LIMITS['register'][0]}/分（按 IP）")
    print()
    print("  本机访问：")
    print(f"    http://127.0.0.1:{port}")
    print()

    others = lan_addresses(port)
    if others:
        print("  同一局域网（同学/手机）访问：")
        for url in others:
            print(f"    {url}")
        print()
        print("  访问不了？多半是防火墙拦了端口。用管理员权限跑一次：")
        print(f"    netsh advfirewall firewall add rule name=\"BlockchainMarket\" "
              f"dir=in action=allow protocol=TCP localport={port}")
    else:
        print("  ! 没检测到局域网 IP，其他设备可能访问不了")

    print()
    print("=" * 64)
    print("  按 Ctrl+C 停止服务（数据已落盘，下次启动原样续用）")
    print("=" * 64)
    print()


if __name__ == "__main__":
    PORT = int(os.environ.get("PORT") or 5000)
    HOST = os.environ.get("HOST") or "0.0.0.0"

    print_banner(PORT)

    # waitress：纯 Python 的多线程 WSGI 服务器。
    # 不用 Flask 自带的开发服务器 —— 它单线程、并发一高就卡，
    # 而且 Flask 官方明确说不要用于生产。
    # （gunicorn 在 Windows 上跑不了，所以选 waitress）
    try:
        from waitress import serve

        serve(app, host=HOST, port=PORT, threads=8)
    except ImportError:
        print("  ! 未安装 waitress，退回 Flask 开发服务器（并发能力较差）")
        print("    建议执行：uv pip install waitress")
        app.run(host=HOST, port=PORT, debug=False, threaded=True)

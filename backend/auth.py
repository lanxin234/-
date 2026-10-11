"""
认证与安全模块

本模块负责三件事：
  1. 密码安全存储   —— scrypt 加盐哈希（绝不存明文）
  2. 会话令牌管理   —— HMAC 签名的无状态 token，带过期
  3. 防暴力破解     —— 失败次数窗口限制

设计原则：
  - 密钥、哈希、比对全部在服务端完成，前端拿不到任何可用于伪造的东西
  - token 里只放用户名和过期时间，签名保证不可篡改
  - 所有比较用 hmac.compare_digest（常量时间），防时序侧信道
"""

import base64
import hashlib
import hmac
import json
import os
import secrets
import time

# ============================================================
#  参数配置
# ============================================================

TOKEN_TTL = 2 * 60 * 60          # token 有效期：2 小时
TOKEN_RENEW_THRESHOLD = 15 * 60  # 剩余不足 15 分钟时自动续期

# 「记住我」勾上时的有效期：30 天。
# 这是局域网自用场景的取舍 —— 共用一台电脑又不想每次输密码，
# 30 天足够久，又不至于像"永不过期"那样一旦泄漏就长期有效。
REMEMBER_TTL = 30 * 24 * 60 * 60

# scrypt 参数（内存 ~8MB，单次约 90ms，兼顾安全与响应速度）
# 注：N 越大越难暴力破解但越慢。演示环境取 2^13（业界常见下限是 2^14），
#     因为在无 GPU 的纯 Python 环境下 2^14 会让登录有 200ms 可感知延迟。
SCRYPT_N = 2 ** 13
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32

# 防暴力破解：同一用户名 5 分钟内失败 5 次就锁定
MAX_FAILURES = 5
FAILURE_WINDOW = 5 * 60
LOCKOUT_DURATION = 5 * 60

# 密码强度要求
MIN_PASSWORD_LEN = 6
MAX_PASSWORD_LEN = 64


# ============================================================
#  密钥管理
# ============================================================
# 首次运行生成随机密钥并持久化，重启服务后旧 token 依然有效。
# 生产环境应从环境变量读取（比如 os.environ["SECRET_KEY"]）。

def _load_secret() -> bytes:
    env = os.environ.get("MARKET_SECRET_KEY")
    if env:
        return env.encode("utf-8")

    key_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".secret_key")
    if os.path.exists(key_file):
        with open(key_file, "rb") as f:
            return f.read()

    key = secrets.token_bytes(32)
    try:
        with open(key_file, "wb") as f:
            f.write(key)
        os.chmod(key_file, 0o600)
    except OSError:
        # 写不了就算了，本次进程内仍可用
        pass
    return key


SECRET_KEY = _load_secret()


def rotate_secret() -> None:
    """
    更换签名密钥 —— 所有已签发的 token 立刻失效。

    用于「连账号一起清空」的场景（自动化测试反复重跑），
    保证上一轮的 token 不会残留生效。
    """
    global SECRET_KEY
    SECRET_KEY = secrets.token_bytes(32)


# ============================================================
#  密码哈希（scrypt）
# ============================================================

def hash_password(password: str) -> str:
    """
    生成密码哈希。格式：scrypt$<salt_b64>$<hash_b64>
    每次调用都用新的随机 salt，所以同一个密码两次哈希结果不同。
    """
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=SCRYPT_DKLEN,
    )
    return "scrypt${}${}".format(
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(digest).decode("ascii"),
    )


def verify_password(password: str, stored: str) -> bool:
    """校验密码。用常量时间比较，防止时序攻击。"""
    if not stored or not stored.startswith("scrypt$"):
        return False
    try:
        _, salt_b64, hash_b64 = stored.split("$", 2)
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
    except (ValueError, base64.binascii.Error):
        return False

    actual = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=SCRYPT_DKLEN,
    )
    return hmac.compare_digest(actual, expected)


def check_password_strength(password: str):
    """返回 (是否合格, 提示信息)"""
    if not password:
        return False, "密码不能为空"
    if len(password) < MIN_PASSWORD_LEN:
        return False, f"密码至少 {MIN_PASSWORD_LEN} 位"
    if len(password) > MAX_PASSWORD_LEN:
        return False, f"密码最长 {MAX_PASSWORD_LEN} 位"
    if password.isdigit():
        return False, "密码不能全是数字，请混合字母"
    if len(set(password)) == 1:
        return False, "密码不能是重复的同一个字符"

    weak = ["123456", "password", "qwerty", "abc123", "111111", "000000", "admin", "888888"]
    if password.lower() in weak:
        return False, "这个密码太常见了，请换一个"
    return True, ""


# ============================================================
#  会话 token（HMAC 签名，无状态）
# ============================================================
# 结构：base64url(payload_json).base64url(hmac_sha256(payload))
# payload = {"u": 用户名, "iat": 签发时间, "exp": 过期时间, "jti": 随机ID}
#
# 为什么这样设计：
#   - 服务端不需要存 session 表，验证只需重算签名
#   - 签名密钥只有服务端有，客户端改了 payload 签名就对不上
#   - 自带过期时间，过期即失效

def _b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64d(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def _sign(payload_b64: str) -> str:
    mac = hmac.new(SECRET_KEY, payload_b64.encode("ascii"), hashlib.sha256)
    return _b64e(mac.digest())


def create_token(username: str, ttl: int = None) -> str:
    """
    为用户签发一个 token。

    参数:
      ttl -- 有效期（秒）。不传就用默认的 2 小时。
             勾了「记住我」时前端会要求长一点（见 REMEMBER_TTL），
             这样关掉浏览器、第二天再打开仍然是登录状态。
    """
    now = int(time.time())
    payload = {
        "u": username,
        "iat": now,
        "exp": now + (ttl if ttl else TOKEN_TTL),
        "jti": secrets.token_hex(8),
    }
    payload_b64 = _b64e(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    return f"{payload_b64}.{_sign(payload_b64)}"


def token_expiry(token: str) -> int:
    """
    取 token 的过期时间戳（用于把已登出的 token 记进黑名单时能顺带记下
    "什么时候可以忘掉它"）。解析不出来就返回 0。
    """
    if not token or not isinstance(token, str) or "." not in token:
        return 0
    payload_b64, _, _sig = token.rpartition(".")
    try:
        payload = json.loads(_b64d(payload_b64).decode("utf-8"))
        return int(payload.get("exp", 0))
    except (ValueError, UnicodeDecodeError, TypeError):
        return 0


def verify_token(token: str):
    """
    校验 token。
    返回 (用户名, 剩余秒数)；无效返回 (None, 0)。
    """
    if not token or not isinstance(token, str) or "." not in token:
        return None, 0

    payload_b64, _, sig = token.rpartition(".")
    if not payload_b64 or not sig:
        return None, 0

    # 1. 先验签名（常量时间）
    expected_sig = _sign(payload_b64)
    if not hmac.compare_digest(sig, expected_sig):
        return None, 0

    # 2. 再解 payload
    try:
        payload = json.loads(_b64d(payload_b64).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None, 0

    # 3. 检查过期
    now = int(time.time())
    exp = int(payload.get("exp", 0))
    if exp <= now:
        return None, 0

    username = payload.get("u")
    if not username or not isinstance(username, str):
        return None, 0

    return username, exp - now


def should_renew(remaining_seconds: int) -> bool:
    """剩余时间不足阈值时建议续期"""
    return 0 < remaining_seconds < TOKEN_RENEW_THRESHOLD


# ============================================================
#  防暴力破解
# ============================================================
# 记录每个用户名的失败次数和时间。演示够用，生产应放 Redis。

_failures = {}   # username -> [时间戳, ...]


def record_failure(username: str):
    now = time.time()
    bucket = _failures.setdefault(username, [])
    bucket.append(now)
    # 只保留窗口内的记录
    _failures[username] = [t for t in bucket if now - t < FAILURE_WINDOW]


def is_locked(username: str):
    """返回 (是否锁定, 剩余秒数)"""
    now = time.time()
    bucket = [t for t in _failures.get(username, []) if now - t < FAILURE_WINDOW]
    _failures[username] = bucket

    if len(bucket) < MAX_FAILURES:
        return False, 0

    # 第 MAX_FAILURES 次失败后开始锁定
    locked_since = bucket[-1]
    remain = int(LOCKOUT_DURATION - (now - locked_since))
    if remain > 0:
        return True, remain

    # 锁定已过，清空记录
    _failures.pop(username, None)
    return False, 0


def clear_failures(username: str):
    _failures.pop(username, None)


def failure_stats(username: str):
    """返回已失败次数，用于提示用户还剩几次机会"""
    return len(_failures.get(username, []))


def clear_all_failures():
    """清空所有失败记录（链重置时调用，让锁定状态一并归零）"""
    _failures.clear()

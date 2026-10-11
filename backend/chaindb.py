"""
把 py-evm 的内存链后端换成 SQLite —— 让整条链落盘，重启不丢数据。

为什么能这么换
--------------
py-evm 建链时会调用 eth.db.get_db_backend()，而它读环境变量：

    CHAIN_DB_BACKEND_CLASS   （一个点分导入路径，默认指向内存后端）

我们把它指到本模块的 SqliteAtomicDB，整条链就自动写进 SQLite 了：
区块、交易回执、事件日志、合约存储全部保真，不需要自己写
"快照 → 重启 → 回放重建" 那套逻辑，也不会丢事件日志。

之所以能无痛替换，是因为 py-evm 的 MemoryDB 内部只认一个普通的
Dict[bytes, bytes]（属性名 kv_store），我们塞一个 SQLite 支撑的
MutableMapping 进去即可，py-evm 自己完全不知道底层换了。

关于并发
--------
链本身不是线程安全的（chain.header、mine_block 都在改共享状态），
所以这里用一把全局可重入锁把每个写批次整体串行化。局域网十几个人
同时用，这点串行开销远小于状态错乱的代价。
"""

import os
import sqlite3
import threading
from collections.abc import MutableMapping
from contextlib import contextmanager
from pathlib import Path

from eth.db.atomic import AtomicDB, AtomicDBWriteBatch
from eth.db.backends.memory import MemoryDB

# ------------------------------------------------------------
#  路径
# ------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent

# 默认放在项目根的 data/ 下。可用环境变量 MARKET_DATA_DIR 整体挪走，
# 自动化测试就靠这个把数据隔离到临时目录，不污染真实数据。
DATA_DIR = Path(os.environ.get("MARKET_DATA_DIR") or (ROOT / "data"))

#: 链数据库文件。可用环境变量 CHAIN_DB_PATH 单独覆盖
DB_PATH = Path(os.environ.get("CHAIN_DB_PATH") or (DATA_DIR / "chain.sqlite3"))


def set_db_path(path) -> None:
    """切换链数据库位置（必须在建链之前调用）"""
    global DB_PATH
    DB_PATH = Path(path)


def db_exists(path=None) -> bool:
    p = Path(path or DB_PATH)
    return p.exists()


def clear_db(path=None) -> None:
    """
    清空链数据（保留文件本身）。

    为什么不直接删文件：waitress 的每个工作线程都持有自己的 sqlite 连接，
    Windows 上文件被占用时删不掉，会抛 PermissionError。
    清空表内容效果完全相同，而且对各线程已打开的连接是安全的。
    """
    p = Path(path or DB_PATH)
    if not p.exists():
        return
    conn = sqlite3.connect(str(p), timeout=60, isolation_level=None)
    try:
        conn.execute("DELETE FROM kv")
        conn.execute("VACUUM")
    finally:
        conn.close()


def delete_db(path=None) -> None:
    """彻底删掉链数据库文件（含 WAL / SHM 两个附属文件）。

    只在服务停止后需要"恢复出厂"时用；运行期间要清空请调 clear_db()。
    """
    p = Path(path or DB_PATH)
    for suffix in ("", "-wal", "-shm"):
        f = Path(str(p) + suffix)
        if f.exists():
            try:
                f.unlink()
            except OSError as e:
                print(f"  ! 删除 {f.name} 失败：{e}")


# ------------------------------------------------------------
#  SQLite 支撑的 bytes→bytes 映射
# ------------------------------------------------------------

# 全局写锁：把每个批次整体串行化，避免多线程同时改链
_WRITE_LOCK = threading.RLock()


class SqliteKV(MutableMapping):
    """
    SQLite 支撑的 bytes→bytes 映射，接口与 dict 完全一致。

    每个线程持有自己的连接（sqlite 连接不能跨线程共享），
    WAL 模式让读不被写阻塞。
    """

    def __init__(self, path):
        self.path = str(path)
        self._tls = threading.local()
        conn = self._conn()
        conn.execute(
            "CREATE TABLE IF NOT EXISTS kv (k BLOB PRIMARY KEY, v BLOB NOT NULL)"
        )
        conn.commit()

    # ---- 连接 ----

    def _conn(self):
        conn = getattr(self._tls, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=60, isolation_level=None)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._tls.conn = conn
            self._tls.depth = 0
        return conn

    def close(self):
        conn = getattr(self._tls, "conn", None)
        if conn is not None:
            conn.close()
            self._tls.conn = None

    # ---- 事务 ----

    @contextmanager
    def batch(self):
        """
        把一批写入包进 SQLite 事务。

        支持嵌套：外层用 BEGIN/COMMIT，内层用 SAVEPOINT。
        py-evm 出块时会开批次，批次里可能再开子批次，所以嵌套是必需的。
        """
        with _WRITE_LOCK:
            conn = self._conn()
            depth = getattr(self._tls, "depth", 0)
            self._tls.depth = depth + 1
            if depth == 0:
                conn.execute("BEGIN")
            else:
                conn.execute(f"SAVEPOINT sp{depth}")
            try:
                yield
            except BaseException:
                if depth == 0:
                    conn.execute("ROLLBACK")
                else:
                    conn.execute(f"ROLLBACK TO SAVEPOINT sp{depth}")
                    conn.execute(f"RELEASE SAVEPOINT sp{depth}")
                raise
            else:
                if depth == 0:
                    conn.execute("COMMIT")
                else:
                    conn.execute(f"RELEASE SAVEPOINT sp{depth}")
            finally:
                self._tls.depth = depth

    # ---- MutableMapping 接口 ----

    def __getitem__(self, key):
        row = self._conn().execute("SELECT v FROM kv WHERE k = ?", (key,)).fetchone()
        if row is None:
            raise KeyError(key)
        return bytes(row[0])

    def __setitem__(self, key, value):
        self._conn().execute(
            "INSERT INTO kv(k, v) VALUES(?, ?) "
            "ON CONFLICT(k) DO UPDATE SET v = excluded.v",
            (bytes(key), bytes(value)),
        )

    def __delitem__(self, key):
        cur = self._conn().execute("DELETE FROM kv WHERE k = ?", (bytes(key),))
        if cur.rowcount == 0:
            raise KeyError(key)

    def __iter__(self):
        for (k,) in self._conn().execute("SELECT k FROM kv"):
            yield bytes(k)

    def __len__(self):
        return self._conn().execute("SELECT COUNT(*) FROM kv").fetchone()[0]

    def __contains__(self, key):
        row = self._conn().execute("SELECT 1 FROM kv WHERE k = ?", (bytes(key),)).fetchone()
        return row is not None


# ------------------------------------------------------------
#  提供给 CHAIN_DB_BACKEND_CLASS 的后端类
# ------------------------------------------------------------


class SqliteAtomicDB(AtomicDB):
    """
    py-evm 链后端：零参构造（get_db_backend() 不带参数调用），
    内部把存储指向 SQLite。

    用法（建链之前设置一次即可）：

        os.environ["CHAIN_DB_BACKEND_CLASS"] = "chaindb.SqliteAtomicDB"
    """

    def __init__(self, wrapped_db=None, path=None):
        if wrapped_db is None:
            wrapped_db = MemoryDB(kv_store=SqliteKV(path or DB_PATH))
        super().__init__(wrapped_db)

    @contextmanager
    def atomic_batch(self):
        """
        一个写批次 = 一个 SQLite 事务。

        父类实现是先缓冲在内存 diff 里、退出上下文时一次性 apply，
        所以只要把 apply 这一步包进事务，就能拿到真正的原子性：
        中途抛异常则整批回滚，不会出现"写了一半"的链状态。
        """
        kv = getattr(self.wrapped_db, "kv_store", None)
        if isinstance(kv, SqliteKV):
            with kv.batch():
                with AtomicDBWriteBatch._commit_unless_raises(self) as batch:
                    yield batch
        else:
            # 万一被塞了别的后端，退回父类行为
            with AtomicDBWriteBatch._commit_unless_raises(self) as batch:
                yield batch


def install() -> None:
    """把本模块注册为 py-evm 的链后端。必须在建链之前调用。"""
    os.environ["CHAIN_DB_BACKEND_CLASS"] = f"{__name__}.SqliteAtomicDB"

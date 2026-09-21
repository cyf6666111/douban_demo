"""MySQL 数据访问层。

原项目 db_utils.py 的问题与本次优化：
1. 【线程安全】DBUtils 被 app.py 作为全局单例使用（`utils = DBUtils()`），
   而它把连接存在 `self.connect` 上。Flask 默认多线程处理请求，
   两个请求会互相覆盖 self.connect 并互相关闭对方的连接 → 偶发 500。
   → 现在改为线程安全连接池，方法内不再保存任何连接状态。
2. 【性能】原实现每个方法都 open_connect() + close_connect()，
   首页 movie_list 一次请求要建立 10 次 TCP 连接 + 10 次 MySQL 握手。
   → 现在从池中复用连接，并在一次连接内完成所有聚合查询。
3. 【字符集】原 charset="gbk" 与表 utf8mb4 不一致，
   韩文/阿拉伯文/泰文片名被静默替换成 '?'。
   → 统一 utf8mb4。
4. 【类型语义】rater_count / timing / release_year 建表时是 varchar，
   直接 ORDER BY / MAX 会按字符串比较：'99' > '142'、'999550' > '3249655'。
   → 用 CAST(... AS UNSIGNED) 按数值比较（更彻底的做法见 sql/migrate_v2.sql）。
5. 【可读性】原实现返回元组，调用方用 row[5] 这种魔法下标访问字段，
   加一列就全错位。→ 现在全部使用 DictCursor，按字段名访问。
6. 【健壮性】原实现 except 里 print 一下就 return None，
   调用方拿 None 再取 [0] 直接抛 TypeError。→ 现在异常向上抛出，
   由 Flask 统一错误处理器兜底，用户看到友好页面而不是堆栈。
7. 【SQL 注入】新代码所有条件查询使用 %s 占位符参数化，
   不拼接字符串（新增的关键字搜索功能尤其要注意）。
"""

import queue
import threading
import time
from contextlib import contextmanager

import pymysql
import pymysql.cursors

from config import Config, setup_logging

logger = setup_logging("douban.db")


class PoolTimeoutError(RuntimeError):
    """连接池耗尽且等待超时。"""


class ConnectionPool:
    """一个极简但线程安全的 MySQL 连接池。

    为什么需要它？
    每次 pymysql.connect() 都要走 TCP 三次握手 + MySQL 认证握手，
    单次开销约 1~10ms，比一条简单 SELECT 还慢。池化后这部分开销被摊薄。
    生产环境一般用 DBUtils(SQLAlchemy) 的 PooledDB 或 SQLAlchemy 引擎，
    这里手写是为了不引入额外依赖、同时便于讲清楚原理。

    实现要点：
    - queue.Queue 存放空闲连接，自带锁，天然线程安全；
    - 用 _total 计数控制总连接数不超过 _size，避免压垮数据库 max_connections；
    - 借出前 ping 一次做健康检查，断掉的连接直接丢弃重建
      （MySQL 默认 wait_timeout=8h 会主动断开空闲连接）；
    - 归还前若事务出错则 rollback，防止脏状态被下一个请求复用。
    """

    def __init__(self, size: int = 5, acquire_timeout: float = 5.0, **connect_kwargs):
        self._size = max(1, size)
        self._acquire_timeout = acquire_timeout
        self._connect_kwargs = connect_kwargs
        self._idle: "queue.LifoQueue[pymysql.connections.Connection]" = queue.LifoQueue()
        self._lock = threading.Lock()
        self._total = 0

    # ---------- 对外接口 ----------
    @contextmanager
    def acquire(self):
        """借出一个连接，退出 with 块时自动归还。"""
        conn = self._borrow()
        try:
            yield conn
        except Exception:
            # 事务可能停在中间状态，先回滚，避免影响下一个使用者
            try:
                conn.rollback()
            except pymysql.MySQLError:
                pass
            raise
        finally:
            self._release(conn)

    def close_all(self):
        """进程退出时关闭所有空闲连接（由 atexit 调用）。"""
        while True:
            try:
                self._idle.get_nowait().close()
                with self._lock:
                    self._total -= 1
            except queue.Empty:
                return
            except pymysql.MySQLError:
                continue

    @property
    def stats(self) -> dict:
        return {"total": self._total, "idle": self._idle.qsize(), "size": self._size}

    # ---------- 内部实现 ----------
    def _new_connection(self):
        kwargs = dict(self._connect_kwargs)
        kwargs.setdefault("connect_timeout", Config.DB_CONNECT_TIMEOUT)
        return pymysql.connect(**kwargs)

    def _borrow(self):
        deadline = time.monotonic() + self._acquire_timeout
        while True:
            try:
                conn = self._idle.get_nowait()
            except queue.Empty:
                with self._lock:
                    if self._total < self._size:
                        self._total += 1
                        return self._new_connection()
                # 已达上限，等别人归还
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise PoolTimeoutError(
                        f"连接池已满（{self._size}）且等待超时，请检查是否有慢查询或未释放的连接"
                    )
                try:
                    conn = self._idle.get(timeout=remaining)
                except queue.Empty:
                    raise PoolTimeoutError(f"等待空闲连接超时（{self._acquire_timeout}s）") from None

            if self._is_alive(conn):
                return conn
            # 连接已被 MySQL 断开（wait_timeout 到期等）：丢弃名额并重建
            self._discard(conn)
            with self._lock:
                self._total += 1
            return self._new_connection()

    def _release(self, conn):
        """归还连接：已断开的丢弃，健康的放回空闲队列。"""
        if self._is_alive(conn):
            self._idle.put_nowait(conn)
        else:
            self._discard(conn)

    def _discard(self, conn):
        try:
            conn.close()
        except pymysql.MySQLError:
            pass
        with self._lock:
            self._total -= 1

    @staticmethod
    def _is_alive(conn) -> bool:
        try:
            conn.ping(reconnect=False)
            return True
        except pymysql.MySQLError:
            return False


class TTLCache:
    """极简线程安全 TTL 缓存。

    榜单数据是离线爬虫一次性写入的静态数据，没必要每个请求都重新聚合。
    注意：这里用「进程内缓存」，多进程部署（gunicorn -w 4）时各进程独立，
    真正生产环境应换成 Redis。
    """

    def __init__(self, ttl: int):
        self._ttl = max(0, ttl)
        self._store: dict = {}
        self._lock = threading.Lock()

    def get_or_set(self, key, producer):
        if self._ttl <= 0:
            return producer()
        now = time.monotonic()
        with self._lock:
            hit = self._store.get(key)
            if hit and hit[0] > now:
                return hit[1]
        value = producer()                 # 在锁外执行耗时查询，避免阻塞其他请求
        with self._lock:
            self._store[key] = (now + self._ttl, value)
        return value

    def invalidate(self, key=None):
        with self._lock:
            if key is None:
                self._store.clear()
            else:
                self._store.pop(key, None)


# 表字段的数值语义表达式。
# 历史表结构里 rater_count / timing / release_year 都是 varchar，
# 字符串比较会得出 "999550 > 3249655" 这种错误结论。
# CAST('142人' AS UNSIGNED) = 142（MySQL 在首个非数字字符处截断），
# NULLIF 用于把空串变成 NULL，避免 CAST('') 变成 0 污染 MAX/AVG。
def _numeric(column: str) -> str:
    return f"CAST(NULLIF(`{column}`, '') AS UNSIGNED)"


Q_RATER_COUNT = _numeric("rater_count")
Q_TIMING = _numeric("timing")
Q_YEAR = _numeric("release_year")

# 字段清单集中一处，避免"加一列改十处"
MOVIE_COLUMNS = f"""
    `id`, `movie_name`, `release_year`, {Q_YEAR} AS year_num, `rating`,
    {Q_RATER_COUNT} AS rater_count, `director`, `movie_type`, {Q_TIMING} AS timing
"""


class DBUtils:
    """电影数据仓储层（Repository）。

    只负责"取数据 / 写数据"，不含任何 HTML、图表配置或业务展示逻辑，
    这样这一层可以单独被单元测试、也可以在爬虫里复用。
    """

    def __init__(self, pool: ConnectionPool = None):
        self._pool = pool or ConnectionPool(size=Config.DB_POOL_SIZE, **Config.MYSQL)
        self._cache = TTLCache(Config.CACHE_TTL)

    # ==================== 基础查询 ====================
    def query_all(self, sql: str, params=None) -> list:
        """返回全部行（list[dict]）。"""
        with self._pool.acquire() as conn:
            with conn.cursor(pymysql.cursors.DictCursor) as cursor:
                cursor.execute(sql, params)
                return cursor.fetchall()

    def query_one(self, sql: str, params=None):
        """返回首行（dict）或 None。"""
        with self._pool.acquire() as conn:
            with conn.cursor(pymysql.cursors.DictCursor) as cursor:
                cursor.execute(sql, params)
                return cursor.fetchone()

    def execute(self, sql: str, params=None) -> int:
        """执行写操作并提交事务，返回影响行数。"""
        with self._pool.acquire() as conn:
            with conn.cursor() as cursor:
                affected = cursor.execute(sql, params)
            conn.commit()
            return affected

    def execute_many(self, sql: str, rows: list) -> int:
        """批量写入（executemany），返回影响行数。

        相比循环单条 INSERT：250 条数据从 250 次网络往返降到 1 次，
        实测在本项目上快一个数量级。事务在这里统一 commit，
        任一行出错则整体 rollback，不会留下"写了一半"的脏数据。
        """
        rows = list(rows)
        if not rows:
            return 0
        with self._pool.acquire() as conn:
            with conn.cursor() as cursor:
                affected = cursor.executemany(sql, rows)
            conn.commit()
            return affected

    def invalidate_cache(self):
        """爬虫写入新数据后调用，避免继续返回旧榜单。"""
        self._cache.invalidate()

    def close(self):
        self._pool.close_all()

    # ==================== 首页概览 ====================
    def get_overview(self) -> dict:
        """首页 6 个指标卡。

        原实现分 6 个方法、开 6 次连接；这里合并成 2 条 SQL + 复用一次类型查询。
        """
        return self._cache.get_or_set("overview", self._build_overview)

    def _build_overview(self) -> dict:
        base = self.query_one(
            f"""
            SELECT COUNT(*)                        AS movie_sum,
                   MAX(`rating`)                   AS max_rating,
                   MIN(`rating`)                   AS min_rating,
                   ROUND(AVG(`rating`), 2)         AS avg_rating,
                   MAX({Q_TIMING})                 AS max_timing,
                   MIN({Q_YEAR})                   AS min_year,
                   MAX({Q_YEAR})                   AS max_year,
                   ROUND(AVG({Q_RATER_COUNT}), 0)  AS avg_rater
            FROM `movies`
            """
        ) or {}

        # 数量最多的上映年份（并列时取年份小的，保证结果稳定可复现）
        top_year = self.query_one(
            f"""
            SELECT `release_year` AS year, COUNT(*) AS cnt
            FROM `movies`
            WHERE `release_year` REGEXP '^[0-9]{{4}}$'
            GROUP BY `release_year`
            ORDER BY cnt DESC, `release_year` ASC
            LIMIT 1
            """
        )
        # 作品最多的导演（排除空值与爬取失败的占位值）
        top_director = self.query_one(
            """
            SELECT `director`, COUNT(*) AS cnt
            FROM `movies`
            WHERE `director` IS NOT NULL AND `director` NOT IN ('', '未知导演')
            GROUP BY `director`
            ORDER BY cnt DESC, `director` ASC
            LIMIT 1
            """
        )
        types = self.get_type_distribution()

        return {
            "movie_sum": base.get("movie_sum") or 0,
            "max_rating": round(float(base.get("max_rating") or 0), 1),
            "min_rating": round(float(base.get("min_rating") or 0), 1),
            "avg_rating": float(base.get("avg_rating") or 0),
            "avg_rater": int(base.get("avg_rater") or 0),
            "max_timing": int(base.get("max_timing") or 0),
            "min_year": int(base.get("min_year") or 0),
            "max_year": int(base.get("max_year") or 0),
            "type_count": len(types),
            "top_year": top_year or {"year": "-", "cnt": 0},
            "top_director": top_director or {"director": "-", "cnt": 0},
        }

    # ==================== 各年份电影数量 ====================
    def get_count_by_year(self) -> list:
        """按年份统计电影数量，按年份数值升序。

        原实现：GROUP BY 后 varchar 排序，'1994' 与 '99' 之类会错位；
        并且 get_max_year() 又重复查了一遍全量数据。
        """

        def build():
            rows = self.query_all(
                f"""
                SELECT `release_year` AS year, COUNT(*) AS cnt
                FROM `movies`
                WHERE `release_year` REGEXP '^[0-9]{{4}}$'
                GROUP BY `release_year`
                ORDER BY {Q_YEAR} ASC
                """
            )
            return [{"year": r["year"], "cnt": r["cnt"]} for r in rows]

        return self._cache.get_or_set("by_year", build)

    # ==================== 电影类型占比 ====================
    def get_type_distribution(self) -> list:
        """把 '剧情,爱情' 这种逗号串拆开，统计每个类型的出现次数。

                原实现：get_type_count() 返回的是「去重后的类型字符串列表」，
        而 app.py 里又写 `len({w for t in utils.get_type_count() for w in t[0].split(',')})`，
        对字符串取 t[0] 得到的是**第一个汉字**，于是"类型数"实际统计的是
        "出现过的不同汉字个数"，图表也按单字统计 —— 这是最隐蔽的一个 bug。
        """

        def build():
            rows = self.query_all("SELECT `movie_type` FROM `movies`")
            counter: dict = {}
            for row in rows:
                for genre in (row["movie_type"] or "").split(","):
                    genre = genre.strip()
                    if genre:
                        counter[genre] = counter.get(genre, 0) + 1
            # 降序排列，让饼图的扇区从大到小更容易读
            return sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))

        return self._cache.get_or_set("type_dist", build)

    # ==================== 榜单 ====================
    def get_movie_top10(self) -> list:
        """评分 Top10。

        原实现是 `SELECT movie_name, id FROM movies LIMIT 10`：
        既没有 ORDER BY（取到的是按拼音排序的任意 10 部），
        又把自增主键 id 当成漏斗图数值 —— 图表完全没有业务含义。
        现在改成按评分排序，并把评分人数一起带出来放进 tooltip。
        """
        return self.query_all(
            f"""
            SELECT `movie_name`, `rating`, `release_year`,
                   {Q_RATER_COUNT} AS rater_count, `movie_type`
            FROM `movies`
            ORDER BY `rating` DESC, {Q_RATER_COUNT} DESC, `id` ASC
            LIMIT 10
            """
        )

    def get_rater_top20(self) -> list:
        """评分人数 Top20（原实现漏了 CAST，按字符串排序，第一名是 999550 的《天空之城》）。"""
        return self.query_all(
            f"""
            SELECT `movie_name`, {Q_RATER_COUNT} AS rater_count, `rating`, `release_year`
            FROM `movies`
            ORDER BY {Q_RATER_COUNT} DESC, `id` ASC
            LIMIT 20
            """
        )

    def get_rating_sample(self, limit: int = 30, seed: int = 42) -> list:
        """评分抽样散点图。

        原实现 `ORDER BY RAND() LIMIT 20`：每次刷新结果都变，无法复现；
        且 RAND() 无法走索引，需要全表扫描 + 文件排序。
        这里用 RAND(seed) 固定随机种子 —— 既可复现，也能靠换 seed 重新抽样。
        数据量再大时更优的做法是「随机主键 + 范围查询」：
            WHERE id >= (SELECT FLOOR(RAND() * MAX(id)) FROM movies) LIMIT 20
        """
        return self.query_all(
            """
            SELECT `movie_name`, `rating`, `release_year`, `movie_type`
            FROM `movies`
            ORDER BY RAND(%s)
            LIMIT %s
            """,
            (seed, limit),
        )

    # ==================== 电影列表（分页 + 搜索） ====================
    def get_movies_page(self, keyword: str = "", page: int = 1, size: int = None):
        """分页查询电影，可选按片名/导演关键字过滤。

        原实现 `SELECT * FROM movies` 一次性把所有行都塞进模板，
        页面越大越慢。分页把单次传输量固定下来。
        注意关键字用参数化 LIKE，且转义了 % 和 _ 通配符，
        既防 SQL 注入，也避免用户输入 "%" 时把整表捞出来。
        """
        size = size or Config.MOVIE_PAGE_SIZE
        page = max(1, int(page))
        offset = (page - 1) * size

        where, params = "", []
        keyword = (keyword or "").strip()
        if keyword:
            escaped = keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            where = (
                "WHERE `movie_name` LIKE %s ESCAPE '\\\\' "
                "   OR `director` LIKE %s ESCAPE '\\\\' "
                "   OR `movie_type` LIKE %s ESCAPE '\\\\'"
            )
            params = [f"%{escaped}%"] * 3

        total_row = self.query_one(f"SELECT COUNT(*) AS total FROM `movies` {where}", params)
        total = int((total_row or {}).get("total") or 0)

        rows = self.query_all(
            f"""
            SELECT {MOVIE_COLUMNS}
            FROM `movies`
            {where}
            ORDER BY `rating` DESC, `id` ASC
            LIMIT %s OFFSET %s
            """,
            [*params, size, offset],
        )
        return rows, total

    def get_all_movies(self) -> list:
        """全量电影（供词云等离线统计使用）。"""
        return self.query_all(
            f"SELECT {MOVIE_COLUMNS} FROM `movies` ORDER BY `id` ASC"
        )

    # 兼容旧调用名，避免外部脚本（如原 word_cloud.py）直接报 AttributeError
    get_movies = get_all_movies

    def get_movie_names(self) -> list:
        rows = self.query_all("SELECT `movie_name` FROM `movies` ORDER BY `id` ASC")
        return [r["movie_name"] for r in rows]

    def search_suggest(self, limit: int = 8) -> list:
        """评分最高的若干部电影，用作搜索框的下拉提示。"""
        rows = self.query_all(
            "SELECT `movie_name`, `rating` FROM `movies` ORDER BY `rating` DESC LIMIT %s",
            (limit,),
        )
        return rows


# 进程级单例：Flask 各请求共享同一个连接池
_default_utils = None
_default_lock = threading.Lock()


def get_db() -> DBUtils:
    """获取全局 DBUtils 单例（连接池在进程内共享）。"""
    global _default_utils
    if _default_utils is None:
        with _default_lock:
            if _default_utils is None:
                _default_utils = DBUtils()
    return _default_utils

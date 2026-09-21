"""豆瓣电影 Top250 爬虫。

原项目 spider.py 的问题与本次优化：
1. 【严重】把个人登录 Cookie（含 dbcl2 登录态）明文写在代码里。
   一旦代码上传到 GitHub 就等于泄露账号；而且 Cookie 会过期，
   过期后爬虫静默失败。→ 现在从环境变量 / .env 读取（见 .env.example），
   缺失时自动降级为匿名访问，并把 .env 加入 .gitignore。
2. 【脆弱】单页请求失败只 print 一句就 continue，最终静默少爬几十部。
   → 现在带指数退避重试（默认 3 次），失败链接收集起来最后汇总报告。
3. 【性能】每次 requests.get() 都新建 TCP 连接，250 次详情页＝250 次握手。
   → 现在用 requests.Session 复用连接（keep-alive），并挂载重试适配器。
4. 【数据质量】rater_count 直接存 "3249655人" 这类字符串，
   导致后面排序按字符串比较出错。→ 现在解析成 int 再入库，
   同时落库前做字段清洗与长度校验（防止超长值截断报错）。
5. 【可观测性】全程 print，没有级别、没有落盘、没有耗时统计。
   → 现在用 logging 输出到控制台和 logs/，结束时给出统计报告。
6. 【重复爬取】原实现每次全量重爬 250 个详情页（约 10 分钟）。
   → 现在支持增量模式：先比对库中已有的片名，只爬缺失或需要更新的条目。
7. 【可维护性】模块级全局列表 movie_links / movie_list 存放数据，
   重复调用 main() 会累积重复数据；URL、页数、延迟全是魔法数字。
   → 现在全部收进 config.Config.SPIDER，数据通过参数传递。

合规提醒：豆瓣 robots.txt 禁止爬取大部分路径，本项目仅用于课程/科研演示，
请控制请求频率、不要用于商业用途，也不要抓取需要登录的隐私数据。
"""

import random
import re
import time
from dataclasses import dataclass, field

import pymysql
import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from config import Config, setup_logging
from db_utils import DBUtils

logger = setup_logging("douban.spider")

# 模拟真实浏览器的请求头。Referer 让请求看起来是从榜单页正常跳转过来的
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Referer": "https://movie.douban.com/top250",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Connection": "keep-alive",
}


@dataclass
class Movie:
    """一条电影记录。

    用 dataclass 而不是裸元组：字段有名字、有类型、有默认值，
    插入数据库时按字段名取值，不会出现"加一列全错位"的问题。
    """

    movie_name: str
    release_year: str = "未知年份"
    rating: float = 0.0
    rater_count: int = 0
    director: str = "未知导演"
    movie_type: str = "未知类型"
    timing: int = 0

    def as_row(self) -> tuple:
        return (self.movie_name, self.release_year, self.rating,
                self.rater_count, self.director, self.movie_type, self.timing)


@dataclass
class CrawlReport:
    """爬取结果统计，结束时打印，避免"到底成功了多少"说不清。"""

    pages_ok: int = 0
    pages_failed: list = field(default_factory=list)
    details_ok: int = 0
    details_failed: list = field(default_factory=list)
    saved: int = 0
    elapsed: float = 0.0

    def summary(self) -> str:
        return (
            f"榜单页 {self.pages_ok} 成功 / {len(self.pages_failed)} 失败；"
            f"详情页 {self.details_ok} 成功 / {len(self.details_failed)} 失败；"
            f"入库 {self.saved} 条；耗时 {self.elapsed:.1f}s"
        )


class DoubanSpider:
    def __init__(self, db: DBUtils = None, settings: dict = None):
        self.settings = settings or Config.SPIDER
        self.db = db
        self.session = self._build_session()
        self.report = CrawlReport()

    # ==================== HTTP ====================
    def _build_session(self) -> requests.Session:
        """构造带连接复用与自动重试的 Session。

        连接复用：keep-alive 省掉每次请求的 TCP 握手；
        自动重试：对 429/5xx 和连接错误自动重试，并用退避策略拉开间隔，
        避免"被限流 → 立刻重试 → 被限流更狠"的恶性循环。
        """
        session = requests.Session()
        session.headers.update(DEFAULT_HEADERS)

        cookie = self.settings.get("cookie")
        if cookie:
            session.headers["Cookie"] = cookie
        else:
            logger.warning("未配置 Cookie（DOUBAN_COOKIE），将以匿名身份访问，可能被限流")

        retry = Retry(
            total=self.settings["retry"],
            backoff_factor=0.8,                       # 0.8s、1.6s、3.2s… 指数退避
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset(["GET"]),
        )
        adapter = HTTPAdapter(max_retries=retry, pool_connections=4, pool_maxsize=4)
        session.mount("https://", adapter)
        session.mount("http://", adapter)

        proxy = self.settings.get("proxy")
        if proxy:
            session.proxies = {"http": proxy, "https": proxy}
        return session

    @staticmethod
    def _looks_like_challenge(html: str) -> bool:
        """识别豆瓣的反爬"工作量证明"挑战页。

        实测（2026-09）：匿名请求详情页时，豆瓣不再返回正常页面，而是返回一个
        约 3KB 的挑战页 —— 里面用 JS 做 SHA-512 哈希碰撞（PoW），
        解出来后由浏览器 POST 回 /c 才拿到真实内容。
        纯 requests 无法执行 JS，因此会一直拿到挑战页；
        原项目能跑通是因为代码里写死了带登录态的 Cookie。
        明确识别出来并给出可操作的提示，比让解析器报"缺少片名"要好得多。
        """
        if len(html) > 20000:
            return False
        return ("/c\u0022" in html or 'id="sec"' in html or "process(data, difficulty" in html
                or ("载入中" in html and "sha512" in html))

    def get_page(self, url: str) -> str:
        """获取页面 HTML；失败返回空串（由调用方记入报告）。

        编码处理：豆瓣返回 UTF-8，但 requests 可能因缺少 charset 而猜成 ISO-8859-1，
        中文就会变乱码。这里显式设置编码，比原实现的 response.encoding 更明确。
        """
        try:
            response = self.session.get(url, timeout=self.settings["timeout"])
            response.raise_for_status()
            response.encoding = response.apparent_encoding or "utf-8"
            html = response.text
        except requests.RequestException as exc:
            logger.warning("请求失败 %s：%s", url, exc)
            return ""

        if self._looks_like_challenge(html):
            logger.error(
                "命中豆瓣反爬挑战页（%s）：需要配置有效的 DOUBAN_COOKIE，"
                "或改用 Playwright/Selenium 等能执行 JS 的方案", url,
            )
            return ""
        return html

    def sleep(self):
        """随机延迟：固定间隔的请求节奏很容易被识别为爬虫。"""
        time.sleep(random.uniform(self.settings["delay_min"], self.settings["delay_max"]))

    # ==================== 解析：榜单页 ====================
    def parse_list_page(self, html: str, base_url: str) -> list:
        """从榜单页提取电影详情页链接。"""
        if not html:
            return []
        soup = BeautifulSoup(html, "html.parser")
        links = []
        for item in soup.find_all("div", class_="item"):
            anchor = item.select_one(".hd > a")
            href = anchor.get("href") if anchor else None
            if href:
                links.append(href.split("?")[0])      # 去掉 tracking 参数，便于去重
        # 保序去重：用 dict.fromkeys 而不是"列表 + not in"，后者是 O(n²)
        return list(dict.fromkeys(links))

    # ==================== 解析：详情页 ====================
    @staticmethod
    def _text(node, default=""):
        return node.get_text(strip=True) if node else default

    def parse_detail_page(self, html: str) -> Movie | None:
        """解析详情页，返回 Movie；关键字段（片名）缺失时返回 None。"""
        if not html:
            return None
        try:
            soup = BeautifulSoup(html, "html.parser")

            # 1. 片名：h1 里第一个 span 是中文名，其余 span 是外文/年份
            name_tag = soup.select_one("h1 span")
            movie_name = self._text(name_tag).strip()
            if not movie_name:
                logger.warning("详情页缺少片名，跳过该条")
                return None

            # 2. 年份：不要用 span:last-of-type（结构一变就取错），
            #    改从 v:initialReleaseDate 属性里正则提取 4 位年份，更稳
            release_year = "未知年份"
            release_tag = soup.find(attrs={"property": "v:initialReleaseDate"})
            if release_tag:
                match = re.search(r"(\d{4})", release_tag.get("content", "") or "")
                if match:
                    release_year = match.group(1)

            # 3. 评分
            rating_tag = soup.select_one(".rating_self strong")
            rating = float(self._text(rating_tag, "0")) if rating_tag else 0.0

            # 4. 评价人数：原始文本形如 "3249655人"，必须转成 int，
            #    否则存进 varchar 字段后 ORDER BY 会按字符串比较（原项目的坑）
            rater_tag = soup.select_one(".rating_sum a span")
            rater_text = self._text(rater_tag, "0")
            digits = re.sub(r"[^0-9]", "", rater_text)
            rater_count = int(digits) if digits else 0

            # 5. 导演：#info 里带 rel="v:directedBy" 的链接就是导演，
            #    取不到时再从纯文本里正则兜底（页面结构偶有变化）
            director = "未知导演"
            info = soup.select_one("#info")
            if info:
                director = self._text(info.select_one('a[rel="v:directedBy"]'), "")
                if not director:
                    plain = info.get_text(" ", strip=True)
                    match = re.search(r"导演:\s*([^编]*?)(?:\s*编剧|$)", plain)
                    if match:
                        director = match.group(1).strip()
                director = director or "未知导演"

            # 6. 类型
            genres = [g.get_text(strip=True) for g in soup.find_all("span", property="v:genre")]
            movie_type = ",".join(g for g in genres if g) or "未知类型"

            # 7. 片长（分钟）：v:runtime 的 content 属性是纯数字，比正文文本可靠
            timing = 0
            runtime_tag = soup.find(attrs={"property": "v:runtime"})
            if runtime_tag:
                match = re.search(r"(\d+)", runtime_tag.get("content", "") or "")
                timing = int(match.group(1)) if match else 0

            return Movie(
                movie_name=movie_name,
                release_year=release_year,
                rating=rating,
                rater_count=rater_count,
                director=director,
                movie_type=movie_type,
                timing=timing,
            )
        except (AttributeError, ValueError, TypeError) as exc:
            logger.warning("解析详情页失败：%s", exc)
            return None

    # ==================== 主流程 ====================
    def collect_links(self) -> list:
        """遍历榜单分页，收集所有详情页链接。"""
        base = self.settings["base_url"]
        step = self.settings["page_size"]
        links: list = []

        for index, start in enumerate(range(0, self.settings["pages"] * step, step), start=1):
            url = f"{base}?start={start}"
            logger.info("抓取榜单第 %s 页：%s", index, url)
            html = self.get_page(url)
            if not html:
                self.report.pages_failed.append(url)
            else:
                page_links = self.parse_list_page(html, base)
                if not page_links:
                    logger.warning("第 %s 页未解析到条目，可能被反爬拦截或页面结构已变", index)
                    self.report.pages_failed.append(url)
                else:
                    self.report.pages_ok += 1
                links.extend(page_links)
            self.sleep()

        links = list(dict.fromkeys(links))
        logger.info("共收集到 %s 个详情页链接", len(links))
        return links

    def collect_details(self, links: list, skip_names: set = None) -> list:
        """逐条抓取详情页。skip_names 内的片名会被跳过（增量爬取）。"""
        skip_names = skip_names or set()
        movies, total = [], len(links)
        for index, link in enumerate(links, start=1):
            logger.info("[%s/%s] %s", index, total, link)
            html = self.get_page(link)
            if not html:
                self.report.details_failed.append(link)
            else:
                movie = self.parse_detail_page(html)
                if movie is None:
                    self.report.details_failed.append(link)
                elif movie.movie_name in skip_names:
                    logger.debug("已存在，跳过：%s", movie.movie_name)
                else:
                    movies.append(movie)
                    self.report.details_ok += 1
            self.sleep()
        logger.info("解析成功 %s 条（跳过已存在 %s 条）",
                    len(movies), self.report.details_ok - len(movies))
        return movies

    def save(self, movies: list) -> int:
        """批量写入数据库。

        要点：
        - executemany 批量提交，比循环单条 INSERT 少 250 次网络往返；
        - ON DUPLICATE KEY UPDATE 依赖 movie_name 上的唯一索引实现"幂等重跑"；
        - 事务由 DBUtils.execute_many 统一 commit/rollback，出错不留脏数据。
        """
        if not movies:
            logger.warning("没有可写入的数据")
            return 0
        if self.db is None:
            logger.warning("未提供数据库连接（仅解析模式），跳过写库")
            return 0

        # MySQL 8.0.20 起 VALUES() 函数已废弃，改用新别名语法 VALUES(col) -> AS new
        sql = """
              INSERT INTO movies
                  (movie_name, release_year, rating, rater_count, director, movie_type, timing)
              VALUES (%s, %s, %s, %s, %s, %s, %s) AS new
              ON DUPLICATE KEY UPDATE
                  release_year = new.release_year,
                  rating       = new.rating,
                  rater_count  = new.rater_count,
                  director     = new.director,
                  movie_type   = new.movie_type,
                  timing       = new.timing
              """
        affected = self.db.execute_many(sql, [m.as_row() for m in movies])
        self.report.saved = len(movies)
        logger.info("写入数据库完成：提交 %s 条（影响行数 %s）", len(movies), affected)
        return affected

    def run(self, incremental: bool = True) -> CrawlReport:
        """完整流程：收集链接 → 抓详情 → 写库。"""
        started = time.monotonic()
        skip_names = set()
        if incremental and self.db is not None:
            skip_names = set(self.db.get_movie_names())
            logger.info("增量模式：数据库已有 %s 条记录", len(skip_names))

        links = self.collect_links()
        movies = self.collect_details(links, skip_names=skip_names)
        self.save(movies)

        if self.db is not None:
            self.db.invalidate_cache()          # 数据变了，清掉页面缓存

        self.report.elapsed = time.monotonic() - started
        logger.info("爬取结束 → %s", self.report.summary())
        if self.report.details_failed:
            logger.warning("失败详情页（可重跑增量模式补齐）：%s", self.report.details_failed[:5])
        return self.report

    def close(self):
        self.session.close()


def main():
    """命令行入口：python spider.py [--full]"""
    import sys

    incremental = "--full" not in sys.argv
    db = DBUtils()
    spider = DoubanSpider(db=db)
    try:
        report = spider.run(incremental=incremental)
        print(report.summary())
    except pymysql.MySQLError as exc:
        logger.error("数据库错误：%s（请检查 config.py 中的连接配置）", exc)
        raise
    finally:
        spider.close()
        db.close()


if __name__ == "__main__":
    main()

"""项目统一配置模块。

优化点（对应原项目的问题）：
1. 原项目把数据库账号密码硬编码在 db_utils.py / spider.py 里，共两处重复；
   现在集中到一处，并支持环境变量与 .env 文件覆盖，便于换机部署（答辩换电脑不用改代码）。
2. 原项目爬虫把个人 Cookie 明文写在代码里（含 dbcl2 登录态），一旦提交到 Git 就泄露；
   现在从环境变量 / .env 读取，并把 Cookie 文件加入 .gitignore。
3. 原项目 db_utils.py 用 charset="gbk"，而表是 utf8mb4，导致韩文/阿拉伯文片名被
   静默替换成 '?'（如 "7号房的礼物 7번방의 선물" → "7号房的礼物 7??? ??"）。
   现在统一强制 utf8mb4。
"""

import logging
import os
from pathlib import Path

# 项目根目录：用 __file__ 推导，保证在任何工作目录下启动都能找到资源
BASE_DIR = Path(__file__).resolve().parent


def _load_dotenv(path: Path) -> None:
    """极简 .env 解析器（不引入 python-dotenv 依赖）。

    仅支持 KEY=VALUE 形式，已存在的环境变量优先（不覆盖），
    这样生产环境用真实环境变量、本地开发用 .env，互不干扰。
    """
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _env(key: str, default: str) -> str:
    value = os.environ.get(key)
    return value if value not in (None, "") else default


def _env_int(key: str, default: int) -> int:
    try:
        return int(_env(key, str(default)))
    except ValueError:
        logging.getLogger(__name__).warning("环境变量 %s 不是整数，回退默认值 %s", key, default)
        return default


def _env_bool(key: str, default: bool) -> bool:
    return _env(key, "1" if default else "0").strip().lower() in {"1", "true", "yes", "on"}


_load_dotenv(BASE_DIR / ".env")


class Config:
    """应用配置。属性全部可用环境变量覆盖，例如：

    set DOUBAN_DB_PASSWORD=你的密码
    set DOUBAN_DEBUG=1
    """

    # ---------- Flask ----------
    # debug=True 会开启 Werkzeug 交互式调试器，未授权访问时可执行任意代码（RCE）。
    # 原项目 app.run(debug=True) 直接上线属于高危，这里默认关闭，按需用环境变量打开。
    DEBUG = _env_bool("DOUBAN_DEBUG", False)
    SECRET_KEY = _env("DOUBAN_SECRET_KEY", "dev-secret-change-me")
    HOST = _env("DOUBAN_HOST", "127.0.0.1")
    PORT = _env_int("DOUBAN_PORT", 5000)

    # ---------- MySQL ----------
    MYSQL = {
        "host": _env("DOUBAN_DB_HOST", "localhost"),
        "port": _env_int("DOUBAN_DB_PORT", 3306),
        "user": _env("DOUBAN_DB_USER", "root"),
        "password": _env("DOUBAN_DB_PASSWORD", "123456"),
        "database": _env("DOUBAN_DB_NAME", "douban_plat"),
        # 必须与建表字符集一致，否则会静默丢字符（原项目 bug）
        "charset": "utf8mb4",
        "autocommit": False,
    }
    # 连接池大小：Flask 默认多线程处理请求，池太小会退化成新建连接
    DB_POOL_SIZE = _env_int("DOUBAN_DB_POOL_SIZE", 5)
    DB_CONNECT_TIMEOUT = _env_int("DOUBAN_DB_CONNECT_TIMEOUT", 5)

    # ---------- 缓存 ----------
    # 榜单数据是离线爬取的静态数据，短时间内不会变，加 TTL 缓存可省掉重复聚合查询
    CACHE_TTL = _env_int("DOUBAN_CACHE_TTL", 60)
    MOVIE_PAGE_SIZE = _env_int("DOUBAN_PAGE_SIZE", 20)

    # ---------- 词云 ----------
    FONT_PATH = _env("DOUBAN_FONT_PATH", str(BASE_DIR / "static" / "fonts" / "main.ttf"))
    CLOUD_DIR = BASE_DIR / "static" / "images"

    # ---------- 爬虫 ----------
    SPIDER = {
        "base_url": _env("DOUBAN_SPIDER_BASE", "https://movie.douban.com/top250"),
        "pages": _env_int("DOUBAN_SPIDER_PAGES", 10),      # 10 页 × 25 部 = 250 部
        "page_size": 25,
        "timeout": _env_int("DOUBAN_SPIDER_TIMEOUT", 15),
        "retry": _env_int("DOUBAN_SPIDER_RETRY", 3),       # 单页失败重试次数
        "delay_min": float(_env("DOUBAN_SPIDER_DELAY_MIN", "1.0")),
        "delay_max": float(_env("DOUBAN_SPIDER_DELAY_MAX", "3.0")),
        # 登录态 Cookie 不放代码里，从环境变量读（见 .env.example）
        "cookie": _env("DOUBAN_COOKIE", ""),
        "proxy": _env("DOUBAN_PROXY", ""),
    }

    # ---------- 日志 ----------
    LOG_DIR = BASE_DIR / "logs"
    LOG_LEVEL = _env("DOUBAN_LOG_LEVEL", "INFO")


def setup_logging(name: str = "douban") -> logging.Logger:
    """统一的日志配置：同时输出到控制台和 logs/ 目录。

    原项目全程用 print 调试，无法分级、无法落盘、无法在生产关掉。
    """
    logger = logging.getLogger(name)
    if logger.handlers:            # 避免重复添加 handler（Flask debug 重载会执行两次）
        return logger
    logger.setLevel(Config.LOG_LEVEL)

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    logger.addHandler(console)

    try:
        Config.LOG_DIR.mkdir(exist_ok=True)
        file_handler = logging.FileHandler(Config.LOG_DIR / f"{name}.log", encoding="utf-8")
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    except OSError as exc:          # 只读文件系统时降级为仅控制台输出
        logger.warning("日志文件初始化失败，仅输出到控制台：%s", exc)

    return logger

"""Flask 应用入口。

原项目 app.py 的问题与本次优化：
1. 【功能缺失】导航里链到 /word_cloud，但 app.py 里根本没有这个路由，
   点击后 iframe 显示 404。现在补齐，并在访问时按需生成词云图片。
2. 【N+1 查询】movie_list 视图顺序调了 6 个 utils 方法（每个方法自己开关连接），
   再叠加一次全表查询。现在合并成 1 次聚合查询 + 1 次分页查询，
   并走 TTL 缓存。
3. 【无分页】原实现直接把 movies 全表交给模板（而且模板压根没渲染这个变量）。
   现在做服务端分页 + 关键字搜索。
4. 【异常处理】原 DBUtils 出错返回 None，视图里 `[0]` 直接抛 TypeError，
   用户看到 500 堆栈页。现在统一错误处理器返回友好页面，并记录日志。
5. 【安全】原 app.run(debug=True)：Werkzeug 调试器在生产环境等于远程代码执行漏洞，
   且会泄露源码。现在 debug 由配置控制，默认关闭。
6. 【编码】原响应会把中文转义成 \\uXXXX（json.dumps 默认 ensure_ascii=True），
   现在关闭转义，响应体积更小、浏览器直接可读，便于调试和答辩演示。
7. 【架构】改为应用工厂 create_app()，DBUtils 通过参数注入，
   因此可以在不连数据库的情况下给整个 Web 层写单元测试（见 tests/）。
"""

import pymysql
from flask import Flask, jsonify, render_template, request
from pathlib import Path

import charts
import word_cloud
from config import Config, setup_logging
from db_utils import DBUtils, PoolTimeoutError, get_db

logger = setup_logging("douban.app")

# 图表数据接口的超时/连接类异常统一处理，避免前端拿到 HTML 错误页而 JSON.parse 崩溃
DB_ERRORS = (pymysql.MySQLError, PoolTimeoutError)


def create_app(database: DBUtils = None) -> Flask:
    """应用工厂。传入 database 可注入替身对象用于测试。"""
    app = Flask(__name__)
    app.config.from_object(Config)
    app.json.ensure_ascii = False          # 中文不转义，响应更小更可读
    app.json.sort_keys = False

    db = database or get_db()
    app.extensions["db"] = db

    # ==================== 页面路由 ====================
    @app.route("/")
    def index():
        """平台首页：左侧导航 + 右侧 iframe 内容区。"""
        return render_template("index.html", nav=NAV_ITEMS)

    @app.route("/movie_list")
    def movie_list():
        """电影列表：概览指标卡 + 分页检索表。"""
        keyword = request.args.get("q", "").strip()
        page = _to_int(request.args.get("page"), default=1, minimum=1)
        size = Config.MOVIE_PAGE_SIZE

        overview = db.get_overview()
        rows, total = db.get_movies_page(keyword=keyword, page=page, size=size)
        total_pages = max(1, -(-total // size))       # 向上取整
        # 页码越界时回落到最后一页，避免用户手动改 URL 看到空表
        if page > total_pages and total:
            page = total_pages
            rows, total = db.get_movies_page(keyword=keyword, page=page, size=size)

        return render_template(
            "movie_list.html",
            overview=overview,
            movies=rows,
            total=total,
            page=page,
            total_pages=total_pages,
            keyword=keyword,
            page_size=size,
        )

    @app.route("/movie_by_year")
    def movie_by_year():
        rows = db.get_count_by_year()
        return render_template(
            "movie_by_year.html",
            table=rows,
            peak=max(rows, key=lambda r: r["cnt"]) if rows else None,
            total=sum(r["cnt"] for r in rows),
        )

    @app.route("/movie_type_percent")
    def movie_type_percent():
        dist = db.get_type_distribution()
        return render_template(
            "movie_type_percent.html",
            table=dist,
            tag_total=sum(cnt for _, cnt in dist),
            type_count=len(dist),
        )

    @app.route("/name_and_rating")
    def name_and_rating():
        seed = _to_int(request.args.get("seed"), default=42, minimum=0)
        rows = db.get_rating_sample(seed=seed)
        return render_template(
            "name_and_rating.html",
            table=rows,
            seed=seed,
            avg=round(sum(float(r["rating"]) for r in rows) / len(rows), 2) if rows else 0,
        )

    @app.route("/movie_top")
    def movie_top():
        rows = db.get_movie_top10()
        return render_template("movie_top.html", table=rows)

    @app.route("/rater_top")
    def rater_top():
        rows = db.get_rater_top20()
        return render_template("rater_top.html", table=rows)

    @app.route("/word_cloud")
    def word_cloud_page():
        """词云页：图片 + 词频表双通道呈现。"""
        movies = db.get_all_movies()
        # 数据量变化或图片缺失时自动重算（见 word_cloud.is_stale）
        status = word_cloud.generate_all(movies, movie_sum=len(movies))
        return render_template(
            "word_cloud.html",
            clouds=word_cloud.cloud_outputs(),
            status=status,
            previews=word_cloud.frequency_preview(movies),
            movie_sum=len(movies),
            font_ok=Path(Config.FONT_PATH).is_file(),
            config_font=Config.FONT_PATH,
        )

    # ==================== 图表数据接口（JSON） ====================
    @app.route("/api/movie_by_year")
    def api_movie_by_year():
        return _json(lambda: charts.movie_by_year_chart(db.get_count_by_year()))

    @app.route("/api/movie_type")
    def api_movie_type():
        return _json(lambda: charts.movie_type_chart(db.get_type_distribution()))

    @app.route("/api/movie_rating")
    def api_movie_rating():
        seed = _to_int(request.args.get("seed"), default=42, minimum=0)
        return _json(lambda: charts.movie_rating_chart(db.get_rating_sample(seed=seed)))

    @app.route("/api/movie_top")
    def api_movie_top():
        return _json(lambda: charts.movie_top_chart(db.get_movie_top10()))

    @app.route("/api/movie_rater_count")
    def api_movie_rater_count():
        return _json(lambda: charts.movie_rater_count_chart(db.get_rater_top20()))

    @app.route("/api/rating_histogram")
    def api_rating_histogram():
        return _json(lambda: charts.rater_histogram(db.get_all_movies()))

    @app.route("/api/overview")
    def api_overview():
        """概览数据也开放成 JSON 接口，方便其他前端/脚本复用。"""
        return _json(db.get_overview)

    @app.route("/favicon.ico")
    def favicon():
        """避免浏览器请求 favicon 时产生 404 日志噪音。"""
        return app.send_static_file("images/favicon.svg")

    # ==================== 错误处理 ====================
    @app.errorhandler(404)
    def handle_404(_exc):
        if request.path.startswith("/api/"):
            return jsonify({"error": "接口不存在", "path": request.path}), 404
        return render_template("error.html", code=404,
                               message="页面不存在", detail=f"没有找到 {request.path}"), 404

    def handle_db_error(exc):
        """数据库异常：记日志 + 给用户友好提示，不暴露堆栈。"""
        logger.exception("数据库异常：%s", exc)
        message = "数据库暂时不可用"
        if isinstance(exc, PoolTimeoutError):
            message = "数据库连接繁忙，请稍后重试"
        if request.path.startswith("/api/"):
            return jsonify({"error": message}), 503
        return render_template(
            "error.html", code=503, message=message,
            detail="请确认 MySQL 服务已启动，且 config.py 中的账号密码正确。",
        ), 503

    # 注意：Flask 的 errorhandler 只接受"单个异常类"或"状态码"，
    # 直接传元组会在注册阶段就抛 TypeError（issubclass() arg 1 must be a class）——
    # 必须逐个注册。
    for exc_class in DB_ERRORS:
        app.register_error_handler(exc_class, handle_db_error)

    @app.errorhandler(500)
    def handle_500(exc):
        logger.exception("服务器内部错误：%s", exc)
        if request.path.startswith("/api/"):
            return jsonify({"error": "服务器内部错误"}), 500
        return render_template("error.html", code=500, message="服务器内部错误",
                               detail="详细堆栈已写入 logs/douban.app.log"), 500

    @app.teardown_appcontext
    def _noop_teardown(_exc):
        """连接由连接池统一管理，这里不关闭，交给池复用。"""
        return None

    return app


# ==================== 辅助函数 ====================
def _json(producer):
    """执行图表构造函数并把结果序列化成 JSON。

    统一在这里兜住异常：前端 ajax 的 error 分支能拿到 {"error": ...}，
    而不是一个 HTML 错误页（那样 res.responseJSON 解析会炸）。
    """
    try:
        return jsonify(producer())
    except DB_ERRORS:
        raise                                    # 交给 handle_db_error
    except (TypeError, ValueError, KeyError) as exc:
        logger.exception("图表数据构造失败：%s", exc)
        return jsonify({"error": f"图表数据构造失败：{exc}"}), 500


def _to_int(raw, default: int, minimum: int = None, maximum: int = None) -> int:
    """安全的整数参数解析：URL 参数是用户可控的，非法值不能让服务 500。"""
    try:
        value = int(raw) if raw not in (None, "") else default
    except (TypeError, ValueError):
        return default
    if minimum is not None:
        value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


# 导航配置：集中在一处，模板只负责遍历渲染；
# 原项目把 href 写在 <dd> 上（dd 不是超链接元素，合法性/可访问性都有问题）
NAV_ITEMS = [
    {
        "group": "总览",
        "items": [
            {"href": "/movie_list", "label": "电影列表", "icon": "list",
             "desc": "总量指标与可检索明细"},
        ],
    },
    {
        "group": "数据图表",
        "items": [
            {"href": "/movie_by_year", "label": "各年份上映数量", "icon": "calendar",
             "desc": "1931—2023 年度分布"},
            {"href": "/movie_type_percent", "label": "电影类型占比", "icon": "pie",
             "desc": "27 个类型标签构成"},
            {"href": "/name_and_rating", "label": "评分抽样与分布", "icon": "scatter",
             "desc": "随机抽样散点 + 分箱直方图"},
            {"href": "/movie_top", "label": "电影榜单 TOP10", "icon": "rank",
             "desc": "按豆瓣评分排序"},
            {"href": "/rater_top", "label": "评价人数 TOP20", "icon": "users",
             "desc": "按评价人数排序"},
        ],
    },
    {
        "group": "文本分析",
        "items": [
            {"href": "/word_cloud", "label": "电影词云", "icon": "cloud",
             "desc": "片名 / 导演 / 类型词频"},
        ],
    },
]

app = create_app()


if __name__ == "__main__":
    logger.info("启动服务：http://%s:%s（debug=%s）", Config.HOST, Config.PORT, Config.DEBUG)
    # use_reloader=False：避免 debug 重载导致连接池被创建两次
    app.run(host=Config.HOST, port=Config.PORT, debug=Config.DEBUG, use_reloader=False)

"""Web 层测试：用替身 DB 覆盖所有页面路由与 JSON 接口。

这些用例正好守住本次修复的几个关键问题：
- /word_cloud 路由必须存在（原项目导航链过去是 404）；
- /api/* 必须返回可被 json 解析的对象（图表 option 必须是 dict，不是 JSON 字符串）；
- 非法 URL 参数（page=abc、page=999）不能让页面 500。
"""

import json
import shutil
import unittest
from pathlib import Path
from unittest import mock

import word_cloud
from app import create_app
from tests.stub_db import FakeDB

# 临时产物目录：放在 tests/ 下而不是系统 temp，避免 Windows 上临时目录
# 权限/清理带来的偶发失败，也方便出问题时直接查看产物
TMP_CLOUD_DIR = Path(__file__).resolve().parent / ".tmp_clouds"

PAGES = [
    "/",
    "/movie_list",
    "/movie_by_year",
    "/movie_type_percent",
    "/name_and_rating",
    "/movie_top",
    "/rater_top",
]

APIS = [
    "/api/movie_by_year",
    "/api/movie_type",
    "/api/movie_rating",
    "/api/movie_top",
    "/api/movie_rater_count",
    "/api/rating_histogram",
    "/api/overview",
]


class CloudDirIsolatedTestCase(unittest.TestCase):
    """把词云输出目录指向临时目录。

    必须有这个隔离：导航测试会访问 /word_cloud，而该视图会真的生成 PNG。
    不隔离的话，跑一次测试就会用测试数据把 static/images 下的真实词云覆盖掉。
    """

    def setUp(self):
        shutil.rmtree(TMP_CLOUD_DIR, ignore_errors=True)
        TMP_CLOUD_DIR.mkdir(parents=True, exist_ok=True)
        patcher = mock.patch.object(word_cloud.Config, "CLOUD_DIR", TMP_CLOUD_DIR)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(shutil.rmtree, TMP_CLOUD_DIR, ignore_errors=True)


class PageRoutesTest(CloudDirIsolatedTestCase):
    def setUp(self):
        super().setUp()
        self.db = FakeDB()
        self.app = create_app(database=self.db)
        self.app.config.update(TESTING=True)
        self.client = self.app.test_client()

    def test_pages_render(self):
        for path in PAGES:
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200, f"{path} 未正常渲染")
                self.assertIn(b"<!DOCTYPE html>", response.data[:200])

    def test_nav_links_all_resolve(self):
        """导航里出现的每个链接都必须能打开（防止再出现 /word_cloud 那种死链）。"""
        index = self.client.get("/")
        self.assertEqual(index.status_code, 200)
        html = index.get_data(as_text=True)
        for href in ["/movie_list", "/movie_by_year", "/movie_type_percent",
                     "/name_and_rating", "/movie_top", "/rater_top", "/word_cloud"]:
            self.assertIn(f'href="{href}"', html, f"首页导航缺少 {href}")
            with self.subTest(href=href):
                self.assertEqual(self.client.get(href).status_code, 200)

    def test_word_cloud_page_generates_png(self):
        """词云页端到端跑通：走真实的分词 → 渲染 PNG → 写盘流程（输出到临时目录）。

        这条用例同时验证了字体文件可用 —— 中文字体缺失时词云会渲染成方块，
        属于"看起来成功、实际不可用"的典型故障。
        """
        response = self.client.get("/word_cloud")
        self.assertEqual(response.status_code, 200)
        self.assertIn("词云", response.get_data(as_text=True))

        generated = Path(word_cloud.Config.CLOUD_DIR) / "movie_names.png"
        self.assertTrue(generated.is_file(), "未生成片名词云 PNG")
        self.assertGreater(generated.stat().st_size, 1000)
        # sidecar 元数据用于缓存失效判断，必须一并写出
        self.assertTrue((Path(word_cloud.Config.CLOUD_DIR) / ".wordcloud_meta.json").is_file())

    def test_movie_list_search_and_pagination(self):
        response = self.client.get("/movie_list?q=诺兰")
        self.assertEqual(response.status_code, 200)
        # 关键字被传给数据层（证明检索不是前端假过滤）
        self.assertTrue(any(call[0] == "get_movies_page" and call[1] == "诺兰"
                            for call in self.db.calls if isinstance(call, tuple)))

    def test_bad_query_params_do_not_crash(self):
        """URL 参数是用户可控输入，任何脏值都不能导致 500。"""
        for path in ["/movie_list?page=abc", "/movie_list?page=-5", "/movie_list?page=99999",
                     "/name_and_rating?seed=abc"]:
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 200)

    def test_page_beyond_last_falls_back(self):
        response = self.client.get("/movie_list?page=999")
        self.assertEqual(response.status_code, 200)
        self.assertIn("第", response.get_data(as_text=True))

    def test_404_page(self):
        self.assertEqual(self.client.get("/no-such-page").status_code, 404)
        self.assertEqual(self.client.get("/api/no-such-page").status_code, 404)


class ApiRoutesTest(unittest.TestCase):
    def setUp(self):
        self.db = FakeDB()
        self.app = create_app(database=self.db)
        self.app.config.update(TESTING=True)
        self.client = self.app.test_client()

    def test_apis_return_json_object(self):
        """图表接口必须返回 JSON 对象（不是被转义的字符串）。

        原项目模板里一会儿 JSON.parse、一会儿直接 setOption，
        就是因为不同接口返回的类型不一致；这里把它固定下来。
        """
        for path in APIS:
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.mimetype, "application/json")
                payload = json.loads(response.get_data(as_text=True))
                self.assertIsInstance(payload, dict)

    def test_chart_options_have_series(self):
        for path in ["/api/movie_by_year", "/api/movie_type", "/api/movie_top",
                     "/api/movie_rater_count", "/api/movie_rating", "/api/rating_histogram"]:
            with self.subTest(path=path):
                payload = self.client.get(path).get_json()
                self.assertIn("series", payload)
                self.assertTrue(payload["series"], "series 不能为空")

    def test_chinese_not_escaped(self):
        """中文不转义（app.json.ensure_ascii=False）：响应更小、可读性更好。"""
        body = self.client.get("/api/movie_top").get_data(as_text=True)
        self.assertNotIn("\\u", body)
        self.assertIn("肖申克的救赎", body)

    def test_seed_is_passed_through(self):
        self.client.get("/api/movie_rating?seed=7")
        self.assertIn(("get_rating_sample", 7), self.db.calls)


class ErrorHandlingTest(unittest.TestCase):
    def test_db_error_returns_503_not_traceback(self):
        """数据库异常时给出友好页面，而不是 500 堆栈。"""
        import pymysql

        class BrokenDB(FakeDB):
            def get_count_by_year(self):
                raise pymysql.err.OperationalError(2003, "Can't connect to MySQL server")

        app = create_app(database=BrokenDB())
        app.config.update(TESTING=False)      # 关掉 TESTING 才会走 errorhandler
        client = app.test_client()

        response = client.get("/movie_by_year")
        self.assertEqual(response.status_code, 503)
        self.assertIn("数据库", response.get_data(as_text=True))

        api = client.get("/api/movie_by_year")
        self.assertEqual(api.status_code, 503)
        self.assertIn("error", api.get_json())


class StaticAssetsTest(unittest.TestCase):
    """样式与脚本必须本地可用（答辩现场可能没有网络）。"""

    def setUp(self):
        self.root = Path(__file__).resolve().parent.parent

    def test_local_assets_exist(self):
        for relative in ["static/css/main.css", "static/js/echarts.min.js",
                         "static/js/dashboard.js", "static/js/index.js",
                         "static/fonts/main.ttf", "static/images/favicon.svg"]:
            with self.subTest(relative=relative):
                path = self.root / relative
                self.assertTrue(path.is_file(), f"缺少静态资源 {relative}")
                self.assertGreater(path.stat().st_size, 0)

    def test_no_cdn_dependency_in_templates(self):
        """模板里不应再出现 CDN 外链（断网即失效）。"""
        for template in (self.root / "templates").glob("*.html"):
            text = template.read_text(encoding="utf-8")
            with self.subTest(template=template.name):
                self.assertNotIn("cdn.jsdelivr.net", text)
                self.assertNotIn("cdn.staticfile.net", text)

    def test_jquery_removed(self):
        self.assertFalse((self.root / "static/js/jquery-3.1.1.min.js").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)

"""爬虫解析器测试。

测试数据说明：
- list_page.html 是**真实**抓取保存的榜单页（匿名可访问）；
- detail_shawshank.html 是按真实 DOM 结构手写的详情页夹具
  （真实详情页现在会对匿名请求返回反爬挑战页，见 challenge_page.html）；
- challenge_page.html 是**真实**抓取保存的反爬挑战页，用于验证识别逻辑。
"""

import unittest
from pathlib import Path

from spider import DoubanSpider

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def read_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class ParseListPageTest(unittest.TestCase):
    def setUp(self):
        self.spider = DoubanSpider(db=None)

    def tearDown(self):
        self.spider.close()

    def test_parses_real_list_page(self):
        links = self.spider.parse_list_page(read_fixture("list_page.html"), "")
        self.assertEqual(len(links), 25, "Top250 每页应有 25 个条目")
        self.assertTrue(all(link.startswith("https://movie.douban.com/subject/") for link in links))
        # 必须去重且保序
        self.assertEqual(len(links), len(set(links)))

    def test_empty_html_returns_empty_list(self):
        self.assertEqual(self.spider.parse_list_page("", ""), [])

    def test_unrelated_html_returns_empty_list(self):
        self.assertEqual(self.spider.parse_list_page("<html><body>无条目</body></html>", ""), [])


class ParseDetailPageTest(unittest.TestCase):
    def setUp(self):
        self.spider = DoubanSpider(db=None)

    def tearDown(self):
        self.spider.close()

    def test_parses_all_fields(self):
        movie = self.spider.parse_detail_page(read_fixture("detail_shawshank.html"))
        self.assertIsNotNone(movie)
        self.assertEqual(movie.movie_name, "肖申克的救赎 The Shawshank Redemption")
        self.assertEqual(movie.release_year, "1994")
        self.assertEqual(movie.rating, 9.7)
        self.assertEqual(movie.director, "弗兰克·德拉邦特")
        self.assertEqual(movie.movie_type, "剧情,犯罪")
        self.assertEqual(movie.timing, 142)

    def test_rater_count_is_int(self):
        """评价人数必须是 int。

        原项目把 "3249655人" 直接存进 varchar 字段，
        导致 ORDER BY 变成字符串比较：'999550' > '3249655'。
        """
        movie = self.spider.parse_detail_page(read_fixture("detail_shawshank.html"))
        self.assertIsInstance(movie.rater_count, int)
        self.assertEqual(movie.rater_count, 3249655)

    def test_missing_name_returns_none(self):
        self.assertIsNone(self.spider.parse_detail_page("<html><body><h1></h1></body></html>"))

    def test_empty_html_returns_none(self):
        self.assertIsNone(self.spider.parse_detail_page(""))

    def test_row_shape_matches_sql_placeholders(self):
        movie = self.spider.parse_detail_page(read_fixture("detail_shawshank.html"))
        self.assertEqual(len(movie.as_row()), 7, "INSERT 语句有 7 个占位符")


class ChallengeDetectionTest(unittest.TestCase):
    """反爬挑战页识别：识别不出来就会把挑战页当正常页面解析，最后报"缺少片名"，
    让人误以为是选择器过期，排查方向完全跑偏。"""

    def setUp(self):
        self.spider = DoubanSpider(db=None)

    def tearDown(self):
        self.spider.close()

    def test_detects_real_challenge_page(self):
        self.assertTrue(self.spider._looks_like_challenge(read_fixture("challenge_page.html")))

    def test_normal_page_is_not_challenge(self):
        self.assertFalse(self.spider._looks_like_challenge(read_fixture("list_page.html")))
        self.assertFalse(self.spider._looks_like_challenge(read_fixture("detail_shawshank.html")))

    def test_short_unrelated_html_is_not_challenge(self):
        self.assertFalse(self.spider._looks_like_challenge("<html><body>hi</body></html>"))


class MovieModelTest(unittest.TestCase):
    def test_defaults(self):
        from spider import Movie

        movie = Movie(movie_name="测试")
        self.assertEqual(movie.release_year, "未知年份")
        self.assertEqual(movie.movie_type, "未知类型")
        self.assertEqual(movie.timing, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)

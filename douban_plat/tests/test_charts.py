"""图表与文本处理测试（纯函数，不需要数据库）。

这个文件能防住一类很隐蔽的回归：pyecharts 参数名写错时，
只有在真正调用图表构造函数时才会抛 TypeError —— 页面会 500。
用冒烟测试把每个图表都构造一遍，比等到打开页面才发现要好。
"""

import json
import unittest

import charts
import word_cloud


class ChartSmokeTest(unittest.TestCase):
    BY_YEAR = [{"year": "1994", "cnt": 12}, {"year": "2010", "cnt": 14}]
    TYPES = [("剧情", 186), ("爱情", 55)]
    ROWS = [
        {"movie_name": "肖申克的救赎 The Shawshank Redemption", "rating": 9.7,
         "release_year": "1994", "rater_count": 3249655, "movie_type": "剧情,犯罪"},
        {"movie_name": "盗梦空间 Inception", "rating": 9.4,
         "release_year": "2010", "rater_count": 2100000, "movie_type": "剧情,科幻"},
    ]

    @staticmethod
    def series_values(option, index=0):
        """pyecharts 对简单数值序列会输出 [9.7, 9.4]，对带样式的点会输出 [{"value": ...}]，
        两种情况都要能取到数值。"""
        data = option["series"][index]["data"]
        return [item["value"] if isinstance(item, dict) else item for item in data]

    @staticmethod
    def axis(option, key):
        axis = option[key]
        return axis[0] if isinstance(axis, list) else axis

    def test_all_charts_return_json_serializable_dict(self):
        builders = {
            "by_year": lambda: charts.movie_by_year_chart(self.BY_YEAR),
            "type": lambda: charts.movie_type_chart(self.TYPES),
            "rating": lambda: charts.movie_rating_chart(self.ROWS),
            "top": lambda: charts.movie_top_chart(self.ROWS),
            "rater": lambda: charts.movie_rater_count_chart(self.ROWS),
            "hist": lambda: charts.rater_histogram(self.ROWS),
        }
        for name, build in builders.items():
            with self.subTest(chart=name):
                option = build()
                self.assertIsInstance(option, dict, "图表 option 必须是 dict")
                # 必须能被 json 序列化，否则 Flask 返回时抛错
                json.dumps(option, ensure_ascii=False)

    def test_empty_data_does_not_raise(self):
        """空库/查询无结果时也不能 500。"""
        with self.subTest("by_year"):
            self.assertIsInstance(charts.movie_by_year_chart([]), dict)
        with self.subTest("type"):
            self.assertIsInstance(charts.movie_type_chart([]), dict)
        with self.subTest("rating"):
            self.assertIsInstance(charts.movie_rating_chart([]), dict)
        with self.subTest("top"):
            self.assertIsInstance(charts.movie_top_chart([]), dict)
        with self.subTest("rater"):
            self.assertIsInstance(charts.movie_rater_count_chart([]), dict)
        with self.subTest("hist"):
            self.assertIsInstance(charts.rater_histogram([]), dict)

    def test_type_chart_groups_tail_into_other(self):
        """27 个类型直接画饼图会挤成一团，长尾必须合并为"其他"。"""
        many = [(f"类型{i}", 30 - i) for i in range(20)]
        option = charts.movie_type_chart(many, top_n=10)
        data = option["series"][0]["data"]
        self.assertEqual(len(data), 11, "应为 Top10 + 其他 = 11 个扇区")
        self.assertIn("其他", data[-1]["name"])

    def test_top_chart_is_bar_not_funnel(self):
        """榜单必须用条形图：原实现用漏斗图 + 自增主键 id 作为数值，毫无业务含义。"""
        option = charts.movie_top_chart(self.ROWS)
        self.assertEqual(option["series"][0]["type"], "bar")

    def test_top_chart_sorted_ascending_for_reversed_axis(self):
        """横向条形图 + reversal_axis：数据需升序送入，最高分才会显示在最上方。"""
        option = charts.movie_top_chart(self.ROWS)
        values = self.series_values(option)
        self.assertEqual(values, sorted(values))

    def test_rater_count_converted_to_wan(self):
        """评价人数换算成"万人"，避免 7 位数撑爆坐标轴。"""
        option = charts.movie_rater_count_chart(self.ROWS)
        values = self.series_values(option)
        self.assertTrue(all(v < 1000 for v in values), "数值应以万人为单位")

    def test_rating_axis_adapts_to_data(self):
        """y 轴范围要随数据自适应，不能写死 8~10。"""
        low_rows = [{"movie_name": "低分片", "rating": 7.2, "release_year": "2020",
                     "rater_count": 1000, "movie_type": "剧情"}]
        option = charts.movie_rating_chart(low_rows)
        y_axis = self.axis(option, "yAxis")
        self.assertLessEqual(y_axis["min"], 7.2)
        self.assertGreaterEqual(y_axis["max"], 7.2)


class TokenizeTitlesTest(unittest.TestCase):
    """分词口径由 word_cloud 统一负责（原实现在两处各写一遍，容易出现分歧）。"""

    def test_strips_foreign_title(self):
        freq = word_cloud.tokenize_titles(["肖申克的救赎 The Shawshank Redemption"])
        self.assertIn("救赎", freq)
        self.assertNotIn("Shawshank", freq)

    def test_joins_names_separated_by_middle_dot(self):
        """去掉间隔号后 jieba 才会把"哈利·波特"识别成一个专有名词。"""
        freq = word_cloud.tokenize_titles(["哈利·波特与魔法石"])
        self.assertIn("哈利波特", freq)
        self.assertNotIn("哈利", freq)

    def test_filters_stopwords_and_single_chars(self):
        freq = word_cloud.tokenize_titles(["未知名称", "之", "的"])
        self.assertEqual(len(freq), 0)

    def test_counts_repeated_words(self):
        freq = word_cloud.tokenize_titles(["盗梦空间", "盗梦空间"])
        self.assertEqual(freq["盗梦"], 2)


class WordCloudFrequencyTest(unittest.TestCase):
    MOVIES = [
        {"movie_name": "盗梦空间 Inception", "director": "克里斯托弗·诺兰",
         "movie_type": "剧情,科幻"},
        {"movie_name": "星际穿越 Interstellar", "director": "克里斯托弗·诺兰",
         "movie_type": "剧情,冒险,科幻"},
        {"movie_name": "未知名称", "director": "未知导演", "movie_type": "未知类型"},
    ]

    def test_director_freq_excludes_placeholder(self):
        freq = word_cloud.build_director_freq(self.MOVIES)
        self.assertEqual(freq["克里斯托弗·诺兰"], 2)
        self.assertNotIn("未知导演", freq)

    def test_type_freq_splits_by_comma_without_jieba(self):
        """类型是结构化标签，按逗号切分即可（原实现对类型跑 jieba 属于过度处理）。"""
        freq = word_cloud.build_type_freq(self.MOVIES)
        self.assertEqual(freq["剧情"], 2)
        self.assertEqual(freq["科幻"], 2)
        self.assertEqual(freq["冒险"], 1)
        self.assertNotIn("未知类型", freq)

    def test_compress_is_monotonic(self):
        """平方根压缩后大小关系不变，只压缩视觉差异。"""
        from collections import Counter

        compressed = word_cloud._compress(Counter({"a": 100, "b": 4, "c": 1}))
        self.assertGreater(compressed["a"], compressed["b"])
        self.assertGreater(compressed["b"], compressed["c"])
        self.assertLess(compressed["a"], 100)

    def test_frequency_preview_keys(self):
        preview = word_cloud.frequency_preview(self.MOVIES, top_n=3)
        self.assertEqual(set(preview), {"片名关键词", "导演", "电影类型"})


if __name__ == "__main__":
    unittest.main(verbosity=2)

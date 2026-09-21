"""测试替身：不需要 MySQL 就能测整个 Web 层。

原项目的视图函数直接引用全局 DBUtils 单例，没法在不连数据库的情况下测试。
改为应用工厂 create_app(database=...) 注入之后，Web 层可以完全离线测试 ——
这正是"依赖注入"最实际的好处。
"""

OVERVIEW = {
    "movie_sum": 3,
    "max_rating": 9.7,
    "min_rating": 8.6,
    "avg_rating": 9.2,
    "avg_rater": 1200000,
    "max_timing": 179,
    "min_year": 1994,
    "max_year": 2010,
    "type_count": 2,
    "top_year": {"year": "1994", "cnt": 2},
    "top_director": {"director": "克里斯托弗·诺兰", "cnt": 2},
}

MOVIES = [
    {"id": 1, "movie_name": "肖申克的救赎 The Shawshank Redemption", "release_year": "1994",
     "rating": 9.7, "rater_count": 3249655, "director": "弗兰克·德拉邦特",
     "movie_type": "剧情,犯罪", "timing": 142},
    {"id": 2, "movie_name": "盗梦空间 Inception", "release_year": "2010",
     "rating": 9.4, "rater_count": 2100000, "director": "克里斯托弗·诺兰",
     "movie_type": "剧情,科幻", "timing": 148},
    {"id": 3, "movie_name": "未知名称", "release_year": "2010",
     "rating": 8.6, "rater_count": 900000, "director": "克里斯托弗·诺兰",
     "movie_type": "剧情", "timing": 179},
]

BY_YEAR = [{"year": "1994", "cnt": 2}, {"year": "2010", "cnt": 1}]
TYPE_DIST = [("剧情", 3), ("犯罪", 1), ("科幻", 1)]


class FakeDB:
    """按真实 DBUtils 的返回结构提供数据。"""

    def __init__(self, movies=None):
        self.movies = movies if movies is not None else list(MOVIES)
        self.calls = []

    def get_overview(self):
        self.calls.append("get_overview")
        return dict(OVERVIEW)

    def get_count_by_year(self):
        self.calls.append("get_count_by_year")
        return list(BY_YEAR)

    def get_type_distribution(self):
        self.calls.append("get_type_distribution")
        return list(TYPE_DIST)

    def get_movie_top10(self):
        self.calls.append("get_movie_top10")
        return self.movies[:10]

    def get_rater_top20(self):
        self.calls.append("get_rater_top20")
        return sorted(self.movies, key=lambda m: -m["rater_count"])[:20]

    def get_rating_sample(self, limit=30, seed=42):
        self.calls.append(("get_rating_sample", seed))
        return self.movies[:limit]

    def get_movies_page(self, keyword="", page=1, size=20):
        self.calls.append(("get_movies_page", keyword, page, size))
        rows = self.movies
        if keyword:
            rows = [m for m in rows if keyword in m["movie_name"]]
        total = len(rows)
        offset = (page - 1) * size
        return rows[offset:offset + size], total

    def get_all_movies(self):
        self.calls.append("get_all_movies")
        return list(self.movies)

    def get_movie_names(self):
        return [m["movie_name"] for m in self.movies]

    def invalidate_cache(self):
        self.calls.append("invalidate_cache")

    def close(self):
        pass

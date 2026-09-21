"""词云生成模块。

原项目 word_cloud.py 的问题与本次优化：
1. 【过度处理】原代码对"剧情,爱情"这样的电影类型跑了 jieba 分词。
   类型本身就是结构化标签，用逗号切分才是正确做法 ——
   跑分词只会把"剧情"再切一遍，徒增耗时还可能切错。
   现在按数据类型选择处理方式：结构化标签用分隔符切，自由文本才分词。
2. 【无效可视化】原代码用上映年份生成词云。年份之间权重几乎相同（多数是 1 部），
   词云完全表达不出差异，属于"为了用词云而用词云"。
   年份趋势已由柱状图页面表达，这里删掉该图，改为保留三张有实际意义的词云。
3. 【静默失败】原代码 except 里回退到"不指定字体"再生成一次 ——
   中文字体缺失时不会报错，而是生成一张全是方块（豆腐块）的图，
   问题被隐藏到答辩现场才发现。现在失败就明确报错并记录日志。
4. 【脆弱】原代码用 movie[5] 这种元组下标取字段，加一列就全错位；
   且用相对路径 'static/fonts/main.ttf'，换工作目录就找不到字体。
   现在改用字段名访问 + 基于 __file__ 的绝对路径。
5. 【缓存失效】词云是离线图片，重新爬取数据后必须重算。
   现在用 sidecar 元数据记录"生成时的数据量"，数据量变化自动重算。
"""

import json
import random
from collections import Counter
from datetime import datetime
from pathlib import Path

import jieba
from wordcloud import WordCloud

from config import Config, setup_logging

logger = setup_logging("douban.wordcloud")

# 中文停用词：爬虫的占位符 + 片名里没有区分度的虚词
STOPWORDS = {
    "未知", "名称", "未知名称", "未知年份", "未知类型", "未知导演",
    "的", "了", "与", "和", "之", "第", "部", "版", "上", "下", "中",
    "篇", "季", "全集", "电影", "剧场版", "系列",
}

# jieba 用户词典：补充影名专有名词。
#
# 踩坑记录：一开始想"去掉间隔号把哈利·波特拼成一词"，结果更糟 ——
# jieba 默认词典里没有"哈利波特"，会切成 ['哈利波', '特与', '魔法石'] 这种碎片。
# 正确做法是保留清洗后的完整词形，并用 add_word 告诉分词器这是一个词。
TITLE_USER_WORDS = [
    "哈利波特", "千与千寻", "阿甘正传", "宝莱坞", "加勒比海盗", "速度与激情",
    "复仇者联盟", "疯狂动物城", "玩具总动员", "星际穿越", "楚门的世界",
    "无间道", "大话西游", "忠犬八公", "指环王", "黑客帝国", "美丽人生",
    "放牛班的春天", "这个杀手不太冷", "天空之城", "龙猫", "狮子王",
]

for _word in TITLE_USER_WORDS:
    jieba.add_word(_word)

# 词云配色：在浅色纸背景上保证对比度
CLOUD_COLORS = ["#A8762A", "#2F7449", "#9A3F36", "#3F6C93", "#6B5A9E", "#7A5A32", "#40695C"]


def _meta_file() -> Path:
    """元数据文件路径。

    刻意不在模块顶层把它算成常量：一旦写成常量，Config.CLOUD_DIR 就被"冻结"，
    测试里想把输出目录改到临时目录就会失效，测试会去覆盖真实产物。
    """
    return Config.CLOUD_DIR / ".wordcloud_meta.json"


# ==================== 词频统计 ====================
def tokenize_titles(names: list) -> Counter:
    """电影片名分词并统计词频。

    片名形如 "肖申克的救赎 The Shawshank Redemption"：
    1. 先按空格切掉外文原名（否则英文单词会稀释中文词频）；
    2. 去掉间隔号"·"让"哈利·波特"连成一个词形，
       再靠 jieba 用户词典（TITLE_USER_WORDS）把它识别成一个词 —— 
       光去掉间隔号不行，jieba 会切成 ['哈利波', '特与'] 这种碎片；
    3. 过滤标点、单字和停用词 —— 单字（"之""的"）没有分析价值。
    """
    counter: Counter = Counter()
    for name in names:
        if not name:
            continue
        chinese_part = str(name).split(" ")[0].replace("·", "")
        for token in jieba.lcut(chinese_part):
            token = token.strip()
            if len(token) < 2 or token in STOPWORDS:
                continue
            if not any("\u4e00" <= ch <= "\u9fff" for ch in token):
                continue          # 片名部分只保留中文词
            counter[token] += 1
    return counter


def build_director_freq(movies: list) -> Counter:
    """导演词频：导演名本身就是一个完整词，不需要分词。"""
    counter: Counter = Counter()
    for movie in movies:
        director = (movie.get("director") or "").strip()
        if director and director not in STOPWORDS:
            counter[director] += 1
    return counter


def build_type_freq(movies: list) -> Counter:
    """电影类型词频：逗号分隔的结构化标签，按分隔符切分即可（无需分词）。"""
    counter: Counter = Counter()
    for movie in movies:
        for genre in (movie.get("movie_type") or "").split(","):
            genre = genre.strip()
            if genre and genre not in STOPWORDS:
                counter[genre] += 1
    return counter


# ==================== 图片渲染 ====================
def _color_func(*args, **kwargs):
    """自定义着色函数。

    用自定义函数而不是 colormap，是为了避免为了配色额外引入 matplotlib
    （wordcloud 的 colormap 参数依赖 matplotlib）。
    """
    color = random.choice(CLOUD_COLORS)
    return f"rgb{tuple(int(color[i:i + 2], 16) for i in (1, 3, 5))}"


def _compress(freq: Counter) -> dict:
    """对词频做平方根压缩。

    原始词频长尾极不均匀（"剧情"186 次 vs 长尾词 1~2 次），
    直接喂给词云会导致头号词占满整张图、长尾词小到看不清。
    开平方后大小关系保持不变，但视觉差异被压缩到可读范围。
    """
    return {word: count ** 0.5 for word, count in freq.items() if count > 0}


def render_word_cloud(freq: Counter, output_path: Path, *, max_font_size: int = 110,
                      min_font_size: int = 16, max_words: int = 160,
                      width: int = 1000, height: int = 620) -> bool:
    """把词频渲染成 PNG。返回是否成功。"""
    data = _compress(freq)
    if len(data) < 3:
        logger.warning("有效词汇不足（%s 个），跳过生成 %s", len(data), output_path.name)
        return False

    font_path = Path(Config.FONT_PATH)
    if not font_path.is_file():
        # 明确报错，不再静默回退（否则中文会渲染成一堆方块）
        logger.error("中文字体不存在：%s，无法生成词云", font_path)
        return False

    try:
        cloud = WordCloud(
            font_path=str(font_path),
            background_color=None,     # 透明背景，与页面纸张底色自然融合
            mode="RGBA",
            width=width,
            height=height,
            scale=2,                   # 2 倍分辨率渲染，避免高分屏发虚
            max_words=max_words,
            max_font_size=max_font_size,
            min_font_size=min_font_size,
            prefer_horizontal=0.92,
            relative_scaling=0.4,      # 字号与词频的相关性，越小视觉越均匀
            collocations=False,        # 关键：关闭二元搭配，否则会拼出无意义短语
            color_func=_color_func,
            random_state=42,           # 固定随机种子，保证每次生成的布局一致
        ).generate_from_frequencies(data)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        cloud.to_file(str(output_path))
        logger.info("词云生成成功：%s（%s 个词）", output_path.name, len(data))
        return True
    except (OSError, ValueError) as exc:
        logger.error("词云生成失败 %s：%s", output_path.name, exc)
        return False


# ==================== 缓存失效控制 ====================
def _read_meta() -> dict:
    try:
        return json.loads(_meta_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_meta(meta: dict) -> None:
    try:
        target = _meta_file()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError as exc:
        logger.warning("词云元数据写入失败：%s", exc)


def cloud_outputs() -> dict:
    """词云的展示配置：键 = 文件名，值 = 页面上的标题与说明。"""
    return {
        "movie_names.png": {
            "title": "片名关键词",
            "desc": "把 250 部电影的片名用 jieba 分词后统计词频，字号越大出现越多。",
        },
        "director_names.png": {
            "title": "导演",
            "desc": "入榜作品最多的导演（同一导演多部作品入选时字号更大）。",
        },
        "types.png": {
            "title": "电影类型",
            "desc": "一部电影可属于多个类型，这里统计的是每个类型标签的入选次数。",
        },
    }


def is_stale(movie_sum: int) -> bool:
    """判断是否需要重新生成：图片缺失，或生成时的数据量与当前不一致。"""
    meta = _read_meta()
    if meta.get("movie_sum") != movie_sum:
        return True
    return any(not (Config.CLOUD_DIR / name).is_file() for name in cloud_outputs())


def generate_all(movies: list, movie_sum: int = None, force: bool = False) -> dict:
    """生成全部词云，返回 {文件名: 是否成功}。

    只有数据量变化或图片缺失时才真正重算，避免每次访问词云页都跑一遍 jieba。
    """
    movie_sum = movie_sum if movie_sum is not None else len(movies)
    if not force and not is_stale(movie_sum):
        logger.info("词云为最新，跳过生成")
        return {name: True for name in cloud_outputs()}

    logger.info("开始生成词云（数据量 %s）", movie_sum)
    jobs = {
        "movie_names.png": (tokenize_titles([m.get("movie_name") for m in movies]), 110, 16),
        "director_names.png": (build_director_freq(movies), 130, 18),
        "types.png": (build_type_freq(movies), 130, 22),
    }

    results = {}
    for filename, (freq, max_size, min_size) in jobs.items():
        results[filename] = render_word_cloud(
            freq, Config.CLOUD_DIR / filename, max_font_size=max_size, min_font_size=min_size
        )

    if any(results.values()):
        _write_meta({
            "movie_sum": movie_sum,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
        })
    return results


def frequency_preview(movies: list, top_n: int = 15) -> dict:
    """页面右侧的词频榜（用表格核对词云，图 + 表互相印证）。"""
    return {
        "片名关键词": tokenize_titles([m.get("movie_name") for m in movies]).most_common(top_n),
        "导演": build_director_freq(movies).most_common(top_n),
        "电影类型": build_type_freq(movies).most_common(top_n),
    }


if __name__ == "__main__":
    # 支持独立运行：python word_cloud.py
    from db_utils import get_db

    _db = get_db()
    try:
        _movies = _db.get_all_movies()
        _result = generate_all(_movies, movie_sum=len(_movies), force=True)
        print("生成结果：", _result)
    finally:
        _db.close()

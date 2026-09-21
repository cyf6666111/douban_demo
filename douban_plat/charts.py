"""图表配置层（pyecharts → ECharts option）。

原项目 charts.py 的问题与本次优化：
1. 【严重】movie_top_chart 把自增主键 id 当成漏斗图的数值 —— 漏斗宽度表示
   "这条记录在数据库里的行号"，没有任何业务含义；而且数据源 SQL 没有 ORDER BY，
   取到的 10 行是随机的。现在按评分排序，并改为横向条形图
   （榜单类数据用条形图比漏斗图更合适：长度可直接比较，不会因为"漏斗必须递减"
   而扭曲数值）。
2. 【一致性】原函数有的返回 JSON 字符串、有的返回 dump_options() 字符串，
   前端一处用 JSON.parse、一处直接用，写法不统一且容易踩坑。
   现在统一返回 dict（由 Flask 直接序列化为 application/json）。
   注意：dump_options() 内部用 json.dumps 默认 ensure_ascii=True，
   中文会被转义成 \\uXXXX，体积翻倍；解析成 dict 后交给 Flask
   并设置 app.json.ensure_ascii = False，响应更小、更易读。
3. 【可读性】散点图 y 轴写死 min=8/max=10，一旦数据超出范围点就画不出来；
   饼图 27 个类型全塞进去，小扇区挤成一团看不清。
   现在改为数据驱动：y 轴按数据自适应并留边距，饼图取 Top10 + "其他"。
4. 【信息量】所有图表补齐 tooltip、单位、坐标轴名称；评分人数换算成"万人"，
   避免 3249655 这种数字撑爆坐标轴。
5. 新增"图表 + 数据表"双通道：图旁边同时给出等价的数据表，
   既能核对数据，也照顾到读屏/无法看图的情况。
"""

import json
from collections import Counter

from pyecharts import options as opts
from pyecharts.charts import Bar, Pie, Scatter

# 与前端 CSS 变量保持一致的配色（琥珀主色 + 次要色）
COLOR_MAIN = "#E0A33E"
COLOR_ALT = "#3E9B63"
PALETTE = [
    "#E0A33E", "#3E9B63", "#C0554B", "#5B7FA6", "#A8762A",
    "#7C6BA8", "#4E8F8A", "#B5714A", "#6E8B4F", "#9C5B7C", "#8C8578",
]


def _to_option(chart) -> dict:
    """把 pyecharts 图表对象转成可被 Flask 直接序列化的 dict。

    这里刻意只使用「字符串模板」形式的 formatter（如 "{b}: {c}"），
    不使用 JS 函数回调，因此 dump_options() 的结果一定是合法 JSON，可以安全 loads。
    如果确实需要 JS 回调，应改为返回 Response(json_str, mimetype="application/json")。
    """
    return json.loads(chart.dump_options())


def _base_init(height: str = "560px"):
    return opts.InitOpts(width="100%", height=height, bg_color="transparent")


def _gradient(color_from: str = "#E9B75C", color_to: str = "#A8762A") -> dict:
    """竖向渐变，让柱状图的柱子有材质感而不是一片纯色。"""
    return {
        "type": "linear", "x": 0, "y": 0, "x2": 0, "y2": 1,
        "colorStops": [{"offset": 0, "color": color_from}, {"offset": 1, "color": color_to}],
    }


# ======================= 1. 各年份上映电影数量 =======================
def movie_by_year_chart(rows: list) -> dict:
    """柱状图：x=年份，y=该年上映并进入 Top250 的电影数。

    数据是 1931—2023 共 58 个离散年份，一屏塞不下，
    所以加 dataZoom（内置滚轮 + 底部滑块）让用户自己缩放区间。
    """
    years = [str(r["year"]) for r in rows]
    counts = [int(r["cnt"]) for r in rows]

    chart = (
        Bar(init_opts=_base_init())
        .add_xaxis(years)
        .add_yaxis(
            "电影数量",
            counts,
            category_gap="48%",
            itemstyle_opts=opts.ItemStyleOpts(color=_gradient(), border_radius=[3, 3, 0, 0]),
            label_opts=opts.LabelOpts(is_show=False),
        )
        .set_global_opts(
            tooltip_opts=opts.TooltipOpts(
                trigger="axis", axis_pointer_type="shadow", formatter="{b} 年 · {c} 部"
            ),
            xaxis_opts=opts.AxisOpts(
                name="上映年份",
                name_location="end",
                name_gap=16,
                axislabel_opts=opts.LabelOpts(rotate=45, font_size=10, interval=0),
                axisline_opts=opts.AxisLineOpts(linestyle_opts=opts.LineStyleOpts(color="#D8D2C4")),
            ),
            yaxis_opts=opts.AxisOpts(
                name="电影数量（部）",
                name_location="end",
                name_gap=16,
                min_interval=1,
                axislabel_opts=opts.LabelOpts(font_size=11),
                splitline_opts=opts.SplitLineOpts(
                    is_show=True, linestyle_opts=opts.LineStyleOpts(color="#EFEAE0", type_="dashed")
                ),
            ),
            legend_opts=opts.LegendOpts(is_show=False),
            datazoom_opts=[
                opts.DataZoomOpts(type_="inside", range_start=0, range_end=100),
                opts.DataZoomOpts(type_="slider", pos_bottom="1%", range_start=0, range_end=100),
            ],
            # 保留边距，避免 name 与轴标签重叠
            toolbox_opts=opts.ToolboxOpts(
                is_show=True, pos_right="2%",
                feature={"saveAsImage": {"title": "保存为图片", "pixelRatio": 2}},
            ),
        )
        .set_series_opts(
            markpoint_opts=opts.MarkPointOpts(
                data=[opts.MarkPointItem(type_="max", name="峰值")],
                symbol_size=52,
                label_opts=opts.LabelOpts(font_size=10, color="#fff"),
            ),
        )
    )
    return _to_option(chart)


# ======================= 2. 电影类型占比 =======================
def movie_type_chart(distribution: list, top_n: int = 10) -> dict:
    """环形饼图：各类型标签出现的次数占比。

    重要口径说明：一部电影有多个类型标签（如"剧情,爱情"），
    所以各扇区之和 = 类型标签总出现次数（700），而不是电影数（250）。
    这一点必须在页面上写清楚，否则会被误读成"剧情片占 74%"。

    27 个类型直接画饼图会挤成一团，这里取 Top10 并把长尾合并为"其他"。
    """
    if not distribution:
        return _to_option(Pie(init_opts=_base_init()))

    head = list(distribution[:top_n])
    tail_total = sum(cnt for _, cnt in distribution[top_n:])
    if tail_total:
        head.append((f"其他（{len(distribution) - top_n} 类）", tail_total))

    total = sum(cnt for _, cnt in head)
    data_pair = [(name, cnt) for name, cnt in head]

    chart = (
        Pie(init_opts=_base_init())
        .add(
            series_name="类型标签数",
            data_pair=data_pair,
            radius=["36%", "62%"],
            center=["50%", "46%"],
            # 从 12 点方向顺时针排列，且数据已按降序传入，视觉上由大到小
            start_angle=90,
            itemstyle_opts=opts.ItemStyleOpts(border_color="#FFFFFF", border_width=2),
        )
        .set_colors(PALETTE)
        .set_global_opts(
            tooltip_opts=opts.TooltipOpts(
                trigger="item",
                formatter="{b}<br/>出现 {c} 次<br/>占全部类型标签 {d}%",
            ),
            # 图例放底部横向滚动：环形图右侧留给外部标签，避免图例与标签打架
            legend_opts=opts.LegendOpts(
                type_="scroll",
                orient="horizontal",
                pos_bottom="0%",
                pos_left="center",
                item_width=10,
                item_height=10,
                textstyle_opts=opts.TextStyleOpts(font_size=11),
            ),
        )
        .set_series_opts(
            label_opts=opts.LabelOpts(
                is_show=True, formatter="{b}\n{d}%", font_size=11, color="#4A443B"
            ),
        )
    )
    return _to_option(chart)


# ======================= 3. 电影评分抽样散点图 =======================
def movie_rating_chart(rows: list) -> dict:
    """散点图：每部抽样电影的评分。

    y 轴按数据自适应（不再写死 8~10），并留 0.2 的上下边距，
    这样即使将来爬到 7 分档的电影也不会"画出边界看不到"。
    """
    if not rows:
        return _to_option(Scatter(init_opts=_base_init()))

    def short(name: str, limit: int = 9) -> str:
        name = name.split(" ")[0]          # 去掉后面的外文原名，只留中文片名
        return name if len(name) <= limit else name[: limit - 1] + "…"

    names = [short(r["movie_name"]) for r in rows]
    ratings = [float(r["rating"]) for r in rows]
    low, high = min(ratings), max(ratings)
    avg = round(sum(ratings) / len(ratings), 2)

    chart = (
        Scatter(init_opts=_base_init())
        .add_xaxis(names)
        .add_yaxis(
            "评分",
            ratings,
            symbol_size=14,
            itemstyle_opts=opts.ItemStyleOpts(
                color=COLOR_MAIN, border_color="#FFFFFF", border_width=1.5, opacity=0.9
            ),
            label_opts=opts.LabelOpts(is_show=False),
        )
        .set_global_opts(
            tooltip_opts=opts.TooltipOpts(
                trigger="item", formatter="{b}<br/>豆瓣评分：{c} 分"
            ),
            xaxis_opts=opts.AxisOpts(
                name="电影（随机抽样）",
                name_location="end",
                name_gap=16,
                axislabel_opts=opts.LabelOpts(rotate=-38, font_size=10),
                axisline_opts=opts.AxisLineOpts(linestyle_opts=opts.LineStyleOpts(color="#D8D2C4")),
            ),
            yaxis_opts=opts.AxisOpts(
                name="评分（分）",
                name_location="end",
                name_gap=16,
                min_=round(max(0, low - 0.2), 1),
                max_=round(min(10, high + 0.2), 1),
                splitline_opts=opts.SplitLineOpts(
                    is_show=True, linestyle_opts=opts.LineStyleOpts(color="#EFEAE0", type_="dashed")
                ),
            ),
            legend_opts=opts.LegendOpts(is_show=False),
            toolbox_opts=opts.ToolboxOpts(
                is_show=True, pos_right="2%",
                feature={"saveAsImage": {"title": "保存为图片", "pixelRatio": 2}},
            ),
        )
        .set_series_opts(
            markline_opts=opts.MarkLineOpts(
                data=[opts.MarkLineItem(y=avg, name="抽样均值")],
                label_opts=opts.LabelOpts(formatter=f"抽样均值 {avg}", font_size=10),
                linestyle_opts=opts.LineStyleOpts(color=COLOR_ALT, type_="dashed", width=1.5),
            )
        )
    )
    return _to_option(chart)


# ======================= 4. 电影榜单 Top10 =======================
def movie_top_chart(rows: list) -> dict:
    """横向条形图：评分最高的 10 部电影。

    为什么不用漏斗图？漏斗图隐含"逐级递减/转化率"的语义，
    适合展示流程转化（曝光→点击→下单），用来排榜单会误导读者。
    排行榜的正确选择是横向条形图：同一基准线起跑，长度可直接比较，
    而且片名很长时横向布局不会像竖排那样被压扁/旋转。
    """
    if not rows:
        return _to_option(Bar(init_opts=_base_init()))

    # 横向条形图要"最高分在最上面"，因此按升序送入 + reversal_axis
    ordered = list(reversed(rows))
    names = [r["movie_name"].split(" ")[0] for r in ordered]
    ratings = [float(r["rating"]) for r in ordered]

    chart = (
        Bar(init_opts=_base_init(height="600px"))
        .add_xaxis(names)
        .add_yaxis(
            "豆瓣评分",
            ratings,
            category_gap="42%",
            itemstyle_opts=opts.ItemStyleOpts(color=_gradient("#E9B75C", "#B07C24")),
            label_opts=opts.LabelOpts(
                is_show=True, position="right", formatter="{c} 分", font_size=11, color="#4A443B"
            ),
        )
        .reversal_axis()
        .set_global_opts(
            tooltip_opts=opts.TooltipOpts(
                trigger="axis", axis_pointer_type="shadow", formatter="{b}<br/>豆瓣评分：{c} 分"
            ),
            xaxis_opts=opts.AxisOpts(
                name="豆瓣评分（分）",
                name_location="end",
                name_gap=16,
                min_=round(min(ratings) - 0.2, 1),
                max_=10,
                splitline_opts=opts.SplitLineOpts(
                    is_show=True, linestyle_opts=opts.LineStyleOpts(color="#EFEAE0", type_="dashed")
                ),
            ),
            yaxis_opts=opts.AxisOpts(
                axislabel_opts=opts.LabelOpts(font_size=11),
                axisline_opts=opts.AxisLineOpts(linestyle_opts=opts.LineStyleOpts(color="#D8D2C4")),
            ),
            legend_opts=opts.LegendOpts(is_show=False),
            toolbox_opts=opts.ToolboxOpts(
                is_show=True, pos_right="2%",
                feature={"saveAsImage": {"title": "保存为图片", "pixelRatio": 2}},
            ),
        )
    )
    # 年份、类型、评价人数等附加信息由页面下方的数据表承载（图表 + 表格双通道）
    return _to_option(chart)


# ======================= 5. 评分人数 Top20 =======================
def movie_rater_count_chart(rows: list) -> dict:
    """横向条形图：评价人数最多的 20 部电影。

    数值统一换算成"万人"：3249655 这种 7 位数会把坐标轴撑得很宽，
    且读者对"万"更敏感。注意不要用 JS 回调做单位换算
    （会破坏 option 的 JSON 可解析性），在 Python 侧一次算好。
    """
    if not rows:
        return _to_option(Bar(init_opts=_base_init()))

    ordered = list(reversed(rows))
    names = [r["movie_name"].split(" ")[0] for r in ordered]
    wan = [round(int(r["rater_count"] or 0) / 10000, 1) for r in ordered]

    chart = (
        Bar(init_opts=_base_init(height="700px"))
        .add_xaxis(names)
        .add_yaxis(
            "评价人数",
            wan,
            category_gap="40%",
            itemstyle_opts=opts.ItemStyleOpts(color=_gradient("#5FB07C", "#2F7449")),
            label_opts=opts.LabelOpts(
                is_show=True, position="right", formatter="{c}", font_size=10, color="#4A443B"
            ),
        )
        .reversal_axis()
        .set_global_opts(
            tooltip_opts=opts.TooltipOpts(
                trigger="axis", axis_pointer_type="shadow", formatter="{b}<br/>评价人数：{c} 万人"
            ),
            xaxis_opts=opts.AxisOpts(
                name="评价人数（万人）",
                name_location="end",
                name_gap=16,
                splitline_opts=opts.SplitLineOpts(
                    is_show=True, linestyle_opts=opts.LineStyleOpts(color="#EFEAE0", type_="dashed")
                ),
            ),
            yaxis_opts=opts.AxisOpts(
                axislabel_opts=opts.LabelOpts(font_size=11),
                axisline_opts=opts.AxisLineOpts(linestyle_opts=opts.LineStyleOpts(color="#D8D2C4")),
            ),
            legend_opts=opts.LegendOpts(is_show=False),
            toolbox_opts=opts.ToolboxOpts(
                is_show=True, pos_right="2%",
                feature={"saveAsImage": {"title": "保存为图片", "pixelRatio": 2}},
            ),
        )
    )
    return _to_option(chart)


# ======================= 6. 评分分布直方图 =======================
def rater_histogram(rows: list) -> dict:
    """评分区间分布直方图（补充视图）。

    把评分按 0.1 分一档分箱，看 Top250 的评分集中在哪个区间，
    比"抽样 30 部"更能反映整体分布。
    """
    buckets: Counter = Counter()
    for r in rows:
        rating = float(r["rating"] or 0)
        buckets[f"{rating:.1f}"] += 1
    labels = sorted(buckets.keys(), key=float)
    values = [buckets[label] for label in labels]

    chart = (
        Bar(init_opts=_base_init(height="420px"))
        .add_xaxis(labels)
        .add_yaxis(
            "电影数量",
            values,
            category_gap="20%",
            itemstyle_opts=opts.ItemStyleOpts(color=_gradient("#7FA6CC", "#3F6C93")),
            label_opts=opts.LabelOpts(is_show=True, position="top", font_size=10, color="#4A443B"),
        )
        .set_global_opts(
            tooltip_opts=opts.TooltipOpts(
                trigger="axis", axis_pointer_type="shadow", formatter="评分 {b} 分 · {c} 部"
            ),
            xaxis_opts=opts.AxisOpts(
                name="豆瓣评分（分）", name_location="end", name_gap=16,
                axislabel_opts=opts.LabelOpts(font_size=10, rotate=35, interval=0),
            ),
            yaxis_opts=opts.AxisOpts(
                name="电影数量（部）", name_location="end", name_gap=16, min_interval=1,
                splitline_opts=opts.SplitLineOpts(
                    is_show=True, linestyle_opts=opts.LineStyleOpts(color="#EFEAE0", type_="dashed")
                ),
            ),
            legend_opts=opts.LegendOpts(is_show=False),
        )
    )
    return _to_option(chart)

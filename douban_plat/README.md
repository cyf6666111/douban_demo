# 豆瓣电影 Top250 数据可视化平台

一个「爬虫 → 存储 → 分析 → 可视化」的完整小型数据平台：
抓取豆瓣电影 Top250，落库 MySQL，再用 Flask + ECharts 做多维可视化展示。

> 数据仅用于课程设计与学习研究，请遵守目标站点 robots 协议与相关法律法规，不要用于商业用途。

---

## 1. 技术栈

| 层次 | 选型 | 说明 |
| --- | --- | --- |
| 数据采集 | requests + BeautifulSoup4 + 正则 | Session 连接复用、指数退避重试、随机延迟限速 |
| 数据存储 | MySQL 8 + PyMySQL | utf8mb4、唯一索引 + `ON DUPLICATE KEY UPDATE` 幂等写入、`executemany` 批量提交 |
| 数据访问 | 自研连接池 + DictCursor | 线程安全复用连接，全部参数化查询 |
| 数据分析 | jieba + collections.Counter | 中文分词、词频统计、停用词过滤 |
| 可视化 | pyecharts（生成 option）+ ECharts 6（本地渲染） | 后端只产出 JSON 配置，前端负责渲染 |
| Web | Flask 3 + Jinja2 | 应用工厂 + 依赖注入，服务端渲染表格 + AJAX 拉图表配置 |
| 词云 | wordcloud + PIL | 离线生成 PNG，带缓存失效判断 |
| 测试 | 标准库 unittest | 42 条用例，无需 MySQL 即可跑通 |

**前端零第三方依赖**：ECharts 使用本地 `static/js/echarts.min.js`，不引 CDN，
断网也能完整演示。

---

## 2. 快速开始

```bash
# 1) 安装依赖
python -m pip install -r requirements.txt

# 2) 准备数据库（两种方式任选）
#    方式 A：全新初始化（推荐，字段类型正确）
mysql -u root -p < sql/schema.sql
#    方式 B：已用旧版建过表，执行迁移修正字段类型
mysql -u root -p douban_plat < sql/migrate_v2.sql

# 3) 配置（可选，不改也能跑；默认连 localhost/root/123456/douban_plat）
copy .env.example .env      # Windows
cp .env.example .env        # macOS / Linux

# 4) 爬取数据（可选，库里已有数据可跳过）
python spider.py            # 增量模式：只补缺失的条目
python spider.py --full     # 全量重爬

# 5) 生成词云（可选，访问 /word_cloud 时会自动生成）
python word_cloud.py

# 6) 启动服务
python app.py               # 默认 http://127.0.0.1:5000
```

### 运行测试

```bash
python -m unittest discover -s tests -t . -v
```

测试完全不依赖 MySQL 与外网（用测试替身注入数据层），可直接放进 CI。

> Windows 控制台看中文日志乱码时：先执行 `chcp 65001`，或设置环境变量 `PYTHONUTF8=1`。

---

## 3. 目录结构

```
douban_plat/
├── app.py                  # Flask 应用工厂 + 全部路由（页面 & JSON 接口）
├── config.py               # 统一配置：环境变量 / .env 覆盖，日志初始化
├── db_utils.py             # 数据访问层：连接池、TTL 缓存、聚合查询、分页检索
├── charts.py               # 图表配置层：pyecharts → ECharts option（dict）
├── word_cloud.py           # 文本分析 + 词云 PNG 生成（含缓存失效）
├── spider.py               # 爬虫：抓取、解析、清洗、批量入库
├── requirements.txt
├── .env.example            # 配置模板（.env 已被 .gitignore 忽略）
├── sql/
│   ├── schema.sql          # 推荐表结构（正确字段类型 + 索引）
│   └── migrate_v2.sql      # 旧表 → 新表迁移脚本
├── templates/              # Jinja2 模板
│   ├── base.html           # 基础布局（原项目 7 个模板各写一遍 head）
│   ├── _macros.html        # 卡片 / 图表容器 / 评分条 / 分页器 宏
│   ├── index.html          # 外壳：侧边导航 + iframe 内容区
│   ├── error.html          # 统一错误页
│   └── movie_*.html ...    # 各可视化页面（图 + 表双通道）
├── static/
│   ├── css/main.css        # 单一样式来源（设计令牌 + 全部组件样式）
│   ├── js/dashboard.js     # 图表运行时：主题、载入/错误态、resize
│   ├── js/index.js         # 外壳导航：hash 路由、选中态、进度条
│   ├── js/echarts.min.js   # ECharts 6（本地化，无 CDN 依赖）
│   ├── fonts/main.ttf      # 词云中文字体（楷体）
│   └── images/             # 词云 PNG、favicon
├── tests/                  # 单元测试（含真实抓取的 HTML 夹具）
│   ├── stub_db.py          # 数据层测试替身
│   ├── test_app.py         # 全部路由 + JSON 接口 + 错误分支
│   ├── test_charts.py      # 图表构造冒烟测试 + 分词
│   └── test_spider.py      # 解析器 + 反爬挑战页识别

```

---

## 4. 页面一览

| 路径 | 内容 |
| --- | --- |
| `/movie_list` | 8 个指标卡 + 可检索分页明细表 |
| `/movie_by_year` | 年度上榜数量柱状图（带缩放）+ 年度明细 |
| `/movie_type_percent` | 类型构成环形图（Top10 + 其他）+ 类型明细 |
| `/name_and_rating` | 评分抽样散点图（固定种子可复现）+ 全量评分分布直方图 |
| `/movie_top` | 评分 TOP10 横向条形图 + 榜单明细 |
| `/rater_top` | 评价人数 TOP20 横向条形图 + 明细 |
| `/word_cloud` | 片名 / 导演 / 类型三张词云 + 词频榜 |
| `/api/*` | 对应的图表 JSON 接口，可单独 curl 调试 |

---


## 5. 已知限制与可扩展方向

- **详情页反爬**：实测匿名请求详情页会返回「工作量证明」挑战页（JS 算 SHA-512），
  纯 `requests` 拿不到内容，需配置有效 Cookie 或改用 Playwright/Selenium。
  代码已能识别挑战页并给出明确日志。
- **进程内缓存**：当前 TTL 缓存基于进程内存，多进程部署（`gunicorn -w 4`）时各进程独立，
  生产环境应换成 Redis。
- **`movie_type` 逗号串存储**：属反范式设计，简单但无法直接用 SQL 按类型聚合，
  数据量大了应拆成 `movie / genre / movie_genre` 三张表。
- **iframe 布局**：实现简单、每个页面可独立分享，但每次切页会重新加载子页面。
  进一步优化可改为前端路由（Vue/React）或 HTMX 局部替换。
- **可加**：用户登录与收藏、评分随时间变化的时序分析、基于协同过滤的简单推荐。

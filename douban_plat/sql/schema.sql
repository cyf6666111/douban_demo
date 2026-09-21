# ============================================================
# 建表语句（推荐结构）
# 在 MySQL 8.0 中执行：mysql -u root -p < sql/schema.sql
# ============================================================

CREATE DATABASE IF NOT EXISTS `douban_plat`
    DEFAULT CHARACTER SET utf8mb4
    DEFAULT COLLATE utf8mb4_0900_ai_ci;

USE `douban_plat`;

DROP TABLE IF EXISTS `movies`;

CREATE TABLE `movies`
(
    `id`           INT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '自增主键',
    `movie_name`   VARCHAR(255) NOT NULL COMMENT '片名（中文译名 + 外文原名）',

    -- 下面三个字段都用"数值语义"的类型，而不是 varchar。
    -- 原表把 rater_count / timing / release_year 建成 varchar，
    -- 导致 ORDER BY / MAX 退化成字符串比较：
    --   '999550' > '3249655'     （评价人数排序出错）
    --   '99'     > '142'         （最长片长算成 99 分钟）
    -- 类型选错是数据库设计里最典型的"隐性 bug"。
    `release_year` SMALLINT UNSIGNED     DEFAULT NULL COMMENT '上映年份，如 1994',
    `rating`       DECIMAL(3, 1)         DEFAULT NULL COMMENT '豆瓣评分，如 9.7（DECIMAL 精确，不用 FLOAT）',
    `rater_count`  INT UNSIGNED          DEFAULT 0    COMMENT '评价人数（纯数值，不含"人"字）',
    `director`     VARCHAR(255)          DEFAULT NULL COMMENT '导演',
    `movie_type`   VARCHAR(255)          DEFAULT NULL COMMENT '类型，多个用英文逗号分隔，如 剧情,犯罪',
    `timing`       SMALLINT UNSIGNED     DEFAULT 0    COMMENT '片长（分钟）',

    `created_at`   TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '入库时间',
    `updated_at`   TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',

    PRIMARY KEY (`id`),

    -- 片名唯一：爬虫的 ON DUPLICATE KEY UPDATE 依赖它实现幂等重跑
    UNIQUE KEY `uk_movie_name` (`movie_name`),

    -- 针对页面上真实用到的排序/过滤建索引
    KEY `idx_rating` (`rating` DESC),
    KEY `idx_rater_count` (`rater_count` DESC),
    KEY `idx_release_year` (`release_year`),
    KEY `idx_director` (`director`)
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4
  COLLATE = utf8mb4_0900_ai_ci COMMENT ='豆瓣电影 Top250';

-- 说明：
-- 1) 字符集必须是 utf8mb4：utf8mb3 存不下 Emoji 和部分生僻字/韩文，
--    而本项目数据里就有韩文片名（如"7号房的礼物 7번방의 선물"）。
-- 2) movie_type 用逗号串存储属于"反范式"设计，好处是简单、查询无需 JOIN；
--    代价是无法直接用 SQL 按类型聚合（需要 SPLIT 或额外一张类型表）。
--    本项目只有 250 行，在应用层切分完全够用；
--    数据量大了应该拆成 movie / genre / movie_genre 三张表并建索引。

-- ============================================================
-- 迁移脚本 v1 → v2：修正字段类型（可选，但强烈建议执行）
--
-- 背景：原表把 release_year / rater_count / timing 建成了 varchar，
-- 于是 ORDER BY / MAX 全部按字符串比较，产生了两类错误结果：
--   ORDER BY rater_count DESC  →  '999550' > '3249655'
--                                （榜首错成《天空之城》而不是《肖申克的救赎》）
--   MAX(timing)                →  '99' > '142'（最长片长错成 99 分钟）
--
-- 代码层已经用 CAST(... AS UNSIGNED) 做了兼容，不执行本脚本页面也是对的；
-- 但 CAST 会让索引失效（对列做函数运算无法走索引），
-- 所以正确做法是把类型改对，然后就可以去掉 CAST。
--
-- 执行：mysql -u root -p douban_plat < sql/migrate_v2.sql
-- 建议先备份：mysqldump -u root -p douban_plat movies > movies_backup.sql
-- ============================================================

USE `douban_plat`;

-- ------------------------------------------------------------
-- 0. 迁移前自检：看看有多少脏数据（非纯数字），迁移会把它变成 NULL
-- ------------------------------------------------------------
SELECT '非纯数字的评价人数' AS 检查项, COUNT(*) AS 条数
FROM `movies`
WHERE `rater_count` IS NOT NULL AND `rater_count` <> '' AND `rater_count` NOT REGEXP '^[0-9]+$'
UNION ALL
SELECT '非纯数字的片长', COUNT(*)
FROM `movies`
WHERE `timing` IS NOT NULL AND `timing` <> '' AND `timing` NOT REGEXP '^[0-9]+$'
UNION ALL
SELECT '非四位数字的年份', COUNT(*)
FROM `movies`
WHERE `release_year` IS NOT NULL AND `release_year` <> '' AND `release_year` NOT REGEXP '^[0-9]{4}$';

-- ------------------------------------------------------------
-- 1. 先加新列（不直接改原列，万一数据有问题还能回滚）
-- ------------------------------------------------------------
ALTER TABLE `movies`
    ADD COLUMN `release_year_new` SMALLINT UNSIGNED DEFAULT NULL COMMENT '上映年份' AFTER `movie_name`,
    ADD COLUMN `rater_count_new`  INT UNSIGNED      DEFAULT 0    COMMENT '评价人数' AFTER `rating`,
    ADD COLUMN `timing_new`       SMALLINT UNSIGNED DEFAULT 0    COMMENT '片长（分钟）' AFTER `movie_type`;

-- ------------------------------------------------------------
-- 2. 数据搬迁：用正则清洗掉非数字字符后再转换
--    REGEXP_REPLACE 需要 MySQL 8.0+
--    空串或非数字一律置 NULL/0，避免 CAST 报 "Truncated incorrect" 警告
-- ------------------------------------------------------------
UPDATE `movies`
SET `release_year_new` = CASE
                             WHEN `release_year` REGEXP '^[0-9]{4}$'
                                 THEN CAST(`release_year` AS UNSIGNED)
                             ELSE NULL
    END,
    `rater_count_new`  = CASE
                             WHEN `rater_count` REGEXP '^[0-9]+$'
                                 THEN CAST(`rater_count` AS UNSIGNED)
                             ELSE 0
        END,
    `timing_new`       = CASE
                             WHEN `timing` REGEXP '^[0-9]+$'
                                 THEN CAST(`timing` AS UNSIGNED)
                             ELSE 0
        END;

-- ------------------------------------------------------------
-- 3. 校验：新旧值必须一致（不一致说明清洗规则需要调整）
-- ------------------------------------------------------------
SELECT `id`, `release_year`, `release_year_new`, `rater_count`, `rater_count_new`,
       `timing`, `timing_new`
FROM `movies`
WHERE COALESCE(CAST(NULLIF(`release_year`, '') AS UNSIGNED), 0) <> COALESCE(`release_year_new`, 0)
   OR COALESCE(CAST(NULLIF(`rater_count`, '') AS UNSIGNED), 0) <> COALESCE(`rater_count_new`, 0)
   OR COALESCE(CAST(NULLIF(`timing`, '') AS UNSIGNED), 0) <> COALESCE(`timing_new`, 0)
LIMIT 20;

-- ------------------------------------------------------------
-- 4. 确认无误后替换列（确认第 3 步没有返回任何行再执行）
-- ------------------------------------------------------------
ALTER TABLE `movies`
    DROP COLUMN `release_year`,
    DROP COLUMN `rater_count`,
    DROP COLUMN `timing`;

ALTER TABLE `movies`
    CHANGE COLUMN `release_year_new` `release_year` SMALLINT UNSIGNED DEFAULT NULL COMMENT '上映年份',
    CHANGE COLUMN `rater_count_new` `rater_count` INT UNSIGNED DEFAULT 0 COMMENT '评价人数',
    CHANGE COLUMN `timing_new` `timing` SMALLINT UNSIGNED DEFAULT 0 COMMENT '片长（分钟）';

-- rating 从 FLOAT 改成 DECIMAL：FLOAT 是近似值，9.7 可能存成 9.699999…
-- 评分这种需要精确比较/显示的字段应该用 DECIMAL
ALTER TABLE `movies`
    MODIFY COLUMN `rating` DECIMAL(3, 1) DEFAULT NULL COMMENT '豆瓣评分';

-- ------------------------------------------------------------
-- 5. 补索引：页面上真实的排序/过滤都会用到
-- ------------------------------------------------------------
ALTER TABLE `movies`
    ADD INDEX `idx_rating` (`rating` DESC),
    ADD INDEX `idx_rater_count` (`rater_count` DESC),
    ADD INDEX `idx_release_year` (`release_year`),
    ADD INDEX `idx_director` (`director`);

-- ------------------------------------------------------------
-- 6. 迁移后验证：这两条的结果与页面显示应当一致
-- ------------------------------------------------------------
-- 评价人数榜首应为《肖申克的救赎》3249655，而不是《天空之城》999550
SELECT `movie_name`, `rater_count`
FROM `movies`
ORDER BY `rater_count` DESC
LIMIT 5;

-- 最长片长应为 237 分钟，而不是 99
SELECT MAX(`timing`) AS 最长片长, MIN(`release_year`) AS 最早年份, MAX(`release_year`) AS 最晚年份
FROM `movies`;

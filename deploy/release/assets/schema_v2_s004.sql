-- 人情账本：多用户 Schema 定稿版 S004
-- 目标 MySQL 8.4 LTS / InnoDB / utf8mb4。
-- S004 修改前数据库回退点：schema-v2-final；发布前计划回退点：backend-p3-plan-r005。
-- 状态：S004 按 D044、D045 删除亲属姓名字段；已通过 MySQL 8.4.11 动态验证。
-- 只用于独立新建库，绝不能作为旧 contact_db 的原地升级脚本。
-- 详见 SCHEMA_SQL_REVIEW.md：本 SQL 不代替认证、用户隔离查询和跨表业务校验。
-- SQL 不创建账号或本人占位记录；首次登录提交真实姓名后由后端事务建档。
-- 正式部署前仍须核对目标实例、库名和备份。DDL 不是整份事务；任何错误应停止，禁止忽略错误继续。

SET NAMES utf8mb4;
SET @schema_v2_previous_sql_mode = @@SESSION.sql_mode;
SET SESSION sql_mode = 'STRICT_TRANS_TABLES,NO_ZERO_IN_DATE,NO_ZERO_DATE,ERROR_FOR_DIVISION_BY_ZERO,NO_ENGINE_SUBSTITUTION';

-- 固定新库名称，不使用 IF NOT EXISTS，避免静默复用既有数据库。
-- 此处仅为评审拟定名称；部署时核对目标实例与库名，出错即停止。
CREATE DATABASE `gift_ledger_v2`
  CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin;
USE `gift_ledger_v2`;

-- 1. 账号：真实姓名仅保存在 contacts；公开注册不能提交管理员身份。
CREATE TABLE `users` (
  `id` INT NOT NULL AUTO_INCREMENT,
  `username` VARCHAR(30) NOT NULL COMMENT '登录名，后端归一化为小写后写入',
  `password_hash` VARCHAR(255) NOT NULL COMMENT '密码哈希；不保存明文',
  `is_admin` TINYINT NOT NULL DEFAULT 0,
  `is_active` TINYINT NOT NULL DEFAULT 1,
  `created_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_users_username` (`username`),
  CONSTRAINT `ck_users_username` CHECK (
    CHAR_LENGTH(`username`) BETWEEN 4 AND 30
    AND ASCII(LEFT(`username`, 1)) BETWEEN 97 AND 122
    AND NOT REGEXP_LIKE(`username`, '[^a-z0-9_]', 'c')
  ),
  CONSTRAINT `ck_users_password_hash` CHECK (REGEXP_LIKE(`password_hash`, '[^[:space:]]')),
  CONSTRAINT `ck_users_admin` CHECK (`is_admin` IN (0, 1)),
  CONSTRAINT `ck_users_active` CHECK (`is_active` IN (0, 1))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin COMMENT='登录账号';

-- 2-4. 三张字典：user_id 为空是系统项，非空是该用户自定义项。
-- name_key 采用忽略首尾 ASCII 空格、英文大小写不敏感的去重口径；
-- 后端还须拒绝自定义项与系统项跨作用域重名，并处理更广泛的 Unicode 空白。
CREATE TABLE `relationship_types` (
  `id` INT NOT NULL AUTO_INCREMENT,
  `user_id` INT DEFAULT NULL COMMENT 'NULL=系统内置，非NULL=用户自定义',
  `type_name` VARCHAR(50) NOT NULL,
  `sort_order` INT NOT NULL DEFAULT 0,
  `is_active` TINYINT NOT NULL DEFAULT 1,
  `scope_key` INT UNSIGNED GENERATED ALWAYS AS (COALESCE(`user_id`, 0)) VIRTUAL,
  `name_key` VARCHAR(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_as_ci
    GENERATED ALWAYS AS (TRIM(`type_name`)) VIRTUAL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_relationship_scope_name` (`scope_key`, `name_key`),
  KEY `idx_relationship_user_active_sort` (`user_id`, `is_active`, `sort_order`),
  CONSTRAINT `ck_relationship_name` CHECK (
    `type_name` = TRIM(`type_name`) AND CHAR_LENGTH(`type_name`) BETWEEN 1 AND 50
  ),
  CONSTRAINT `ck_relationship_active` CHECK (`is_active` IN (0, 1)),
  CONSTRAINT `fk_relationship_user` FOREIGN KEY (`user_id`)
    REFERENCES `users` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin COMMENT='关系类型：系统预置及用户自定义';

CREATE TABLE `kinship_types` (
  `id` INT NOT NULL AUTO_INCREMENT,
  `user_id` INT DEFAULT NULL COMMENT 'NULL=系统内置，非NULL=用户自定义',
  `type_name` VARCHAR(50) NOT NULL,
  `sort_order` INT NOT NULL DEFAULT 0,
  `is_active` TINYINT NOT NULL DEFAULT 1,
  `scope_key` INT UNSIGNED GENERATED ALWAYS AS (COALESCE(`user_id`, 0)) VIRTUAL,
  `name_key` VARCHAR(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_as_ci
    GENERATED ALWAYS AS (TRIM(`type_name`)) VIRTUAL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_kinship_scope_name` (`scope_key`, `name_key`),
  KEY `idx_kinship_user_active_sort` (`user_id`, `is_active`, `sort_order`),
  CONSTRAINT `ck_kinship_name` CHECK (
    `type_name` = TRIM(`type_name`) AND CHAR_LENGTH(`type_name`) BETWEEN 1 AND 50
  ),
  CONSTRAINT `ck_kinship_active` CHECK (`is_active` IN (0, 1)),
  CONSTRAINT `fk_kinship_user` FOREIGN KEY (`user_id`)
    REFERENCES `users` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin COMMENT='亲属称谓：系统预置及用户自定义';

CREATE TABLE `event_types` (
  `id` INT NOT NULL AUTO_INCREMENT,
  `user_id` INT DEFAULT NULL COMMENT 'NULL=系统内置，非NULL=用户自定义',
  `type_name` VARCHAR(50) NOT NULL,
  `sort_order` INT NOT NULL DEFAULT 0,
  `is_active` TINYINT NOT NULL DEFAULT 1,
  `scope_key` INT UNSIGNED GENERATED ALWAYS AS (COALESCE(`user_id`, 0)) VIRTUAL,
  `name_key` VARCHAR(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_as_ci
    GENERATED ALWAYS AS (TRIM(`type_name`)) VIRTUAL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_event_type_scope_name` (`scope_key`, `name_key`),
  KEY `idx_event_type_user_active_sort` (`user_id`, `is_active`, `sort_order`),
  CONSTRAINT `ck_event_type_name` CHECK (
    `type_name` = TRIM(`type_name`) AND CHAR_LENGTH(`type_name`) BETWEEN 1 AND 50
  ),
  CONSTRAINT `ck_event_type_active` CHECK (`is_active` IN (0, 1)),
  CONSTRAINT `fk_event_type_user` FOREIGN KEY (`user_id`)
    REFERENCES `users` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin COMMENT='事件类型：系统预置及用户自定义';

-- 5. 联系人：唯一索引保证每用户至多一个本人，不保证一定存在或禁止删除。
-- 后端专用建档流程保证本人存在；编辑、停用、导入、恢复均须保护本人身份。
-- 已被事件或流水引用的普通联系人由外键限制删除，只能停用；停用不影响历史记录。
CREATE TABLE `contacts` (
  `id` INT NOT NULL AUTO_INCREMENT,
  `user_id` INT NOT NULL,
  `name` VARCHAR(50) NOT NULL COMMENT '真实姓名，可重名',
  `relation_type_id` INT NOT NULL COMMENT '关系类型，ID=1表示自己',
  `location` VARCHAR(50) DEFAULT NULL,
  `phone` VARCHAR(20) DEFAULT NULL,
  `remark` TEXT,
  `is_active` TINYINT NOT NULL DEFAULT 1,
  `created_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `self_user_id` INT GENERATED ALWAYS AS (
    CASE WHEN `relation_type_id` = 1 THEN `user_id` ELSE NULL END
  ) VIRTUAL COMMENT '内部本人唯一键，不可手工填写',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_contacts_user_id` (`user_id`, `id`),
  UNIQUE KEY `uq_contacts_one_self` (`self_user_id`),
  KEY `idx_contacts_user_relation` (`user_id`, `relation_type_id`),
  KEY `idx_contacts_user_active_name` (`user_id`, `is_active`, `name`),
  KEY `idx_contacts_relation_fk` (`relation_type_id`),
  CONSTRAINT `ck_contacts_name` CHECK (REGEXP_LIKE(`name`, '[^[:space:]]')),
  CONSTRAINT `ck_contacts_active` CHECK (`is_active` IN (0, 1)),
  CONSTRAINT `fk_contacts_user` FOREIGN KEY (`user_id`)
    REFERENCES `users` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT,
  CONSTRAINT `fk_contacts_relation` FOREIGN KEY (`relation_type_id`)
    REFERENCES `relationship_types` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin COMMENT='用户独立联系人';

-- 6. 事件：同用户、同事主、同称谓、同事件类型、同办事日期复用。
-- 不保存亲属姓名；同日不同亲属应选择“长子”“次子”等精确称谓区分。
-- 后端须验证字典归属；普通外键只验证字典 ID 存在。
CREATE TABLE `event_records` (
  `id` INT NOT NULL AUTO_INCREMENT,
  `user_id` INT NOT NULL,
  `contact_id` INT NOT NULL COMMENT '事主、收礼方',
  `relative_type_id` INT NOT NULL,
  `event_type_id` INT NOT NULL,
  `event_date` DATE NOT NULL,
  `remark` TEXT,
  `created_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_events_user_id` (`user_id`, `id`),
  UNIQUE KEY `uq_events_identity` (
    `user_id`, `contact_id`, `relative_type_id`, `event_type_id`, `event_date`
  ),
  KEY `idx_events_user_date` (`user_id`, `event_date`),
  KEY `idx_events_user_type` (`user_id`, `event_type_id`),
  KEY `idx_events_relative_fk` (`relative_type_id`),
  KEY `idx_events_type_fk` (`event_type_id`),
  CONSTRAINT `ck_events_date` CHECK (
    `event_date` >= '1000-01-01'
    AND LAST_DAY(`event_date`) IS NOT NULL
    AND DAYOFMONTH(`event_date`) BETWEEN 1 AND DAYOFMONTH(LAST_DAY(`event_date`))
  ),
  CONSTRAINT `fk_events_user` FOREIGN KEY (`user_id`)
    REFERENCES `users` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT,
  CONSTRAINT `fk_events_host` FOREIGN KEY (`user_id`, `contact_id`)
    REFERENCES `contacts` (`user_id`, `id`) ON DELETE RESTRICT ON UPDATE RESTRICT,
  CONSTRAINT `fk_events_relative` FOREIGN KEY (`relative_type_id`)
    REFERENCES `kinship_types` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT,
  CONSTRAINT `fk_events_type` FOREIGN KEY (`event_type_id`)
    REFERENCES `event_types` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin COMMENT='用户独立事件';

-- 7. 流水：不存收支方向；同一随礼人在同一事件中只允许一条流水。
-- 事主与随礼人不同且必须有一方是本人，由后端在所有写入路径校验。
-- 不记录实际随礼日期；统计按所属事件的 event_date，created_at 只记录系统录入时间。
CREATE TABLE `gift_records` (
  `id` INT NOT NULL AUTO_INCREMENT,
  `user_id` INT NOT NULL,
  `event_record_id` INT NOT NULL,
  `contact_id` INT NOT NULL COMMENT '随礼人、送礼方',
  `amount` DECIMAL(10,2) NOT NULL DEFAULT '0.00',
  `gift_details` VARCHAR(255) DEFAULT NULL,
  `remark` TEXT,
  `created_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '系统录入时间，不作为收支统计日期',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_gifts_user_event_giver` (`user_id`, `event_record_id`, `contact_id`),
  KEY `idx_gifts_user_giver` (`user_id`, `contact_id`),
  CONSTRAINT `ck_gifts_amount` CHECK (`amount` >= 0),
  CONSTRAINT `ck_gifts_cash_or_goods` CHECK (
    `amount` > 0 OR (
      `gift_details` IS NOT NULL
      AND REGEXP_LIKE(`gift_details`, '[^[:space:]]')
    )
  ),
  CONSTRAINT `fk_gifts_user` FOREIGN KEY (`user_id`)
    REFERENCES `users` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT,
  CONSTRAINT `fk_gifts_event` FOREIGN KEY (`user_id`, `event_record_id`)
    REFERENCES `event_records` (`user_id`, `id`) ON DELETE RESTRICT ON UPDATE RESTRICT,
  CONSTRAINT `fk_gifts_giver` FOREIGN KEY (`user_id`, `contact_id`)
    REFERENCES `contacts` (`user_id`, `id`) ON DELETE RESTRICT ON UPDATE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin COMMENT='用户独立随礼流水';

-- 系统预置数据。D010：正式部署前名称仍需用户最终确认。
INSERT INTO `relationship_types` (`id`, `user_id`, `type_name`, `sort_order`, `is_active`) VALUES
(1, NULL, '自己', 1, 1),
(2, NULL, '亲戚', 2, 1),
(3, NULL, '同事', 3, 1),
(4, NULL, '初高中同学', 4, 1),
(5, NULL, '大学同学', 5, 1),
(6, NULL, '战友', 6, 1),
(7, NULL, '生意伙伴', 7, 1),
(8, NULL, '邻居', 8, 1),
(9, NULL, '朋友', 9, 1);

INSERT INTO `kinship_types` (`id`, `user_id`, `type_name`, `sort_order`, `is_active`) VALUES
(1, NULL, '本人', 1, 1),
(2, NULL, '父亲', 2, 1),
(3, NULL, '母亲', 3, 1),
(4, NULL, '岳父', 4, 1),
(5, NULL, '岳母', 5, 1),
(6, NULL, '公公', 6, 1),
(7, NULL, '婆婆', 7, 1),
(8, NULL, '长子', 8, 1),
(9, NULL, '次子', 9, 1),
(10, NULL, '长女', 10, 1),
(11, NULL, '次女', 11, 1),
(12, NULL, '祖父 (爷爷)', 12, 1),
(13, NULL, '祖母 (奶奶)', 13, 1);

INSERT INTO `event_types` (`id`, `user_id`, `type_name`, `sort_order`, `is_active`) VALUES
(1, NULL, '结婚', 1, 1),
(2, NULL, '添子 (满月/百天)', 2, 1),
(3, NULL, '生日 (寿宴)', 3, 1),
(4, NULL, '驾归瑶池 (白事)', 4, 1),
(5, NULL, '乔迁新居', 5, 1),
(6, NULL, '榜上有名 (升学)', 6, 1),
(7, NULL, '金日开业', 7, 1),
(8, NULL, '微恙慰问 (生病就医)', 8, 1),
(9, NULL, '日常拜年', 9, 1);

SET SESSION sql_mode = @schema_v2_previous_sql_mode;

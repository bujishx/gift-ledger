-- 人情账本：多用户 Schema 当前新库基线 S011
-- 目标 MySQL 8.4 LTS / InnoDB / utf8mb4。
-- 历史 S006/S007/S008 基线分别封存于同名 .S006.sql/.S007.sql/.S008.sql；旧库升级使用 Alembic 迁移。
-- 当前按 D102、D103、D108、D116、D133、D138、D139、D142 实施字典、联系人及事件身份规则。
-- 新库核对结构后登记 alembic stamp schema_v2_s011；不得将当前 SQL 登记为旧版本。
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
  `system_code` VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin DEFAULT NULL
    COMMENT '系统内置项稳定标识；用户自定义项必须为空',
  `type_name` VARCHAR(50) NOT NULL,
  `sort_order` INT NOT NULL DEFAULT 0,
  `is_active` TINYINT NOT NULL DEFAULT 1,
  `scope_key` INT UNSIGNED GENERATED ALWAYS AS (COALESCE(`user_id`, 0)) VIRTUAL,
  `name_key` VARCHAR(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_as_ci
    GENERATED ALWAYS AS (TRIM(`type_name`)) VIRTUAL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_relationship_scope_name` (`scope_key`, `name_key`),
  UNIQUE KEY `uq_relationship_system_code` (`system_code`),
  KEY `idx_relationship_user_active_sort` (`user_id`, `is_active`, `sort_order`),
  CONSTRAINT `ck_relationship_name` CHECK (
    `type_name` = TRIM(`type_name`) AND CHAR_LENGTH(`type_name`) BETWEEN 1 AND 50
  ),
  CONSTRAINT `ck_relationship_active` CHECK (`is_active` IN (0, 1)),
  CONSTRAINT `ck_relationship_system_owner` CHECK (
    (`user_id` IS NULL AND `system_code` IS NOT NULL)
    OR (`user_id` IS NOT NULL AND `system_code` IS NULL)
  ),
  CONSTRAINT `ck_relationship_system_code` CHECK (
    `system_code` IS NULL OR REGEXP_LIKE(`system_code`, '^[a-z][a-z0-9._]{0,63}$', 'c')
  ),
  CONSTRAINT `fk_relationship_user` FOREIGN KEY (`user_id`)
    REFERENCES `users` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin COMMENT='关系类型：系统预置及用户自定义';

CREATE TABLE `kinship_types` (
  `id` INT NOT NULL AUTO_INCREMENT,
  `user_id` INT DEFAULT NULL COMMENT 'NULL=系统内置，非NULL=用户自定义',
  `system_code` VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin DEFAULT NULL
    COMMENT '系统内置项稳定标识；用户自定义项必须为空',
  `type_name` VARCHAR(50) NOT NULL,
  `sort_order` INT NOT NULL DEFAULT 0,
  `is_active` TINYINT NOT NULL DEFAULT 1,
  `scope_key` INT UNSIGNED GENERATED ALWAYS AS (COALESCE(`user_id`, 0)) VIRTUAL,
  `name_key` VARCHAR(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_as_ci
    GENERATED ALWAYS AS (TRIM(`type_name`)) VIRTUAL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_kinship_scope_name` (`scope_key`, `name_key`),
  UNIQUE KEY `uq_kinship_system_code` (`system_code`),
  KEY `idx_kinship_user_active_sort` (`user_id`, `is_active`, `sort_order`),
  CONSTRAINT `ck_kinship_name` CHECK (
    `type_name` = TRIM(`type_name`) AND CHAR_LENGTH(`type_name`) BETWEEN 1 AND 50
  ),
  CONSTRAINT `ck_kinship_active` CHECK (`is_active` IN (0, 1)),
  CONSTRAINT `ck_kinship_system_owner` CHECK (
    (`user_id` IS NULL AND `system_code` IS NOT NULL)
    OR (`user_id` IS NOT NULL AND `system_code` IS NULL)
  ),
  CONSTRAINT `ck_kinship_system_code` CHECK (
    `system_code` IS NULL OR REGEXP_LIKE(`system_code`, '^[a-z][a-z0-9._]{0,63}$', 'c')
  ),
  CONSTRAINT `fk_kinship_user` FOREIGN KEY (`user_id`)
    REFERENCES `users` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin COMMENT='亲属称谓：系统预置及用户自定义';

CREATE TABLE `event_types` (
  `id` INT NOT NULL AUTO_INCREMENT,
  `user_id` INT DEFAULT NULL COMMENT 'NULL=系统内置，非NULL=用户自定义',
  `system_code` VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin DEFAULT NULL
    COMMENT '系统内置项稳定标识；用户自定义项必须为空',
  `type_name` VARCHAR(50) NOT NULL,
  `sort_order` INT NOT NULL DEFAULT 0,
  `is_active` TINYINT NOT NULL DEFAULT 1,
  `scope_key` INT UNSIGNED GENERATED ALWAYS AS (COALESCE(`user_id`, 0)) VIRTUAL,
  `name_key` VARCHAR(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_as_ci
    GENERATED ALWAYS AS (TRIM(`type_name`)) VIRTUAL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_event_type_scope_name` (`scope_key`, `name_key`),
  UNIQUE KEY `uq_event_type_system_code` (`system_code`),
  KEY `idx_event_type_user_active_sort` (`user_id`, `is_active`, `sort_order`),
  CONSTRAINT `ck_event_type_name` CHECK (
    `type_name` = TRIM(`type_name`) AND CHAR_LENGTH(`type_name`) BETWEEN 1 AND 50
  ),
  CONSTRAINT `ck_event_type_active` CHECK (`is_active` IN (0, 1)),
  CONSTRAINT `ck_event_type_system_owner` CHECK (
    (`user_id` IS NULL AND `system_code` IS NOT NULL)
    OR (`user_id` IS NOT NULL AND `system_code` IS NULL)
  ),
  CONSTRAINT `ck_event_type_system_code` CHECK (
    `system_code` IS NULL OR REGEXP_LIKE(`system_code`, '^[a-z][a-z0-9._]{0,63}$', 'c')
  ),
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
  `relation_type_id` INT NOT NULL COMMENT '关系类型，ID=1表示本人',
  `location` VARCHAR(50) DEFAULT NULL,
  `phone` VARCHAR(20) DEFAULT NULL COMMENT '电话，入库前移除全部空白字符',
  `remark` TEXT,
  `identity_hash` BINARY(32) NOT NULL
    COMMENT '服务端按规范化姓名和联系人备注生成的内部身份指纹',
  `is_active` TINYINT NOT NULL DEFAULT 1,
  `created_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `self_user_id` INT GENERATED ALWAYS AS (
    CASE WHEN `relation_type_id` = 1 THEN `user_id` ELSE NULL END
  ) VIRTUAL COMMENT '内部本人唯一键，不可手工填写',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_contacts_user_id` (`user_id`, `id`),
  UNIQUE KEY `uq_contacts_one_self` (`self_user_id`),
  UNIQUE KEY `uq_contacts_user_identity` (`user_id`, `identity_hash`),
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

-- 6. 事件：同用户按事主ID、亲属称谓ID、事件类型ID、首尾去空白的事件备注判重。
-- 办事日期仍必填并用于统计，但不参与身份；重复发生的同类事件以备注区分。
-- 不保存亲属姓名；不同亲属应选择精确称谓，必要时在事件备注中区分。
-- 后端须验证字典归属；普通外键只验证字典 ID 存在。
CREATE TABLE `event_records` (
  `id` INT NOT NULL AUTO_INCREMENT,
  `user_id` INT NOT NULL,
  `contact_id` INT NOT NULL COMMENT '事主、收礼方',
  `relative_type_id` INT NOT NULL,
  `event_type_id` INT NOT NULL,
  `event_date` DATE DEFAULT NULL,
  `date_precision` VARCHAR(7) NOT NULL DEFAULT 'day',
  `event_year` SMALLINT UNSIGNED DEFAULT NULL,
  `event_month` TINYINT UNSIGNED DEFAULT NULL,
  `date_source` VARCHAR(20) NOT NULL DEFAULT 'web',
  `remark` TEXT,
  `identity_hash` BINARY(32) NOT NULL
    COMMENT '服务端按事主、亲属称谓、事件类型和首尾去空白备注生成的内部身份指纹',
  `created_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_events_user_id` (`user_id`, `id`),
  UNIQUE KEY `uq_events_user_identity` (`user_id`, `identity_hash`),
  KEY `idx_events_user_host` (`user_id`, `contact_id`),
  KEY `idx_events_user_date` (`user_id`, `event_date`),
  KEY `idx_events_user_type` (`user_id`, `event_type_id`),
  KEY `idx_events_relative_fk` (`relative_type_id`),
  KEY `idx_events_type_fk` (`event_type_id`),
  CONSTRAINT `ck_events_date` CHECK (
    `date_source` IN ('web', 'legacy_import') AND (`date_source` = 'legacy_import' OR `date_precision` = 'day') AND ((`date_precision` = 'day' AND `event_date` IS NOT NULL AND `event_date` >= '1000-01-01' AND LAST_DAY(`event_date`) IS NOT NULL AND DAYOFMONTH(`event_date`) BETWEEN 1 AND DAYOFMONTH(LAST_DAY(`event_date`)) AND `event_year` IS NOT NULL AND `event_year` = YEAR(`event_date`) AND `event_month` IS NOT NULL AND `event_month` = MONTH(`event_date`)) OR (`date_precision` = 'month' AND `event_date` IS NULL AND `event_year` IS NOT NULL AND `event_year` BETWEEN 1000 AND 9999 AND `event_month` IS NOT NULL AND `event_month` BETWEEN 1 AND 12) OR (`date_precision` = 'year' AND `event_date` IS NULL AND `event_year` IS NOT NULL AND `event_year` BETWEEN 1000 AND 9999 AND `event_month` IS NULL) OR (`date_precision` = 'unknown' AND `event_date` IS NULL AND `event_year` IS NULL AND `event_month` IS NULL))
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

CREATE TABLE `legacy_import_batches` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `user_id` INT NOT NULL,
  `batch_id` VARCHAR(64) NOT NULL,
  `result_json` TEXT NOT NULL,
  `created_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_import_batches_user_batch` (`user_id`, `batch_id`),
  CONSTRAINT `fk_import_batches_user` FOREIGN KEY (`user_id`)
    REFERENCES `users` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin COMMENT='用户旧账导入完成结果，用于幂等重试';

-- 系统预置数据。显示名称按 D138 更新；system_code 是跨环境稳定身份，既有 ID 保留。
INSERT INTO `relationship_types`
  (`id`, `user_id`, `system_code`, `type_name`, `sort_order`, `is_active`) VALUES
(1, NULL, 'relationship.self', '我', 1, 1),
(2, NULL, 'relationship.relative', '亲戚', 2, 1),
(3, NULL, 'relationship.friend', '朋友', 3, 1),
(4, NULL, 'relationship.classmate', '同学', 4, 1),
(5, NULL, 'relationship.colleague', '同事', 5, 1),
(6, NULL, 'relationship.neighbor', '邻居', 6, 1),
(7, NULL, 'relationship.military_comrade', '战友', 7, 1),
(8, NULL, 'relationship.business_partner', '生意伙伴', 8, 1),
(10, NULL, 'relationship.fellow_townsman', '同乡', 9, 1),
(9, NULL, 'relationship.other', '其他', 10, 1);

INSERT INTO `kinship_types`
  (`id`, `user_id`, `system_code`, `type_name`, `sort_order`, `is_active`) VALUES
(1, NULL, 'kinship.self', '本人', 1, 1),
(2, NULL, 'kinship.spouse', '之配偶', 2, 1),
(3, NULL, 'kinship.father', '之父亲', 3, 1),
(4, NULL, 'kinship.mother', '之母亲', 4, 1),
(5, NULL, 'kinship.wife_father', '之岳父', 5, 1),
(6, NULL, 'kinship.wife_mother', '之岳母', 6, 1),
(7, NULL, 'kinship.husband_father', '之公公', 7, 1),
(8, NULL, 'kinship.husband_mother', '之婆婆', 8, 1),
(9, NULL, 'kinship.first_son', '之长子', 9, 1),
(10, NULL, 'kinship.second_son', '之次子', 10, 1),
(11, NULL, 'kinship.first_daughter', '之长女', 11, 1),
(12, NULL, 'kinship.second_daughter', '之次女', 12, 1),
(17, NULL, 'kinship.grandchildren', '之孙辈', 13, 1),
(13, NULL, 'kinship.paternal_grandfather', '之祖父', 14, 1),
(14, NULL, 'kinship.paternal_grandmother', '之祖母', 15, 1),
(15, NULL, 'kinship.maternal_grandfather', '之外祖父', 16, 1),
(16, NULL, 'kinship.maternal_grandmother', '之外祖母', 17, 1);

INSERT INTO `event_types`
  (`id`, `user_id`, `system_code`, `type_name`, `sort_order`, `is_active`) VALUES
(1, NULL, 'event.wedding', '婚礼', 1, 1),
(2, NULL, 'event.newborn', '新生儿', 2, 1),
(3, NULL, 'event.visit_patient', '探病慰问', 3, 1),
(4, NULL, 'event.funeral', '白事', 4, 1),
(5, NULL, 'event.housewarming', '乔迁', 5, 1),
(6, NULL, 'event.education', '升学', 6, 1),
(7, NULL, 'event.business_opening', '开业', 7, 1),
(8, NULL, 'event.new_year_visit', '拜年', 8, 1),
(9, NULL, 'event.birthday', '生日', 9, 1);

SET SESSION sql_mode = @schema_v2_previous_sql_mode;

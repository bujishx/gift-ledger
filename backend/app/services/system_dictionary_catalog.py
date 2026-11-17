"""三类系统字典的稳定身份、当前显示名称和旧模板别名。"""

from __future__ import annotations

from dataclasses import dataclass

SELF_RELATIONSHIP_FIXED_ID = 1
SELF_RELATIONSHIP_SYSTEM_CODE = "relationship.self"


@dataclass(frozen=True)
class SystemDictionarySeed:
    system_code: str
    name: str
    sort_order: int
    legacy_id: int | None


RELATIONSHIP_SEEDS = (
    SystemDictionarySeed(
        SELF_RELATIONSHIP_SYSTEM_CODE,
        "我",
        1,
        SELF_RELATIONSHIP_FIXED_ID,
    ),
    SystemDictionarySeed("relationship.relative", "亲戚", 2, 2),
    SystemDictionarySeed("relationship.friend", "朋友", 3, 9),
    SystemDictionarySeed("relationship.classmate", "同学", 4, 4),
    SystemDictionarySeed("relationship.colleague", "同事", 5, 3),
    SystemDictionarySeed("relationship.neighbor", "邻居", 6, 8),
    SystemDictionarySeed("relationship.military_comrade", "战友", 7, 6),
    SystemDictionarySeed("relationship.business_partner", "生意伙伴", 8, 7),
    SystemDictionarySeed("relationship.fellow_townsman", "同乡", 9, None),
    SystemDictionarySeed("relationship.other", "其他", 10, None),
)

KINSHIP_SEEDS = (
    SystemDictionarySeed("kinship.self", "本人", 1, 1),
    SystemDictionarySeed("kinship.spouse", "之配偶", 2, None),
    SystemDictionarySeed("kinship.father", "之父亲", 3, 2),
    SystemDictionarySeed("kinship.mother", "之母亲", 4, 3),
    SystemDictionarySeed("kinship.wife_father", "之岳父", 5, 4),
    SystemDictionarySeed("kinship.wife_mother", "之岳母", 6, 5),
    SystemDictionarySeed("kinship.husband_father", "之公公", 7, 6),
    SystemDictionarySeed("kinship.husband_mother", "之婆婆", 8, 7),
    SystemDictionarySeed("kinship.first_son", "之长子", 9, 8),
    SystemDictionarySeed("kinship.second_son", "之次子", 10, 9),
    SystemDictionarySeed("kinship.first_daughter", "之长女", 11, 10),
    SystemDictionarySeed("kinship.second_daughter", "之次女", 12, 11),
    SystemDictionarySeed("kinship.grandchildren", "之孙辈", 13, None),
    SystemDictionarySeed("kinship.paternal_grandfather", "之祖父", 14, 12),
    SystemDictionarySeed("kinship.paternal_grandmother", "之祖母", 15, 13),
    SystemDictionarySeed("kinship.maternal_grandfather", "之外祖父", 16, None),
    SystemDictionarySeed("kinship.maternal_grandmother", "之外祖母", 17, None),
)

EVENT_SEEDS = (
    SystemDictionarySeed("event.wedding", "婚礼", 1, 1),
    SystemDictionarySeed("event.newborn", "新生儿", 2, 2),
    SystemDictionarySeed("event.visit_patient", "探病慰问", 3, 8),
    SystemDictionarySeed("event.funeral", "白事", 4, 4),
    SystemDictionarySeed("event.housewarming", "乔迁", 5, 5),
    SystemDictionarySeed("event.education", "升学", 6, 6),
    SystemDictionarySeed("event.business_opening", "开业", 7, 7),
    SystemDictionarySeed("event.new_year_visit", "拜年", 8, 9),
    SystemDictionarySeed("event.birthday", "生日", 9, 3),
)

SEEDS_BY_CATEGORY = {
    "relationship": RELATIONSHIP_SEEDS,
    "kinship": KINSHIP_SEEDS,
    "event": EVENT_SEEDS,
}

# 仅供旧账 Excel 导入使用。键与旧 S005—S008 模板名称精确一致, 值是稳定身份。
# 正常字典列表、改名和搜索不得使用这些别名, 也不得做模糊匹配。
S005_EXCEL_ALIASES_BY_CATEGORY = {
    "relationship": {
        "自己": SELF_RELATIONSHIP_SYSTEM_CODE,
        "本人": SELF_RELATIONSHIP_SYSTEM_CODE,
        "初高中同学": "relationship.classmate",
        "大学同学": "relationship.classmate",
    },
    "kinship": {
        "祖父 (爷爷)": "kinship.paternal_grandfather",
        "祖母 (奶奶)": "kinship.paternal_grandmother",
        "祖父（爷爷）": "kinship.paternal_grandfather",
        "祖母（奶奶）": "kinship.paternal_grandmother",
        "外祖父（外公）": "kinship.maternal_grandfather",
        "外祖母（外婆）": "kinship.maternal_grandmother",
        "之祖父（爷爷）": "kinship.paternal_grandfather",
        "之祖母（奶奶）": "kinship.paternal_grandmother",
        "之外祖父（外公）": "kinship.maternal_grandfather",
        "之外祖母（外婆）": "kinship.maternal_grandmother",
        "配偶": "kinship.spouse",
        "父亲": "kinship.father",
        "母亲": "kinship.mother",
        "岳父": "kinship.wife_father",
        "岳母": "kinship.wife_mother",
        "公公": "kinship.husband_father",
        "婆婆": "kinship.husband_mother",
        "长子": "kinship.first_son",
        "次子": "kinship.second_son",
        "长女": "kinship.first_daughter",
        "次女": "kinship.second_daughter",
    },
    "event": {
        "结婚": "event.wedding",
        "添子 (满月/百天)": "event.newborn",
        "生日 (寿宴)": "event.birthday",
        "驾归瑶池 (白事)": "event.funeral",
        "乔迁新居": "event.housewarming",
        "榜上有名 (升学)": "event.education",
        "金日开业": "event.business_opening",
        "微恙慰问 (生病就医)": "event.visit_patient",
        "日常拜年": "event.new_year_visit",
        "新生儿（满月／百日）": "event.newborn",
        "白事（丧葬）": "event.funeral",
        "生日／寿宴": "event.birthday",
    },
}


def final_names(category: str) -> tuple[str, ...]:
    return tuple(seed.name for seed in SEEDS_BY_CATEGORY[category])


__all__ = [
    "EVENT_SEEDS",
    "KINSHIP_SEEDS",
    "RELATIONSHIP_SEEDS",
    "S005_EXCEL_ALIASES_BY_CATEGORY",
    "SEEDS_BY_CATEGORY",
    "SELF_RELATIONSHIP_FIXED_ID",
    "SELF_RELATIONSHIP_SYSTEM_CODE",
    "SystemDictionarySeed",
    "final_names",
]

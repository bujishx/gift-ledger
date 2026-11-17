# ruff: noqa: RUF002
"""当前用户旧账模板：批准的 v4 布局、真实本人说明和启用字典快照。

COLUMN_HEADERS 保留旧版纯列名契约；MARKED_COLUMN_HEADERS 是新版可见列首标记。
静态资源是已验收的无业务数据布局，下载时只填说明和当前用户字典，填写区始终空白。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from io import BytesIO
from math import ceil
from pathlib import Path

from openpyxl import load_workbook
from openpyxl.comments import Comment
from openpyxl.workbook.defined_name import DefinedName

from app.services.excel_workbook import AMOUNT_FORMAT, ExcelColumn
from app.services.system_dictionary_catalog import final_names

TEMPLATE_VERSION = "old-ledger-v4"
OLD_LEDGER_TEMPLATE_FILENAME = "gift-ledger-old-ledger-template-v4.xlsx"
EXPLANATION_SHEET_TITLE = "填写说明"
DATA_SHEET_TITLE = "旧账导入模板"
DICTIONARY_SHEET_TITLE = "下拉字典"
INPUT_ROWS = 5000
BASE_LAYOUT = Path(__file__).resolve().parent.parent / "resources" / "old-ledger-v4-layout.xlsx"

_LABELS = (
    "事主姓名",
    "事主常住地",
    "事主联系人备注",
    "事主关系类型",
    "亲属称谓",
    "事件类型",
    "办事日期",
    "事件备注",
    "随礼人姓名",
    "随礼人常住地",
    "随礼人联系人备注",
    "随礼人关系类型",
    "礼金金额",
    "实物明细",
    "流水备注",
)
_REQUIRED = (
    "必填",
    "选填",
    "选填",
    "必填",
    "必填",
    "必填",
    "必填，可填不详",
    "选填",
    "必填",
    "选填",
    "选填",
    "必填",
    "必填，元",
    "金额为0时必填",
    "选填",
)
_WIDTHS = (16, 18, 24, 20, 20, 18, 24, 28, 16, 18, 24, 22, 20, 28, 28)
COLUMN_HEADERS = _LABELS
MARKED_COLUMN_HEADERS = tuple(
    f"{name}\n（{required}）" for name, required in zip(_LABELS, _REQUIRED, strict=True)
)
TEMPLATE_COLUMN_HEADERS = MARKED_COLUMN_HEADERS
DISPLAY_HEADERS = MARKED_COLUMN_HEADERS
COLUMNS = tuple(
    ExcelColumn(label, width, AMOUNT_FORMAT if index == 12 else "@")
    for index, (label, width) in enumerate(zip(_LABELS, _WIDTHS, strict=True))
)
EXPLANATION_LINES = (
    f"旧账导入模板（版本：{TEMPLATE_VERSION}）",
    "不要填写收支方向，系统自动推导方向。双方必须有一方是本人，两方不能是同一个人。",
    "礼金金额为零时，“实物明细”必须填写。同一位随礼人在同一个事件里只能有一笔流水。",
    "不填写实际随礼日期，也不填写亲属姓名。只要有任何一行不符合规则，整批都不导入。",
    *(
        f"· {label}：{'、'.join(final_names(category))}"
        for category, label in (
            ("relationship", "关系类型"),
            ("kinship", "亲属称谓"),
            ("event", "事件类型"),
        )
    ),
)


def _literal(cell, value: str) -> None:
    # A dictionary/name beginning with '=' is a literal, never an Excel formula.
    cell.value = value
    cell.data_type = "s"


def build_old_ledger_template(
    *,
    self_name: str | None = None,
    dictionary_names: Mapping[str, Sequence[str]] | None = None,
) -> bytes:
    """HTTP download supplies the authenticated user's name and visible active dictionaries."""
    names = dictionary_names or {
        category: final_names(category) for category in ("relationship", "kinship", "event")
    }
    workbook = load_workbook(BASE_LAYOUT)
    workbook.properties.title = f"旧账导入模板 {TEMPLATE_VERSION}"
    notes = workbook[EXPLANATION_SHEET_TITLE]
    person = self_name or "本人信息中的真实姓名"
    sample = "示例联系人" if person == "王强" else "王强"
    text = {
        "A1": "往来有礼 · 旧账导入模板",
        "A2": f"版本 {TEMPLATE_VERSION} · 一行填写一笔记账",
        "A3": "填写页是空白的，示例只在说明页。上传预检不写入账本，核对并确认后才整批导入。",
        "A4": "当前本人",
        "B4": f"{person}；关系类型为“我”。本人姓名填写此真实姓名，不把“我”写成姓名。",
        "A5": "字典快照",
        "B5": "下拉包含系统项目及当前账本启用的个人项目。下载后新增名称可手填，上传时再次核对。",
        "A62": "网页导入流程",
        "A70": "填写体验与文件保存",
        "B71": (
            "四个字典列可下拉，也可手填列表外名称，确认警告后继续；"
            "上传时明确核对映射或个人新增项。"
        ),
        "A75": "保存原账",
        "B75": (
            "Excel校验不能代替上传预检。保留原旧账文件，核对最终计划，"
            "处理全部错误及待确认项后再整批确认。"
        ),
        "A77": (
            "下拉名称为下载时系统及本账本启用项目快照；示例联系人仅用于说明。"
            "不填写实际随礼日期或亲属姓名。"
        ),
        "A49": (
            f"以上示例中，{sample}的联系人备注均填“高中同学”、关系为“同学”；"
            "本人常住地及联系人备注留空。"
        ),
        "B41": person,
        "G41": f"{sample}（同学）",
        "B43": sample,
        "G43": f"{person}（我）",
        "B45": person,
        "G45": f"{sample}（同学）",
        "B47": sample,
        "G47": f"{person}（我）",
    }
    for address, value in text.items():
        _literal(notes[address], value)
    for row in notes:
        for cell in row:
            if isinstance(cell.value, str) and "，演示本人为李明" in cell.value:
                _literal(cell, cell.value.replace("，演示本人为李明", ""))
    for number in (41, 43, 45, 47):
        notes.row_dimensions[number].height = max(34, ceil(len(person) / 6) * 15)
    # Remove demo profile and demo personal dictionary text from all input prompts/comments.
    data = workbook[DATA_SHEET_TITLE]
    for number, title in enumerate(MARKED_COLUMN_HEADERS, 1):
        _literal(data.cell(1, number), title)
        if data.cell(1, number).comment:
            hint = data.cell(1, number).comment.text.replace("，演示本人为李明", "")
            data.cell(1, number).comment = Comment(hint, "往来有礼")
    for rule in data.data_validations.dataValidation:
        rule.prompt = (rule.prompt or "").replace("，演示本人为李明", "")
        rule.error = (rule.error or "").replace("，演示本人为李明", "")
    dictionary = workbook[DICTIONARY_SHEET_TITLE]
    maximum = max(dictionary.max_row, *(len(tuple(values)) + 1 for values in names.values()))
    for row in dictionary.iter_rows(min_row=2, max_row=maximum, max_col=3):
        for cell in row:
            cell.value = None
    for category, range_name, column in (
        ("relationship", "RelationshipOptions", "A"),
        ("kinship", "KinshipOptions", "B"),
        ("event", "EventOptions", "C"),
    ):
        values = tuple(names[category])
        if not values:
            raise ValueError("模板字典不能为空")
        for number, value in enumerate(values, 2):
            _literal(dictionary[f"{column}{number}"], value)
        reference = "'下拉字典'!$" + column + "$2:$" + column + "$" + str(len(values) + 1)
        workbook.defined_names.add(DefinedName(range_name, attr_text=reference))
    dictionary.sheet_state = "hidden"
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()

# ruff: noqa: RUF002
"""可复用的内存 xlsx 工作簿生成器。

该模块只负责把结构化数据写成 Excel 字节，不包含任何业务判断，供“查询结果导出”、
“全部数据导出”和“旧账导入模板”共用。

统一约定：

- 表头使用中文，冻结首行并开启筛选；
- 金额固定两位小数，日期与时间使用明确格式，避免办公软件改格式；
- 所有文本单元格在写入前统一做公式注入防护：左侧去空白后以 ``= + - @`` 开头的
  文本会在最前面加一个半角单引号，使其保持普通文本并仍然可读。金额、日期和时间
  按数值写入，不作为文本处理。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from io import BytesIO
from typing import Any

from openpyxl import Workbook
from openpyxl.cell.cell import Cell
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

AMOUNT_FORMAT = "0.00"
DATE_FORMAT = "yyyy-mm-dd"
DATETIME_FORMAT = "yyyy-mm-dd hh:mm:ss"

# Excel 会把左侧去空白后以这些字符开头的文本当作公式、运算符或特殊前缀。
FORMULA_TRIGGER_CHARS = ("=", "+", "-", "@")
FORMULA_GUARD_PREFIX = "'"
FORMULA_CELL_TYPE = "f"

_HEADER_FONT = Font(bold=True)
_HEADER_FILL = PatternFill(fill_type="solid", fgColor="D9D9D9")
_HEADER_ALIGNMENT = Alignment(horizontal="center", vertical="center")
_NOTE_ALIGNMENT = Alignment(vertical="top", wrap_text=True)


@dataclass(frozen=True, slots=True)
class ExcelColumn:
    """数据表的一列：中文表头、列宽，以及可选的数值格式。"""

    header: str
    width: int = 16
    number_format: str | None = None


@dataclass(frozen=True, slots=True)
class ExcelDataSheet:
    """数据表：首行表头，其后为数据行。"""

    title: str
    columns: Sequence[ExcelColumn]
    rows: Sequence[Sequence[Any]] = ()
    freeze_header: bool = True
    auto_filter: bool = True


@dataclass(frozen=True, slots=True)
class ExcelNoteSheet:
    """说明页：每一行文本写入 A 列，自动换行。"""

    title: str
    lines: Sequence[str]
    width: int = 96


def protect_formula_text(value: str) -> str:
    """防止文本注入公式，同时保持可读。

    仅当文本左侧去空白后以 ``= + - @`` 开头时才在前面补一个单引号；其余文本原样返回。
    """

    if value.lstrip().startswith(FORMULA_TRIGGER_CHARS):
        return FORMULA_GUARD_PREFIX + value
    return value


def _write_cell(cell: Cell, value: Any, number_format: str | None) -> None:
    if isinstance(value, str):
        cell.value = protect_formula_text(value)
        return
    cell.value = value
    if number_format is not None and value is not None:
        cell.number_format = number_format


def _add_data_sheet(workbook: Workbook, sheet: ExcelDataSheet) -> None:
    worksheet = workbook.create_sheet(sheet.title)
    column_count = len(sheet.columns)
    for column_index, column in enumerate(sheet.columns, start=1):
        if column.width > 0:
            worksheet.column_dimensions[get_column_letter(column_index)].width = column.width
        header_cell = worksheet.cell(row=1, column=column_index)
        header_cell.value = protect_formula_text(column.header)
        header_cell.font = _HEADER_FONT
        header_cell.fill = _HEADER_FILL
        header_cell.alignment = _HEADER_ALIGNMENT
    for row_offset, row in enumerate(sheet.rows):
        row_index = row_offset + 2
        if len(row) != column_count:
            raise ValueError(
                f"工作表“{sheet.title}”第 {row_index} 行有 {len(row)} 列，"
                f"与表头的 {column_count} 列不一致"
            )
        for column_index, value in enumerate(row):
            cell = worksheet.cell(row=row_index, column=column_index + 1)
            _write_cell(cell, value, sheet.columns[column_index].number_format)
    if sheet.freeze_header:
        worksheet.freeze_panes = "A2"
    if sheet.auto_filter and column_count:
        last_row = max(1, len(sheet.rows) + 1)
        worksheet.auto_filter.ref = f"A1:{get_column_letter(column_count)}{last_row}"


def _add_note_sheet(workbook: Workbook, sheet: ExcelNoteSheet) -> None:
    worksheet: Worksheet = workbook.create_sheet(sheet.title)
    worksheet.column_dimensions["A"].width = sheet.width
    for row_index, line in enumerate(sheet.lines, start=1):
        cell = worksheet.cell(row=row_index, column=1)
        cell.value = protect_formula_text(line)
        cell.alignment = _NOTE_ALIGNMENT


def build_workbook(
    sheets: Sequence[ExcelDataSheet | ExcelNoteSheet],
    *,
    document_title: str | None = None,
) -> bytes:
    """按给定顺序生成内存工作簿并返回 xlsx 字节。"""

    if not sheets:
        raise ValueError("工作簿至少需要一个工作表")
    workbook = Workbook()
    workbook.remove(workbook.active)
    for sheet in sheets:
        if isinstance(sheet, ExcelNoteSheet):
            _add_note_sheet(workbook, sheet)
        else:
            _add_data_sheet(workbook, sheet)
    if document_title:
        workbook.properties.title = document_title
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()

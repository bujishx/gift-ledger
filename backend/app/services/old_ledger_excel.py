# ruff: noqa: RUF002
"""旧账 Excel 的安全解析器。

本模块只做一件事：把上传的 xlsx 字节变成结构化的旧账流水行，并给出逐行的中文错误。
它**不做任何业务判断**——不决定联系人是新建还是复用、不创建字典、不推导收支方向、
不写数据库。这些后续步骤由主服务在拿到 :class:`ParsedOldLedgerWorkbook` 之后处理。

安全边界（上传文件一律按不可信处理）：

1. 先校验压缩大小、ZIP 结构和解压后体积，再交给 openpyxl；
2. 拒绝宏/VBA、外部链接（含 ``.rels`` 里的 ``TargetMode="External"``）、ActiveX 控件、
   OLE 对象、数字签名和嵌入文件等主动内容；
3. 接受原版「填写说明」「旧账导入模板」两页，或新版末尾附隐藏「下拉字典」三页；
4. 表头接受固定的 15 个纯列名或新版带必填标记的显示表头，不猜测、重排或截断列；
5. 拒绝任何公式单元格（全部工作表、隐藏字典页、模板列之外的列同样扫描），且必须用
   ``data_only=False`` 检测，不能靠缓存值绕过；
6. 工作表声明的使用范围（dimension）有明确上限，伪造超大维度会被直接拒绝，
   公式扫描范围也因此始终有界；
7. 模板 15 列之外不允许出现任何表头或数据，不静默截断额外列。

两种错误的边界很清楚：

- **文件级问题**（空文件、不是 xlsx、结构不对、表头不对、超出行数……）抛出
  :class:`OldLedgerWorkbookError`，整个文件不可用；
- **行级问题**（日期写错、金额是布尔值……）不抛异常，转成
  :class:`OldLedgerRowError` 放进结果里，同一行多个问题会给出多条错误。
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from io import BytesIO
from typing import Any, Final
from zipfile import BadZipFile, ZipFile, ZipInfo

from defusedxml.ElementTree import fromstring as _safe_xml_fromstring
from openpyxl import load_workbook
from openpyxl.workbook.workbook import Workbook
from openpyxl.worksheet.worksheet import Worksheet

from app.services.event_dates import EventDateValue, parse_event_date
from app.services.old_ledger_template import (
    COLUMN_HEADERS,
    DATA_SHEET_TITLE,
    EXPLANATION_SHEET_TITLE,
)

# ---------------------------------------------------------------- 常量与契约

SHEET_TITLES: Final[tuple[str, ...]] = (EXPLANATION_SHEET_TITLE, DATA_SHEET_TITLE)
COLUMN_COUNT: Final[int] = len(COLUMN_HEADERS)

DEFAULT_MAX_FILE_BYTES: Final[int] = 10 * 1024 * 1024
DEFAULT_MAX_ROWS: Final[int] = 5000
DEFAULT_MAX_UNCOMPRESSED_BYTES: Final[int] = 50 * 1024 * 1024
MAX_ZIP_ENTRIES: Final[int] = 1000
MAX_COMPRESSION_RATIO: Final[int] = 200
# 工作表「使用范围」的扫描上限。声明超过这些值的工作表按可疑维度直接拒绝,
# 公式扫描与数据行迭代也因此始终有界, 不会因伪造 dimension 造成无界扫描。
MAX_SHEET_COLUMNS: Final[int] = 64  # 模板只要 15 列, 64 列已是极宽松的余量
MAX_BLANK_ROW_BUFFER: Final[int] = 1024  # 数据表允许的空白行缓冲(含表头行)
MAX_EXPLANATION_SHEET_ROWS: Final[int] = 200  # 说明页只有几十行说明文字

ERROR_EMPTY_FILE: Final[str] = "empty_file"
ERROR_FILE_TOO_LARGE: Final[str] = "file_too_large"
ERROR_NOT_ZIP: Final[str] = "not_zip"
ERROR_ZIP_TOO_MANY_ENTRIES: Final[str] = "zip_too_many_entries"
ERROR_ZIP_ENTRY_TOO_LARGE: Final[str] = "zip_entry_too_large"
ERROR_ZIP_TOTAL_TOO_LARGE: Final[str] = "zip_total_too_large"
ERROR_ZIP_UNSAFE_PATH: Final[str] = "zip_unsafe_path"
ERROR_ZIP_DUPLICATE_ENTRY: Final[str] = "zip_duplicate_entry"
ERROR_ZIP_COMPRESSION_RATIO: Final[str] = "zip_compression_ratio"
ERROR_UNREADABLE_WORKBOOK: Final[str] = "unreadable_workbook"
ERROR_ACTIVE_CONTENT: Final[str] = "active_content"
ERROR_UNEXPECTED_SHEETS: Final[str] = "unexpected_sheets"
ERROR_MISSING_DATA_SHEET: Final[str] = "missing_data_sheet"
ERROR_HEADER_MISMATCH: Final[str] = "header_mismatch"
ERROR_EXTRA_COLUMNS: Final[str] = "extra_columns"
ERROR_SUSPICIOUS_DIMENSION: Final[str] = "suspicious_dimension"
ERROR_TOO_MANY_ROWS: Final[str] = "too_many_rows"

ERROR_DATE_REQUIRED: Final[str] = "date_required"
ERROR_DATE_INVALID: Final[str] = "date_invalid"
ERROR_AMOUNT_REQUIRED: Final[str] = "amount_required"
ERROR_AMOUNT_INVALID: Final[str] = "amount_invalid"
ERROR_TEXT_INVALID: Final[str] = "text_invalid"

# 严格日期: 只认 yyyy-mm-dd, 不认 2024/5/20、20240520、带时间的字符串等模糊写法。
_STRICT_DATE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})\Z"
)
# 金额字符串: 可选负号 + 纯数字; 不允许千分位、科学计数法、正号或小数尾缀。
_DECIMAL_STRING_PATTERN: Final[re.Pattern[str]] = re.compile(r"-?(?:0|[1-9]\d*)(?:\.\d+)?\Z")
# 主动内容部件前缀: 宏/VBA、外部链接缓存、OLE 嵌入、ActiveX 控件及其属性、
# 功能区扩展(含 customUI14)、数字签名部件。小写比较。
_ACTIVE_CONTENT_PREFIXES: Final[tuple[str, ...]] = (
    "xl/vba",
    "xl/macrosheets",
    "xl/external",
    "xl/embeddings",
    "xl/activex",
    "xl/ctrlprops",
    "xl/digitalsignatures",
    "customui",
    "_xmlsignatures",
)
# 二进制部件一律视为主动内容: 本模板是纯文本工作簿, 任何路径下的 .bin/.vba
# (打印机设置、OLE 残留、控件二进制等)都不应出现。
_BINARY_PART_SUFFIXES: Final[tuple[str, ...]] = (".bin", ".vba")
# 压缩炸弹特征: office 工作簿里的文本/标记类部件压缩比通常远低于该上限。
_TEXT_PART_SUFFIXES: Final[tuple[str, ...]] = (".xml", ".rels", ".vml")

_ACTIVE_CONTENT_MESSAGE: Final[str] = "文件包含宏、外部链接或嵌入对象等主动内容，已拒绝解析。"
_UNREADABLE_MESSAGE: Final[str] = "工作簿无法打开（文件损坏或已加密），请重新导出后再上传。"


# ---------------------------------------------------------------- 对外数据结构


@dataclass(frozen=True, slots=True)
class OldLedgerRowError:
    """一行里的一个具体问题，供预检直接展示给用户。"""

    row_number: int
    field: str
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class OldLedgerRow:
    """旧账模板里的一行流水，列名与模板 15 列一一对应。

    文本列已去除首尾空白，空白单元格统一为 ``None``；日期为 ``date``，
    礼金金额为 ``Decimal``（保留填写精度）。``row_number`` 是 Excel 里的真实行号
    （首行表头是 1），预检提示直接用这个数字。
    """

    row_number: int
    event_contact_name: str | None
    event_contact_residence: str | None
    event_contact_note: str | None
    event_contact_relation_type: str | None
    kinship_name: str | None
    event_type_name: str | None
    event_date: date | None
    event_note: str | None
    gift_contact_name: str | None
    gift_contact_residence: str | None
    gift_contact_note: str | None
    gift_contact_relation_type: str | None
    amount: Decimal | None
    gift_detail: str | None
    gift_note: str | None
    errors: tuple[OldLedgerRowError, ...] = ()
    date_precision: str = "day"
    event_year: int | None = None
    event_month: int | None = None
    raw_values: tuple[Any, ...] = ()

    @property
    def is_valid(self) -> bool:
        """该行是否没有任何问题。"""

        return not self.errors


@dataclass(frozen=True, slots=True)
class ParsedOldLedgerWorkbook:
    """整个文件的解析结果：数据行 + 逐行错误，外加解析时的口径记录。"""

    rows: tuple[OldLedgerRow, ...]
    errors: tuple[OldLedgerRowError, ...]
    data_sheet_title: str
    column_headers: tuple[str, ...]
    max_rows: int
    max_file_bytes: int
    max_uncompressed_bytes: int

    @property
    def has_errors(self) -> bool:
        """是否存在任何一行不符合要求（存在则整批不导入）。"""

        return bool(self.errors)

    @property
    def valid_rows(self) -> tuple[OldLedgerRow, ...]:
        """没有任何问题的行。"""

        return tuple(row for row in self.rows if row.is_valid)

    @property
    def invalid_rows(self) -> tuple[OldLedgerRow, ...]:
        """存在问题的行。"""

        return tuple(row for row in self.rows if not row.is_valid)


class OldLedgerWorkbookError(Exception):
    """文件级错误：整个文件不可用。

    只有这一类问题才抛异常；单行内容问题走
    :class:`OldLedgerRowError`，不影响其它行的解析。
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def __str__(self) -> str:
        return self.message


# ---------------------------------------------------------------- 压缩包预检


def _read_zip_entries(content: bytes, limit: int) -> tuple[ZipInfo, ...]:
    with ZipFile(BytesIO(content)) as archive:
        entries = archive.infolist()
        total = sum(entry.file_size for entry in entries)
        for entry in entries:
            if entry.file_size > limit:
                raise OldLedgerWorkbookError(
                    ERROR_ZIP_ENTRY_TOO_LARGE,
                    f"压缩包内单个文件解压后约 {entry.file_size} 字节，超过限制 {limit} 字节。",
                )
            _check_compression_ratio(entry)
        if total > limit:
            raise OldLedgerWorkbookError(
                ERROR_ZIP_TOTAL_TOO_LARGE,
                f"压缩包解压后合计约 {total} 字节，超过限制 {limit} 字节。",
            )
    return tuple(entries)


def _check_compression_ratio(entry: ZipInfo) -> None:
    if not entry.filename.endswith(_TEXT_PART_SUFFIXES):
        return
    if entry.compress_size <= 0 or entry.file_size <= entry.compress_size * MAX_COMPRESSION_RATIO:
        return
    raise OldLedgerWorkbookError(
        ERROR_ZIP_COMPRESSION_RATIO,
        f"压缩包内「{entry.filename}」的压缩比异常，疑似压缩炸弹，已拒绝解析。",
    )


def _check_entry_names(entries: tuple[ZipInfo, ...]) -> None:
    seen: set[str] = set()
    for entry in entries:
        normalized = entry.filename.replace("\\", "/")
        if normalized.startswith("/") or _is_windows_absolute(normalized):
            raise OldLedgerWorkbookError(
                ERROR_ZIP_UNSAFE_PATH,
                f"压缩包内含绝对路径条目「{entry.filename}」，已拒绝解析。",
            )
        if ".." in normalized.split("/"):
            raise OldLedgerWorkbookError(
                ERROR_ZIP_UNSAFE_PATH,
                f"压缩包内含上级目录穿越条目「{entry.filename}」，已拒绝解析。",
            )
        if normalized in seen:
            raise OldLedgerWorkbookError(
                ERROR_ZIP_DUPLICATE_ENTRY,
                f"压缩包内含重复条目「{entry.filename}」，已拒绝解析。",
            )
        seen.add(normalized)


def _is_windows_absolute(name: str) -> bool:
    return len(name) >= 2 and name[0].isalpha() and name[1] == ":"


def _check_active_content(entries: tuple[ZipInfo, ...]) -> None:
    for entry in entries:
        if _has_active_content(entry.filename):
            raise OldLedgerWorkbookError(ERROR_ACTIVE_CONTENT, _ACTIVE_CONTENT_MESSAGE)


def _has_active_content(name: str) -> bool:
    lowered = name.replace("\\", "/").lower()
    if lowered.startswith(_ACTIVE_CONTENT_PREFIXES):
        return True
    basename = lowered.rsplit("/", 1)[-1]
    if basename.startswith("vbaproject"):
        return True
    # 纯文本模板里不应出现任何二进制部件: 打印机设置、OLE 残留、控件二进制等一律拒绝。
    return lowered.endswith(_BINARY_PART_SUFFIXES)


def _check_external_relationship_targets(content: bytes, entries: tuple[ZipInfo, ...]) -> None:
    """解析包里所有 ``.rels``, 发现外部目标(``TargetMode="External"``)即拒绝。

    xlsx 可以不建 ``xl/externalLinks/``, 只在关系文件里声明外部超链接或外部数据源,
    所以必须看 ``.rels`` 内容本身, 不能只靠部件名。条目大小在 ZIP 预检时已受限,
    这里不会读出超过既定解压上限的数据。
    """

    rels_names = tuple(
        entry.filename
        for entry in entries
        if entry.filename.replace("\\", "/").lower().endswith(".rels")
    )
    if not rels_names:
        return
    with ZipFile(BytesIO(content)) as archive:
        for name in rels_names:
            # 读取条目内容可能遇到 CRC 损坏等底层异常, 统一转成稳定错误, 不泄漏成 500。
            with _stable_file_guard():
                _reject_external_targets(archive.read(name), name)


def _reject_external_targets(data: bytes, name: str) -> None:
    try:
        root = _safe_xml_fromstring(data)
    except (KeyboardInterrupt, SystemExit, MemoryError):
        raise
    except Exception as error:
        # 损坏或带 DTD/实体的 .rels 都按「无法安全打开」处理, 不把解析器异常漏成 500。
        raise OldLedgerWorkbookError(
            ERROR_UNREADABLE_WORKBOOK,
            "工作簿的关系文件无法安全解析（文件损坏或含可疑结构），请重新导出后再上传。",
        ) from error
    # TargetMode 只应出现在 Relationship 元素上; 这里宽松地扫描所有元素,
    # 任何声明为 External 的目标一律拒绝。
    for element in root.iter():
        target_mode = (element.get("TargetMode") or "").strip().lower()
        if target_mode == "external":
            raise OldLedgerWorkbookError(
                ERROR_ACTIVE_CONTENT,
                f"「{name}」声明了指向外部地址的链接关系，已拒绝解析。",
            )


# ---------------------------------------------------------------- 单元格解析


def _normalize_text(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise _TextValueError(value)
    stripped = value.strip()
    return stripped or None


class _TextValueError(Exception):
    """内部信号：某个单元格应该是普通文本但不是。"""


def _parse_text(value: Any, *, field: str, row_number: int, errors: list[OldLedgerRowError]):
    try:
        return _normalize_text(value)
    except _TextValueError:
        errors.append(
            OldLedgerRowError(
                row_number,
                field,
                ERROR_TEXT_INVALID,
                f"「{field}」应填写普通文本，当前单元格不是文本，请改成文本后重新上传。",
            )
        )
        return None


def _parse_date(value: Any, *, field: str, row_number: int, errors: list[OldLedgerRowError]):
    if value is None or (isinstance(value, str) and not value.strip()):
        errors.append(
            OldLedgerRowError(
                row_number,
                field,
                ERROR_DATE_REQUIRED,
                f"「{field}」是必填项，可填写完整日期、年月、年份或“不详”。",
            )
        )
        return None

    try:
        return parse_event_date(value).event_date
    except ValueError:
        pass

    errors.append(
        OldLedgerRowError(
            row_number,
            field,
            ERROR_DATE_INVALID,
            f"「{field}」无法识别，请填写 yyyy-mm-dd、yyyy-mm、yyyy 或“不详”，不要补造月日。",
        )
    )
    return None


def _parse_amount(value: Any, *, field: str, row_number: int, errors: list[OldLedgerRowError]):
    if value is None:
        errors.append(
            OldLedgerRowError(
                row_number,
                field,
                ERROR_AMOUNT_REQUIRED,
                f"「{field}」是必填项，纯实物礼金请填 0。",
            )
        )
        return None

    if isinstance(value, bool):
        return _invalid_amount(field, row_number, errors)

    if isinstance(value, Decimal):
        if not value.is_finite():
            return _invalid_amount(field, row_number, errors)
        return value

    if isinstance(value, int):
        return Decimal(value)

    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return _invalid_amount(field, row_number, errors)
        return Decimal(str(value))

    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            errors.append(
                OldLedgerRowError(
                    row_number,
                    field,
                    ERROR_AMOUNT_REQUIRED,
                    f"「{field}」是必填项，纯实物礼金请填 0。",
                )
            )
            return None
        if _DECIMAL_STRING_PATTERN.fullmatch(stripped) is None:
            return _invalid_amount(field, row_number, errors)
        try:
            return Decimal(stripped)
        except InvalidOperation:
            return _invalid_amount(field, row_number, errors)

    return _invalid_amount(field, row_number, errors)


def _invalid_amount(field: str, row_number: int, errors: list[OldLedgerRowError]):
    errors.append(
        OldLedgerRowError(
            row_number,
            field,
            ERROR_AMOUNT_INVALID,
            f"「{field}」无法识别，请填写普通数字（不支持千分位、科学计数法或布尔值）。",
        )
    )
    return None


# ---------------------------------------------------------------- 工作簿读取


@contextmanager
def _stable_file_guard() -> Iterator[None]:
    """把不可信输入触发的任意解析异常稳定转换成文件级错误。

    read-only 工作簿是惰性解析的: 损坏 XML 的异常可能在任何一步抛出
    (ElementTree 的 ``ParseError`` 继承自 ``SyntaxError``, 不在常见捕获清单里)。
    这里统一兜底, 保证对外只出现 :class:`OldLedgerWorkbookError`。
    Ctrl+C(``KeyboardInterrupt``)、``SystemExit`` 和真正的内存不足(``MemoryError``)
    必须原样向上传播, 不能被伪装成“文件损坏”。
    """

    try:
        yield
    except OldLedgerWorkbookError:
        raise
    except (KeyboardInterrupt, SystemExit, MemoryError):
        raise
    except Exception as error:
        raise OldLedgerWorkbookError(ERROR_UNREADABLE_WORKBOOK, _UNREADABLE_MESSAGE) from error


def _check_sheet_order(workbook: Workbook) -> None:
    titles = tuple(workbook.sheetnames)
    if titles in (SHEET_TITLES, (*SHEET_TITLES, "下拉字典")):
        return
    if titles and DATA_SHEET_TITLE not in titles:
        raise OldLedgerWorkbookError(
            ERROR_MISSING_DATA_SHEET,
            f"文件里找不到「{DATA_SHEET_TITLE}」工作表，请从最新模板重新填写。",
        )
    raise OldLedgerWorkbookError(
        ERROR_UNEXPECTED_SHEETS,
        f"文件的工作表应为「{SHEET_TITLES[0]}」「{SHEET_TITLES[1]}」，可有末尾「下拉字典」，且顺序一致，"
        f"当前为：{'、'.join(titles) if titles else '（无）'}。",
    )


def _check_worksheet_dimension(sheet_title: str, worksheet: Worksheet, *, row_limit: int) -> None:
    """拒绝可疑的 dimension 声明, 保证后续扫描范围有界。"""

    declared_rows = worksheet.max_row
    declared_columns = worksheet.max_column
    if declared_rows is not None and declared_rows > row_limit:
        raise OldLedgerWorkbookError(
            ERROR_SUSPICIOUS_DIMENSION,
            f"「{sheet_title}」工作表声明的行数约 {declared_rows} 行，"
            f"超出允许上限 {row_limit} 行，文件疑似异常，已拒绝解析。",
        )
    if declared_columns is not None and declared_columns > MAX_SHEET_COLUMNS:
        raise OldLedgerWorkbookError(
            ERROR_SUSPICIOUS_DIMENSION,
            f"「{sheet_title}」工作表声明的列数约 {declared_columns} 列，"
            f"超出允许上限 {MAX_SHEET_COLUMNS} 列，文件疑似异常，已拒绝解析。",
        )


def _reject_formulas_in_row(row: tuple[Any, ...], *, row_number: int, sheet_title: str) -> None:
    for cell in row:
        if cell.data_type == "f":
            raise OldLedgerWorkbookError(
                ERROR_ACTIVE_CONTENT,
                f"「{sheet_title}」第 {row_number} 行的「{_header_for(cell.column)}」含有公式，"
                "模板不接受公式单元格，请粘贴为数值后重新上传。",
            )


def _scan_formulas(sheet_title: str, worksheet: Worksheet, *, row_limit: int) -> None:
    """全工作表扫描公式: 覆盖说明页/数据表的使用范围内所有行列, 不止模板 15 列。"""

    for row_number, row in enumerate(
        worksheet.iter_rows(max_row=row_limit, max_col=MAX_SHEET_COLUMNS), start=1
    ):
        _reject_formulas_in_row(row, row_number=row_number, sheet_title=sheet_title)


def _read_header_row(worksheet: Worksheet) -> tuple[Any, ...]:
    """读取数据表第 1 行(覆盖到扫描列上限), 同时拒绝表头行里的公式。"""

    row = next(
        worksheet.iter_rows(min_row=1, max_row=1, max_col=MAX_SHEET_COLUMNS),
        None,
    )
    if row is None:
        return (None,) * MAX_SHEET_COLUMNS
    _reject_formulas_in_row(row, row_number=1, sheet_title=DATA_SHEET_TITLE)
    return tuple(cell.value for cell in row)


def _check_headers(header_values: tuple[Any, ...]) -> None:
    """表头必须与模板数量、顺序和文字完全一致。

    字符串按原样比较, 不做首尾空白容错; 非字符串表头(数字、日期等)直接判为不匹配。
    """

    for index, expected in enumerate(COLUMN_HEADERS, start=1):
        actual = header_values[index - 1]
        from app.services.old_ledger_template import DISPLAY_HEADERS

        if isinstance(actual, str) and actual in (expected, DISPLAY_HEADERS[index - 1]):
            continue
        if actual is None:
            description = "（空）"
        elif isinstance(actual, str):
            description = f"「{actual}」"
        else:
            description = "非文本内容"
        raise OldLedgerWorkbookError(
            ERROR_HEADER_MISMATCH,
            f"第 {index} 列表头应为「{expected}」，实际为 {description}，"
            "请勿调整模板列顺序或文字。",
        )


def _reject_extra_header_columns(header_values: tuple[Any, ...]) -> None:
    """第 16 列及以后不允许出现额外表头, 也不允许静默忽略。"""

    for offset, value in enumerate(header_values[COLUMN_COUNT:], start=COLUMN_COUNT + 1):
        if not _is_blank_cell(value):
            raise OldLedgerWorkbookError(
                ERROR_EXTRA_COLUMNS,
                f"数据表第 1 行在模板 {COLUMN_COUNT} 列之外（第 {offset} 列）"
                "还有额外表头，请使用最新模板，不要增删列。",
            )


def _reject_extra_column_values(values: tuple[Any, ...], *, row_number: int) -> None:
    """数据行在模板 15 列之外不允许出现非空值, 不静默截断。"""

    for offset, value in enumerate(values[COLUMN_COUNT:], start=COLUMN_COUNT + 1):
        if not _is_blank_cell(value):
            raise OldLedgerWorkbookError(
                ERROR_EXTRA_COLUMNS,
                f"第 {row_number} 行在模板 {COLUMN_COUNT} 列之外（第 {offset} 列）"
                "还有内容，请删除模板列之外的填写。",
            )


def _is_blank_cell(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _is_blank_row(values: tuple[Any, ...]) -> bool:
    return all(_is_blank_cell(value) for value in values)


def _header_for(column: int) -> str:
    if 1 <= column <= len(COLUMN_HEADERS):
        return COLUMN_HEADERS[column - 1]
    return f"第{column}列"


def _read_data_sheet(
    worksheet: Worksheet, *, max_rows: int, scan_row_limit: int
) -> list[OldLedgerRow]:
    """一次遍历数据表: 表头精确校验(含额外列)、全范围公式扫描、数据行收集。"""

    header_values = _read_header_row(worksheet)
    _check_headers(header_values)
    _reject_extra_header_columns(header_values)

    rows: list[OldLedgerRow] = []
    for row_number, row in enumerate(
        worksheet.iter_rows(min_row=2, max_row=scan_row_limit, max_col=MAX_SHEET_COLUMNS),
        start=2,
    ):
        _reject_formulas_in_row(row, row_number=row_number, sheet_title=DATA_SHEET_TITLE)
        values = tuple(cell.value for cell in row)
        if _is_blank_row(values):
            # 完全空白的行直接忽略: 中间空行、尾部空行都不算数据。
            continue
        _reject_extra_column_values(values, row_number=row_number)
        if len(rows) >= max_rows:
            raise OldLedgerWorkbookError(
                ERROR_TOO_MANY_ROWS,
                f"可导入的数据行超过上限 {max_rows} 行，请拆分文件后分批导入。",
            )
        rows.append(_parse_row(row_number, values[:COLUMN_COUNT]))
    return rows


def _parse_row(row_number: int, values: tuple[Any, ...]) -> OldLedgerRow:
    """把一行原始单元格解析成结构化行；任何问题都收集成逐行错误，不抛异常。"""

    errors: list[OldLedgerRowError] = []

    def text_at(index: int) -> str | None:
        return _parse_text(
            values[index],
            field=COLUMN_HEADERS[index],
            row_number=row_number,
            errors=errors,
        )

    # 15 列顺序固定 (事主 4 列 / 亲属称谓 / 事件类型 / 办事日期 / 事件备注 /
    # 随礼人 4 列 / 礼金金额 / 实物明细 / 流水备注)。
    event_date = _parse_date(
        values[6],
        field=COLUMN_HEADERS[6],
        row_number=row_number,
        errors=errors,
    )
    amount = _parse_amount(
        values[12],
        field=COLUMN_HEADERS[12],
        row_number=row_number,
        errors=errors,
    )
    try:
        parsed_date = parse_event_date(values[6])
    except (ValueError, TypeError):
        parsed_date = EventDateValue(None, "unknown")

    return OldLedgerRow(
        row_number=row_number,
        event_contact_name=text_at(0),
        event_contact_residence=text_at(1),
        event_contact_note=text_at(2),
        event_contact_relation_type=text_at(3),
        kinship_name=text_at(4),
        event_type_name=text_at(5),
        event_date=event_date,
        event_note=text_at(7),
        gift_contact_name=text_at(8),
        gift_contact_residence=text_at(9),
        gift_contact_note=text_at(10),
        gift_contact_relation_type=text_at(11),
        amount=amount,
        gift_detail=text_at(13),
        gift_note=text_at(14),
        errors=tuple(errors),
        date_precision=parsed_date.date_precision,
        event_year=parsed_date.year,
        event_month=parsed_date.month,
        raw_values=values,
    )


# ---------------------------------------------------------------- 对外入口


def parse_old_ledger_workbook(
    content: bytes,
    *,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    max_rows: int = DEFAULT_MAX_ROWS,
    max_uncompressed_bytes: int = DEFAULT_MAX_UNCOMPRESSED_BYTES,
) -> ParsedOldLedgerWorkbook:
    """解析旧账导入模板工作簿。

    文件级问题抛 :class:`OldLedgerWorkbookError`；行级问题写入结果的 ``errors``。
    任何情况下都不访问数据库，也不决定联系人和字典的业务归属。
    """

    if not content:
        raise OldLedgerWorkbookError(
            ERROR_EMPTY_FILE, "上传的文件是空的，请选择填写好的旧账模板文件。"
        )
    if len(content) > max_file_bytes:
        raise OldLedgerWorkbookError(
            ERROR_FILE_TOO_LARGE,
            f"文件大小约 {len(content)} 字节，超过上限 {max_file_bytes} 字节。",
        )

    try:
        entries = _read_zip_entries(content, max_uncompressed_bytes)
    except BadZipFile as error:
        raise OldLedgerWorkbookError(
            ERROR_NOT_ZIP, "文件不是有效的 xlsx 工作簿（压缩包已损坏或格式不对）。"
        ) from error

    if len(entries) > MAX_ZIP_ENTRIES:
        raise OldLedgerWorkbookError(
            ERROR_ZIP_TOO_MANY_ENTRIES,
            f"压缩包内有 {len(entries)} 个条目，超过上限 {MAX_ZIP_ENTRIES} 个。",
        )
    _check_entry_names(entries)
    _check_active_content(entries)
    _check_external_relationship_targets(content, entries)

    workbook = _open_workbook(content)
    try:
        with _stable_file_guard():
            _check_sheet_order(workbook)
            data_sheet = workbook[DATA_SHEET_TITLE]
            explanation_sheet = workbook[EXPLANATION_SHEET_TITLE]
            _check_worksheet_dimension(
                EXPLANATION_SHEET_TITLE, explanation_sheet, row_limit=MAX_EXPLANATION_SHEET_ROWS
            )
            # 说明页同样不允许公式: 全范围扫描, 不只看模板列。
            _scan_formulas(
                EXPLANATION_SHEET_TITLE, explanation_sheet, row_limit=MAX_EXPLANATION_SHEET_ROWS
            )
            if "下拉字典" in workbook.sheetnames:
                dictionary_sheet = workbook["下拉字典"]
                _check_worksheet_dimension("下拉字典", dictionary_sheet, row_limit=6000)
                _scan_formulas("下拉字典", dictionary_sheet, row_limit=6000)
            # The approved template preformats 5,000 blank input rows even when a
            # deployment lowers its nonblank row limit. Keep the XML scan bounded
            # while enforcing max_rows against actual populated rows below.
            scan_row_limit = max(max_rows, DEFAULT_MAX_ROWS) + MAX_BLANK_ROW_BUFFER
            _check_worksheet_dimension(DATA_SHEET_TITLE, data_sheet, row_limit=scan_row_limit)
            rows = _read_data_sheet(data_sheet, max_rows=max_rows, scan_row_limit=scan_row_limit)
    finally:
        workbook.close()

    errors = tuple(error for row in rows for error in row.errors)
    return ParsedOldLedgerWorkbook(
        rows=tuple(rows),
        errors=errors,
        data_sheet_title=DATA_SHEET_TITLE,
        column_headers=COLUMN_HEADERS,
        max_rows=max_rows,
        max_file_bytes=max_file_bytes,
        max_uncompressed_bytes=max_uncompressed_bytes,
    )


def _open_workbook(content: bytes) -> Workbook:
    with _stable_file_guard():
        return load_workbook(
            BytesIO(content),
            read_only=True,
            data_only=False,
            keep_links=False,
        )


__all__ = [
    "COLUMN_COUNT",
    "COLUMN_HEADERS",
    "DATA_SHEET_TITLE",
    "DEFAULT_MAX_FILE_BYTES",
    "DEFAULT_MAX_ROWS",
    "DEFAULT_MAX_UNCOMPRESSED_BYTES",
    "ERROR_ACTIVE_CONTENT",
    "ERROR_AMOUNT_INVALID",
    "ERROR_AMOUNT_REQUIRED",
    "ERROR_DATE_INVALID",
    "ERROR_DATE_REQUIRED",
    "ERROR_EMPTY_FILE",
    "ERROR_EXTRA_COLUMNS",
    "ERROR_FILE_TOO_LARGE",
    "ERROR_HEADER_MISMATCH",
    "ERROR_MISSING_DATA_SHEET",
    "ERROR_NOT_ZIP",
    "ERROR_SUSPICIOUS_DIMENSION",
    "ERROR_TEXT_INVALID",
    "ERROR_TOO_MANY_ROWS",
    "ERROR_UNEXPECTED_SHEETS",
    "ERROR_UNREADABLE_WORKBOOK",
    "ERROR_ZIP_COMPRESSION_RATIO",
    "ERROR_ZIP_DUPLICATE_ENTRY",
    "ERROR_ZIP_ENTRY_TOO_LARGE",
    "ERROR_ZIP_TOO_MANY_ENTRIES",
    "ERROR_ZIP_TOTAL_TOO_LARGE",
    "ERROR_ZIP_UNSAFE_PATH",
    "EXPLANATION_SHEET_TITLE",
    "MAX_BLANK_ROW_BUFFER",
    "MAX_EXPLANATION_SHEET_ROWS",
    "MAX_SHEET_COLUMNS",
    "MAX_ZIP_ENTRIES",
    "SHEET_TITLES",
    "OldLedgerRow",
    "OldLedgerRowError",
    "OldLedgerWorkbookError",
    "ParsedOldLedgerWorkbook",
    "parse_old_ledger_workbook",
]

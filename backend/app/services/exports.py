from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Contact
from app.schemas.dictionaries import DictionaryCategory
from app.schemas.exports import QueryExportFilters
from app.services.dictionaries import list_dictionary_items
from app.services.excel_workbook import (
    AMOUNT_FORMAT,
    DATE_FORMAT,
    DATETIME_FORMAT,
    ExcelColumn,
    ExcelDataSheet,
    ExcelNoteSheet,
    build_workbook,
)
from app.services.export_data import (
    ExportPackage,
    all_data_export_package,
    query_export_package,
)

_AMOUNT_HEADERS = {
    "我随出金额",
    "我收到金额",
    "礼金差额",
    "现金合计",
    "礼金金额",
}
_DATE_HEADERS = {"办事日期", "最近往来日期"}
_DATETIME_HEADERS = {"登记时间", "创建时间"}
_SHORT_HEADERS = {"状态", "排序", "流水笔数", "实物份数", "我随出笔数", "我收到笔数"}
_LONG_HEADERS = {
    "联系人备注",
    "事主备注",
    "随礼人备注",
    "事件备注",
    "流水备注",
    "实物明细",
    "相关联系人",
}


@dataclass(frozen=True, slots=True)
class ExcelDownload:
    filename: str
    content: bytes


def _render_export_package(package: ExportPackage) -> bytes:
    note_lines = (
        package.title,
        "",
        *(f"{label}：{value}" for label, value in package.metadata),
    )
    sheets: list[ExcelNoteSheet | ExcelDataSheet] = [ExcelNoteSheet("导出说明", note_lines)]
    for table in package.tables:
        sheets.append(
            ExcelDataSheet(
                table.name,
                tuple(_excel_column(header) for header in table.columns),
                table.rows,
            )
        )
    return build_workbook(sheets, document_title=package.title)


def _excel_column(header: str) -> ExcelColumn:
    number_format = None
    if header in _AMOUNT_HEADERS:
        number_format = AMOUNT_FORMAT
    elif header in _DATE_HEADERS:
        number_format = DATE_FORMAT
    elif header in _DATETIME_HEADERS:
        number_format = DATETIME_FORMAT

    if header in _SHORT_HEADERS:
        width = 12
    elif header in _LONG_HEADERS:
        width = 24
    elif header in _DATETIME_HEADERS:
        width = 20
    else:
        width = 16
    return ExcelColumn(header, width, number_format)


def query_excel(
    session: Session,
    user_id: int,
    filters: QueryExportFilters,
) -> ExcelDownload:
    package = query_export_package(session, user_id, filters)
    return ExcelDownload(package.filename, _render_export_package(package))


def all_data_excel(session: Session, user_id: int) -> ExcelDownload:
    package = all_data_export_package(session, user_id)
    return ExcelDownload(package.filename, _render_export_package(package))


def old_ledger_template_excel(
    session: Session | None = None, user_id: int | None = None
) -> ExcelDownload:
    from app.services.old_ledger_template import (
        OLD_LEDGER_TEMPLATE_FILENAME,
        build_old_ledger_template,
    )

    name = None
    dictionaries = None
    if session is not None and user_id is not None:
        person = session.scalar(
            select(Contact).where(Contact.user_id == user_id, Contact.relation_type_id == 1)
        )
        if person is None:
            from app.services.ledger_query_common import LedgerQueryValidationError

            raise LedgerQueryValidationError("SELF_CONTACT_REQUIRED", "请先完成本人信息")
        name = person.name
        dictionaries = {
            category.value: [
                    item.type_name
                for item in list_dictionary_items(
                    session, category, user_id, include_inactive=False
                )
            ]
            for category in DictionaryCategory
        }
    return ExcelDownload(
        OLD_LEDGER_TEMPLATE_FILENAME,
        build_old_ledger_template(self_name=name, dictionary_names=dictionaries),
    )


__all__ = [
    "ExcelDownload",
    "all_data_excel",
    "old_ledger_template_excel",
    "query_excel",
]

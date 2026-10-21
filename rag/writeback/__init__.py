"""Excel 写回模块入口。"""

from . import diff
from .excel import ExcelTarget, save_report

__all__ = ["ExcelTarget", "save_report", "diff"]

"""统计报表模块（report）。

- ``StatisticsService``：按周期统计（车次/营收/平均时长/在场峰值）+ 明细查询（FR-6）；
- ``PdfReportGenerator``：ReportLab(CID 中文字体) + Matplotlib 图表，导出 PDF。
"""
from .pdf import PdfReportGenerator
from .reports import ReportService
from .statistics import StatisticsService

__all__ = ["StatisticsService", "PdfReportGenerator", "ReportService"]

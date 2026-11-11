"""PDF 报表生成（report/pdf.py）。

对应设计文档 §3.5（FR-6）。跨平台策略：
- 中文用 ReportLab **内置 CID 字体 STSong-Light**（无需捆绑字体文件，
  Windows/macOS/HarmonyOS 一致，见 config.report.font_name）；
- 图表用 Matplotlib（Agg 后端，无界面依赖）渲染为 PNG 后内嵌；图表坐标/图例
  一律用 ASCII 标签，规避 Matplotlib 缺中文字体导致的方块问题。
"""
from __future__ import annotations

import logging
import os
import tempfile
from datetime import datetime
from pathlib import Path

from reportlab.lib import colors           # noqa: E402
from reportlab.lib.pagesizes import A4, landscape  # noqa: E402
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle  # noqa: E402
from reportlab.lib.units import mm          # noqa: E402
from reportlab.pdfbase import pdfmetrics    # noqa: E402
from reportlab.pdfbase.cidfonts import UnicodeCIDFont  # noqa: E402
from reportlab.lib.enums import TA_RIGHT  # noqa: E402
from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer,  # noqa: E402
                                Table, TableStyle, Image, LongTable)
from xml.sax.saxutils import escape  # noqa: E402

from ..core.config import AppConfig, get_config
from ..core.domain import PeriodStats
from ..core.timeutil import to_local
from .reports import payment_method_label

log = logging.getLogger("report.pdf")

_FONT_REGISTERED = False

# P1：日报/周报明细使用横向 A4（A4 297x210，左右边距 18mm → 可用宽 261mm）
REPORT_MARGIN = 18 * mm
DETAIL_PAGESIZE = landscape(A4)
DETAIL_USABLE_WIDTH = DETAIL_PAGESIZE[0] - 2 * REPORT_MARGIN
# 列宽（mm）：日期/车牌/入场时间/出场时间/停放分钟/金额/支付方式/车位号
VEHICLE_COLS_WITH_DATE = (24, 34, 46, 46, 20, 24, 24, 24)     # 242mm <= 261mm
VEHICLE_COLS_WITHOUT_DATE = (34, 46, 46, 20, 24, 24, 24)       # 218mm <= 261mm


def vehicle_col_widths(*, with_date: bool) -> list[float]:
    cols = VEHICLE_COLS_WITH_DATE if with_date else VEHICLE_COLS_WITHOUT_DATE
    return [value * mm for value in cols]


def report_pagesize(preview: dict):
    """P1：日报/周报（含车辆明细）横向 A4；月/季/年报保持纵向。"""
    return (DETAIL_PAGESIZE
            if preview.get("level") in ("vehicle_day", "day") else A4)


def _ensure_font(font_name: str) -> str:
    """注册 CID 中文字体（幂等）。失败则回退内置 Helvetica。"""
    global _FONT_REGISTERED
    try:
        if not _FONT_REGISTERED:
            pdfmetrics.registerFont(UnicodeCIDFont(font_name))
            # CID 字体需显式注册字体族，否则 Paragraph(标题/表头) 找不到 bold/italic 映射
            try:
                pdfmetrics.registerFontFamily(
                    font_name, normal=font_name, bold=font_name,
                    italic=font_name, boldItalic=font_name)
            except Exception:  # noqa: BLE001 - 注册失败仍可继续（纯文本段落）
                log.exception("注册 CID 字体族失败: %s", font_name)
            _FONT_REGISTERED = True
        return font_name
    except Exception:  # noqa: BLE001 - 字体不可用时回退，保证仍能出 PDF
        log.exception("注册 CID 字体 %s 失败，回退 Helvetica", font_name)
        return "Helvetica"


PDF_LABELS = {
    "zh": {
        "title": "停车场运营统计报表",
        "period_range": "统计周期：{period}　区间：{range}（不含末日）",
        "generated": "生成时间：{generated} {tz}",
        "truncated": "提示：车辆明细超过单次导出上限，明细已截断；汇总仍完整。",
        "summary_heading": "一、汇总指标",
        "summary_header": ("指标", "数值"),
        "summary_rows": (("进场车次（完整记录）", "count_in"),
                          ("出场车次（完整记录）", "count_out"),
                          ("营收总额（元）", "revenue"),
                          ("平均停放时长（分钟）", "avg_minutes"),
                          ("在场峰值（辆）", "peak_occupancy")),
        "vehicle_heading": "二、车辆明细",
        "daily_heading": "二、每日汇总",
        "vehicle_heading3": "三、车辆明细",
        "week_heading": "二、周汇总",
        "month_heading": "二、月汇总",
        "vehicle_headers": {"date": "日期", "plate": "车牌",
                            "entry": "入场时间", "exit": "出场时间",
                            "duration": "停放分钟", "amount": "金额(元)",
                            "method": "支付方式", "space": "车位号"},
        "daily_headers": ["日期", "进场", "出场", "营收（元）"],
        "week_headers": ["周起始", "周结束", "进场", "出场", "营收（元）"],
        "month_headers": ["月份", "进场", "出场", "营收（元）"],
        "em_dash": "—",
        "periods": {"day": "按天", "week": "按周", "month": "按月",
                    "quarter": "按季度", "year": "按年"},
    },
    "en": {
        "title": "Parking Operation Report",
        "period_range": "Period: {period} | Range: {range} (end exclusive)",
        "generated": "Generated at: {generated} {tz}",
        "truncated": "Note: vehicle detail exceeds the export limit and has "
                     "been truncated; the summary is complete.",
        "summary_heading": "1. Summary",
        "summary_header": ("Metric", "Value"),
        "summary_rows": (("Entries (complete records)", "count_in"),
                          ("Exits (complete records)", "count_out"),
                          ("Total revenue (CNY)", "revenue"),
                          ("Average duration (minutes)", "avg_minutes"),
                          ("Peak occupancy (vehicles)", "peak_occupancy")),
        "vehicle_heading": "2. Vehicle details",
        "daily_heading": "2. Daily summary",
        "vehicle_heading3": "3. Vehicle details",
        "week_heading": "2. Weekly summary",
        "month_heading": "2. Monthly summary",
        "vehicle_headers": {"date": "Date", "plate": "Plate",
                            "entry": "Entry time", "exit": "Exit time",
                            "duration": "Duration (min)",
                            "amount": "Amount (CNY)",
                            "method": "Payment method", "space": "Space no."},
        "daily_headers": ["Date", "Entries", "Exits", "Revenue (CNY)"],
        "week_headers": ["Week start", "Week end", "Entries", "Exits",
                         "Revenue (CNY)"],
        "month_headers": ["Month", "Entries", "Exits", "Revenue (CNY)"],
        "em_dash": "-",
        "periods": {"day": "Day", "week": "Week", "month": "Month",
                    "quarter": "Quarter", "year": "Year"},
    },
}


def pdf_labels(lang: str = "zh") -> dict:
    return PDF_LABELS["en"] if lang == "en" else PDF_LABELS["zh"]


class PdfReportGenerator:
    def __init__(self, cfg: AppConfig | None = None):
        self.cfg = cfg or get_config()

    def generate(self, stats: PeriodStats, out_path: str | os.PathLike) -> str:
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        font = _ensure_font(self.cfg.report.font_name or "STSong-Light")

        styles = getSampleStyleSheet()
        title_st = ParagraphStyle("t", parent=styles["Title"], fontName=font, fontSize=18)
        h_st = ParagraphStyle("h", parent=styles["Heading2"], fontName=font, fontSize=13)
        body_st = ParagraphStyle("b", parent=styles["Normal"], fontName=font, fontSize=10)
        local_start = to_local(stats.start)
        local_end = to_local(stats.end)
        local_generated = to_local(stats.generated_at)

        story = [
            Paragraph("停车场运营统计报表", title_st),
            Paragraph(
                f"统计周期：{self._period_cn(stats.period)}　"
                f"区间：{local_start:%Y-%m-%d} ~ {local_end:%Y-%m-%d}（不含末日）",
                body_st),
            Paragraph(f"生成时间：{local_generated:%Y-%m-%d %H:%M:%S} "
                      f"{self.cfg.server.timezone}", body_st),
            Spacer(1, 6 * mm),
            Paragraph("一、汇总指标", h_st),
            self._summary_table(stats, font),
            Spacer(1, 6 * mm),
            Paragraph("二、每日趋势", h_st),
        ]

        chart = self._render_chart(stats)
        if chart:
            story.append(Image(chart, width=170 * mm, height=80 * mm))
            story.append(Spacer(1, 4 * mm))

        story.append(Paragraph("三、每日明细", h_st))
        story.append(self._byday_table(stats, font))

        doc = SimpleDocTemplate(str(out_path), pagesize=A4,
                                leftMargin=18 * mm, rightMargin=18 * mm,
                                topMargin=18 * mm, bottomMargin=18 * mm,
                                title="停车场运营统计报表")
        try:
            doc.build(story)
        finally:
            if chart and os.path.exists(chart):
                try:
                    os.remove(chart)
                except OSError:
                    pass
        log.info("已生成 PDF 报表: %s", out_path)
        return str(out_path)

    # ----------------------------- 组件 -----------------------------
    @staticmethod
    def _period_cn(period: str) -> str:
        return {"day": "按天", "week": "按周", "month": "按月",
                "quarter": "按季度", "year": "按年"}.get(period, period)

    def _summary_table(self, stats: PeriodStats, font: str) -> Table:
        data = [
            ["指标", "数值"],
            ["进场车次", str(stats.count_in)],
            ["出场车次", str(stats.count_out)],
            ["营收总额（元）", f"{stats.revenue:.2f}"],
            ["平均停放时长（分钟）", f"{stats.avg_minutes:.1f}"],
            ["在场峰值（辆）", str(stats.peak_occupancy)],
        ]
        t = Table(data, colWidths=[70 * mm, 60 * mm])
        t.setStyle(TableStyle([
            ("FONTNAME", (0, 0), (-1, -1), font),
            ("FONTSIZE", (0, 0), (-1, -1), 10),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2f6df6")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
            ("ALIGN", (1, 0), (1, -1), "RIGHT"),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1),
             [colors.white, colors.HexColor("#f2f5ff")]),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]))
        return t

    def _byday_table(self, stats: PeriodStats, font: str) -> Table:
        data = [["日期", "进场", "出场", "营收（元）"]]
        for d in stats.by_day:
            data.append([d.date, str(d.count_in), str(d.count_out),
                         f"{d.revenue:.2f}"])
        if len(data) == 1:
            data.append(["—", "0", "0", "0.00"])
        t = Table(data, colWidths=[45 * mm, 28 * mm, 28 * mm, 40 * mm], repeatRows=1)
        t.setStyle(TableStyle([
            ("FONTNAME", (0, 0), (-1, -1), font),
            ("FONTSIZE", (0, 0), (-1, -1), 9),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2f6df6")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.lightgrey),
            ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1),
             [colors.white, colors.HexColor("#f7f9ff")]),
        ]))
        return t


    # ----------------------------- N2：预览数据 → PDF -----------------------------
    def generate_from_preview(self, preview: dict, out_path: str | os.PathLike,
                              lang: str = "zh") -> str:
        """用 ``GET /api/reports/preview`` 同一数据生成 PDF（摘要 + 汇总 + 明细/分页）。

        i18n：lang=zh（默认，保持旧输出）/ en（英文标签；数据值/ISO 时间不变）。
        """
        lang = "en" if lang == "en" else "zh"
        l = pdf_labels(lang)
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        font = _ensure_font(self.cfg.report.font_name or "STSong-Light")
        styles = getSampleStyleSheet()
        title_st = ParagraphStyle("t2", parent=styles["Title"], fontName=font, fontSize=18)
        h_st = ParagraphStyle("h2", parent=styles["Heading2"], fontName=font, fontSize=13)
        body_st = ParagraphStyle("b2", parent=styles["Normal"], fontName=font, fontSize=10)
        period = preview.get("period", "")
        period_display = l["periods"].get(period, period)
        summary = preview.get("summary", {})
        story = [
            Paragraph(l["title"], title_st),
            Paragraph(l["period_range"].format(
                period=period_display, range=self._format_range(preview)), body_st),
            Paragraph(l["generated"].format(
                generated=preview.get("generated_at", ""),
                tz=self.cfg.server.timezone), body_st),
        ]
        if preview.get("truncated"):
            story.append(Paragraph(l["truncated"], body_st))
        story += [
            Spacer(1, 5 * mm),
            Paragraph(l["summary_heading"], h_st),
            self._summary_table_preview(summary, font, lang=lang),
        ]
        level = preview.get("level")
        if level == "vehicle_day":
            story += [Spacer(1, 5 * mm), Paragraph(l["vehicle_heading"], h_st),
                      self._vehicle_table(preview.get("by_day", []), font,
                                          with_date=False, lang=lang)]
        elif level == "day":
            story += [Spacer(1, 5 * mm), Paragraph(l["daily_heading"], h_st),
                      self._daily_table(preview.get("by_day", []), font, lang=lang),
                      Spacer(1, 5 * mm), Paragraph(l["vehicle_heading3"], h_st),
                      self._vehicle_table(preview.get("by_day", []), font,
                                          with_date=True, lang=lang)]
        elif level == "week":
            story += [Spacer(1, 5 * mm), Paragraph(l["week_heading"], h_st),
                      self._week_table(preview.get("weeks", []), font, lang=lang)]
        else:
            story += [Spacer(1, 5 * mm), Paragraph(l["month_heading"], h_st),
                      self._month_table(preview.get("months", []), font, lang=lang)]
        doc = SimpleDocTemplate(str(out_path), pagesize=report_pagesize(preview),
                                leftMargin=REPORT_MARGIN, rightMargin=REPORT_MARGIN,
                                topMargin=REPORT_MARGIN, bottomMargin=REPORT_MARGIN,
                                title=l["title"])
        doc.build(story)
        log.info("已生成 PDF 报表(N2/P1,i18n=%s): %s pagesize=%s", lang, out_path,
                 report_pagesize(preview))
        return str(out_path)

    def _summary_table_preview(self, summary: dict, font: str,
                               lang: str = "zh") -> Table:
        l = pdf_labels(lang)
        data = [[l["summary_header"][0], l["summary_header"][1]]]
        for label, key in l["summary_rows"]:
            value = summary.get(key, 0)
            if key == "revenue":
                data.append([label, f"{float(value):.2f}"])
            elif key == "avg_minutes":
                data.append([label, f"{float(value):.1f}"])
            else:
                data.append([label, str(value)])
        t = Table(data, colWidths=[75 * mm, 55 * mm])
        t.setStyle(self._table_style(font))
        return t

    @staticmethod
    def _detail_styles(font: str):
        head = ParagraphStyle("v_head", fontName=font, fontSize=8, leading=10,
                              textColor=colors.white, wordWrap="CJK")
        cell = ParagraphStyle("v_cell", fontName=font, fontSize=7.5, leading=9,
                              wordWrap="CJK")
        num = ParagraphStyle("v_num", parent=cell, alignment=TA_RIGHT)
        return head, cell, num

    @staticmethod
    def _short_time(value) -> str:
        """ISO 本地时间 → `YYYY-MM-DD HH:MM`（PDF 顶部区间可读格式）。"""
        text = str(value or "")
        try:
            return datetime.fromisoformat(text).strftime("%Y-%m-%d %H:%M")
        except ValueError:
            return text.replace("T", " ")[:16]

    def _format_range(self, preview: dict) -> str:
        return (f"{self._short_time(preview.get('start'))} ~ "
                f"{self._short_time(preview.get('end'))}")

    @staticmethod
    def _two_line_time(value) -> str:
        """时间拆成“日期<br/>时间”两行，避免长 ISO 串横向溢出。"""
        text = str(value or "")
        if len(text) >= 19 and "T" in text:
            return f"{escape(text[:10])}<br/>{escape(text[11:19])}"
        return escape(text)

    def _vehicle_table(self, by_day: list, font: str, *, with_date: bool,
                       lang: str = "zh") -> LongTable:
        """P1：车辆明细分页表（横向 A4 + Paragraph 换行 + repeatRows 表头；i18n）。"""
        l = pdf_labels(lang)
        headers = l["vehicle_headers"]
        dash = l["em_dash"]
        head_style, cell_style, num_style = self._detail_styles(font)
        header = ([Paragraph(headers["date"], head_style)] if with_date else []) + [
            Paragraph(headers["plate"], head_style),
            Paragraph(headers["entry"], head_style),
            Paragraph(headers["exit"], head_style),
            Paragraph(headers["duration"], head_style),
            Paragraph(headers["amount"], head_style),
            Paragraph(headers["method"], head_style),
            Paragraph(headers["space"], head_style)]
        data: list[list] = [header]
        for day in by_day:
            for v in day.get("vehicles", []):
                row = ([Paragraph(escape(str(day.get("date", ""))), cell_style)]
                       if with_date else []) + [
                    Paragraph(escape(str(v.get("plate") or dash)), cell_style),
                    Paragraph(self._two_line_time(v.get("entry_time")), cell_style),
                    Paragraph(self._two_line_time(v.get("exit_time")), cell_style),
                    Paragraph(str(v.get("duration_minutes", 0)), num_style),
                    Paragraph(f"{float(v.get('amount', 0)):.2f}", num_style),
                    Paragraph(escape(payment_method_label(
                        v.get("payment_method"), lang) or dash), cell_style),
                    Paragraph(escape(str(v.get("space_no") or dash)), cell_style)]
                data.append(row)
        if len(data) == 1:
            data.append([Paragraph(dash, cell_style) for _ in header])
        widths = vehicle_col_widths(with_date=with_date)
        table = LongTable(data, colWidths=widths, repeatRows=1, splitByRow=1)
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2f6df6")),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1),
             [colors.white, colors.HexColor("#f7f9ff")]),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ("LEFTPADDING", (0, 0), (-1, -1), 3),
            ("RIGHTPADDING", (0, 0), (-1, -1), 3),
        ]))
        return table

    def _daily_table(self, by_day: list, font: str,
                     lang: str = "zh") -> Table:
        l = pdf_labels(lang)
        data = [list(l["daily_headers"])]
        for day in by_day:
            data.append([day.get("date", ""), str(day.get("count_in", 0)),
                         str(day.get("count_out", 0)),
                         f"{float(day.get('revenue', 0)):.2f}"])
        if len(data) == 1:
            data.append([l["em_dash"], "0", "0", "0.00"])
        t = Table(data, colWidths=[45 * mm, 28 * mm, 28 * mm, 40 * mm], repeatRows=1)
        t.setStyle(self._table_style(font, font_size=9))
        return t

    def _week_table(self, weeks: list, font: str, lang: str = "zh") -> Table:
        l = pdf_labels(lang)
        data = [list(l["week_headers"])]
        for week in weeks:
            data.append([week.get("week_start", ""), week.get("week_end", ""),
                         str(week.get("count_in", 0)),
                         str(week.get("count_out", 0)),
                         f"{float(week.get('revenue', 0)):.2f}"])
        if len(data) == 1:
            data.append([l["em_dash"], l["em_dash"], "0", "0", "0.00"])
        t = Table(data, colWidths=[40 * mm, 40 * mm, 24 * mm, 24 * mm, 34 * mm],
                  repeatRows=1)
        t.setStyle(self._table_style(font, font_size=9))
        return t

    def _month_table(self, months: list, font: str,
                     lang: str = "zh") -> Table:
        l = pdf_labels(lang)
        data = [list(l["month_headers"])]
        for month in months:
            data.append([month.get("month", ""), str(month.get("count_in", 0)),
                         str(month.get("count_out", 0)),
                         f"{float(month.get('revenue', 0)):.2f}"])
        if len(data) == 1:
            data.append([l["em_dash"], "0", "0", "0.00"])
        t = Table(data, colWidths=[45 * mm, 28 * mm, 28 * mm, 40 * mm], repeatRows=1)
        t.setStyle(self._table_style(font, font_size=9))
        return t

    @staticmethod
    def _table_style(font: str, *, font_size: int = 10):
        return TableStyle([
            ("FONTNAME", (0, 0), (-1, -1), font),
            ("FONTSIZE", (0, 0), (-1, -1), font_size),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2f6df6")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
            ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1),
             [colors.white, colors.HexColor("#f7f9ff")]),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ])

    def _render_chart(self, stats: PeriodStats) -> str | None:
        days = [d.date for d in stats.by_day]
        if not days:
            return None
        # Font discovery (system_profiler on macOS) can be slow on a fresh
        # machine. It belongs to chart generation, not application startup.
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        cin = [d.count_in for d in stats.by_day]
        cout = [d.count_out for d in stats.by_day]
        rev = [d.revenue for d in stats.by_day]
        x = range(len(days))
        # 标签过多时抽稀，避免拥挤
        step = max(1, len(days) // 12)
        ticks = list(x)[::step]
        labels = [days[i][5:] for i in ticks]      # MM-DD

        fig, ax1 = plt.subplots(figsize=(9, 4), dpi=self.cfg.report.matplotlib_dpi)
        w = 0.4
        ax1.bar([i - w / 2 for i in x], cin, width=w, label="entry", color="#2f6df6")
        ax1.bar([i + w / 2 for i in x], cout, width=w, label="exit", color="#7aa7ff")
        ax1.set_ylabel("count")
        ax1.set_xticks(ticks)
        ax1.set_xticklabels(labels, rotation=45, ha="right", fontsize=8)
        ax1.legend(loc="upper left", fontsize=8)

        ax2 = ax1.twinx()
        ax2.plot(list(x), rev, color="#ff7043", marker="o", ms=3,
                 lw=1.5, label="revenue")
        ax2.set_ylabel("revenue (CNY)", color="#ff7043")
        ax2.tick_params(axis="y", labelcolor="#ff7043")
        ax2.legend(loc="upper right", fontsize=8)

        fig.tight_layout()
        fd, path = tempfile.mkstemp(suffix=".png")
        os.close(fd)
        fig.savefig(path)
        plt.close(fig)
        return path

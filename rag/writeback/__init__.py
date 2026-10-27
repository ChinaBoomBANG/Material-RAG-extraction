"""Excel 写回模块入口。

这里**不要** re-export ``ExcelTarget`` / ``diff``：它们会连带拉起
pandas（约 304 ms）与 openpyxl（约 181 ms），而 Web 服务启动路径只需要
``writeback.clean``，平白多付近 0.5 秒启动开销。

需要时请显式导入具体模块：
    from rag.writeback.excel import ExcelTarget, save_report
    from rag.writeback.diff import format_report
"""

"""Web 模块入口。

这里**不要** re-export ``app``：那会让 ``import rag.web.app as X`` 拿到 FastAPI 实例
而不是模块（Python 会沿属性链取到包上的 app），排查时极难定位。
需要应用对象请显式导入：``from rag.web.app import app``。
"""

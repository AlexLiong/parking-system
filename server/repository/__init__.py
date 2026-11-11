"""数据访问层包（repository）。

SQLAlchemy ORM 模型即建表；每张表一个 Repository 类，方法即 SQL 语义封装。
所有 Repository 只被服务层调用，服务层不直接写 SQL（分层约束，§3.8）。
"""

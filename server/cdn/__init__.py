"""ESCAH 离线资源链路。

一个 package，把所有「从 CDN 到本地网页」的事管起来：

.. code-block:: text

   python -m pipeline.cli info              # 看当前状态
   python -m pipeline.cli refresh           # 重新拉清单
   python -m pipeline.cli diff              # 清单对比（谁更新了）
   python -m pipeline.cli new               # 列出云上有、本地没有的角色
   python -m pipeline.cli update 417        # 一键接入一个角色（增量）
   python -m pipeline.cli check 69          # 检查某角色本地是否齐全
   python -m pipeline.cli i18n-cov          # 各语言译文覆盖率

设计约束见 :mod:`pipeline.config` 与 :mod:`pipeline.stages` 的模块文档。
"""

__all__ = ["config", "net", "manifest", "select", "stages", "i18n_store", "cli"]
__version__ = "1.0"

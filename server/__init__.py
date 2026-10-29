"""ESCAH 离线阅读器 · 本地服务端

纯标准库实现（Pillow 为可选增强：webp 转换 / 超分）。

模块划分
--------
paths    路径常量、配置读写
jobs     后台任务与进度事件流
net      多线程分段下载（断点续传 / 限速 / 重试）
packs    资源包清单、依赖求解、下载链接解密
media    解包、webp 转换、超分、覆盖层生成（"应用"）
exports  CG 打包、视频落盘、译文导入导出
updater  应用自更新（检查 GitHub Release + 退出后替换文件）
api      HTTP 路由与静态站点服务
"""

__version__ = "1.0.0"

#: 数据结构（packs.json / state.json）变更时递增
SCHEMA = 1

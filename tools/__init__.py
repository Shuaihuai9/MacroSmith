"""开发阶段命令行测试工具包。

这些脚本不依赖 Qt，用于在**打包 exe 之前**用命令行验证核心功能：

* ``python tools/record_cli.py``   录制一段时间并保存 JSON
* ``python tools/play_cli.py``     回放脚本（模式一原速 / 模式二单步）
* ``python tools/selftest.py``     一键自检（录制链路除外，全部自动校验）

它们是“先命令行验证、后封装 exe”流程的一部分，正式发布时可以不打包。
"""

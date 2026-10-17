# -*- mode: python ; coding: utf-8 -*-
"""MacroSmith 宏匠 —— PyInstaller 打包配置（onedir + windowed）。

用法（在项目根目录执行）::

    python -m PyInstaller MacroSmith.spec --noconfirm

产物：dist\\MacroSmith\\MacroSmith.exe（整个 MacroSmith 文件夹交给 Inno Setup 打包）

设计要点：
- onedir 而非 onefile：启动快、杀软误报率低，Inno Setup 会把整个文件夹收进安装包
- windowed（console=False）：GUI 程序无黑框；日志走 %APPDATA%\\MacroSmith\\logs
- datas 只带 assets 资源（icon.ico + 赞赏码 donate_qr.jpg）：用户数据
  （config.json / scripts）运行时落在 %APPDATA%，
  **不打包任何开发期脚本**（data/scripts 里的用户测试脚本不进发布包）
"""

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[('assets/icon.ico', 'assets')],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='MacroSmith',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon='assets/icon.ico',
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name='MacroSmith',
)

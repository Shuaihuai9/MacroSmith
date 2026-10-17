; ============================================================================
; MacroSmith 宏匠 —— Inno Setup 安装包脚本
; ============================================================================
; 用法（三选一）：
;   1. 右键本文件 → Compile（装了 Inno Setup 后有右键菜单）
;   2. 打开 Inno Setup Compiler → File → Open → 打开本文件 → Build → Compile
;   3. 命令行：& "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" MacroSmith.iss
;
; 前置条件：先跑 PyInstaller 打包（见 打包发布步骤.md），
; 产物 dist\MacroSmith\ 文件夹必须已存在。
;
; 产物：installer\Output\MacroSmith-Setup-1.0.0.exe
;
; 用户数据说明（重要）：
;   - 本脚本只安装 Program Files 下的程序文件；
;   - 用户数据（config.json / scripts / logs）在 %APPDATA%\MacroSmith\，
;     卸载时**不会**被删除（Inno 默认只清 {app} 目录），重装后脚本还在。
; ============================================================================

#define MyAppName "MacroSmith"
#define MyAppNameCN "宏匠"
#define MyAppVersion "1.0.0"
#define MyAppExeName "MacroSmith.exe"

[Setup]
; AppId 必须永远不变：它决定了「升级安装 = 覆盖旧版本」的识别依据。
; 一旦发布过 1.0 就不要再改，否则用户机器上会出现两个独立安装。
AppId={{7A3C2E91-4B6D-4F8A-9C1E-2D5B8A0F3E64}
AppName={#MyAppNameCN} {#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppNameCN} {#MyAppName} v{#MyAppVersion}
AppPublisher=MacroSmith
; 安装界面左侧展示的图标
WizardStyle=modern
; 默认装到 Program Files；普通用户无需选择便携模式
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppNameCN}
; 卸载信息里显示的名称与图标
UninstallDisplayName={#MyAppNameCN} {#MyAppName}
UninstallDisplayIcon={app}\{#MyAppExeName}
; 安装器自身的图标
SetupIconFile=..\assets\icon.ico
; 压缩：lzma2 固实压缩，体积最小（PySide6 目录体积大，值得开）
Compression=lzma2/max
SolidCompression=yes
; 装到 Program Files 需要管理员权限
PrivilegesRequired=admin
; 程序运行中时提示用户关闭（Restart Manager 自动检测 MacroSmith.exe）
CloseApplications=yes
; 输出位置与文件名
OutputDir=Output
OutputBaseFilename=MacroSmith-Setup-{#MyAppVersion}
; 安装器界面语言（见步骤文档第 2 步：确认 Languages 文件夹里有没有这个文件）
ShowLanguageDialog=no

[Languages]
; ★ 若你的 Inno Setup 安装目录 Languages 文件夹里没有 ChineseSimplified.isl，
;   注释掉下面这行即可退回英文界面；下载方法见《打包发布步骤.md》第 2 步。
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; \
    GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
; 可选：开机自启（默认不勾选）。不需要就整行删掉。
; Name: "autostart"; Description: "开机自动启动宏匠"; \
;     GroupDescription: "其他任务："; Flags: unchecked

[Files]
; 核心：把 PyInstaller 产物整包收进来（不含任何用户脚本/配置）
Source: "..\dist\MacroSmith\*"; DestDir: "{app}"; \
    Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
; 开始菜单
Name: "{group}\{#MyAppNameCN}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\卸载 {#MyAppNameCN}"; Filename: "{uninstallexe}"
; 可选桌面快捷方式（上面 Tasks 里勾选才创建）
Name: "{autodesktop}\{#MyAppNameCN}"; Filename: "{app}\{#MyAppExeName}"; \
    Tasks: desktopicon

[Run]
; 安装完成后立即运行（用户可取消勾选）
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#MyAppNameCN}}"; \
    Flags: nowait postinstall skipifsilent

[Uninstall]
; 注意：这里故意不写 UninstallDelete。
; %APPDATA%\MacroSmith\（config.json / scripts / logs）是用户数据，
; 卸载时保留 —— 用户重装后录制的脚本不会丢。
; 如果将来要做「彻底清除」选项，再单独加带 confirmation 的删除逻辑。

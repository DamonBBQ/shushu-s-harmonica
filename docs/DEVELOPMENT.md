# 开发与打包

源码环境的安装命令见 [README](../README.md#安装方法)。数据模型、线程通信、输入保护和时序推导见 [设计文档](DESIGN.md)。

## 项目结构

```text
.
├── main.py                  # 程序入口
├── core/
│   ├── score.py             # MIDI / TXT 解析与单音化
│   ├── mapping.py           # 音高映射、移调与八度折返
│   ├── engine.py            # 演奏线程、时序和状态机
│   ├── input_backend.py     # Win32 输入、前台进程检测和测试后端
│   ├── hotkeys.py           # 全局热键
│   └── updater.py           # 联网曲库更新、校验和备份
├── ui/                      # 主窗口、置顶迷你浮窗和主题
├── scores/                  # 内置曲谱和 Git 归属清单
├── tools/                   # 曲谱生成工具
├── data/                    # 供生成工具使用的键位数据和速度资料
├── docs/                    # 文档和界面截图
├── tests/                   # 单元测试与测试曲谱
├── requirements.txt
└── build_exe.bat            # Windows 打包脚本
```

## 打包为 exe

在 Windows 上双击 `build_exe.bat`。脚本安装项目依赖与 PyInstaller，生成窗口模式的 `HarmonicaPlayer`，并将 `scores/` 复制到 EXE 同级目录。

输出文件位于 `dist\HarmonicaPlayer\HarmonicaPlayer.exe`。分发时保留整个 `HarmonicaPlayer` 目录，曲谱和 `.git-scores.json` 都放在 EXE 旁的 `scores/` 中。实奏时以管理员身份运行 EXE。

曲谱可通过应用内的「检查更新」获取。程序功能发生变更时，使用更新后的源码重新打包。 [曲库更新说明](LIBRARY.md#检查更新)

## 图片键位谱生成工具

在仓库根目录执行 `python tools/build_from_image_tabs.py`，可按 `data/tabs.json` 和 `data/bpm.json` 生成早期键位图对应的试奏 TXT。输出位于 `scores/图片曲谱/`，键位说明同步写入 `docs/图片曲谱_键位版.md`。 [图片曲谱键位版](图片曲谱_键位版.md)

## 测试与验证范围

安装依赖后，运行离线回归测试。

```bash
python -m unittest discover -s tests -v
```

测试覆盖曲谱解析、倍速可演奏性、失焦暂停、急停、曲末休止、循环间隔、热键注册、浮窗选曲、透明度同步和在线曲库更新。

扫描码识别、热键响应、无边框窗口中的浮窗焦点及不同进程名的匹配仍待 Windows 游戏实机验证。 [测试策略与记录](DESIGN.md#14-测试策略与记录)

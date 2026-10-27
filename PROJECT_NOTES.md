# MousePet 项目上下文

给接手的 Agent（或未来的自己）看的速查。最后更新 2026-10-27，版本 **2.1**。

## 是什么

Windows 桌面宠物：统计鼠标移动距离、左右键、滚轮和键盘，右侧显示键盘仪表盘；支持三个形象（大肥鱼 / ChatGPT Q 版 / Claude Q 版）。Python 3.9+ 标准库（tkinter + ctypes），**运行时不需要第三方包**。

## 位置与仓库

- 本地：`D:\dshwork\mouse-pet`（DSH 工作区标题为「桌宠」）
- GitHub：https://github.com/yang-zhenchen/mouse-pet
- Release：`v2.1`（`MousePet-v2.1-win64.zip`）
- 网络：本机代理 `127.0.0.1:7890`；仓库里已设 `git config http.proxy`

## 运行 / 打包

```powershell
python mouse_pet.py                     # 会自动转 pythonw，无控制台
cscript //nologo 启动鼠标桌宠.vbs        # 双击启动
cscript //nologo 启动鼠标桌宠.vbs /find-python   # 看它选了哪个 Python
build_exe.bat                           # PyInstaller 打包到 dist\MousePet\
```

## 架构要点

- **Tracker**（钩子线程）：`WH_MOUSE_LL` + `WH_KEYBOARD_LL`；`MAX_JUMP` 过滤跨屏跳变；记录 `keys` / `combos` / `hours` / `kpm` 等；每 5 秒 flush 到 `data/stats.json`；启动时 `restore()` 接上当天。
- **MousePet**：双层窗口。`root` 画圆角卡片（`-alpha` 透明度），`front` 画角色和文字（`-transparentcolor` 键控透明）；`_front_on_top()` 维持层序。
- **布局**：完整 380×224，迷你 128×138；所有坐标是逻辑值 × `self.s`（dpi/96）。
- **键盘仪表盘**：手速 / 峰值、24 小时节奏条、组合键 TOP5、今日人格。
- **形象**：`SKIN_PREFIX` = fish→`dfy`、chatgpt→`gpt`、claude→`claude`；`assets/` 下每个角色 10 档高度。
- **表情**：大肥鱼 16 格 + `_blink`；ChatGPT / Claude 只有 idle / happy / cheer；`_current_expr()` 决定。
- **反应引擎**（`_auto_expr`）：kpm≥300 `proud`、≥420 `cheer`；10 秒退格≥10 `pout`；同键 3 秒≥6 `sulk`；3 秒点击≥6 `surprised`；5 秒滚轮≥12 `orb`；Ctrl+C/V `smug`；3 秒≥6 键 `sing`；悬停 2 秒 `shy`、6 秒 `blush`；无输入 60 秒 `drowsy`、5 分钟 `sleepy`、15 分钟 `sleep`。
- **人格**：8 个（还在热身 / 复制粘贴工程师 / 退格狂魔 / 撤销依赖症 / 深夜键盘侠 / 键盘机关枪 / 键盘马拉松 / 稳重型选手），见 `README.md`。
- **菜单**：形象（3 个）、表情（按角色过滤）、透明度、置顶、迷你、最近 7 天、重置、开机自启、管理员重启、退出。

## 数据文件

- `data/stats.json`：按天 `{px,left,right,wheel,keys,combos,hours,kpm_peak,typing_seconds,backspace}`，保留 120 天。
- `data/config.json`：位置、透明度、`animal`、`pose`。
- `data/mousepet.log`：日志。
- `data/` 不进 git（个人数据）。

## 素材许可

- **代码**：MIT（`LICENSE`），仅代码。
- **大肥鱼**：YunYueSama/codex-deepseek-pet，自定义「大肥鱼项目署名许可 1.0」。
- **ChatGPT / Claude**：savanna0425/llm-pet，形象作者 ZipZipPipe，**必须署名、禁止商用**。
- 详见 `THIRD_PARTY_NOTICES.md`；项目保持免费非商业。

## 已知限制

- 仅 Windows（Win32 钩子 + Tk `-transparentcolor`）。
- UIPI：普通权限看不到管理员窗口里的输入；游戏反作弊也可能拦截。
- exe 未签名，首次运行会弹 SmartScreen。

## 待办 / 想法

- **E 彩蛋**：用户有自己的想法，还没定（打字时宠物反馈之类）。
- llm-pet 的 `actions.png` 16 格还没用（走路左右、单眼眨、星星等）。
- 走路 / 眨眼的逐帧动画（目前是静态帧切换）。
- 人格扩充：摸鱼选手、快捷键大师等。
- macOS 移植：CGEventTap + 辅助功能权限，圆角透明要重做。

## 版本轨迹

1.4 键盘 TOP10 → 1.5 键盘仪表盘 → 1.6 Q 版表情 → 1.7 16 表情菜单 → 1.8 真眨眼 + 文字描边 → 1.9 数据联动反应 → 2.0 三形象 → 2.1 表情菜单过滤。

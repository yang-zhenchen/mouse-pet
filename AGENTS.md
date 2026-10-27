# MousePet

Windows 桌面宠物（Python + tkinter + ctypes）。

**开始工作前先读 [`PROJECT_NOTES.md`](PROJECT_NOTES.md)**，里面有架构、约定、数据格式、素材许可和待办。

## 快速命令

```powershell
python mouse_pet.py          # 启动（自动转后台）
build_exe.bat                # 打包到 dist\MousePet\
git push                     # 已配置 http.proxy，直接推
```

## 约定

- 只用 Python 标准库，不要引入运行时第三方依赖。
- 改完先 `python -m py_compile mouse_pet.py`。
- 角色素材有署名/非商业限制，新增素材前先看 `THIRD_PARTY_NOTICES.md`。
- 个人数据（`data/`）、构建产物（`build/`、`dist/`、`_release/`）不进 git。

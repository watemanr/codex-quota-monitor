# Codex 用量采集器 · macOS

这是现有 Windows 采集器的 macOS 适配版，版本 1.3.0。复用额度采集、上传队列、定时器及重置检测，增加 macOS 原生菜单栏图标、系统通知和上传密钥钥匙串保存。

**当前交付状态：源码与打包工具已完成，Windows 环境通过 13 项 macOS 适配逻辑测试和 44 项原有客户端测试。也通过了非 Windows 隔离容器中的 13 项适配测试和 43 项共享测试（Windows DPAPI 测试跳过）。尚未在真实 Mac 上运行或打包，不包含已经验证的 `.app` / DMG。** Mac 上运行下面的构建脚本后，必须通过窗口和菜单栏离线自检，才会产生可分发 ZIP。

## 功能

- 5 小时 / 每周剩余额度，手动读取及上传。
- 读取、上传分别设置间隔；0 关闭对应功能。
- 菜单栏显示例如 `14% W`；鼠标悬停可查看 `14%/weekly · 62%/5h`、账号及采样时间。
- 菜单栏可打开 / 隐藏窗口、开始 / 停止自动、退出。
- 设置菜单可以开关后台常驻与额度通知。关闭窗口后是否继续运行取决于常驻开关。
- 额度归零、计划内重置、疑似突发重置提醒，以及上次重置时间 / 类型。
- 上传密钥保存在 macOS 钥匙串；配置 JSON 只存引用。取消「记住密钥」后删除对应的钥匙串项目。
- 服务地址默认留空，继续使用原有网页和 `UPLOAD_TOKEN`。

## 在 Mac 上构建 `.app`

需要已经登录桌面会话的 **macOS 11 或更新版本**，以及带 Tk 的 Python 3.12–3.15。推荐 [python.org 的 macOS 安装包](https://www.python.org/downloads/macos/)；构建脚本先检查 Python/Tk，再把依赖安装在项目内的 `macos/.build-env`，不会修改系统 Python。

从 Gitea 克隆项目，或解压源码包，在项目根目录执行：

```bash
bash macos/build.command
```

也可以在 Finder 双击 `macos/build.command`。默认构建当前 Mac 的架构：Apple Silicon 为 `arm64`，Intel 为 `x86_64`。如果要指定 Python 路径：

```bash
PYTHON_BIN=/Library/Frameworks/Python.framework/Versions/3.12/bin/python3 bash macos/build.command
```

构建过程会运行测试，在本机临时目录生成 App、签名并校验，再启动 1 次离线自检；自检不读取 Codex 登录、不获取真实额度、不上传。使用临时目录可以避免 iCloud Drive、Dropbox 等 FileProvider 同步目录附加 Finder 元数据而破坏签名。全部验证成功后才把产物移到 `macos/dist`：

```text
macos/dist/CodexUsageCollector-macOS-arm64.zip       # Apple Silicon
macos/dist/CodexUsageCollector-macOS-x86_64.zip      # Intel
对应 ZIP 的 .sha256 校验文件
```

首次构建需要联网下载已固定版本的 PyObjC 和 PyInstaller。后续运行 `.app` 不需要另外安装 Python。请通过 ZIP 分发 App，保留应用内部符号链接与权限。构建脚本不在 `dist` 中留下裸 `.app`，因为 FileProvider 同步目录可能在签名后异步附加 Finder 元数据；需要本机试运行时可双击 `macos/run.command`，它会从 ZIP 解压到临时目录、复核签名后运行。

双架构构建可使用 `ARCH=universal2 bash macos/build.command`，前提是 Python/Tk 和所有依赖都包含 arm64 与 x86_64 两种架构；当前未验证双架构构建成功。[PyInstaller 架构说明](https://pyinstaller.org/en/stable/feature-notes.html#macos-multi-arch-support)。Windows 上不能使用这个脚本直接生成可验证的 macOS App。

默认使用临时签名，不包含 Apple Developer ID 签名或公证，只适合本机测试；从其他电脑下载后可能被 Gatekeeper 阻止。若已有 Developer ID Application 身份，可设置 `CODE_SIGN_IDENTITY`，构建脚本会保留 hardened runtime 并加入可信时间戳。正式对外发布仍须使用自己的 Apple 开发者账户完成公证并 stapling；不要通过关闭整个系统的应用安全检查来运行软件。

## 开始使用

1. 解压生成的 ZIP，将其中的 `CodexUsageCollector.app` 复制到「应用程序」后打开；构建机也可以双击 `macos/run.command` 试运行。
2. 在 Mac 上用 ChatGPT 账号登录 Codex。登录目录默认是 `CODEX_HOME`，否则为 `~/.codex`。
3. 填写固定的网页账号名称。不同实际账号使用不同名称，避免混合历史。
4. 服务地址填写原网页根地址；上传密钥填服务器的 `UPLOAD_TOKEN`，不要填网页登录密码。
5. 先点「仅采集」查看额度；准备好后点「采集并上传」。
6. 定时任务需要点击「开始自动」；再次打开 App 不会自动开始联网。电脑睡眠期间不能采样，唤醒后按定时器规则继续。

菜单栏中 `W` 表示 weekly、`H` 表示 5h；数据缺失时不补造 0。悬浮和菜单栏显示的是最近一次采样，移动鼠标不会触发网络请求。

首次有额度事件时系统可能询问通知权限；拒绝或系统通知不可用时，回退为应用内弹窗。系统勿扰模式仍可能阻止通知展示。

## 登录存储限制

**本版只读取 Codex 登录目录中的 `auth.json`，与现有 Windows 版一致；尚不支持仅保存在系统钥匙串中的 Codex 登录。** 如果找不到该文件，程序会报错，不会把缺失额度当成 0，也不会擅自修改 Codex 登录配置或导出钥匙串中的 OpenAI 凭证。

Codex 官方同时支持文件和系统凭据库等登录存储方式，见 [官方认证说明](https://learn.chatgpt.com/docs/auth)。实际 Mac 上需要验证你的登录方式是否兼容。这里只限制读取 Codex 登录；**本软件自身的上传密钥**已经使用 macOS 钥匙串保存，两者是不同用途。

包含分析选项时复用已有近 30 天分析与聊天排行采集；上游接口和本机任务索引格式可能变化。失败时保留实时额度，不会估算缺失分析。仅显示读取与上传过程，不调用模型或兑换额度。

## 本机数据与更新

数据保存在：

```text
~/Library/Application Support/CodexUsageCollector/
```

其中 `settings.json` 保存非敏感配置及钥匙串引用；`last_collection.json` 保存最近采样；`pending.sqlite3` 保存未确认上传；`usage_monitor.sqlite3` 保存重置历史。OpenAI 凭据和上传密钥不会写入导出的采样 JSON。

上传密钥的钥匙串服务名为 `xyz.814305.codex-quota-collector.upload`。同一 Mac 上不同设置目录使用独立引用。重新签名或更换 App 后，系统可能要求重新允许钥匙串访问。

更新时退出旧 App 再替换 `.app`，本机设置与历史目录保留。不复制 Windows 的 DPAPI 密钥；在 Mac 上重新填写上传密钥。

## 源码开发与验证

```bash
python3 -m venv macos/.build-env
macos/.build-env/bin/python -m pip install -r macos/requirements.txt
macos/.build-env/bin/python macos/source/mac_app.py
macos/.build-env/bin/python -m unittest discover -s macos/source -p 'test_*.py'
macos/.build-env/bin/python -m unittest discover -s windows/source -p 'test_*.py'
```

源码启动时系统通知回退为应用内提示；原生通知需从打包后的 App 运行。Windows 专属 DPAPI 测试在 Mac 上跳过，其余共享采集 / 上传测试照常运行。`--smoke-test --data-dir <独立测试目录>` 用于离线原生界面自检。

平台适配位于 `macos/source/`；共享逻辑暂存于 `windows/source/`，打包时一并加载，所以不能只复制 macos/source 单个目录运行。

## 重置卡统计（v1.3.0）

拉取新版源码后重新运行 `macos/build.command`。实时额度采集会同时读取真实重置卡总数，在主窗口显示并上传到网页形成增减记录；无需额外配置，不会兑换或使用重置卡。缺失字段显示未知。Mac 与 Windows 使用同一采集与解析代码。

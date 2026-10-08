# Codex 额度观察与桌面采集器

接收多个 Codex 账号的 5 小时 / 每周额度采样，保存历史，并根据采样变化判断计划内重置、疑似突发重置及额度归零。Windows 客户端独立运行，读取和上传过程不调用模型。

## 项目内容

| 目录 | 内容 |
| --- | --- |
| `server/` | Flask + SQLite 网页服务、Docker Compose、Nginx 示例及测试 |
| `windows/` | Windows 采集器 v1.3.0 可执行程序、使用说明和第三方许可 |
| `windows/source/` | 共享 Tkinter 客户端源码、Windows 构建脚本和测试 |
| `macos/` | macOS 菜单栏 / 钥匙串适配及 App 打包工具（待 Mac 实机验证） |

## 网页服务

```bash
cd server
sudo bash deploy.sh
```

需要 Linux、Docker 与 Docker Compose 插件。部署脚本生成独立的随机上传密钥、API 访问密钥，以及 48 字符的初始网页登录密码；生成值保存在服务器 `/etc/codex-quota-monitor.env`，不会写入 Git。服务监听 HTTP 8765，域名和 HTTPS 可交给现有 Nginx。

网页登录后，顶部「账号管理」可改名、合并或删除账号及其历史；旧账号名仍可接收采集器上传并归入新名字。右上角「修改密码」可持久修改登录密码；也可在部署后的 `/opt/codex-quota-monitor/app/docker-compose.yml` 中设置 `WEB_PASSWORD` 并重新创建容器。改网页密码不会影响采集器上传。未变化的 Docker 密码配置在容器重启时不会覆盖网页改密。

- [服务部署、接口和判断规则](server/README.md)
- 网页「Bark 推送」可按账号设置重置提醒、低额度阈值与定时报告；设备 key 仅保存在服务器。
- 额度总览显示 Tibo 最近帖文的纯文字缓存，每 10 分钟更新；保留原帖链接供核对。
- 额度总览显示采集器最近上传状态与重置事件时间轴；「更多」提供基于真实连续采样的额度天气预报。
- [Docker Compose](server/docker-compose.yml)
- [Nginx 示例](server/nginx.conf.example)
- [上传 OpenAPI](server/static/openapi.json)

## Windows 客户端

运行 [CodexUsageCollector.exe](windows/CodexUsageCollector.exe)，填写网页地址及服务器生成的 `UPLOAD_TOKEN`。默认网页地址留空，上传密钥只填写值，不要添加 `Bearer`。程序读取本机已登录 Codex 的用量信息，缺少窗口时不会补造数值。

- 多个账号以固定账号名上传，在网页中分别展示。
- 读取和上传可设置不同间隔；任一间隔设为 0 即关闭该项。
- 支持后台托盘、归零 / 重置通知，以及上次重置时间和类型。
- 悬浮提示优先显示当前已采集账号的 weekly 剩余额度，例如 `14%/weekly`；有 5h 数据时也会显示。
- 未确认上传持久保存在本机队列，重试保持采样时间和唯一 ID。
- 默认设置和本机数据在 `%LOCALAPPDATA%/CodexUsageCollector`，与程序目录分开。
- 重启程序后点击「开始自动」启用定时运行。

[Windows 详细说明](windows/README.md)。内含的 EXE 是 v1.3.0，可用 `windows/SHA256.txt` 校验。

从源码运行或重新构建（Windows，Python 3.12）：

```powershell
python windows/source/windows_app.py
powershell -ExecutionPolicy Bypass -File windows/source/build.ps1
```

## macOS 客户端

macOS 适配源码及打包脚本已加入，支持菜单栏额度、系统通知、钥匙串保存上传密钥和现有采集 / 上传功能。当前尚未在真实 Mac 上打包验证；[Mac 说明](macos/README.md)列出构建要求及登录存储限制。

在 Mac 的项目根目录运行 `bash macos/build.command`，通过离线原生界面自检后会生成 `.app` 和 ZIP。默认按构建机架构选择 Apple Silicon 或 Intel。

## 验证

```bash
python -m pip install -r server/requirements.txt
python -m unittest discover -s server -p tests.py
python -m unittest discover -s windows/source -p 'test_*.py'
```

Windows 客户端测试建议在 Windows 运行。服务测试用独立临时数据库，不修改正式历史。首次入库前已验证服务器 63 项测试及客户端测试，并检查密码、密钥和本机数据未入库。

## 数据与判断边界

网页只能分析实际上传的采样。突然重置是根据前后用量和计划重置时间推断，不能确认平台内部原因；两次采样间发生并再次耗尽的额度可能漏检。聊天分析依赖采集端实际返回的明细，缺失数据不会估算或伪造。

本仓库不包含网页登录密码、上传密钥、OpenAI 登录凭证、真实账号采样或用户设置。第三方组件许可在 `windows/licenses/`。

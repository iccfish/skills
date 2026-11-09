# 缺少本机组件时的安装概述

只提供操作概述和官方下载入口。安装、升级、开启 HTTP 或修改设置由用户决定；发现失败本身不触发这些操作。下载时按 Windows 与 Everything 主程序架构选择安装包，使用官方入口选择当前版本，不固定过期的安装包地址。

## 未发现 Everything

提示“未发现本机 Everything 配置”，不能直接断言软件未安装。已安装但未运行、配置在未覆盖的目录或当前权限不可读，也可能导致发现失败。

1. 已安装时，先启动 Everything，让它生成配置并完成初始索引，再执行 `discover`。其他位置的已知 HTTP 地址或 es.exe 可以用 `add` 手工登记。
2. 未安装时，从 [Everything 官方下载页](https://www.voidtools.com/downloads/) 选择匹配架构的标准安装版，按向导安装；也可下载便携 ZIP，解压到固定目录后运行 `Everything.exe`。
3. 不选择 Lite：官方说明 Everything 1.4 Lite 不提供 HTTP/ETP，也不允许 IPC，本技能的两种通道都需要相应能力。启动并完成索引后，再安装/启用下述所需组件。

[官方安装说明](https://www.voidtools.com/support/everything/installing_everything/)。

## 发现 Everything，但未配置或未开启 HTTP 插件

`plugin_filename` 无 HTTP 标记、插件配置缺失或插件验证失败时，脚本返回 `http_setup_required`。这表示尚未确认可用的插件服务，不证明服务一定被关闭；认证、端口或网络请求失败同时在 `diagnostics` 报告实际连接错误，先按原因排查，不直接让用户重装插件。已有可用 ES 时可继续用 ES，无需为本机查询开启 HTTP。

- **Everything 1.5**：从 [HTTP Server 官方插件下载页](https://github.com/voidtools/http_server/releases) 或 [官网插件页面](https://www.voidtools.com/support/everything/plugins/) 下载匹配主程序架构的安装器，运行后选择添加；便携版可将插件 DLL 放入 Everything 安装目录的 `plugins` 子目录，然后退出并重启 Everything。
- **启用服务**：进入“工具 → 选项 → HTTP 服务器”，勾选启用，设置监听端口及需要的用户名和密码。仅供本机访问时可将绑定地址设为 `127.0.0.1`；只检索信息时可关闭文件下载。随后执行 `discover`，由脚本验证 JSON 查询后登记。
- **Everything 1.4 标准版**：内置 HTTP，无需安装 1.5 插件，直接在 HTTP 服务器选项页启用。当前自动发现按插件约定执行，1.4 内置 HTTP 应按实际地址和凭据使用 `add` 手工添加。

插件加载与 HTTP 服务启用是两个步骤；DLL 存在不表示服务已启用。官方 1.5 文档使用 `Plugins.ini`；脚本优先读取用户约定的同目录 `Plugin.ini`，不存在时兼容 `Plugins.ini`。仍以 `[http_server64.dll]` 的配置及实际 JSON 查询验证为准，其他架构或配置布局可手工添加 HTTP 实例。

[官方 HTTP 配置说明](https://www.voidtools.com/support/everything/http/)。

## 发现 Everything，但没有 ES 工具

提示“在本次发现目录中未找到 es.exe”，不能断言整台机器都没有 ES。

1. 打开 [ES 官方发布下载页](https://github.com/voidtools/ES/releases)，或 [官网全部下载页](https://www.voidtools.com/downloads/) 的“Everything Command-line Interface”部分，下载匹配 Windows 架构的 ZIP。本技能要求 ES 1.1.0.37 或更新版本。
2. 将 `es.exe` 解压到 Everything 安装目录，使它能被本机发现流程找到。若放在其他固定目录，使用 `add` 设置本机绝对 `es_path`，无需为本技能修改 PATH。
3. 保持 Everything 主程序运行并完成索引，再执行 `discover`。ES 与 Everything 主程序是独立组件，不能把 Everything.exe 重命名成 es.exe，也不能用 ES 连接远程 HTTP 实例。

[官方 ES 使用说明](https://www.voidtools.com/support/everything/command_line_interface/)。

## 提示的呈现

`list` 和 `discover` 的 `discovery.guidance` 包含 `code`、`summary`、`overview` 与 `downloads`；空配置搜索失败时，错误 JSON 的 `guidance` 同样保留指引。向用户按实际命中的情况展示概述和可点击的官方下载链接。HTTP 与 ES 可任选一条满足当前任务，不要求全部安装。完成用户选择的配置后，用 `discover` 或手动添加实例后的只读查询验证。

来源核对日期：2026-11-09。上述链接指向项目官方维护的页面；HTTP 插件适用于 1.5，1.4 内置 HTTP 的版本差异以官方说明为准。

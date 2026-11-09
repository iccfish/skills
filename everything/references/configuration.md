# 配置与本机发现

配置路径优先级：全局参数 `--config FILE`（子命令前）、`EVERYTHING_CONFIG`、`AGENT_SKILLS_CONFIG_DIR/everything.json`、`XDG_CONFIG_HOME/agent-skills/everything.json`、`~/.config/agent-skills/everything.json`。UTF-8 JSON，支持 BOM。源码更新不替换本机配置。

配置完整示例（地址、用户名、密码均为示例）：

```json
{
  "version": 1,
  "default_instance": "office-http",
  "instances": [
    {
      "id": "office-http",
      "name": "办公文件索引",
      "mode": "http",
      "base_url": "https://files.example.com/everything",
      "username_env": "OFFICE_EVERYTHING_USER",
      "password_env": "OFFICE_EVERYTHING_PASSWORD",
      "timeout_seconds": 10
    },
    {
      "id": "local-es",
      "mode": "es",
      "es_path": "C:\\Tools\\Everything\\es.exe",
      "instance_name": "",
      "timeout_seconds": 10
    }
  ]
}
```

`id` 唯一且只含字母、数字、`_`、`.`、`-`。`name` 可选显示名。HTTP 的 `base_url` 是查询根地址，可带反向代理路径前缀，不能带用户名、密码、查询串或 fragment。每次请求在根地址后附加 `/?search=...&json=1`。无认证时省略凭据；也支持本地字符串 `username`、`password`，同名 `_env` 优先且缺失变量时报错，不回退到其他密码。超时范围 1–600 秒，默认 10 秒。

HTTP 使用 Basic 认证，默认不使用系统代理，禁止重定向，保留 TLS 校验。客户端要求响应兼容官方 Everything JSON 查询 API；返回 HTML 登录页或其他插件协议时明确报错。不会下载文件、自动开启 HTTP 服务或改写 Everything INI。

ES 的 `es_path` 必须为本机绝对盘符路径，不接受 URL、UNC 或 Everything.exe；`instance_name` 是本机 Everything 命名实例，不是网络主机。HTTP 认证字段可存储在各实例中，ES 模式不使用这些字段。`base_url`/`host`/`port` 不能用于 ES。请将 ES 1.1.0.37+ 放入 Everything 目录以便发现；其他路径可手动添加。

## 管理命令

`add --file FILE` 接收单个实例对象（不是完整配置），检查配置字段并保存；不要求实例当前在线。`update --file FILE` 要求对象包含已有 `id`，其余字段合并；`username_env`/`password_env` 可设为 `""` 取消环境变量引用，`username`/`password` 可设为 `""` 清空。切换模式时清空旧模式的地址字段。`add/update --default` 设置默认实例。`remove ID` 删除实例并清除指向它的默认值。保存采用临时文件替换；同时运行多个配置写操作不受支持。

配置和包含密码的输入文件放在仓库外、仅当前用户可读的位置。自动发现会复制插件明文凭据到该配置；不要把真实 Plugin.ini、配置、认证头或原始响应放进源码。实例列表仅报告凭据是否已配置。

## 本机发现流程

1. 先在 `%APPDATA%\Everything` 下查找 `Everything.ini`（包括子目录）；没有找到时查询正在运行的 `Everything.exe`、`Everythin64g.exe`，同时兼容常见的 `Everything64.exe`，从进程可执行文件目录找 `Everything.ini`。读不到进程路径时不会提权或扫描整盘。
2. 无 INI 则结束，不推测安装目录或登记不可用的默认实例。
3. INI 的 `plugin_filename` 包含 `http_server` 时，优先读取 INI 同目录 `Plugin.ini`，不存在时兼容官方 1.5 文档中的 `Plugins.ini`；从 `[http_server64.dll]` 取 `port`、`username`、`password`，构造 `http://127.0.0.1:PORT`，用该凭据查询 1 条 JSON 结果。有效响应才登记 `local-http`；零条匹配但合法响应也算连通。这个插件发现约定来自用户要求；它不是内置 HTTP 设置键的推断，也不读取内置 `http_server_*` 值替代。
4. 检查 INI 目录，以及 APPDATA 配置与安装分离时的运行进程目录，是否有 `es.exe`；读取可选 `instance_name`，执行版本检查和 1 条本机 IPC 查询，成功才登记 `local-es`。两条通道独立验证，一条失败不影响另一条；两条都失败则不添加。
5. 再次发现按模式和接口地址（ES 另含 `instance_name`）去重；若默认 ID 已被占用，用 `-2` 等后缀，不覆盖用户配置。

删除最后一个实例后，下次 `list/search` 仍会按要求自动发现；删除本身不会立刻重新添加。已有其他实例时，需要显式 `discover` 才扫描本机。

发现结果还会针对未发现 Everything、缺少 HTTP 配置、缺少 ES 输出概述与官方下载入口，见[安装指引](installation.md)。没有检测到组件时只给建议，不自动安装或改动服务设置。

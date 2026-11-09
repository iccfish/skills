# 下载、站点配置和同步

## 发现目标

1. `GET /api/downloaders/stations` 读 `stations` 配置：数据库 `id` 用于 PATCH/DELETE 站点，业务 `clientId` 用于下载任务；二者不能互换。响应只暴露 `hasPassword`，不是密码。
2. `GET /api/downloaders` 获取运行状态与能力，核对是否启用、协议与连接错误；站点配置列表不能证明下载器在线。
3. `GET /api/downloaders/torrents?client=CLIENT_ID` 只查目标站点；省略或 all 会查询全部。用当前返回的 hash 精确定位任务。离线或报错的实例不能解释为“没有下载任务”。
4. `GET /api/downloaders/mounts` 发现挂载信息；下载器内部路径、宿主 `hostPath`、磁盘索引 `path` 不属于同一个命名空间，结合站点 `pathMappings` 核对。

读取任务文件和详情分别用 `/api/downloaders/files`、`/api/downloaders/torrent-details`，均要求 `clientId` 和 40 位十六进制 hash。目录剩余空间用 `/api/downloaders/free-space`，传 `clientId` 与下载器内部 `path`。

## 新增下载

`POST /api/downloaders/add`：

- 必填 `clientId` 和 `source`，本技能客户端还要求请求中持久保存 `idempotencyKey`（8–128 字符，字母/数字/`.`/`_`/`:`/`-`）。为一个逻辑请求生成一次并保留在本地请求文件，不自动换键重试。
- URL/magnet：`source={"kind":"url","urls":["..."]}`。Transmission 每次只接受一个 URL；qBittorrent 与 Transmission 的选项和能力不同，不假设所有选项通用。
- 种子文件：先用 `POST /api/downloaders/torrent-file/parse` 的 multipart 字段 `file` 解析；1 字节到 10 MiB。该操作不创建下载，返回的 metainfo 等字段用于后续 `source={"kind":"metainfo","metainfo":"...","filename":"..."}`。
- 使用 `savePath` 时传下载器内部目录；`paused` 等布尔字段用真正 JSON 布尔值。对 qBittorrent 的 metainfo 设置非空 `fileSelections` 时须覆盖全部文件且索引不越界；URL 模式不能据此保证逐文件设置。按 live schema 填写 wanted 与 priority。
- 不把下载站登录秘密、带 tracker passkey 的 URL 或 metainfo 写入公共仓库。

当前契约：同实例、同键、同参数的记录保留 24 小时；成功重放会标记 `replayed:true`，并不再次添加。键冲突、执行中或状态未知返回 409，不应重发上游。Redis 不可用时不降级提交；保留期到期、记录丢失、不同键或无键不保证去重。

`operationStatus=accepted` 不是完成；`already_present` 表示已有可识别的任务且不修改它。仅 `duplicateKnown:true` 时可根据 duplicate 判断重复；`duplicateKnown:false` 且 duplicate=false 不能证明新建成功。后续设置失败或超时可能已经创建任务，应按返回 `verification` 及目标站点任务列表对账，不换键重试。

## 已授权任务控制

所有请求先 `describe` 获取 schema，再加 `--allow-mutation`：

| POST 路径 | 关键字段与效果 |
| --- | --- |
| `/api/downloaders/action` | clientId、hashes、action=start/stop/recheck/reannounce |
| `/api/downloaders/files-wanted` | clientId、hash、fileIndexes、wanted 布尔 |
| `/api/downloaders/files-priority` | clientId、hash、fileIndexes、priority=low/normal/high |
| `/api/downloaders/location` | clientId、hashes、location（下载器内部目录；可能实际移动文件） |
| `/api/downloaders/remove` | clientId、hashes、deleteData 必须显式布尔；false 仅移除任务，true 删除数据 |

重校验、通告、移动路径、改变选中文件都是变更；移除任务授权不自动包含删除磁盘数据。写后通过同一 clientId+hash 读任务/文件/详情验证，报告尚未完成的异步阶段。不要直接调用原下载器 RPC 绕过 NasCtl。

## 站点配置

`POST /api/downloaders/stations` 创建，`PATCH /api/downloaders/stations/{id}` 更新，`DELETE` 删除配置。先读当前配置再做最小 PATCH；省略密码保留旧值，空字符串/null 会清空。`pathMappings` 省略/null 保留，`[]` 清空；不能用返回的 `hasPassword` 构造密码。clientId 要唯一且符合 live schema。删除配置不等于移除下载任务，不扩大用户授权。

## 两种同步

- `/api/downloaders/sync-mt-tags`：必须 `{"dryRun":true}` 先计划，再按授权 `false` 写回 MT 标签。需检查 downloaderErrors、writeErrors、failed、skipped、planned/applied 和对应标签数组；计划条数不是成功写入数。来自某下载站的不完整快照不能用来推断所有 MT 标签都已同步。
- `/api/downloaders/sync-mount-spaces`：必须 `dryRun` 和 mounts 数组，每项 rootId、hostName、path、totalSpace、freeSpace；最多 1000 项且 rootId 不重复，空数组有效。hostName/path 必须与实时数据库完全一致，空间单位字节且 freeSpace≤totalSpace。逐项检查 planned/updated/unchanged/failed/skipped。

预演只说明当时计划，实际执行前目标可能变化。HTTP 200 或部分 applied 不表示全量成功。脚本允许 dryRun=true 无写开关；dryRun=false 必须有授权并加 `--allow-mutation`。

## 转码队列

`GET /api/transcode/tasks` 返回 tasks、queue 及可能的 queueError；queueError 非空时 queue=[] 不证明队列空。`DELETE /api/transcode/queue` 用 JSON 字段 id 移除项目；`POST /api/transcode/queue/clear` 清空队列，均是变更。清空响应中的删除数是 Redis 键操作的结果，不是被取消的任务数，也不保证停止已经运行的转码进程。

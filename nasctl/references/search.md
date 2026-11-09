# 检索与对象关联

先用 `describe` 查询本次接口。以下是 2026-10-23 契约中的非显然差异；若实时契约变化，以现场为准。

## 搜索入口

| POST 路径 | 查什么 | 关键条件与差异 |
| --- | --- | --- |
| `/api/files/search` | 全局内容文件索引 | `hashType` 为 AUTO/SHA1/SHA256/MD5/ED2K/ED2K19M；`hash,name,size,mode,exts` |
| `/api/files/disk-search` | 物理磁盘文件和目录条目 | `sha256,name,target,size,exts,sids,roots,searchPath,scope`；target=all/file/folder |
| `/api/media/search` | 媒体记录 | `name` 是完整文件名等值；`sha1,size`；`durationFrom/To` 是非负秒数字符串 |
| `/api/torrents/search` | 本地种子索引 | `scope=file/torrent`；`name,infoHash,size,mode`；默认 scope=file，无筛选时会按 torrent 浏览 |
| `/api/mt/search` | MT 站点记录 | `field=all/name/smallDescr/descr`；`name,infoHash,categories,size,id`；`id` 是 MT 站点编号字符串 |
| `/api/transcode/search` | 转码历史 | `name` 对原/目标文件名等值；`size` 只接受非负整数字节数的纯数字字符串，不接受单位表达式 |

不要把磁盘索引当作实时文件系统，也不要把种子索引当作当前下载任务。前者来自文件数据库，后者的运行状态需通过 `/api/downloaders/torrents` 核实。

## 范围、名称、大小与分页

- `FULLTEXT` 是全文检索；`EXACT` 是全文检索后逐词 LIKE 复核，并非完整文件名相等；完整等值用 `EQUAL`，前缀用 `PREFIX`。MT 名称仅支持 FULLTEXT/EXACT，其他值可能回退 EXACT。媒体和转码接口的 name 不走这套模式。
- 通用大小表达式接受比较、AND/OR、括号和 K/M/G/T/P（1024 进制，可带 B），如 `>1GB AND <20GB`。转码历史例外，只接受纯字节数字字符串。种子 scope=file 比较单个文件，scope=torrent 比较内容总量；按 size 排序始终用种子完整内容大小。
- 磁盘 `searchPath` 非空时须只选一个 sid 和一个 root；它是索引路径边界，以 `/` 开头，不能带 `.`、`..`、NUL，最长 500 字符。`/` 表示该挂载根，不是跨服务器范围。scope=current 查现存；scope=all 包含墓碑记录。
- 页码用 JSON 数字正整数；不要依赖数字字符串的兼容转换，各接口不一致。小页起步，例如 pageSize=50。磁盘支持 50/100/200/500/1000，其他搜索支持 50/100/200/500。
- 每页同时读取 `rows,page,pageSize,total,totalPages`；totalPages 至少为 1，空页不一定是全局无结果。按当前请求实际返回字段翻页，避免无限循环和宣称已经穷尽变化中的索引。

## 从命中项追溯位置或来源

| GET 路径 | 参数 / 用途 |
| --- | --- |
| `/api/files/servers` | 无参数，发现 sid 与挂载 root；空间为字节、允许空值 |
| `/api/files/list` | 必须 sid、root；可传 path、scope、page、pageSize；读指定索引目录 |
| `/api/files/refs` | 必须 sha256（64 位十六进制），可传 deleted=1/true 看墓碑及 page |
| `/api/media/{id}/raw` | id 为媒体记录编号，读原始媒体分析 |
| `/api/torrents/files` | tid 是本地种子数据库编号，列出种子内容文件 |
| `/api/mt/by-info-hash` | infoHash 是 40 位十六进制种子哈希，查关联 MT 记录 |
| `/api/mt/detail/{id}` | id 来自 MT 查询结果；先看 schema 区分数据库行编号与站点编号 |
| `/api/mt/category-counts` | 发现有效分类和数量；不猜 category ID |

SHA256（64 位）、媒体 SHA1（40 位）、种子 infoHash（40 位）及数据库整数 ID 是不同标识，不能仅按长度相似替换。`GET /api/mt/image?url=...` 是抓图缓存入口，有外部请求和缓存副作用，不参与纯查询验证；需要图片时使用记录给出的可信 URL，不随意构造代理目标。

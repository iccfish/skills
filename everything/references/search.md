# 搜索选项

## 客户端参数映射

HTTP 请求使用 GET 和 `json=1`；ES 以本机进程执行，不经过 shell。表中选项仅控制查询。

| 客户端参数 | HTTP | ES |
| --- | --- | --- |
| `--query TEXT` / `--query-file FILE` | `search`，URL 编码 | `-argv` 后的单个检索式参数 |
| `--limit N` | `count` | 1.1.0.37：`-n`；1.1.0.38+：`-viewport-count` 并设置 `-n offset+limit` |
| `--offset N` | `offset` | 1.1.0.37：`-offset`；1.1.0.38+：`-viewport-offset` |
| `--regex` | `regex=1` | `-regex <检索式>` |
| `--case` | `case=1` | `-case` |
| `--whole-word` | `wholeword=1` | `-whole-word` |
| `--match-path` | `path=1` | `-match-path` |
| `--diacritics` | `diacritics=1` | `-diacritics` |
| `--sort date-modified` | `sort=date_modified` | `-sort date-modified` |
| `--descending` | `ascending=0` | `-sort-descending` |

默认查询为空（当前索引全部结果）、不区分大小写、非正则、不匹配全字、不匹配变音符、不强制匹配完整路径、按名称升序。HTTP 匹配选项显式传 0/1；ES 的大小写、全字、变音符和全路径开关显式用 `-no-*` 关闭。ES 正则是 `-regex <检索式>`，普通查询直接传检索式，不使用不受支持的 `-no-regex`。客户端每页默认 100 条，最大 10000，偏移从 0 起。下一页将 `offset` 增加上一页请求的 `limit`。HTTP 用 `total` 判断是否读完；ES 没有总数，页面不足请求条数可结束，正好满页时继续查下一页。

HTTP 支持 `name`、`path`、`size`、`date-modified` 排序；ES 另外支持 `extension`、`date-created`、`attributes`。HTTP 不支持的排序会报错，不悄悄换成默认值。所有排序默认升序，查最大/最新时显式加 `--descending`。

HTTP 请求显式启用 `path_column`、`size_column`、`date_modified_column`、`date_created_column`、`attributes_column`。ES 导出临时 UTF-8 CSV，包含名称、路径、大小、创建/修改时间及属性，大小以字节、日期为 ISO-8601；保留 CSV 原始列名及字符串值。临时文件使用后自动删除。

## 检索式示例

两种通道都使用 Everything 检索语法。以下为原始检索式，不是 PowerShell 命令：

| 目的 | 检索式 |
| --- | --- |
| 多关键词同时出现 | `合同 2026` |
| 两个词任意匹配 | `合同|协议` |
| 排除词 | `报告 !草稿` |
| 精确连续短语 | `"年度总结"` |
| 文件类型 | `ext:pdf;docx` |
| 限定盘符及目录 | `D:\资料\ ext:pdf` |
| 带空格的目录 | `"D:\Project Files\" ext:pdf` |
| 仅文件 / 仅文件夹 | `file: ext:zip` / `folder: 项目` |
| 大文件 | `size:>100mb` |
| 指定日期以后修改 | `dm:>=2026-11-01` |
| 分组 | `<ext:pdf|ext:docx> 合同` |

通配符 `*`/`?` 用于名称匹配；开启 `--regex` 后按正则表达式解释，不把通配符表达式直接当正则。通常默认只匹配名称；有路径限定、全路径匹配选项时可检索路径。脚本不复制 GUI 书签和筛选器，ES 不能访问它们；类型筛选用 `ext:` 等检索式表达。

带引号、反斜杠、`|`、`<`、`>` 的复杂检索式，写入仓库外 UTF-8 文件再运行：

```text
python <技能目录>/scripts/everything.py search --instance local-es --query-file <检索式文件> --limit 30 --sort size --descending
```

`--query` 与 `--query-file` 不能同时用。ES 检索式不能以未加引号的 `-` 或 `/` 开头，以防被解析成 ES 控制开关；搜索此类文件名可用原始检索式 `"-report"`，或改用 HTTP。不会允许用户检索式注入 `-exit`、`-save-settings` 等操作。

## 来源与版本边界

2026-11-09 核对：

- [官方 HTTP 文档](https://www.voidtools.com/zh-cn/support/everything/http/)：HTTP 查询串与返回列。
- [官方 ES 文档](https://www.voidtools.com/zh-cn/support/everything/command_line_interface/)：ES 的本机 IPC、查询/排序、CSV 与退出码。
- [Everything 主程序命令行选项](https://www.voidtools.com/zh-cn/support/everything/command_line_options/)：这是 `Everything.exe` 的控制/GUI 参数，不能与 ES 参数混用，如主程序 `-path` 与 ES `-path-column` 含义不同。
- [官方 ES 源码仓库说明](https://github.com/voidtools/ES)：补充中文文档未列出的 `-argv`、`-utf8-bom`、`-date-format`；[1.1.0.37 发布记录](https://github.com/voidtools/ES/releases/tag/1.1.0.37)说明加入 `-argv`。
- [官方搜索说明](https://www.voidtools.com/support/everything/searching/)：运算符、目录、类型、大小和日期检索。

具体插件是否支持上述 HTTP JSON 参数要实际探测。不能仅因 Plugin.ini 存在就认定它兼容官方内置 HTTP API。

本机 ES 1.1.0.38 的帮助与实测还表明：`-n` 限制查询找到的总条数，输出分页使用 `-viewport-offset/-viewport-count`。客户端对这个版本单独映射分页，避免 `-n 2 -offset 2` 返回空页。

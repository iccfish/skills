# MrDoc 私人笔记本 —— MrDoc Token API 接口参考（v1.1.1）

> 以下为 WorkBuddy 源技能附带的源码分析和历史验证记录；迁移并未重新执行其中的写入测试，不能将表中的“实测”视为本次 Codex 验证。

本参考记录源技能针对 **MrDoc 1.1.1** 的历史分析与测试；当前站点以本地配置和实时只读结果为准。

以下契约以 **v1.1.1 官方源码 `app_api/urls.py` + `app_api/views.py` + `app_doc/utils.py`** 为准，
并用该站点的实际请求逐条验证过（验证状态见表末）。

> **相比 0.9.6 的重大变化**：目录层级限制已解除（不再只认三级）；`get_docs` 不再只返回本人文档；
> 新增附件上传；`modify_doc` 加了环检测与协作编辑权限。详见第 4 节。

## 1. 鉴权与基础约定

- 鉴权方式：**GET 参数 `token`**，非 Header。所谓 APIKEY 就是 MrDoc 的「用户 Token」，sha224 生成的 56 位 hex。
- 所有接口都在 `/api/` 前缀下，基址 `配置中的 base_url/api/<name>/`。
- 响应统一为 JSON：`{"status": true|false, "data": ...}`；失败时 `data` 是中文错误描述（如 `"token无效"`、`"非法请求"`、`"系统异常"`）。
- 未注册的路径返回 **整页 404 HTML**，不是 JSON——排查时认准这一点。
- 写接口（create/modify/delete/upload）均为 `@csrf_exempt`，可无 Cookie 调用；同时接受
  `application/x-www-form-urlencoded` 与 `application/json` 两种请求体。
- 参数校验缺失时接口**不报错而是静默写坏数据**（见第 6 节坑 1），必须自行保证字段齐全。

## 2. 接口清单（1.1.1 实际注册的全部 21 条路由）

| 路径 | 方法 | 参数 | 说明 |
|---|---|---|---|
| `check_token/` | GET | `token` | 校验 Token，返回 `{is_writer, username, user_type}` |
| `get_timestamp/` | GET | — | 服务器时间戳，供 oauth 用 |
| `oauth0/` | GET | `username`、`timestamp`、`randstr`、`hashstr`、`redirecturl` | Token 换登录态（免密登录网页端） |
| `manage_token/` | GET/POST | 需登录态 | 网页端 Token 管理，Token API 调不到 |
| `get_projects/` | GET | `sort_name`(默认 create_time)、`sort`(0 升 1 降)、`filter_name`(self/colla)、`kw` | 有浏览+新增权限的文集列表，含 `icon` |
| `get_project/` | GET | `pid` | 单个文集详情（Serializer 全字段） |
| `get_docs/` | GET | `pid`、`limit`(默认 **10**)、`page`(默认 1)、`sort`、`kw` | 文集下**全部已发布文档**（含他人创建），`kw` 匹配标题+正文 |
| `get_level_docs/` | GET | `pid`、`page`(默认 1)、`is_page`(无效，见坑 6) | 文集目录树，**不限层级**，`sub` 嵌套 |
| `get_self_docs/` | GET | `limit`(默认 **10**)、`page`、`kw`、`sort` | 本人全部文档（跨文集），带 `summary` 摘要 |
| `get_doc/` | GET | `did` | 单篇文档全文（**限本人创建**，见坑 5） |
| `get_doc_previous_next/` | GET | `did` | 返回 `{previous, next}` 的 did |
| `get_imgs/` | GET | `limit`、`page` | 本人图片素材列表（新增） |
| `get_attachments/` | GET | `kw`、`limit`、`page` | 附件列表（新增，**当前版本必报错**，见坑 8） |
| `create_project/` | POST | `name`、`desc`、`role`(默认 1) | 新建文集 |
| `create_doc/` | POST | `pid`、`title`、`doc`、`parent_doc`(默认0)、`editor_mode`(默认1) | 新建文档，返回新 `did` |
| `modify_doc/` | POST | `pid`、`did`、`title`、`doc`、`parent_doc` | 整篇覆盖，**每次自动写历史版本** |
| `delete_doc/` | POST | `did` | 软删除（`status=3`），无恢复接口 |
| `upload_img/` | POST | 文件字段 `image`，或表单字段 `data`(base64) | 上传图片，返回 `{success:1, url}` |
| `upload_img_url/` | POST | `url` | 传远程图片 URL / data URI |
| `upload_file/` | POST | 文件字段 **`attachment_upload`** | 上传附件（新增），返回 `{name, url}` |
| `ai_chat_stream/` | POST | — | AI 流式对话，需站点开启 AI（`ai_status=1`） |

**1.1.1 相比 0.9.6 新增的 5 条**：`manage_token/`、`get_imgs/`、`get_attachments/`、`upload_file/`、`ai_chat_stream/`。

## 3. 关键数据结构

### 3.1 文档对象（`get_doc` 返回）

以下字段值均为合成示例，不来自真实用户的文档、文集或返回记录。

```json
{
  "id": 101,
  "name": "文档标题",
  "content": "<h2>…</h2>",      // 渲染后的 HTML；新建后未在网页端保存过时为 null
  "md_content": "## 原始 Markdown…",  // pre_content，Markdown 原文
  "parent_doc": 0,
  "sort": 9999,
  "top_doc": 1,                  // 所属文集 pid
  "project_name": "示例文集",
  "status": 1,                   // 1 正常 3 已删除（软删）
  "editor_mode": 1,
  "create_time": "2024-01-01T00:00:00.000",
  "modify_time": "2024-01-01T00:00:00.000",
  "create_user": "example-user"
}
```

- **读内容用 `md_content`；`content` 是渲染产物且可能为 null**。
- `get_self_docs` / `get_docs` 的列表项不带正文，只带 `summary`（已去标签的纯文本摘要）。

### 3.2 `editor_mode` 取值

| 值 | 含义 | 正文写在哪个字段 | API 可写 |
|---|---|---|---|
| 1 | Markdown（默认） | `pre_content` / `md_content` | ✅ |
| 2 | Markdown（同 1 处理） | 同上 | ✅ |
| 3 | 富文本 | `content`（HTML），**无 `md_content`** | ✅（`create_doc` 会过 `sanitize_html`） |
| 4 | 在线表格 | `modify_doc` 里直接 `pass` | ❌ 改不了 |

### 3.3 `get_level_docs` 的分页字段（容易误解）

```json
{"status": true, "total": 20, "total_page": 1, "current": 1, "data": [...]}
```

- **`total` 是「顶层节点数」，不是文档总数**。顶层节点下即使包含多篇子文档，也不会按全部文档数计入 `total`。
- 要文档总数：用 `get_projects` 的 `total`（那个是真数），或自己递归数 `sub`。
- 分页单位是顶层节点，每页 20 个。

## 4. 1.1.1 的行为变化（写操作必读）

### 4.1 目录树不再限层级 ✅

`get_level_docs` 已重写为：

```python
doc_nodes = list(Doc.objects.filter(top_doc=pid, status=1).values('id','name','editor_mode','parent_doc').order_by('sort'))
doc_list = build_doc_tree(doc_nodes, child_key='sub')
```

`build_doc_tree`（`app_doc/utils.py`）是通用递归建树，注释原文写「构建不限层级的树形结构」。
顺带修掉了老问题：**父文档不存在（已删/跨文集/自引用）的节点会被提为根节点，不再从目录里静默消失**。

实测：建 L1→L2→L3→L4 四级，`get_level_docs` 完整返回 4 层嵌套。

### 4.2 `get_docs` 不再只返回本人文档

0.9.6 是 `Doc.objects.filter(top_doc=pid, create_user=token.user)`，1.1.1 去掉了 `create_user` 限定，
改为按文集权限判定。同时新增 `kw`，服务端匹配 `name__icontains | pre_content__icontains`。

### 4.3 `modify_doc` 三处增强

```python
doc = Doc.objects.get(id=doc_id)
if not is_edit_authorized(token, doc):            # 1) 协作编辑权限，不再只看 create_user
    return JsonResponse({'status': False, 'data': '非法请求'})
if parent_doc not in ['', '0'] and not check_doc_parent_valid(doc_id, parent_doc):
    return ... '不能将文档的上级文档设置为其下级文档'   # 2) 环检测
parent_id = doc.parent_doc if parent_doc == '' else parent_doc   # 3) 空值保留原层级
```

实测：把自己挂到自己下面会被拒；不传 `parent_doc` 不会把文档提到顶层。

### 4.4 附件上传（新能力，但有双重限制）

```python
result = handle_attachment_upload(attachment, token.user, request)   # app_doc/util_upload_file.py
```

- 字段名 `attachment_upload`，multipart POST。
- **后缀白名单**来自 `SysSetting(types='doc', name='attachment_suffix')`，**未配置时默认只有 `['zip']`**。
  实测传 `.txt` 返回 `{"status": false, "data": "不支持的格式"}`。
- 大小上限同样来自 `SysSetting(types='doc', name='attachment_size')`，未配置时 50MB。
- zip 会做炸弹检测。

## 5. 已验证 / 未验证清单

| 接口 | 状态 | 说明 |
|---|---|---|
| `check_token` | ✅ 实测 | `{"is_writer":true,"username":"example-user","user_type":"admin"}` |
| `get_projects` | ✅ 实测 | 验证过 `icon`、`total` 等字段；不保留用户文集数量 |
| `get_project` | ✅ 实测 | |
| `get_docs` | ✅ 实测 | 含他人文档；`kw` 服务端搜标题+正文；无 total 字段 |
| `get_level_docs` | ✅ 实测 | **4 级完整返回**；`total` 是顶层数；分页按顶层节点 |
| `get_self_docs` | ✅ 实测 | `kw` 搜标题+正文；分页需循环取 |
| `get_doc` | ✅ 实测 | `md_content` 与 `content` 均可取；限本人创建 |
| `get_doc_previous_next` | ✅ 实测 | |
| `create_doc` | ✅ 实测 | 建后 `content` 为 null，属正常 |
| `modify_doc` | ✅ 实测 | 含「不传 title 清空标题」的坑复现、环检测复现 |
| `delete_doc` | ✅ 实测 | 软删除，目录立即不显示 |
| `upload_file` | ⚠️ 部分实测 | 不带文件返回「未选择文件」、传 txt 返回「不支持的格式」，**成功路径未实测**（会留附件垃圾） |
| `get_attachments` | ❌ 实测不可用 | 恒返回 `{"status": false, "data": "系统异常"}`，见坑 8 |
| `get_imgs` | ✅ 实测 | 返回图片素材列表 |
| `upload_img` / `upload_img_url` | ⚠️ 未实测 | 会往素材库写文件，脚本已按其字段实现 |
| `create_project` | ⚠️ 未实测 | 会新建文集，未做真实调用 |
| `ai_chat_stream` | ⚠️ 未验证 | 需站点开启 AI，未调用 |

## 6. 已确认的坑

1. **`modify_doc` 缺 `title` 会把文档名写成空串**，且返回 `{"status":true,"data":"ok"}` —— 静默数据损坏。
   **1.1.1 仍未修**（实测：改后 `name` 为空串）。脚本 `_rewrite()` 强制先 `get_doc` 取原标题。
2. **`get_doc` 只允许读 `create_user == 本人` 的文档**（`Doc.objects.get(create_user=token.user, id=did)`）。
   这与 `modify_doc` 已支持协作编辑矛盾：**别人写在你协作文集里的文档，能改但读不到**，
   而脚本所有写操作都依赖 `get_doc` 取标题，所以实际上改不了。
3. **`get_docs` / `get_self_docs` 默认 `limit=10`**，不翻页会以为只有 10 篇。
4. **`get_docs` 不返回 `total`**，翻页只能靠「本页条数 < limit」判断结束。
5. **`get_level_docs` 的 `total` 是顶层节点数**，不是文档总数；`get_projects` 的 `total` 才是真数。
6. **`get_level_docs` 的 `is_page` 参数无效**：源码 `is_page = request.GET.get('is_page', True)` 配
   `if is_page is False`，query string 传进来永远是字符串，`is False` 恒不成立，**无法关闭分页**。
   顶层节点超过 20 个时必须自己按 `total_page` 翻页。
7. **`get_doc` 的 `content` 可能为 null**（API 新建、网页端尚未保存过），必须回退到 `md_content`。
8. **`get_attachments` 在 1.1.1 里根本跑不起来**：`app_api/views.py` 用到了 `Attachment`，
   但文件顶部只 import 了 `Project, Doc, DocHistory, Image, ProjectCollaborator`，漏了 `Attachment`，
   于是 `NameError` 被 `except` 吞掉，恒返回「系统异常」。**上传能成、列表拿不到**。
9. **`create_doc` 只能写自己"创建"的文集**（`Project.objects.filter(create_user=token.user)` 判定）。
   被协作授权的文集会被拒，返回 `{"status":false,"data":"非法请求"}`。
10. **`delete_doc` 只能删自己创建的文档**（`doc.create_user == token.user`）。
11. **文档站内链接**：规范形式是 `/project-<pid>/doc-<did>/`；`/doc/<did>/` 也能打开（短路径）。
    注意**未登录访问私密文集时，404 页同样返回 HTTP 200**，判断可达性要看 `<title>` 而非状态码。

## 7. 官方文档出处

- API 索引：<https://doc.mrdoc.pro/d/api-developer>（部分页面仍停留在 0.9.x 的接口说明，以源码为准）
- 源码（版本对齐的依据）：
  - 路由：<https://github.com/zmister2016/MrDoc/blob/v1.1.1/app_api/urls.py>
  - 视图：<https://github.com/zmister2016/MrDoc/blob/v1.1.1/app_api/views.py>
  - 建树：<https://github.com/zmister2016/MrDoc/blob/v1.1.1/app_doc/utils.py>
  - 附件：<https://github.com/zmister2016/MrDoc/blob/v1.1.1/app_doc/util_upload_file.py>

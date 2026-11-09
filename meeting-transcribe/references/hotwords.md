# 热词与权重

热词用于提高人名、产品名、缩写和专业术语的识别倾向，不保证强制输出或替代人工校准。当前接入默认 `qwen-audio-3.1-asr-flash-filetrans` 的即时词表 `parameters.vocabulary`，不创建云端词表 ID。

## 本地管理

以下 `scripts/transcribe.py` 应替换为已安装技能的实际路径；这些是独立的本地管理命令，不带媒体、恢复或其他操作参数，不联网、不安装依赖、不要求云端凭据。

```powershell
# 添加：省略权重时保存为 1
python scripts/transcribe.py --hotword-add "示例产品"
python scripts/transcribe.py --hotword-add "示例缩写" --hotword-weight 3

# 查询全部或单个热词
python scripts/transcribe.py --hotword-list
python scripts/transcribe.py --hotword-query "示例产品"

# 修改权重；改词时未指定权重则保留原权重，也可同时修改
python scripts/transcribe.py --hotword-update "示例产品" --hotword-weight 5
python scripts/transcribe.py --hotword-update "示例产品" --hotword-rename "新示例产品"

# 删除
python scripts/transcribe.py --hotword-remove "示例缩写"
```

添加已有词、查询/修改/删除不存在的词、改名覆盖另一个已有词，均报错且不写入。重复词按去除首尾空白后的完整字符串区分，大小写不自动合并；空词、换行和控制字符不允许。修改必须指定新词或新权重，不默默重置为 1。

普通权重为整数 1–5；超级热词可设为 50，其他数值、布尔、字符串和小数均不接受。总词数最多 2000，权重 50 的词最多 50 个；脚本拒绝超限，不让服务端随机丢弃词条。权重越高偏置越强，优先从默认 1 开始。

## 配置与保存

热词在现有主配置中使用 `hotwords` 字段，管理命令默认保存到 `~/.config/agent-skills/meeting-transcribe.json`，遵循 `AGENT_SKILLS_CONFIG_DIR` / `XDG_CONFIG_HOME`；`--config` 优先于 `MEETING_TRANSCRIBE_CONFIG`。可以直接编辑仓库外配置：

```json
{
  "hotwords": {
    "示例产品": 1,
    "示例缩写": 3
  }
}
```

命令行添加时默认权重为 1；手工 JSON 的每个词都应填写整数权重。未配置 `hotwords` 等价于空词表，原有配置继续可用。

`--config FILE` 可为热词管理创建新文件。字段选择与转写一致：指定文件若含 `hotwords`，整个字段覆盖默认词表，空对象 `{}` 明确关闭热词；指定文件若不含该字段，读取默认配置的词表，首次修改时将有效词表保存到指定文件，不复制默认文件的凭据或其他字段。管理写入只改选定文件的 `hotwords`，原有配置字段保持不变，损坏 JSON 或并发变更拒绝覆盖。

真实热词可能包含私人姓名或业务名，应放在仓库外配置；不要把用户词表追加进技能示例、README 或验证记录。列表/查询按用户请求展示词表，输出不包含其他配置或凭据。

## 转写传递与恢复

配置保存后，正常提交新媒体即可自动携带词表：

```powershell
python scripts/transcribe.py "会议.m4a"
```

非空词表传入 `parameters.vocabulary`，语言提示、音轨和说话人分离参数保持原有行为；空词表不发送该参数。不把词表写入公共日志或云任务恢复记录。

修改热词只影响之后新提交的识别任务。`--resume` 继续读取原任务；`--from-json` 只处理已有结果，都不会重提或重新收费转写，也不会用新词表改变既有识别结果。需要对已有文本纠错时继续使用原有按句校准功能。

使用自定义 `model` 时，需先核对其 API 是否支持同一 `vocabulary` 参数，不能把这套参数视为所有 ASR 模型通用契约。

接口依据：[百炼录音文件识别 API](https://help.aliyun.com/zh/model-studio/qwen-audio-asr-filetrans-http-api)。

# 人工纠错与重放

原始 `.asr.json` 和 `-原始转写稿.txt` 不变；调整保存在 `.meta.json`，展示为 `-归属校准版.txt`。所有句序号从 0 起，锚定原始句，拆句后不随列表下标移动。首次运行先冻结声纹暂定归属；已有归属快照后的校准不重新匹配，也不入库。未确认版和 `.attribution.json` 均保持不变，校准版从首次归属加全部有效调整重放生成。

用户用自然语言描述时间、原话、改字及说话人时，智能体先检索定位再执行；有歧义先询问，不能推断整段都属于某人。拆句后同一原始索引可出现多次，此时补充原话定位片段，不能猜测。局部姓名使用 `--attribute`，不要为了一个插话执行全编号 `--speaker-map`。

```bash
# 只替换唯一命中的原文，保留未改字符的时间戳；新文字只在被替换区间内插值
python scripts/transcribe.py --from-json "会议.asr.json" --replace-text "5=生态西统=>生态系统"
# 摘出的片段单独锁定姓名；其余片段保留当前归属
python scripts/transcribe.py --from-json "会议.asr.json" --attribute "8=是不是..原创性=李四"
# .. 表示整句，但只改这句身份，不改变该编号下其他句子
python scripts/transcribe.py --from-json "会议.asr.json" --attribute "12=..=李四"
```

`--attribute` 用独立的确定性身份 ID 表示人工指定的人，按字级时间戳切分；同一句后续仍可给唯一定位的剩余片段命名。不能唯一定位或片段跨越已有切分时保留记录并提示 skipped，需调整定位或撤销旧切分后重新操作。`--replace-text` 仅在当前拆分片段里唯一命中才修改；跨分段或重复原文不猜测。局部人工姓名后续声纹不能覆盖。

自然语言修改只生成审核稿，用户明确“继续处理”后再执行 `--continue`。有归属校准版时，处理内容来自首次归属加同一次已保存的累计调整重放，不从 TXT 猜回音频时间戳。直接在编辑器修改 TXT 不会自动进入后续处理，需转为明确纠错记录。

```bash
python scripts/transcribe.py --from-json "会议.asr.json" --find "关键词"
python scripts/transcribe.py --from-json "会议.asr.json" --show-edits
python scripts/transcribe.py --from-json "会议.asr.json" --speaker-map "0=张三,1=李四"
python scripts/transcribe.py --from-json "会议.asr.json" --reassign "12,13=1"
python scripts/transcribe.py --from-json "会议.asr.json" --reassign "00:12:30-00:12:55=李四"
python scripts/transcribe.py --from-json "会议.asr.json" --merge "3=1"
python scripts/transcribe.py --from-json "会议.asr.json" --split "8=也就是说..这件事=1"
python scripts/transcribe.py --from-json "会议.asr.json" --split "00:10:25=也就是说=李四"
python scripts/transcribe.py --from-json "会议.asr.json" --text "5=修正后的句子"
```

`--split` 起点/终点是原文片段：`..终点` 从句首摘取，`起点..` 或只有起点摘到句尾。时间定位须唯一落在某句范围。姓名目标取自已确认映射或本轮 `--speaker-map`，不存在时不猜测身份。

拆句优先使用词时间戳，缺少精确时间戳时只能估计字符边界；不能据估计时间保证声纹片段纯净。拼接文本保持原文。对已拆句做整句 `--text` 会合并该原始句的分段，并提示。

## 撤销与清空

```bash
# 查看每条记录的一基编号和稳定 id
python scripts/transcribe.py --from-json "会议.asr.json" --show-edits
# 撤销第 2 条，或把 2 换成记录 id；可重复 --undo-edit
python scripts/transcribe.py --from-json "会议.asr.json" --undo-edit 2
# 清空包括姓名在内的全部调整，重新确认；仍可同轮传入新映射
python scripts/transcribe.py --from-json "会议.asr.json" --clear-edits
```

撤销是追加 `undo` 记录，让目标记录在重放中失效，再从原始结果重算；日志保留历史。不能用反向 merge 撤销，也不支持撤销 undo 本身。需要重新做该操作时再次提交原操作即可。

撤销早期拆句等操作后，后续依赖记录可能无法定位；检查 skipped/warnings/conflicts，不自动编造替代位置。清空时不再复用人工 speaker_map，回到冻结的首次归属；用户本轮明确提供的姓名仍有效。

## 日志结构

`edits.adjustments` 保存带稳定 `id` 的操作列表，旧日志加载时生成确定性 id；`edits.summary` 是重算的净效果，不能编辑它代替调整记录。

| op | 关键字段 |
|---|---|
| rename | speaker, name |
| merge | from, to |
| reassign | items[{idx, begin_ms}], to |
| reassign_range | start_ms, end_ms, to；按句/拆分段起点是否落入区间定位原始句组 |
| split | idx, begin_ms, carve{from,to,speaker} 或 parts[{text,speaker}] |
| text | idx, begin_ms, old, text |
| replace_text | idx, begin_ms, old, new；逐字替换并更新字级时间戳 |
| attribute | idx, begin_ms, carve{from,to,speaker}, name；局部姓名锁定 |
| undo | target（前面的非 undo 记录 id） |

普通纠错优先用 CLI。需要精确 parts 时可追加记录后重生成；不要修改或删除历史记录、重复 id，操作前保留备份。损坏日志会报错并保留原文件。

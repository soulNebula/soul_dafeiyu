# 提示词覆盖（prompt_store）

> P4-b：把写死在代码里的提示词变成「可编辑 + 热加载 + 可校验 + 可回滚」。
> 文件：`agi/data/prompts.json`（本地数据，gitignore，不提交）。
> 前端直接按下面这个 JSON 结构读写，**不要嵌套其它结构**；不 import 本模块也能用。

## 1. 文件格式（稳定协议）

```json
{
  "version": 1,
  "prompts": {
    "proactive.kind.care": "覆盖后的文本"
  },
  "updated_at": 1730000000.0
}
```

- `prompts` 是白名单键 → 字符串；空字符串等价于「删除该覆盖」（回退代码默认）。
- 单条 ≤ **4000 字**；整个文件 ≤ **64KB**；只允许白名单键。
- 修改文件后**下一条消息 / 下一次 tick 自动生效**（mtime 热加载，无需重启）。
- 写回请用「临时文件 + 原子替换」，或调用 `prompt_store.set_many()`（前端也可直接写）。
- 回滚：**删除 `agi/data/prompts.json`** 即全部回到代码默认。

## 2. 键清单

| 键 | 含义 | 影响位置 |
| --- | --- | --- |
| `proactive.persona_constraints` | 轻度人格化边界（数字生活/禁物理起居） | proactive 每条 prompt |
| `proactive.kind.care` | care 类型指令 | 主动消息 care |
| `proactive.kind.share` | share 类型指令 | 主动消息 share |
| `proactive.kind.light` | light 类型指令 | 主动消息 light |
| `proactive.kind.followup` | followup 类型指令 | 主动消息 followup |
| `proactive.kind.silence_ack` | silence_ack 类型指令 | 主动消息 silence_ack |
| `proactive.anti_fabrication` | P0-9 不编造共同经历约束 | proactive 每条 prompt |
| `proactive.punctuation` | P0-10/13 标点/分句约束 | proactive 每条 prompt |
| `proactive.continuity` | P0-17 连续性约束（continuation/new_topic 规则） | 有上下文时的 proactive prompt |
| `proactive.voice_fewshot` | 真实语气 few-shot 段（含「参考语气，不要照抄」说明） | proactive 每条 prompt |
| `chat.system` | 被动对话 SYSTEM 段 | `chat.send` 的 system message |

默认值由 `prompt_store.defaults()` **从现有代码常量导出**（`PERSONA_CONSTRAINTS`、
`GROUNDING_CONSTRAINTS`、`RUNON_CONSTRAINTS`、`CONTINUITY_CONSTRAINTS`、
`VOICE_FEWSHOT_SECTION`、`KIND_INSTRUCTION`、`chat.SYSTEM`），不会在 store 里复制第二份。
未配置时 prompt 与改动前逐字节一致（既有 proactive 基线锁、chat_inner sha256 锁保持绿）。

## 3. 怎么改

前端：调用后端接口读写上述 JSON（结构见第 1 节）。

命令行（示例）：

```bash
python - <<'PY'
from agi.selflearn import prompt_store
prompt_store.set_many({
    "proactive.kind.care": "你写的新 care 指令……",
    "chat.system": "覆盖后的 SYSTEM……",
})
print(prompt_store.snapshot()["overrides"])
PY
```

- 保存前先 `prompt_store.validate(payload)`；未知键/非字符串/超长会被拒绝。
- `set_many({key: ""})` 删除单个覆盖；`delete_all()` 删除整个文件回默认。
- 覆盖生效日志：`prompt_override`（按 version 去重，JSONL）；读失败写节流
  `prompt_store_error` warning。

## 4. 改完先「试生成」

改提示词会影响线上人格，**不要直接上线**：

1. 先只改 `prompts.json`，在本地/测试环境触发一次 `proactive.tick` 或一次对话；
2. 检查输出：是否仍符合禁物理起居、不编造、结尾无标点、长度/口语约束；
3. 通过后再让线上热加载生效；有问题直接删除 `agi/data/prompts.json` 回滚（无需重启）。

## 5. 测试

```bash
python -m pytest agi/tests/test_prompt_store.py \
  agi/tests/test_proactive_voice.py agi/tests/test_proactive_style.py \
  agi/tests/test_proactive_content.py agi/tests/test_chat_inner.py -q
```

覆盖/回滚相关用例在 `agi/tests/test_prompt_store.py`；基线字节锁仍由原有
proactive 测试与 `test_chat_inner.py` 守护。

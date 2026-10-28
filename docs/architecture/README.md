# architecture/ · 架构与设计规格（00–14）

> 15 份设计文档，2026-10-23 定稿，**此后未再修改**。定位是「设计意图」，不是验收合同。
> 施工图入口：[`PLAN.md`](PLAN.md)；执行路线：[`../plans/19-整体开发路线.md`](../plans/19-整体开发路线.md)。

---

## 一、阅读顺序（时间有限就按这个）

1. **00** 总览与架构 —— 先知道有哪五层、哪些做不到
2. **02** 记忆与人格规格 —— 全系统最该先钉死的地方
3. **03** 在线对话与防幻觉 —— 一次回复的完整时序
4. **05** 路线图与验收 —— 里程碑、指标、风险
5. **08** 总结与最小验证 —— 一页纸看完前面七份
6. 其余按需：01（管线）、04（学习）、06/07（技能/错误）、09–14（情绪/信任/主动/调度）

---

## 二、索引

| 文件 | 一句话 | 对应实现（`selflearn/`） | 审计 ✅/项 |
|---|---|---|---|
| `00-总览与架构.md` | 目标、边界、五层、模块清单 | 整体 | 7/49 |
| `01-数据与媒体管线.md` | S0–S9 离线工序与验收 | `ingest` `claims` `gates` `search` `profile` `image_understanding` `emoji` | 5/38 |
| `02-记忆与人格规格.md` | 表结构、七道闸门、检索、人格分层、遗忘 | `claims` `gates` `memory` `retrieval` `persona_layers` | 0/25 |
| `03-在线对话与防幻觉.md` | 时序、预算、两段式、校验、降级 | `chat` `generation` `guard` `citation` `nli` | 3/22 |
| `04-学习闭环与打分.md` | 七维打分、60 桶、Thompson、提案、灰度 | `scoring` `proposals` | 6/19 |
| `05-路线图与验收.md` | M0–M4、总指标、风险登记册、待决策 | — | 3/43 |
| `06-技能习得与认知闸门.md` | 技能树 0–5、三重闸门、Learn Loop 八步 | `skilltree` `skills` `learn_loop` `research` `verifier` | 2/20 |
| `07-从错误中学习.md` | 错误账本、五类归因、三次法则、错误预算 | `errors` `error_three` | 2/15 |
| `08-总结与最小验证.md` | 十条判断、E0 实验 | — | 2/13 |
| `09-情绪状态与边界系统.md` | 五维状态、状态机、五级边界、防操控 | `state` `affect` | 6/26 |
| `10-记仇与信任账本.md` | Mood/Trust/Memory、G1–G4、恢复曲线 | `trust` | 6/28 |
| `11-分享欲与主动表达.md` | drive、四环节、时机、冷却 | `sharing` `share_store` | 1/18 |
| `12-主动行为的评分与决策.md` | score/confidence/LCB、四象限、探索预算 | `initiative` `proactive` | 0/14 |
| `13-弱验证技能怎么学.md` | checklist、成对比较、黄金集、裁判校准 | `weak_validation` `blind` `drift` `evaluation` | 2/17 |
| `14-按需运行与唤醒机制.md` | L0/L1/L2、self-scheduling、S0–S4 开关 | `wake` `scheduler` `ticker` `autonomy` | 0/10 |

> 审计口径：357 个可判定项里 ✅45（12.6%）、⚠️181、❌99、📊30。
> 详情：[`../archive/sources/wx_box/docs/00-14_完成度审计_20261024.md`](../archive/sources/wx_box/docs/00-14_完成度审计_20261024.md)。

---

## 三、权威边界

| 问题 | 以谁为准 |
|---|---|
| 系统应该长什么样 | 本目录 00–14 |
| 实际能跑成什么样 | `../archive/sources/wx_box/docs/00-14_完成度审计…` |
| 硬件/延迟/数据的事实 | `../plans/18-wx_box合并评估.md` |
| 现在按什么做 | `../plans/19-整体开发路线.md` |
| 嗓音层的接口细节 | `../plans/16-嗓音层_v1_可执行方案.md` |

**原则：不改写 00–14 的原文。** 修订一律写进 `plans/`，并在对应文档顶部加一行指向。

---

## 四、和 `plans/` 的分工

```
architecture/ 设计意图：要做什么、为什么这么做
plans/   执行决策：现在做什么、先做哪一步、什么算过
../archive/sources/ 参考实现：别人怎么做的、哪些能抄
```

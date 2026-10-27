# tools/train：6GB 显存上的 2B LoRA SFT smoke

RTX 4050 Laptop（**6141 MiB 专用显存**，桌面常驻 ~350–460 MiB）+ 16GB RAM 上，
把 `Qwen/Qwen3.5-2B`（多模态，纯文本用法）的 LoRA SFT 环境跑通，并用
`agi/soul/data/soul_sft_v0.jsonl`（W4-A 灵魂蒸馏集，context → InnerState JSON）
做 20 步 smoke，为 W4 正式训练定参数。

> 环境、模型、缓存、输出、日志全部在仓库外 `E:\agent\`；仓库内只新增本目录。
> 旧的 `C:\Users\Administrator\.soul-train` 已废弃删除。

## 一、目录结构

```
E:\agent\
├── venv312\                  # Python 3.12.6 venv
├── models\
│   └── Qwen3.5-2B\           # 用户手动从 ModelScope 下载（单文件 safetensors 4.55GB）
├── cache\
│   ├── pip\                  # PIP_CACHE_DIR
│   ├── modelscope\           # MODELSCOPE_CACHE
│   └── wheelhouse\           # torch cu124 本地 wheel（aliyun 镜像下载）
├── output\
│   └── smoke_adapter\        # 20 步 smoke 产出的 LoRA adapter（65MB）
└── logs\
    ├── smoke_run3.log        # 首次跑通的完整输出
    ├── smoke_run4.log        # 可重跑验证（+ nvidia-smi 采样）
    ├── smoke_lora.jsonl      # 每次 smoke 的 summary + 每步记录（追加）
    └── model_files.json      # ModelScope 文件清单/大小校验
```

## 二、环境复现（已验证）

```powershell
# 1) venv（py 3.12.6）
py -3.12 -m venv E:\agent\venv312

# 2) 纯 Python 依赖（清华镜像）
$env:PIP_CACHE_DIR='E:\agent\cache\pip'
E:\agent\venv312\Scripts\python.exe -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple `
  "transformers==5.20.2" tokenizers safetensors "datasets==5.1.0" "modelscope==1.40.2" `
  jinja2 sentencepiece pyyaml regex typer tqdm numpy Pillow
E:\agent\venv312\Scripts\python.exe -m pip install --no-deps "peft==0.21.2" "accelerate==1.15.0" psutil
E:\agent\venv312\Scripts\python.exe -m pip install --no-deps "bitsandbytes==0.50.2"   # 4bit 降级备选

# 3) torch 2.6.0+cu124：官方源约 1.4MB/s 太慢，改用阿里云 wheel 镜像后 curl 下载再本地安装
curl -L -C - -o "E:\agent\cache\wheelhouse\torch-2.6.0+cu124-cp312-cp312-win_amd64.whl" `
  "https://mirrors.aliyun.com/pytorch-wheels/cu124/torch-2.6.0%2Bcu124-cp312-cp312-win_amd64.whl"
E:\agent\venv312\Scripts\python.exe -m pip install --no-deps `
  "E:\agent\cache\wheelhouse\torch-2.6.0+cu124-cp312-cp312-win_amd64.whl"

# 4) 验证
E:\agent\venv312\Scripts\python.exe -c "import torch;print(torch.__version__, torch.cuda.is_available(), torch.version.cuda)"
```

实测版本（2026-10-25）：

| 组件 | 版本 |
|---|---|
| Python | 3.12.6 |
| torch | **2.6.0+cu124**（`torch.cuda.is_available() == True`，CUDA 12.4 runtime） |
| transformers | 5.20.2（内置 `qwen3_5` 支持） |
| peft | 0.21.2 |
| accelerate | 1.15.0（`device_map={"": 0}` 需要） |
| datasets | 5.1.0 |
| bitsandbytes | 0.50.2（win_amd64 wheel 可装；仅作 4bit 降级备选，smoke 未用） |
| 驱动 | 616.92 / CUDA UMD 13.4（向下兼容 cu124） |

## 三、模型

- **路径**：`E:\agent\models\Qwen3.5-2B`（用户手动从 ModelScope 下载，非脚本下载）
- **Repo**：ModelScope `Qwen/Qwen3.5-2B`（没有单独的 `-Instruct`；HuggingFace 不可达）
- **权重**：`model.safetensors-00001-of-00001.safetensors` = 4,548,221,488 B（4.55GB，单文件）
- **结构**：`model_type=qwen3_5`、`Qwen3_5ForConditionalGeneration`、dense 2.27B、
  24 层 = 18 层 linear_attention + 6 层 full_attention、hidden 2048、
  vocab 248320、tie_word_embeddings，**带 vision_config（多模态）**
- **校验**：`safetensors` 读取 632 个 tensor，含 `model.language_model.*` 与 `model.visual.*`；
  `AutoConfig` / `AutoTokenizer(Qwen3_5Tokenizer)` 正常。`AutoProcessor` 会因缺 torchvision
  失败——纯文本训练不需要，**不要装 torchvision**。
- 文本备选：`Qwen/Qwen3-1.7B`（纯文本 4.06GB）放 `E:\agent\models\Qwen3-1.7B`，
  用 `--model-path` 指过去即可；2B 已跑通，无需降级。

## 四、数据与样本构造

数据：`agi/soul/data/soul_sft_v0.jsonl`（v0 pilot 300 条，每条
`{id, source, context, user_msg, target(InnerState), teacher, ts}`）。

构造（`smoke_lora.py::build_messages/encode_sample`）：

```
messages = [system] + [context 中的 user/assistant 历史...] + [user_msg]
assistant = json.dumps(target, ensure_ascii=False)   # 纯 InnerState JSON，不含 think 标签
```

- chat template 用 `apply_chat_template(..., add_generation_prompt=True, enable_thinking=False)`；
  Qwen3.5 即使关思考，prompt 结尾也会带 `<|im_start|>assistant\n<think>\n\n</think>\n\n`，
  这段属于 **prompt 前缀（label=-100）**，只有 target JSON + eos 计 loss。
- 超长时先丢最旧的历史消息，再硬截断（保留 system 头 + 尾部 user 句），保证 target 不被截掉。
- 50 条样本 token 长度：avg 275 / max 341 / min 234（prompt avg 150），seq 512 余量充足。

## 五、怎么跑

```powershell
cd C:\Users\Administrator\Desktop\SoulAgent

# 0) 只验证数据/分词/长度（秒级，不加载权重）
E:\agent\venv312\Scripts\python.exe tools\train\smoke_lora.py --dry-run

# 1) 20 步 smoke（bf16、LoRA r16 α32、seq512、bs1、grad ckpt，视觉塔自动 offload 到 CPU）
E:\agent\venv312\Scripts\python.exe tools\train\smoke_lora.py --max-steps 20 --save-adapter

# 2) 显存不够时的降级（按顺序试）
... --max-seq-len 256
... --load-4bit            # QLoRA，bitsandbytes 0.50.2 已装
... --model-path E:\agent\models\Qwen3-1.7B
```

脚本默认：模型 `E:\agent\models\Qwen3.5-2B`、日志 `E:\agent\logs\smoke_lora.jsonl`、
adapter `E:\agent\output\smoke_adapter`；每步打印 `[step] {step,loss,step_seconds,
peak_alloc_gb,peak_reserved_gb,tokens,prompt_tokens,answer_tokens}`，结束打印 `[smoke] SUMMARY`。

## 六、Smoke 实测结果（2026-10-25）

两轮均 20/20 步跑完，loss 明显下降，脚本可重跑：

| 指标 | run3（01:20） | run4（重跑 + nvidia-smi 采样） |
|---|---|---|
| 参数 | bf16 / r16 / α32 / seq512 / bs1 / grad ckpt / 视觉塔 CPU | 同左 |
| 20 步总耗时 | 27.16s（1.358 s/step） | 25.16s（1.258 s/step） |
| loss 首 → 末 | **2.2534 → 0.9025**（mean 1.0246） | **2.2534 → 0.9139**（mean 1.0262） |
| 峰值显存（torch allocated） | 4.779 GB | 4.779 GB |
| 峰值显存（torch reserved） | 5.578 GB | 5.578 GB |
| **峰值显存（nvidia-smi 整卡）** | — | **5849 / 6141 MiB = 5.71 GB** |
| 可训练参数 | 16.82M（372 个 LoRA 模块） | 同左 |
| 权重加载态显存 | alloc 4.123GB → 视觉塔挪 CPU 后 3.505GB → LoRA 后 3.568GB | 同左 |

- 峰值时整卡仅剩 ~292 MiB 余量：**能跑，但很紧**；跑训练时关掉浏览器/游戏/其他占 GPU 程序。
- 训练期间整卡利用率正常，未见 OOM、未见 NaN，loss 单调有波动地下降。
- adapter 落盘 `E:\agent\output\smoke_adapter\`（adapter_model.safetensors 67MB + config），
  可复用于推理验证。
- 降级路径实测：`--max-seq-len 256` 跑 2 步正常，峰值 reserved 从 5.578GB 降到
  **4.885GB**（余量翻倍），1.62 s/step（短序列反而略慢，因为固定开销占比高）。
- 完整日志：`E:\agent\logs\smoke_run3.log` / `smoke_run4.log` / `smoke_seq256.log`；
  每步记录：`E:\agent\logs\smoke_lora.jsonl`。

## 七、正式训练推荐参数（6GB）

以 v0 300 条为例（正式集可能换 v1，先按 300 估）：

```powershell
# 结构同 smoke，只是步数/轮数/累积变大：脚本加 --max-steps 900 --grad-accum 8 即可
E:\agent\venv312\Scripts\python.exe tools\train\smoke_lora.py `
  --max-steps 900 --grad-accum 8 --lr 1e-4 --max-seq-len 512 `
  --save-adapter E:\agent\output\soul_lora_v0
```

| 参数 | 建议值 | 理由 |
|---|---|---|
| LoRA | r=16, α=32, dropout=0.05 | smoke 实测 16.82M 可训练参数，显存/效果平衡 |
| dtype | bf16 | 4050 支持 bf16；fp16 无 GradScaler 易 NaN |
| seq | 512（若想留余量用 384） | v0 实测最长 341 token；512 已含余量 |
| batch | 1（显存硬约束） | 峰值已 5.71GB，bs>1 必 OOM |
| grad accumulation | 8（等效 bs=8） | 补 batch 太小的问题；每步仍 ~1.3s |
| lr | 1e-4（LoRA 常用 1e-4~2e-4） | smoke 用 2e-4；正式略降更稳 |
| epochs | 3（300 条 ≈ 900 步 ≈ 20 分钟） | 数据量小，多轮易过拟合，靠 val 选 |
| grad checkpoint | 必须开（默认开） | 关掉峰值会 +2GB 直接 OOM |
| 视觉塔 | 必须 offload（默认） | 省 ~0.6GB；加 `--keep-vision` 必 OOM |
| 评估 | 每 epoch 用 val 29 条算 loss/JSON 合法率 | v0 的 val 切分已存在 |
| 4bit QLoRA | 仅当需要 bs>1/seq>512 时 | 权重 4.55GB→~1.4GB，但慢一些、精度略降 |

时间估算：300 条 × 3 epoch = 900 步 × ~1.3s ≈ **20 分钟/次**（不含加载 ~10s）。

## 八、6GB 显存注意事项 / 踩过的坑

1. **peft 的 `target_modules` 传字符串是 `re.fullmatch`**，pattern 要写成
   `r".*(q_proj|...|out_proj)$"`（加 `.*` 前缀），否则报 `NoMatchingPeftModuleError`。
   也可直接传 list（peft 用 endswith 匹配，最稳）。
2. **多模态模型必须用 `AutoModelForImageTextToText` 加载**（`AutoModelForCausalLM`
   对 `qwen3_5` 映射到 base model，不是 chat 模型）。
3. **视觉塔要 offload 到 CPU**（`model.model.visual.to("cpu")`），省约 0.6GB；
   纯文本 batch 不会调用它。注意：offload 后 `next(model.parameters()).device`
   会是 **cpu**（视觉参数排在前面），设备要用 `model.get_input_embeddings().weight.device`。
4. **Qwen3.5 关思考仍留空 `<think>` 块**：prompt 前缀原样保留并 mask，target 里不要塞 think 标签。
5. torch 官方 `download.pytorch.org` 实测 ~1.4MB/s（2.5GB 要半小时），阿里云
   `https://mirrors.aliyun.com/pytorch-wheels/cu124` 约 4.6–8MB/s，快 3–5 倍；
   先 curl 到本地再 `pip install --no-deps` 最稳（可断点续传）。
6. ModelScope 直连可用、HuggingFace 不可达；本脚本不访问 HF
   （kernel 走 torch 兜底 / 本地模型路径），`HF_HOME` 已指到 `E:\agent\cache\hf`。
7. Windows 上 `PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True` 会告警
   "not supported on this platform"，不影响运行，脚本未强制设置。
8. 桌面常驻占 350–460 MiB，训练时峰值只剩 ~292 MiB 余量——**训练前先关占 GPU 的程序**。
9. Qwen3.5 的线性注意力层在没装优化 kernel 时走 PyTorch 参考实现，stderr 会打印
   `causal_conv1d_fn / chunk_gated_delta_rule falling back ... much slower`；这是**正常降级**，
   1.26s/step 就是这条路径的实测值。Windows 上 `causal_conv1d` / `flash-linear-attention`
   （依赖 triton）没有可靠 wheel，不建议为 smoke 折腾；若以后换 Linux 训练机可装来提速。
10. `use_cache=True is incompatible with gradient checkpointing` 告警无害——脚本已把
   `use_cache` 关掉；smoke 是训练模式，不需要 KV cache。

# 多卡设备接入方案（8× NVIDIA V100 16GB）

> 目标读者：设备到货后按本文逐项执行的人（Linux 运维 / 项目 owner）。
> 状态：**方案稿，基于 2026-10-29 的当前仓库与公开资料**；所有「版本 / 参数」在目标机器上启动前必须用官方
> 文档与 `--help` 再确认一次（vLLM/驱动迭代很快，本文把需要现场核对的点标成 ⚠）。
> 约束：只新增本文档，不改业务代码；当前主链仍走 `agi/data/llm_router.json` 的 OpenAI 兼容接口，
> **换服务端不需要改业务代码**，只改路由配置。

### 本文结构与执行顺序（照着读/照着做）

| 步骤 | 章节 | 看完要做什么 |
|---|---|---|
| 1 | §0 结论 + 到货第一天验证 | 先确认 vLLM 能在 sm70 启动；不通过就改走 llama.cpp 退路 |
| 2 | §1 目标与显存预算 | 选定组合 A/B/C/D（32B 统一 / 7B 高并发 / 32B 文本 / 训练窗口） |
| 3 | §3 部署 | 驱动 → vLLM → 模型下载 → 起 8130 → systemd |
| 4 | §6 验证与基准 | 健康检查 → 文本/识图 → 并发阶梯 → 记录指标 |
| 5 | §4 迁移 | 改 llm_router.json（新增 backend、定向切用途）→ 分阶段 → 回滚预案 |
| 6 | §5 硬件/机房 | 电源/散热/二手验收（到货前就要确认） |
| 7 | §7 风险清单 | 把「待用户拍板」的 6 个问题定下来 |

---

## 0. 先读：结论与到货第一天的动作

### 0.1 一句话结论

**8×V100 16GB（128GB 显存）能把“识图 + 文本 + 高并发”统一到一台 vLLM 上：首推
Qwen2.5-VL-32B-Instruct（FP16, TP=8）单实例；想要高并发识图则用 Qwen2.5-VL-7B
（FP16 单卡放不下，必须 TP=2，可做 4 组副本）。**

不能直接跑的：

- **Qwen2.5-VL-72B / Qwen2.5-72B 文本**：FP16 权重约 146GB > 128GB，8×16GB **放不下**
  （除非 32GB 卡或量化；V100 上量化内核支持很差，见 §2.3）。
- 任何 **bf16** 期望：V100（Volta, sm_70）**没有 bf16**，vLLM 必须显式 `--dtype half`。
- **最新版 vLLM**：截至本文编写时，vLLM **0.20+ 官方已不带 sm_70 kernel**，V100 上装最新版会
  “no kernel image is available”。需要**锁定仍支持 sm_70 的版本**（社区反馈 ≤0.18.x 可用，
  ⚠ 以官方 release/PR 为准），或最后退路用 llama.cpp 多卡。**这是本方案最大风险，先验证再谈其他。**

### 0.2 到货第一天（30 分钟验证，别急着上业务）

| # | 动作 | 命令/判据 | 通过标准 |
|---|---|---|---|
| 1 | 确认 8 卡都识别、型号/显存/ECC 正常 | `nvidia-smi -L`；`nvidia-smi --query-gpu=index,name,memory.total,ecc.mode.current --format=csv` | 8× `Tesla V100 ... 16384MiB`，ECC enabled |
| 2 | 确认拓扑（PCIe 还是 NVLink） | `nvidia-smi topo -m` | 记下 `NV#` 或 `PHB/SYS`；TP=8 前必须知道 |
| 3 | 确认驱动/CUDA 可用 | `nvidia-smi`；`python -c "import torch;print(torch.__version__, torch.cuda.get_device_capability())"` | capability `(7, 0)` |
| 4 | 单卡验证 vLLM 能否在 sm70 起服务（1B/3B 小模型即可） | `vllm serve Qwen/Qwen2.5-0.5B-Instruct --dtype half --gpu-memory-utilization 0.6 --port 8199` 然后 `curl :8199/v1/models` | 返回 200；若 sm70 报错 → 按 §2.3 换版本/llama.cpp |
| 5 | 单卡 Qwen2.5-VL-3B 识图验证 | §6.3 的图片请求脚本 | 返回非空中文描述 |
| 6 | 8 卡 TP 小模型冒烟 | `--tensor-parallel-size 8` 起 3B/7B | 8 卡都吃显存、请求成功 |
| 7 | 记录基线（温度/功耗/噪音） | `nvidia-smi dmon`、`nvidia-smi -q -d POWER,TEMPERATURE` | 满载 < 85°C，功耗符合 §5 预算 |

> 只有 1–3 通过才开始搬生产；4 失败则本方案改走 llama.cpp 路线（§2.4），不要硬装最新 vLLM。

---

## 1. 目标与显存预算

### 1.1 目标场景

| 场景 | 模型（建议） | 并发目标 | 延迟目标（待实测校准） | 显存结论 |
|---|---|---|---|---|
| 微信对话识图（主用） | Qwen2.5-VL-32B-Instruct FP16 TP8 | 8–12 路 8k 上下文 | 单图 + 短答 P95 ≤ 6s | ✅ 推荐 |
| 高并发识图/批量理解 | Qwen2.5-VL-7B-Instruct FP16 TP2 ×4 副本 | 16–32 路 | 单图 P95 ≤ 3s | ✅ 推荐 |
| 长文本摘要/关键词/聚类 | Qwen2.5-32B-Instruct FP16 TP8 | 8–16 路 | 1k 输入/256 输出 P95 ≤ 5s | ✅ |
| 高量便宜提取 | 现有 8125 Qwen2.5-VL-3B（4050）或 V100 上 3B 副本 | 高 | 现有水平 | ✅ 保留旧服务最省事 |
| 小模型灵魂（状态 JSON） | 0.5B（8123 现有 GGUF） | 单用户 | ~0.5–1s/条 | ✅ 保留 4050/CPU，别搬 |
| LoRA 批量训练 | 0.5B / 1.5B / 3B / 7B（LoRA/QLoRA） | — | — | ✅ 7B 需 ZeRO/FSDP；32B 需验证 |
| 同时“识图+聊天+训练” | — | — | — | ❌ 32B 服务占满 8 卡；**V100 不支持 MIG**，只能分时/分卡 |

### 1.2 显存估算公式（先记这个，再查表）

```
FP16 权重(GB)  ≈ 参数量(B) × 2 / 1e9          # 1B 参数 ≈ 2GB
KV(GB/序列)    ≈ 2 × layers × kv_heads × head_dim × 2B × tokens / 1e9
                 # 系数 2 = K 和 V；2B = FP16；head_dim = hidden_size / num_attention_heads
可用显存       ≈ 卡数 × 16GB × gpu_memory_utilization(建议 0.90)
单请求可用量   ≈ 可用显存 − 权重 − 激活/运行时（约 1–3GB/卡）
```

> 下表按公开结构估算（Qwen2.5 系 GQA），**不是实测**；到货后用附录 A 的脚本读真实
> `config.json` 重算 KV。所有 FP16 数字未含视觉编码器/激活/fragmentation。

### 1.3 单模型显存预算表（8×16GB=128GB，FP16）

| 模型 | 参数量(约) | FP16 权重 | KV/8k 上下文/序列(约) | 最小 TP | 8×16GB 可行性 | 建议 |
|---|---|---|---|---|---|---|
| Qwen2.5-VL-3B | 3.8B | 7.6GB | ~0.3GiB | 1 | ✅ 可 8 副本 | 用于高并发识图替身；或留在 4050 |
| Qwen2.5-VL-7B | 8.3B | 16.6GB | ~0.45GiB | **2**（单卡 16GB 放不下权重） | ✅ TP2 ×4 副本 | 高并发识图首选 |
| Qwen2.5-VL-32B | 33B | ~66GB | ~2GiB | **8**（TP4=16.5GB/卡 已超） | ✅ 单实例 | **主推识图+文本统一** |
| Qwen2.5-VL-72B | 73B | ~146GB | ~2.5GiB | 8（仍不够） | ❌ | 16GB 卡不可行 |
| Qwen2.5-32B（文本） | 32.8B | ~66GB | ~2GiB | **8** | ✅ 单实例 | 长文本主力 |
| Qwen2.5-14B（文本） | 14.7B | ~29GB | ~0.7GiB | 4（TP2 每卡 14.7GB 太满） | ✅ TP4 | 性价比备选 |
| Qwen2.5-7B（文本） | 7.6B | ~15.2GB | ~0.45GiB | **2**（单卡 15.2+KV 会 OOM） | ✅ TP2/TP4 | 轻量文本 |
| 灵魂 0.5B（现有） | 0.5B | ~1GB | 很小 | 1 | ✅ | **保留 8123，不搬** |

> 结论：**32B 是这套卡的“最右端”**；72B 必须 32GB 卡或量化，不在本方案内。

### 1.4 推荐组合（按用途二选一）

| 组合 | GPU 分配 | 能跑什么 | 并发估算（理论/目标） | 适合 |
|---|---|---|---|---|
| **A. 统一 32B VL（推荐）** | 8 卡全部 TP8 → 8130 | 识图 + 文本 + 提取 | 8k 上下文理论 ~24 路；目标 **8–12 路** | 想“一个端点全包”，延迟优先 |
| **B. 高并发 7B VL** | 4 副本 × TP2 → 8130–8133（或一个 DP+TP 实例） | 高并发识图；文本用小模型/旧 8125 | 8k 理论每副本 ~55 路；目标 **16–32 路** | 批量识图/多用户 |
| **C. 32B 文本 + 旧 3B 识图** | 8 卡 TP8 → 8130 文本；识图仍走 4050/8125 | 长文本，不增加识图并发 | 文本 8–16 路 | 先解决文本吞吐 |
| **D. 训练窗口** | 训练时 8 卡全给训练，推理临时回落 4050/API | LoRA 0.5B–7B | — | 每周/夜间训练 |

> 8 张卡不能同时“32B 服务 + 32B 训练”；V100 无 MIG，没有硬隔离。上线前要定“服务优先/训练窗口”。

### 1.5 KV 容量与并发估算（粗算，供采购/压测目标参考）

| 配置 | 权重占用/卡 | KV 可用/卡(0.9 util) | KV/token/卡 | 8k 并发理论 | 实践目标 |
|---|---|---|---|---|---|
| 32B VL TP8 | 8.25GB | ~6.1GB | 32KiB | ~24 路 | **8–12 路** |
| 7B VL TP2（每副本） | 8.3GB | ~6.1GB | 28KiB | ~55 路 | 每副本 **12–20 路** |
| 32B 文本 TP8 | 8.25GB | ~6.1GB | 32KiB | ~24 路 | 8–16 路 |
| 3B TP1 | 7.6GB | ~6.8GB | 36KiB | ~24 路 | 8–16 路/卡 |

> 表内已扣权重，未扣视觉 token（一张 1024×1024 图约 1.3k tokens，见 §6.4）与碎片；**以 §6 压测为准**。

### 1.6 训练可行性（同机 LoRA，仅参考）

| 模型 | 方式 | 显存结论 | 建议 |
|---|---|---|---|
| 0.5B / 1.5B | LoRA（FP16） | 单卡/双卡足够，8 卡是加速 | ✅ 首选 |
| 3B | LoRA | 4–8 卡 | ✅ |
| 7B | LoRA + ZeRO-2/3 或 FSDP | 8 卡可行；单卡 QLoRA 需验证 bitsandbytes 在 Volta 的 4bit 支持 | ⚠ 先小步验证 |
| 32B | LoRA + FSDP/ZeRO-3，FP16 base 分片（8.25GB/卡） | 理论可行、工程复杂 | ❌ 不作为首期目标 |
| 7B 全参微调 | — | 优化器状态 + 梯度远超单卡，8×16GB 很紧 | ❌ 不推荐；用 LoRA |

> V100 **无 bf16**：训练必须 FP16 + loss scaling（`torch.cuda.amp.GradScaler` 或框架 `--fp16`）；
> 不要照抄 A100/bf16 教程的 `--bf16`。

---

## 2. 服务端选型：llama.cpp vs vLLM

### 2.1 对比表

| 维度 | llama.cpp（现状，8123/8125） | vLLM（推荐新增） |
|---|---|---|
| 多卡方式 | `--split-mode layer/row` + `--tensor-split`，层切分为主 | **张量并行（TP）**、数据并行（DP）、流水线并行 |
| 连续批处理 | 有限（`-np` 并行槽，调度简单） | ✅ PagedAttention + continuous batching，多并发吞吐强 |
| OpenAI 兼容 | `llama-server` 提供 `/v1/chat/completions` | ✅ 原生 OpenAI 兼容（`/v1/chat/completions`、`/v1/models`） |
| 多模态 | GGUF + `--mmproj`（Qwen VL 系可用但版本敏感） | ✅ Qwen2.5-VL 官方 recipe（image_url 输入） |
| 量化 | GGUF Q4/Q5/Q8 强项 | FP16 为主；AWQ/GPTQ/FP8 内核在 V100 上支持差 |
| V100(sm70) | ✅ 支持（CUDA 后端） | ⚠ **新版已不带 sm70**，要锁旧版本；这是最大不确定性 |
| 部署复杂度 | 低（单 exe + gguf） | 中（Python venv / 驱动 / 版本矩阵） |
| 适合 | 单卡/小模型/低并发/边缘设备 | 多卡/高并发/多模态/统一 OpenAI 服务 |

### 2.2 为什么多卡建议 vLLM

1. **张量并行是“模型并行”**：32B FP16（66GB）单卡放不下，TP=8 才能把权重切到 8 张 16GB；
   llama.cpp 的 `--split-mode layer` 对单请求延迟/吞吐的扩展性不如 TP+continuous batching。
2. **连续批处理**：多用户/批量识图时 vLLM 的 PagedAttention 能显著提高吞吐；llama.cpp 的
   `-np` 槽位数有限。
3. **多模态一等公民**：Qwen2.5-VL 有官方 vLLM recipe（`--limit-mm-per-prompt`、
   `--mm-encoder-tp-mode` 等），Web/OpenAI 客户端传 `image_url` 即可。
4. **与项目现状兼容**：项目所有本地模型都走 OpenAI 兼容接口（`backends.local.base_url`），
   vLLM 直接替换 base_url，业务零改。

### 2.3 V100（Volta, sm_70）注意事项（必须逐条确认）

| 事项 | 说明 / 处理 |
|---|---|
| **bf16 不存在** | V100 只有 FP16；vLLM 启动必须 `--dtype half`（或 `float16`）。默认 `auto` 可能选 bf16 导致报错/精度问题 |
| **无 FlashAttention** | FlashAttention 需要 sm80+；V100 上 vLLM 会选兼容 attention backend。若启动报 attention backend 不可用，显式设置 `VLLM_ATTENTION_BACKEND=XFORMERS`（⚠ 以当前版本官方 env 文档为准，见附录 E） |
| **无 TF32 / FP8 加速** | 不要按 A100 的 `--dtype bfloat16`/FP8 配置；FP8 是 Hopper/Ada 的东西 |
| **精心优化量化不可用** | AWQ/GPTQ-Marlin/INT4 等高性能内核通常要求 sm75/sm80；V100 上要么回退慢内核要么直接报错。**首期只用 FP16**，不要拿量化省显存 |
| **vLLM 版本** | 截至 2026-10：vLLM **0.20+ 的官方轮子不再带 sm_70**（论坛/PR 结论）；社区反馈 **≤0.18.x** 仍可用。⚠ 到货后按 §0.2 第 4 步实测，再锁定版本 |
| **社区 fork** | 若锁定版本功能不够，可用社区 sm70 分支（如 `vllm-sm70`）救急；⚠ 非官方，升级/安全需自担 |
| **PCIe vs NVLink** | PCIe V100 **无 NVLink**，TP=8 的 all-reduce 走 PCIe（gen3 x16 ≈32GB/s），TP 扩展效率有限；SXM2 版才有 300GB/s NVLink。采购前确认 |
| **驱动/系统** | 用数据中心驱动（V100 仍在数据中心驱动支持范围），Ubuntu 22.04/24.04 优先；Windows 原生不支持 vLLM |

### 2.4 如果 vLLM sm70 走不通：llama.cpp 多卡退路

```bash
# 例：32B 级模型 GGUF Q4_K_M，8 卡层切分，OpenAI 兼容端口 8130
CUDA_VISIBLE_DEVICES=0,1,2,3,4,5,6,7 \
llama-server -m /data/models/Qwen2.5-VL-32B-Instruct-Q4_K_M.gguf \
  --mmproj /data/models/Qwen2.5-VL-32B-Instruct-mmproj-f16.gguf \
  -ngl 99 --split-mode layer --tensor-split 1,1,1,1,1,1,1,1 \
  -c 16384 -np 1 --host 0.0.0.0 --port 8130 --jinja
```

> 说明：`--split-mode`/`--tensor-split`/`--mmproj` 是 llama.cpp 现有参数（以 `llama-server --help` 为准）；
> Q4 会牺牲精度，且 32B Q4 GGUF 在 V100 上的 token/s 需实测。这条路**只作退路**，不作为首选。

---

## 3. 部署步骤（Linux 优先；Windows 见 §3.7）

### 3.1 系统与驱动

```bash
# 1) 系统：Ubuntu 22.04 / 24.04 LTS（推荐）；内核/BIOS 开 above-4G decoding、IOMMU 按厂商建议
sudo apt update && sudo apt install -y build-essential curl git python3-venv python3-pip

# 2) 安装数据中心驱动（版本以 NVIDIA 官方 V100 支持列表为准；下面 <版本> 需替换）
sudo ubuntu-drivers list
sudo apt install -y nvidia-driver-<版本>-server
sudo reboot

# 3) 验证（8 卡、ECC、拓扑、CUDA）
nvidia-smi
nvidia-smi -L
nvidia-smi --query-gpu=index,name,memory.total,ecc.mode.current,driver_version --format=csv
nvidia-smi topo -m
```

> 不要手装随机 runfile；优先发行版/厂商提供的 server 驱动，避免与 CUDA 运行时错位。

### 3.2 Python 环境与 vLLM 安装

```bash
python3 -m venv /opt/vllm
source /opt/vllm/bin/activate
python -m pip install -U pip
pip install -U uv

# ⚠ 先查可用版本，选一个“明确仍带 sm70 kernel”的版本（社区反馈 ≤0.18.x；以官方 release 为准）
pip index versions vllm
VLLM_VER=<你验证过的版本>
uv pip install "vllm==${VLLM_VER}"

# 记录版本，写进验收报告
python -c "import vllm, torch; print('vllm', vllm.__version__, 'torch', torch.__version__)"
```

> 安装后**不要立刻上 32B**：按 §0.2 第 4 步先用 0.5B/3B 在单卡验证 sm70 能启动；否则 32B 加载几十 GB
> 才发现不兼容，浪费时间。

### 3.3 模型下载（国内网络）

```bash
# 方式 A：ModelScope（国内推荐）
pip install modelscope
modelscope download --model Qwen/Qwen2.5-VL-32B-Instruct \
  --local_dir /data/models/Qwen2.5-VL-32B-Instruct

# 方式 B：HuggingFace CLI（新版命令；旧版为 huggingface-cli download）
export HF_ENDPOINT=https://hf-mirror.com
pip install -U "huggingface_hub[cli]"
hf download Qwen/Qwen2.5-VL-32B-Instruct \
  --local-dir /data/models/Qwen2.5-VL-32B-Instruct
```

> 磁盘预算：32B FP16 权重约 66GB（下载缓存 + 最终目录 ≈ 130GB）；7B 约 17GB；预留 ≥300GB NVMe 给模型+日志+训练数据。

### 3.4 启动 vLLM（32B VL，TP=8，推荐组合 A）

```bash
export CUDA_VISIBLE_DEVICES=0,1,2,3,4,5,6,7
# V100：显式 FP16；attention backend 如报错再放开下面这行（以当前版本支持为准）
# export VLLM_ATTENTION_BACKEND=XFORMERS

vllm serve Qwen/Qwen2.5-VL-32B-Instruct \
  --served-model-name qwen2.5-vl-32b \
  --host 0.0.0.0 --port 8130 \
  --tensor-parallel-size 8 \
  --dtype half \
  --max-model-len 16384 \
  --gpu-memory-utilization 0.90 \
  --limit-mm-per-prompt '{"image":2,"video":0}' \
  --enable-prefix-caching
```

参数说明（均为 vLLM 官方 CLI 参数）：

| 参数 | 作用 | V100 备注 |
|---|---|---|
| `--tensor-parallel-size 8` | 8 卡张量并行 | PCIe 卡扩展效率有限，先实测 |
| `--dtype half` | FP16 | **必须**，V100 无 bf16 |
| `--max-model-len` | 上下文上限 | 32B 建议 16384；越大会吃 KV |
| `--gpu-memory-utilization 0.90` | 显存利用率 | OOM 就降到 0.85；并发高可试 0.92 |
| `--limit-mm-per-prompt` | 每 prompt 图片/视频数 | 控制图片并发，32B 建议 image=2 |
| `--enable-prefix-caching` | 前缀缓存 | 压测时按 recipe 关掉（`--no-enable-prefix-caching`） |

> 版本差异：若你的 vLLM 版本不支持某个参数，`vllm serve --help` 会直接列出；**不要照抄报错参数**。

### 3.5 启动 vLLM（7B VL 高并发，组合 B）

```bash
export CUDA_VISIBLE_DEVICES=0,1,2,3,4,5,6,7
# 8 卡 = TP2 × DP4（每副本 2 卡）；DP+TP 组合以当前版本 parallel 文档为准
vllm serve Qwen/Qwen2.5-VL-7B-Instruct \
  --served-model-name qwen2.5-vl-7b \
  --host 0.0.0.0 --port 8130 \
  --tensor-parallel-size 2 --data-parallel-size 4 \
  --dtype half --max-model-len 16384 \
  --gpu-memory-utilization 0.90 \
  --limit-mm-per-prompt '{"image":2,"video":0}'
```

或更稳的“多实例”方式（每对卡一个进程，端口 8130–8133）：

```bash
for pair in "0,1 8130" "2,3 8131" "4,5 8132" "6,7 8133"; do
  gpus=${pair% *}; port=${pair#* }
  CUDA_VISIBLE_DEVICES=$gpus vllm serve Qwen/Qwen2.5-VL-7B-Instruct \
    --served-model-name qwen2.5-vl-7b --host 0.0.0.0 --port $port \
    --tensor-parallel-size 2 --dtype half --max-model-len 16384 \
    --gpu-memory-utilization 0.90 --limit-mm-per-prompt '{"image":2,"video":0}' &
done
```

> 多实例更易回滚/单副本重启，适合先上线；但要在上层（nginx/网关或路由侧）做负载分配。

### 3.6 systemd 常驻（可选）

```ini
# /etc/systemd/system/vllm-qwen25vl32b.service
[Unit]
Description=vLLM Qwen2.5-VL-32B (8x V100)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=vllm
Environment=CUDA_VISIBLE_DEVICES=0,1,2,3,4,5,6,7
# Environment=VLLM_ATTENTION_BACKEND=XFORMERS
ExecStart=/opt/vllm/bin/vllm serve /data/models/Qwen2.5-VL-32B-Instruct \
  --served-model-name qwen2.5-vl-32b --host 0.0.0.0 --port 8130 \
  --tensor-parallel-size 8 --dtype half --max-model-len 16384 \
  --gpu-memory-utilization 0.90 --limit-mm-per-prompt '{"image":2,"video":0}'
Restart=on-failure
RestartSec=20
LimitNOFILE=65535

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now vllm-qwen25vl32b
journalctl -u vllm-qwen25vl32b -f
```

### 3.7 Windows / WSL2 方案（不推荐但可行）

| 方案 | 说明 |
|---|---|
| 专用 Linux 服务器（推荐） | 8 卡 V100 服务器装 Ubuntu，Windows 通过局域网调 `http://<linux-ip>:8130/v1` |
| Windows + WSL2 | 需 Win11 + 最新驱动 + `wsl --update`；WSL2 可透传 GPU，但**多卡 PCIe P2P/NCCL 行为与原生 Linux 不同**，TP=8 性能/稳定性需实测；vLLM 官方不提供 Windows 原生支持 |
| Windows + llama.cpp | 现有 8123/8125 方式；多卡 `--split-mode` 可用，但不解决高并发/32B 统一服务 |

> 若必须 Windows：先 WSL2 单卡跑通 vLLM，再逐级加卡；不要假设 8 卡 TP 与 Linux 等价。

---

## 4. 迁移步骤（业务零改，只改路由）

### 4.1 现状（来自 `agi/data/llm_router.json`）

| backend/用途 | 现状 |
|---|---|
| `backends.local` | `kind: openai`，`base_url: http://127.0.0.1:8125/v1`，`model: local-vl-3b`，`timeout_s: 60` |
| `backends.api` | `kind: deepseek`，`model: deepseek-flash`，`timeout_s: 30` |
| 本地用途 | `ingest_summary`、`comment_cluster`、`keyword_extract`、`relevance_score`、`selflearn_image_understanding`（识图，失败回落 api） |
| API 用途 | `chat_reply`、`mood_judge`、`video_understanding`（must_api）、`share_copy`、`persona_reflect` |
| `transcribe` | `local_whisper`（独立后端） |

### 4.2 改路由配置（推荐“新增后端 + 定向切换”，不要直接改 local）

编辑 `agi/data/llm_router.json`（改前先备份）：

```bash
cp agi/data/llm_router.json agi/data/llm_router.json.bak_$(date +%Y%m%d_%H%M%S)
```

增量（示例）：

```json
{
  "backends": {
    "local": {
      "kind": "openai",
      "base_url": "http://127.0.0.1:8125/v1",
      "model": "local-vl-3b",
      "timeout_s": 60
    },
    "local_gpu8": {
      "kind": "openai",
      "base_url": "http://192.168.1.50:8130/v1",
      "model": "qwen2.5-vl-32b",
      "timeout_s": 120,
      "api_key": ""
    }
  },
  "purposes": {
    "selflearn_image_understanding": {
      "backend": "local_gpu8",
      "fallback": ["local", "api"],
      "max_tokens": 500,
      "temperature": 0.2
    },
    "ingest_summary": {
      "backend": "local_gpu8",
      "fallback": ["local", "api"],
      "max_tokens": 400
    },
    "comment_cluster": {"backend": "local_gpu8", "fallback": ["local", "api"]},
    "keyword_extract": {"backend": "local_gpu8", "fallback": ["local", "api"], "max_tokens": 200}
  }
}
```

要点：

- `local_gpu8` 是**新增别名**，原 `local`（8125）完全保留 → 回滚只需改回用途。
- `api_key` 可选：若 vLLM 加了 `--api-key`，这里填同一个值；不加则留空。
- **router 自带 mtime 热加载**：`agi/llm/router.py::default_router()` 按配置文件 mtime 判断是否重建，
  改完 JSON 后下一次调用即生效；如果担心进程内旧对象，重启 801 网关最稳。
- `chat_reply`（微信最终回复）仍走 `api`：先不要把主对话切本地 32B，质量/延迟要单独评测再定。

### 4.3 分阶段切换

| 阶段 | 动作 | 验证 | 回滚 |
|---|---|---|---|
| 0 并跑 | 新 vLLM 起在 8130，业务不切；直接 curl 验证文本/识图 | 200 + 合理中文 | 不停旧服务 |
| 1 识图 | 只把 `selflearn_image_understanding` 切 `local_gpu8`，fallback 旧 local/api | 控制台/日志里识图请求走 8130；失败自动回落 | 改回 `local` 或恢复备份 |
| 2 提取 | 切 `ingest_summary`/`keyword_extract`/`comment_cluster` | 延迟/错误率不升 | 同上 |
| 3 可选 | 评估 `chat_reply`、`mood_judge` 是否走本地 32B（先做盲评） | 质量/延迟达标 | 恢复 api |

### 4.4 配置校验与探活（不用真发微信）

```bash
cd <SoulAgent 仓库>
C:/Python314/python.exe - <<'PY'
from agi.llm import router
cfg = router.load_config()
print("warnings:", router.validate_config(cfg))
print("backends:", router.available_backends())          # 会探活 8130/8125
print("purposes:", router.purposes())
# 单用途冒烟（会真实打模型；中文短问题）
print(router.chat("selflearn_image_understanding",
                  [{"role": "user", "content": "用一句话说明 1+1=2"}]))
PY
```

> `available_backends()` 对 `openai` 后端探 `GET {base_url}/models`；vLLM 的 `/v1/models` 可被探活。
> 识图真实请求见 §6.3。

### 4.5 8123 灵魂模型（0.5B）怎么处理

| 方案 | 说明 | 延迟 |
|---|---|---|
| **保留 4050（推荐）** | 8123 不动；0.5B 服务与主模型解耦，V100 训练/重启不影响灵魂 | ~0.5–1s/条（GGUF） |
| 迁到 V100 的 vLLM（同一 TP8 实例） | 可行但没收益：0.5B 在 16GB 上是浪费；若服务重启会连带影响 | 跨网络 + 排队 |
| 迁到 CPU | `llama-server -m soul.gguf -ngl 0 --port 8123`；省 GPU，延迟上升 | 更慢，仅兜底 |
| 迁到 V100 独立小实例 | 8 卡都在 32B 时无卡可分；除非用组合 B（7B）才有空闲卡 | 取决于组合 |

### 4.6 回滚方案（必须写进值班手册）

```bash
# 1) 恢复路由备份（router 按 mtime 自动重载；或重启 801）
cp agi/data/llm_router.json.bak_YYYYMMDD_HHMM /data/.../agi/data/llm_router.json
# 2) 确认旧 8125/8123 还在（它们从没停）
curl -s http://127.0.0.1:8125/v1/models
curl -s http://127.0.0.1:8123/v1/models
# 3) 看用量日志确认没有大批 8130 请求
tail -n 50 agi/data/llm_usage.jsonl
# 4) 极端情况：停 vLLM 服务（不影响业务，router fallback 会回旧 local/api）
sudo systemctl stop vllm-qwen25vl32b
```

> 回滚不影响业务代码；旧 8123/8125 保留是本次迁移的硬前提，**不要拆旧服务**。

---

## 5. 硬件 / 机房

### 5.1 V100 版本差异（采购前确认）

| 版本 | 显存 | 功耗 | 互联 | 建议 |
|---|---|---|---|---|
| V100 PCIe | 16GB / 32GB | 250W | 无 NVLink，PCIe gen3 x16 | 标准服务器易装；TP=8 走 PCIe，扩展一般 |
| V100 SXM2 | 16GB / 32GB | 300W | NVLink 2.0，8 卡 300GB/s | 8 卡训练/TP 更合适，但需专用 baseboard/DGX 类服务器 |

> 用户计划“8×V100 16GB”：先确认是 **PCIe 还是 SXM2**、是否有 NVLink 背板——这直接决定
> TP=8 是否值得，以及 §6 的并发预期。

### 5.2 服务器配置建议

| 部件 | 要求 | 说明 |
|---|---|---|
| CPU/主板 | 双路 Xeon/EPYC，≥64 lanes PCIe | 8×x16 需要足够 lane；不足时用 PLX/riser，注意带宽 |
| 内存 | ≥128GB（建议 256GB） | 模型加载/OS cache/训练数据 |
| 系统盘 | NVMe ≥1TB | 模型+日志+venv；32B FP16 约 66GB/份 |
| 数据盘 | NVMe ≥2–4TB | 训练数据/dataset/checkpoint |
| 电源 | **≥3000W（2×1600W 冗余）** | 8×250W=2kW + 主机 400–600W；**单 1600W 不够** |
| 市电 | 220V ≥16A 建议 32A 独立回路 | 16A 理论上限 3.5kW，长期 80% 负载只剩 2.8kW，紧 |
| 网络 | 1–10GbE | Windows 侧走局域网调 8130；注意只在内网 |
| 机箱/散热 | 4U 8-GPU 风道 / 空调 | 8 卡满载 ~2.5kW 热量；机房温度 < 28°C |
| 噪音 | 70–85dBA（4U 高转速风扇） | 不要放卧室/办公室开放区 |
| UPS | 建议在线式 | 训练中断可恢复，但避免硬断电损坏 |

### 5.3 二手 V100 风险（重点）

| 风险 | 检查方法 | 处理 |
|---|---|---|
| HBM 显存错误 | `nvidia-smi -q -d ECC`；`cuda-memtest`/gpu-burn 跑 30min | ECC 有不可纠正错误 → 退换 |
| 矿卡/高温历史 | 看风扇/背板痕迹、HBM 温度；`nvidia-smi -q -d TEMPERATURE` | 温度异常/掉卡 → 退换 |
| Xid 错误 | `dmesg | grep -i xid`；长跑压测 | 有 Xid 79/48 等 → 退换 |
| SXM2 兼容 | 确认 baseboard/整机型号、NVLink 背板 | 不要买“只有 SXM2 卡没有服务器” |
| 驱动/EOL | V100 是 2017 年产品，数据中心驱动支持窗口有限 | 锁定能用驱动版本；不要随便升级 |
| 保修/来源 | 二手商保修、序列号 | 至少 3 个月换新；来源不明不买 |

---

## 6. 验证与基准（到货后照此验收）

### 6.1 服务健康

```bash
curl -s http://<host>:8130/v1/models | python -m json.tool
curl -s -X POST http://<host>:8130/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"qwen2.5-vl-32b","messages":[{"role":"user","content":"用一句话回复：在吗"}],"max_tokens":64}'
```

验收：HTTP 200；`choices[0].message.content` 非空中文；`usage` 有 tokens 计数；GPU 显存被 8 卡分占。

### 6.2 文本并发压测（项目自带口径）

vLLM 官方 bench（参数见附录 E 的 Benchmark CLI）：

```bash
vllm bench serve \
  --host <host> --port 8130 \
  --backend openai-chat --endpoint /v1/chat/completions \
  --model qwen2.5-vl-32b \
  --dataset-name random --random-input-len 1000 --random-output-len 256 \
  --num-prompts 200 --request-rate 8
```

或项目侧自写脚本（不依赖 vllm 包，只靠 httpx；附录 B）：

| 阶梯 | 并发 | 观测 |
|---|---|---|
| 1 | 1 | 单请求 TTFT/TPOT（基线） |
| 2 | 4 | P95 TTFT、吞吐 tok/s |
| 3 | 8 | 是否 OOM/排队；P95 是否 ≤2×单路 |
| 4 | 16 | 稳定性（10 分钟）；显存/温度/功耗 |

记录表（每档必填）：

| 指标 | 单请求 | 并发 4 | 并发 8 | 并发 16 | 目标 |
|---|---|---|---|---|---|
| TTFT P50 / P95 (ms) | | | | | 32B ≤ 2000 / 4000（待校准） |
| TPOT (ms/token) | | | | | ≤ 150（V100 32B 估算值，以实测为准） |
| 输出吞吐 (tok/s) | | | | | ≥ 300（目标，待校准） |
| 失败率 | | | | | 0 |
| 显存峰值/卡 | | | | | < 15GB |
| 温度/整机功耗 | | | | | < 85°C / < 3kW |

### 6.3 识图请求（OpenAI image_url 格式）

```python
# save as tools/bench/check_vl.py；用 idea/.venv 或系统 python 均可（httpx）
import base64, json, time
import httpx

IMG = r"<AGENT_HOME>/datasets/sample.jpg"       # 任意一张本地图
b64 = base64.b64encode(open(IMG, "rb").read()).decode()
payload = {
    "model": "qwen2.5-vl-32b",
    "max_tokens": 256,
    "messages": [{"role": "user", "content": [
        {"type": "text", "text": "用一两句话描述这张图，并指出有没有文字。"},
        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
    ]}],
}
t0 = time.perf_counter()
r = httpx.post("http://<host>:8130/v1/chat/completions",
               json=payload, timeout=120)
print(r.status_code, round((time.perf_counter()-t0)*1000), "ms")
print(json.dumps(r.json(), ensure_ascii=False)[:800])
```

### 6.4 图片 token 预算（决定 `--max-model-len` 与并发）

- Qwen2.5-VL 默认 28×28 像素对应 1 个视觉 token（patch 14 + merge 2）；1024×1024 ≈ **1.3k tokens**，
  1280×720 ≈ 1.2k，超大图会到 4k–16k tokens（取决于 `max_pixels`）。
- 建议：客户端把长边压到 ≤1280px；服务端 `--limit-mm-per-prompt '{"image":2,"video":0}'`；
  `--max-model-len 16384` 给单图+历史留足空间。
- 若要做“多图对比”，把 `image` 上限调到 4，同时把 `max-model-len`/并发降下来。

### 6.5 成本对比（电费 vs API；关键结论：**当前用量下本地不省钱**）

假设（可替换成你的实际价）：电价 ¥0.6/kWh；API 按仓库历史估算 ¥1/M 输入 + ¥4/M 输出，混合约 ¥2/M tokens。

| 项 | 估算 | 说明 |
|---|---|---|
| 8 卡空闲 | 0.5–0.7kW × 24h × 0.6 ≈ **¥7–10/天** | 服务器待机也不便宜 |
| 服务 8h + 空闲 16h | (2.5×8 + 0.6×16) × 0.6 ≈ **¥17.8/天** ≈ ¥534/月 | 白天服务 |
| 满载 24h | 2.8kW × 24h × 0.6 ≈ **¥40/天** ≈ ¥1210/月 | 训练/压测 |
| 当前 API 用量 | `budget.daily_api_tokens=300000` → ≈ 9M tokens/月 × ¥2/M ≈ **¥18/月** | 远低于电费 |
| 电费平衡点 | ≈ 19.2 元/天 ÷ 2 元/M ≈ **~10M tokens/天** | 低于此量，买卡是买“隐私/离线/训练/并发” |
| 硬件折旧 | 月成本 ≈ 采购价 / 36（3 年） | 二手价按实际填 |

> 所以本方案的价值主张是：**数据不出内网 + 可训练自己的模型 + 8 卡并发识图 + 不再受 API 限流**，
> 而不是“比 API 便宜”。采购决策应按这三点算。

---

## 7. 风险与未决问题清单

| # | 风险/问题 | 严重度 | 触发条件 | 缓解 |
|---|---|---|---|---|
| 1 | vLLM 新版不支持 V100 sm70 | **高** | 装最新版启动报 no kernel image | 锁定 ≤0.18.x（社区结论，⚠官方核实）；先用 0.5B 验证；备选 llama.cpp |
| 2 | 单 1600W 电源不够 | **高** | 8 卡满载 2.5–3kW | 2×1600W 冗余或 3000W；独立 32A 回路 |
| 3 | 二手 V100 硬件故障 | 高 | HBM/Xid/温度异常 | §5.3 验收；保修；先单卡压测 24h |
| 4 | PCIe 无 NVLink，TP8 扩展差 | 中 | 32B TP8 吞吐低于预期 | 采 SXM2/NVLink 服务器；或改用 7B DP 副本 |
| 5 | 32B + 训练争抢 GPU | 中 | 同时跑 | 分时（训练窗口）或组合 B 留卡；V100 无 MIG |
| 6 | bf16 误用 | 中 | 默认 dtype=auto | 强制 `--dtype half`；训练用 FP16+loss scaling |
| 7 | 量化在 V100 不可用/很慢 | 中 | 想用 AWQ/GPTQ/FP8 省显存 | 首期不用量化；72B 不在 16GB 卡方案内 |
| 8 | Qwen2.5-VL 版本与 vLLM 版本不匹配 | 中 | 新模型/旧 vLLM | 查 recipe + `vllm serve --help`；锁版本后写进部署记录 |
| 9 | 图片 token 爆上下文 | 中 | 大图/多图 | 客户端限长边；`--limit-mm-per-prompt`；`--max-model-len 16384` |
| 10 | 路由配置写错导致降级频繁 | 中 | base_url/model 写错 | 改前备份；`validate_config` + `available_backends`；fallback 链保留 |
| 11 | Windows 上强上 vLLM/WSL2 多卡 | 中 | 不想用 Linux | 优先独立 Linux 服务器；WSL2 仅验证用 |
| 12 | 网络暴露 8130 到公网 | 中 | 图省事 0.0.0.0+端口映射 | 只在内网/防火墙白名单；必要时 vLLM `--api-key` |
| 13 | 散热/噪音/机房电费 | 低 | 8 卡满载 | 空调/风道；放设备间；UPS |
| 14 | V100 驱动 EOL | 低 | 未来升级 | 记录驱动/CUDA/vLLM 版本，冻结升级节奏 |
| 15 | Qwen 模型许可/合规 | 低 | 对外分发 | 遵循 Qwen 许可；仅自用；不上传用户数据 |

**待用户拍板的问题**

1. 8 卡服务器是 **PCIe V100 还是 SXM2/NVLink**？后者成本高但 TP/训练差距大。
2. 主机系统选 **Ubuntu 专用机** 还是坚持 Windows+WSL2？
3. 主对话 `chat_reply` 是否允许切本地 32B（需要先做质量盲评，不建议首期切）？
4. 业务量目标（日 token / 并发识图峰值）？决定选组合 A 还是 B。
5. 训练与服务是“分时”还是“分卡”（组合 B 可留 2–4 卡做训练）？
6. 预算上限（整机/电费/二手保修），以及能否接受二手 V100 风险。

---

## 附录 A：显存/KV 计算脚本（按真实 config 重算）

```python
# python calc_kv.py /data/models/Qwen2.5-VL-32B-Instruct/config.json
import json
import sys

cfg = json.load(open(sys.argv[1], encoding="utf-8"))
# Qwen2.5-VL config 里语言模型字段可能在顶层，也可能在 text_config
t = cfg.get("text_config") if isinstance(cfg.get("text_config"), dict) else cfg
layers = t["num_hidden_layers"]
hidden = t["hidden_size"]
heads = t["num_attention_heads"]
kv_heads = t.get("num_key_value_heads") or heads
head_dim = hidden // heads
params_b = t.get("num_parameters", 0)

kv_bytes = 2 * layers * kv_heads * head_dim * 2  # K+V, FP16
print(f"layers={layers} hidden={hidden} heads={heads} kv_heads={kv_heads} head_dim={head_dim}")
print(f"KV per token = {kv_bytes/1024:.1f} KiB")
for ctx in (4096, 8192, 16384, 32768):
    print(f"  ctx {ctx:>6}: {kv_bytes*ctx/1e9:.2f} GB/序列")
print("权重约 = 参数量(B) × 2GB；用 config/tokenizer 里的实际参数量或权重文件大小核对")
```

## 附录 B：最小冒烟 + 并发脚本（不装 vllm 包）

```python
# python smoke_vllm.py --base http://<host>:8130/v1 --model qwen2.5-vl-32b
import argparse, asyncio, time
import httpx

async def one(client, base, model, i):
    body = {"model": model, "max_tokens": 64, "messages": [
        {"role": "user", "content": f"第 {i} 条：用一句话回复‘在’。"}]}
    t0 = time.perf_counter()
    r = await client.post(base + "/chat/completions", json=body)
    return r.status_code, (time.perf_counter() - t0) * 1000, len(r.text)

async def main(base, model, concurrency):
    async with httpx.AsyncClient(timeout=120) as c:
        t0 = time.perf_counter()
        out = await asyncio.gather(*[one(c, base, model, i) for i in range(concurrency)])
        wall = (time.perf_counter() - t0) * 1000
    codes = [o[0] for o in out]
    lat = sorted(o[1] for o in out)
    print(f"n={concurrency} ok={codes.count(200)}/{len(codes)} "
          f"p50={lat[len(lat)//2]:.0f}ms p95={lat[int(len(lat)*0.95)-1]:.0f}ms wall={wall:.0f}ms")

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--concurrency", type=int, default=8)
    a = ap.parse_args()
    asyncio.run(main(a.base.rstrip("/"), a.model, a.concurrency))
```

## 附录 C：`llm_router.json` 迁移 diff 模板

```diff
@@ backends
+    "local_gpu8": {
+      "kind": "openai",
+      "base_url": "http://<linux-ip>:8130/v1",
+      "model": "qwen2.5-vl-32b",
+      "timeout_s": 120
+    },
@@ purposes.selflearn_image_understanding
-      "backend": "local",
-      "fallback": ["api"],
+      "backend": "local_gpu8",
+      "fallback": ["local", "api"],
```

> 改完立刻 `validate_config` + 一次单用途调用；`agi/data/llm_usage.jsonl` 里确认 backend 字段变成新别名。

## 附录 D：命令速查

| 目的 | 命令 |
|---|---|
| 8 卡状态 | `nvidia-smi` / `nvidia-smi topo -m` |
| 服务健康 | `curl -s http://HOST:8130/v1/models` |
| 文本冒烟 | `curl -s -X POST http://HOST:8130/v1/chat/completions -H 'Content-Type: application/json' -d '{"model":"qwen2.5-vl-32b","messages":[{"role":"user","content":"在吗"}]}'` |
| 显存/温度/功耗 | `nvidia-smi dmon -s pucvmet` |
| 进程显存 | `nvidia-smi --query-compute-apps=pid,used_memory --format=csv` |
| 服务日志 | `journalctl -u vllm-qwen25vl32b -f` |
| 路由探活 | `python -c "from agi.llm import router; print(router.available_backends())"` |
| 路由改后重载 | 无需重启（router 按 mtime）；不确定时重启 801 网关 |
| 回滚 | 恢复 `llm_router.json.bak_*`；旧 8125/8123 不停 |

## 附录 E：参考资料（⚠ 版本相关，执行前复核）

1. vLLM Qwen2.5-VL 官方 recipe（`vllm serve`、`--limit-mm-per-prompt`、`--mm-encoder-tp-mode`、bench）：
   <https://docs.vllm.ai/projects/recipes/en/latest/Qwen/Qwen2.5-VL.html>
2. vLLM Engine Arguments（`--tensor-parallel-size`/`--dtype`/`--max-model-len`/`--gpu-memory-utilization`）：
   <https://docs.vllm.ai/en/stable/configuration/engine_args/>
3. vLLM 环境变量（attention backend 等，具体版本支持以页面为准）：
   <https://docs.vllm.ai/en/stable/configuration/env_vars/>
4. vLLM GPU 安装（驱动/CUDA/预构建镜像）：
   <https://docs.vllm.ai/en/stable/getting_started/installation/gpu/>
5. vLLM 并行与扩展（TP/DP 说明）：
   <https://docs.vllm.ai/en/latest/serving/parallelism_scaling/>
6. vLLM Benchmark CLI（`vllm bench serve`）：
   <https://docs.vllm.ai/en/latest/benchmarking/cli/>
7. V100 与 vLLM 0.20+ 不支持 sm70 的社区讨论（本文风险 #1 的来源）：
   <https://discuss.vllm.ai/t/support-for-v100-sm-70-on-vllm-0-20/2605>
8. V100 for Volta 社区分支（最后手段，非官方）：
   <https://github.com/jajmangold/vllm-sm70>
9. NVIDIA Tesla V100 数据表（功耗/显存/互联）：
   <https://www.nvidia.com/content/dam/en-zz/zh_tw/Solutions/design-visualization/grid-vpc-vapps/volta-v100-datasheet-update-a4-636418-r4-tw.pdf>
10. Qwen2.5-VL 模型卡（输入格式/图片像素建议）：
    <https://huggingface.co/Qwen/Qwen2.5-VL-32B-Instruct>

---

## 变更记录

| 日期 | 内容 |
|---|---|
| 2026-10-29 | 初稿：8×V100 16GB 目标/选型/部署/迁移/硬件/验证/风险 |

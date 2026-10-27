# -*- coding: utf-8 -*-
"""PyTorch + PEFT 的 LoRA SFT 最小骨架（可直接在 IDEA 里改/跑）。

用法：改 BASE_MODEL / DATA 后点「运行」；显存不足把 LOAD_4BIT 改 True。
依赖：pip install torch transformers peft accelerate [bitsandbytes]
"""
import json
import torch
from torch.utils.data import DataLoader, Dataset
from transformers import AutoModelForCausalLM, AutoTokenizer
from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training

BASE_MODEL = r"E:\agent\models\Qwen2.5-0.5B-Instruct"
DATA = "data/train/sft_sample.jsonl"      # 每行 {"messages":[...], "response":"..."}
OUT = "runs/idea_lora_smoke"
LOAD_4BIT = False
MAX_LEN = 512
STEPS = 20
LR = 2e-4

device = "cuda" if torch.cuda.is_available() else "cpu"
print("device:", device)

tok = AutoTokenizer.from_pretrained(BASE_MODEL, trust_remote_code=True)
if tok.pad_token is None:
    tok.pad_token = tok.eos_token
model = AutoModelForCausalLM.from_pretrained(
    BASE_MODEL, torch_dtype=torch.bfloat16 if device == "cuda" else torch.float32,
    device_map=None, trust_remote_code=True,
)
if LOAD_4BIT:
    from transformers import BitsAndBytesConfig
    model = AutoModelForCausalLM.from_pretrained(
        BASE_MODEL, quantization_config=BitsAndBytesConfig(
            load_in_4bit=True, bnb_4bit_quant_type="nf4",
            bnb_4bit_compute_dtype=torch.bfloat16),
        device_map="auto", trust_remote_code=True,
    )
    model = prepare_model_for_kbit_training(model)
else:
    model = model.to(device)

lora = LoraConfig(r=16, lora_alpha=32, lora_dropout=0.05, bias="none",
                  task_type="CAUSAL_LM",
                  target_modules=["q_proj", "k_proj", "v_proj", "o_proj",
                                  "gate_proj", "up_proj", "down_proj"])
model = get_peft_model(model, lora)
model.print_trainable_parameters()


class ChatDataset(Dataset):
    def __init__(self, path):
        self.rows = [json.loads(x) for x in open(path, encoding="utf-8") if x.strip()]

    def __len__(self):
        return len(self.rows)

    def __getitem__(self, i):
        r = self.rows[i]
        msgs = list(r.get("messages") or []) + [{"role": "assistant", "content": r["response"]}]
        text = tok.apply_chat_template(msgs, tokenize=False)
        enc = tok(text, truncation=True, max_length=MAX_LEN, padding=False)
        # 只对 assistant 段计算 loss：这里用最简单做法（真实场景按模板切分 label）
        enc["labels"] = enc["input_ids"][:]
        return {k: torch.tensor(v) for k, v in enc.items()}


loader = DataLoader(ChatDataset(DATA), batch_size=1, shuffle=True)
opt = torch.optim.AdamW([p for p in model.parameters() if p.requires_grad], lr=LR)
model.train()
step = 0
for batch in loader:
    step += 1
    batch = {k: v.to(device) for k, v in batch.items()}
    out = model(**batch)
    loss = out.loss
    loss.backward()
    opt.step()
    opt.zero_grad()
    print(f"step {step} loss {loss.item():.4f}")
    if step >= STEPS:
        break
model.save_pretrained(OUT)
print("adapter saved to", OUT)

# -*- coding: utf-8 -*-
"""自学习对话体网页：聊天 + 反馈 + 用户记忆/自人格面板。"""
from __future__ import annotations

import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import acceptance, db, errors, learn_flow, learn_loop, memory, personality, proactive, report, scheduler, skills, skilltree, state, trust
from .chat import SelfLearnAgent
from .config import DB_PATH

_LOCK = threading.Lock()
_AGENT: SelfLearnAgent | None = None


def _agent() -> SelfLearnAgent:
    global _AGENT
    if _AGENT is None:
        _AGENT = SelfLearnAgent(DB_PATH)
    return _AGENT


PAGE = r"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>Self-Learning Agent</title><style>
:root{--bg:#0e1116;--panel:#161b22;--line:#2a313c;--text:#e6edf3;--dim:#8b949e;--blue:#2f81f7;--ai:#21262d}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.6 -apple-system,'Segoe UI',sans-serif}
.wrap{max-width:1080px;margin:0 auto;padding:18px}.top{display:flex;justify-content:space-between;align-items:center}
h1{font-size:18px;margin:0}.sub{color:var(--dim);font-size:12px}.grid{display:grid;grid-template-columns:1fr 300px;gap:12px;margin-top:12px}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px}
#chat{height:60vh;overflow:auto;display:flex;flex-direction:column;gap:8px}
.msg{max-width:80%;padding:8px 11px;border-radius:10px;white-space:pre-wrap;word-break:break-word}
.msg.user{align-self:flex-end;background:var(--blue)}.msg.ai{align-self:flex-start;background:var(--ai);border:1px solid var(--line)}
.meta{font-size:10px;color:var(--dim);margin-top:2px}.fb{display:flex;gap:4px;margin-top:4px}.fb button{background:#30363d;color:var(--text);border:0;border-radius:6px;padding:2px 7px;font-size:11px;cursor:pointer}
.row{display:flex;gap:8px;margin-top:10px}textarea{flex:1;min-height:58px;background:var(--ai);border:1px solid var(--line);color:var(--text);border-radius:8px;padding:8px;resize:vertical}
button.primary{background:var(--blue);color:#fff;border:0;border-radius:8px;padding:0 16px;font-weight:600;cursor:pointer}
.item{border-bottom:1px dashed var(--line);padding:5px 0;font-size:12px}.item b{color:#9ecbff}.tag{display:inline-block;background:var(--ai);border:1px solid var(--line);border-radius:12px;padding:0 7px;font-size:10px;color:var(--dim);margin:2px 2px 2px 0}
.hint{color:var(--dim);font-size:12px;margin-top:8px}
</style></head><body><div class="wrap">
<div class="top"><div><h1>Self-Learning Agent</h1><div class="sub">它从自己的回复结果里学习聊天技巧；记忆由它自己决定，人格与用户信息分层。</div></div>
<div><button id="proactive" style="background:#30363d;color:var(--text);border:0;border-radius:8px;padding:6px 12px">它想主动说</button> <button id="internal" style="background:#30363d;color:var(--text);border:0;border-radius:8px;padding:6px 12px">内部</button> <button id="export" style="background:#30363d;color:var(--text);border:0;border-radius:6px;padding:6px 10px">导出脑</button> <button id="report" style="background:#30363d;color:var(--text);border:0;border-radius:8px;padding:6px 12px">成长报告</button> <button id="refresh" style="background:#30363d;color:var(--text);border:0;border-radius:8px;padding:6px 12px">刷新</button></div></div>
<div class="grid"><div class="panel"><div id="chat"></div>
<div class="row" style="align-items:center"><input id="image" type="file" accept="image/*" style="font-size:12px"><span class="sub" id="fileHint"></span></div>
<div class="row"><textarea id="text" placeholder="和它聊一句；也可以发一张图…"></textarea><button class="primary" id="send">发送</button></div>
<div class="hint" id="hint">每轮它自己决定记什么；你点反馈后它才会更新自己的策略/人格。</div></div>
<div class="panel" id="sidePanel" style="display:none"><div><b>它自己的记忆</b> <span class="sub" id="memCount"></span></div><div id="memories" style="max-height:26vh;overflow:auto"></div>
<div style="margin-top:12px"><b>自画像</b> <span class="sub" id="personaCount"></span></div><div id="persona"></div>
<div style="margin-top:12px"><b>聊天策略（已学会）</b> <span class="sub" id="skillCount"></span></div><div id="skills"></div>
<div style="margin-top:12px"><b>技能树</b> <span class="sub">L3 才可交付</span></div><div id="skillTree"></div>
<div style="margin-top:12px"><b>当前状态</b> <span class="sub" id="stateLine"></span> <button id="repair" style="background:#30363d;color:var(--text);border:0;border-radius:6px;padding:2px 7px;font-size:11px">修复一次</button></div>
<div id="proactiveBox" style="margin-top:8px"></div>
<div id="reportBox" style="margin-top:8px;max-height:30vh;overflow:auto;font-size:11px;color:var(--dim);white-space:pre-wrap"></div>
<div class="hint">人格只从它自己的行动结果形成，不由用户定义。</div></div></div></div>
<script>
const $=id=>document.getElementById(id),esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function add(role,text,turnId){const d=document.createElement('div');d.className='msg '+role;const t=document.createElement('div');t.textContent=text;d.appendChild(t);
const m=document.createElement('div');m.className='meta';m.textContent=role==='user'?'你':'AI';d.appendChild(m);
if(role==='ai'&&turnId){const f=document.createElement('div');f.className='fb';['good','ok','bad'].forEach(r=>{const b=document.createElement('button');b.textContent=r;b.onclick=()=>fb(turnId,r);f.appendChild(b)});d.appendChild(f)}
$('chat').appendChild(d);$('chat').scrollTop=$('chat').scrollHeight;return d}
async function api(path,body){const r=await fetch(path,{headers:{'Content-Type':'application/json'},method:body?'POST':'GET',body:body?JSON.stringify(body):undefined});if(!r.ok)throw new Error(await r.text());return r.json()}
async function fb(turnId,reaction){try{const r=await api('/api/feedback',{turn_id:turnId,reaction});$('hint').textContent=`反馈 ${reaction}：策略已更新；自人格反思 ${r.reflected?.written||0} 条`;await load()}catch(e){$('hint').textContent='反馈失败：'+e.message}}
async function load(){try{const s=await api('/api/status');$('memCount').textContent=`（${s.memories.length}）`;$('personaCount').textContent=`（${s.persona.length}）`;$('skillCount').textContent=`（${s.variants.length}）`;$('stateLine').textContent=`心情 ${s.state.self.mood} / 分享欲 ${s.state.self.sharing} / 关系 ${s.trust.stage}`;
$('memories').innerHTML=s.memories.map(m=>`<div class="item"><b>${esc(m.kind)}</b> ${esc(m.content)} <button data-mid="${esc(m.id)}" style="background:transparent;color:#f85149;border:0;font-size:11px;cursor:pointer">删除</button><div class="meta">${esc(m.evidence)}</div></div>`).join('')||'<div class="sub">还没有记忆</div>';
$('memories').querySelectorAll('button[data-mid]').forEach(b=>b.onclick=()=>forgetMemory(b.dataset.mid));
$('persona').innerHTML=s.persona.map(p=>`<div class="item"><b>${esc(p.key)}</b> ${esc(p.value)}</div>`).join('')||'<div class="sub">还没有形成自画像</div>';
$('skills').innerHTML=s.variants.map(v=>`<div class="item"><span class="tag">${v.status==='gray'?'灰度':''}Lv${v.skill_level??0} · α${v.alpha} β${v.beta}</span> ${esc(v.hint)}</div>`).join('')||'<div class="sub">还没有学会聊天策略；自然回应只是基线</div>';
$('skillTree').innerHTML=(s.skill_tree||[]).map(n=>`<div class="item"><span class="tag">L${n.level} ${esc(n.status)}</span> ${esc(n.name)}</div>`).join('');
$('chat').innerHTML='';const h=await api('/api/history');h.forEach(m=>add(m.role==='user'?'user':'ai',m.text));}catch(e){$('hint').textContent='读取失败：'+e.message}}
function readImage(file){return new Promise((res,rej)=>{const fr=new FileReader();fr.onload=()=>res(fr.result);fr.onerror=rej;fr.readAsDataURL(file)})}
async function send(){const text=$('text').value.trim();const file=$('image').files[0];if(!text&&!file)return;let image=null;if(file){try{image=await readImage(file)}catch(e){$('hint').textContent='读图失败';return}}
$('text').value='';if($('image'))$('image').value='';$('fileHint').textContent='';add('user',text||'[图片]');$('send').disabled=true;try{const r=await api('/api/chat',{text,image});if(r.skipped){$('hint').textContent='它这次没回：'+(r.skip_reason||'心情不太好')}else{const bs=(r.bubbles&&r.bubbles.length)?r.bubbles:[r.reply];bs.forEach((b,i)=>add('ai',b,i===0?r.turn_id:null));$('hint').textContent='回复已生成。给反馈后它会学习策略。'}await load()}catch(e){add('ai','出错：'+e.message)}finally{$('send').disabled=false}}
$('image').addEventListener('change',()=>{const f=$('image').files[0];$('fileHint').textContent=f?'已选择：'+f.name+'（会自动交给模型识图）':''});
async function proactive(){try{const r=await api('/api/proactive',{});if(!r.available){$('proactiveBox').innerHTML='<div class="sub">现在不想主动说：'+esc(r.reason||'')+'</div>';return}
$('proactiveBox').innerHTML=`<div class="item"><b>它想主动说</b><br>${esc(r.text)}<div class="meta">${esc(r.reason)} · 候选，不自动发送</div><button data-pid="${esc(r.id)}" data-act="accepted" style="margin-top:5px;background:#2f81f7;color:#fff;border:0;border-radius:6px;padding:2px 7px;font-size:11px">放进输入框</button> <button data-pid="${esc(r.id)}" data-act="rejected" style="background:#30363d;color:var(--text);border:0;border-radius:6px;padding:2px 7px;font-size:11px">跳过</button></div>`;
$('proactiveBox').querySelectorAll('button[data-pid]').forEach(b=>b.onclick=()=>decideProactive(b.dataset.pid,b.dataset.act));}catch(e){$('proactiveBox').innerHTML='<div class="sub">主动候选失败：'+esc(e.message)+'</div>'}}
async function decideProactive(pid,act){try{const r=await api('/api/proactive/decision',{id:pid,action:act});if(r.ok&&act==='accepted'){$('text').value=r.proposal.text;$('text').focus()} $('proactiveBox').innerHTML='<div class="sub">'+(act==='accepted'?'已放进输入框；要不要发由你决定。':'已跳过。')+'</div>';}catch(e){$('hint').textContent='处理失败：'+e.message}}
async function forgetMemory(id){if(!confirm('让这条记忆失效？'))return;try{await api('/api/memory/forget',{id});await load()}catch(e){$('hint').textContent='删除失败：'+e.message}}
$('export').onclick=async()=>{try{const r=await api('/api/export');const blob=new Blob([JSON.stringify(r,null,2)],{type:'application/json'});const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download='selflearn-brain.json';a.click();URL.revokeObjectURL(a.href)}catch(e){$('hint').textContent='导出失败：'+e.message}};
$('report').onclick=async()=>{try{const r=await api('/api/report');$('reportBox').textContent=r.markdown}catch(e){$('reportBox').textContent='报告失败：'+e.message}};async function proactiveTick(){if(document.visibilityState!=='visible')return;try{const r=await api('/api/proactive/tick',{});if(r.available){(r.messages||[r.message]).forEach(b=>add('ai',b));$('hint').textContent='它主动开口了：'+r.reason}}catch(e){}}
setInterval(proactiveTick,30000);
$('internal').onclick=()=>{const p=document.querySelector('#sidePanel');p.style.display=p.style.display==='none'?'block':'none'};$('proactive').onclick=proactive;$('send').onclick=send;$('refresh').onclick=load;
$('repair').onclick=async()=>{try{const r=await api('/api/trust/repair',{note:'用户选择修复'});$('hint').textContent='已修复 '+(r.repaired||0)+' 条；信任 '+r.trust}catch(e){$('hint').textContent='修复失败：'+e.message}};$('text').addEventListener('keydown',e=>{if(e.key==='Enter'&&(e.ctrlKey||e.metaKey))send()});load();
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    server_version = "SelfLearn/0.1"

    def log_message(self, *args):
        return

    def _json(self, code, data):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/favicon.ico":
            self.send_response(204); self.end_headers(); return
        if path in ("/", "/index.html"):
            body = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers(); self.wfile.write(body); return
        if path == "/api/report":
            self._json(200, {"ok": True, "markdown": report.markdown(DB_PATH)}); return
        if path == "/api/learn":
            self._json(200, learn_loop.status(DB_PATH)); return
        if path == "/api/acceptance":
            self._json(200, acceptance.run(DB_PATH)); return
        if path == "/api/status":
            con = db.connect(DB_PATH)
            try:
                skills.ensure_defaults(DB_PATH)
                mems = [dict(r) for r in con.execute(
                    "SELECT * FROM user_memories WHERE status='active' ORDER BY created_at DESC LIMIT 20")]
                skills.ensure_defaults(DB_PATH)
                counts = db.counts(DB_PATH)
            finally:
                con.close()
            self._json(200, {"ok": True, "counts": counts, "memories": mems,
                             "persona": personality.list_persona(DB_PATH),
                             "variants": skills.variant_stats(DB_PATH),
                             "state": state.load(DB_PATH), "trust": trust.snapshot(DB_PATH),
                             "lessons": errors.active_lessons(DB_PATH),
                             "proactive": proactive.list_proactive(DB_PATH, 5),
                             "skill_tree": skilltree.list_nodes(DB_PATH),
                             "due": scheduler.due(DB_PATH),
                             "acceptance": acceptance.run(DB_PATH)})
            return
        if path == "/api/history":
            con = db.connect(DB_PATH)
            try:
                rows = [dict(r) for r in con.execute(
                    "SELECT role,text,ts FROM messages ORDER BY ts DESC,id DESC LIMIT 80")]
            finally:
                con.close()
            self._json(200, rows[::-1]); return
        self._json(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:  # noqa: BLE001
            self._json(400, {"ok": False, "error": "bad json"}); return
        if path == "/api/chat":
            text = str(body.get("text") or "").strip()
            image = body.get("image")
            if image is not None:
                image = str(image)
                if not image.startswith("data:image/") or len(image) > 12_000_000:
                    self._json(400, {"ok": False, "error": "图片格式/大小不合法"}); return
            if (not text and not image) or len(text) > 2000:
                self._json(400, {"ok": False, "error": "消息为空或过长"}); return
            with _LOCK:
                result = _agent().send(text, image=image)
            self._json(200 if result.get("ok") else 500, result); return
        if path == "/api/memory/forget":
            mid = str(body.get("id") or "")
            self._json(200 if mid else 400, memory.forget(DB_PATH, mid) if mid else {"ok": False, "error": "缺少 id"})
            return
        if path == "/api/export":
            data = memory.export_brain(DB_PATH)
            body = json.dumps(data, ensure_ascii=False, default=str).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Disposition", 'attachment; filename="selflearn-brain.json"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers(); self.wfile.write(body); return
        if path == "/api/learn/from-url":
            skill = str(body.get("skill") or "").strip()
            url = str(body.get("url") or "").strip()
            if not skill or not url.startswith(("http://", "https://")):
                self._json(400, {"ok": False, "error": "需要 skill + http(s) url"}); return
            with _LOCK:
                result = learn_flow.learn_from_url(DB_PATH, skill, url,
                                                   keyword=str(body.get("keyword") or ""),
                                                   client=_agent().client)
            self._json(200, result); return
        if path == "/api/proactive/tick":
            with _LOCK:
                result = proactive.tick(DB_PATH, _agent().client)
            self._json(200, result); return
        if path == "/api/proactive":
            with _LOCK:
                result = proactive.propose(DB_PATH)
            self._json(200, result); return
        if path == "/api/proactive/decision":
            result = proactive.decide(DB_PATH, str(body.get("id") or ""), str(body.get("action") or ""))
            self._json(200 if result.get("ok") else 400, result); return
        if path == "/api/trust/repair":
            result = trust.repair(DB_PATH, str(body.get("note") or ""))
            self._json(200, result); return
        if path == "/api/report":
            self._json(200, {"ok": True, "markdown": report.markdown(DB_PATH)}); return
        if path == "/api/learn":
            self._json(200, learn_loop.status(DB_PATH)); return
        if path == "/api/acceptance":
            self._json(200, acceptance.run(DB_PATH)); return
        if path == "/api/feedback":
            turn_id = str(body.get("turn_id") or "")
            reaction = str(body.get("reaction") or "")
            if not turn_id or reaction not in ("good", "ok", "bad"):
                self._json(400, {"ok": False, "error": "需要 turn_id + good/ok/bad"}); return
            with _LOCK:
                result = _agent().feedback(turn_id, reaction, str(body.get("note") or ""))
            self._json(200 if result.get("ok") else 400, result); return
        self._json(404, {"ok": False, "error": "not found"})


def main(argv=None) -> int:
    global DB_PATH
    parser = argparse.ArgumentParser(prog="python -m agi.selflearn.web")
    parser.add_argument("--port", type=int, default=8082)
    parser.add_argument("--db", default=str(DB_PATH))
    args = parser.parse_args(argv)
    DB_PATH = Path(args.db)
    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"SelfLearn web: http://127.0.0.1:{args.port}/", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""
本地持仓录入页（方案 A：无需企微自建应用 / 域名 / 备案）
=================================================================
为什么需要它：
  未认证企业微信无法配置「接收消息」回调（需 ICP 备案域名），而【群机器人 webhook】
  可以正常发消息（已实测 errcode=0）。本服务在本地起一个网页：
      【文字录入】持仓（代码 数量 单价）→ 复用 holding_analyzer 的多维分析 + 多空议会
      → 结果通过【群机器人 webhook】推送到企微群（无需任何公网/域名/备案）。
  相比截图，文字录入【完全绕开视觉 OCR（省视觉 token）】，且格式极简、顺序无关。
  同时内置【盘中异动自动提醒】：交易时段每 5 分钟扫描持仓，命中异动阈值即推企微群。

用法：
  python holding_upload_server.py
  浏览器打开 http://127.0.0.1:8899
    · 文本框粘贴「代码 数量 单价」（每行一只）→ 「分析并推送企微」
    · 「每日总结」用已存持仓做总结；「清空持仓」清除；「立即异动扫描」手动触发
    · 截图录入仍保留为可选（需配置视觉后端）

依赖：仅标准库 + requests（文字录入无需 pillow）。
环境变量（可选）：WX_UPLOAD_HOST（默认 127.0.0.1）、WX_UPLOAD_PORT（默认 8899）。
前置：.env 中需配置 WECHAT_WEBHOOK（群机器人地址）；文字/异动分析复用现有多维数据与免费文本 LLM。
"""
import base64
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import requests

# 把 strategies 目录与仓库根目录都加入 path，确保 holding_analyzer 的
# `from strategy_config / stock_analyzer`（strategies 下）以及
# `from hedge_fund.data.cn_stock_data`（包内）都能解析（否则多维分析会降级为空）。
_HERE = os.path.dirname(os.path.abspath(__file__))          # .../hedge_fund/strategies
_REPO_ROOT = os.path.dirname(os.path.dirname(_HERE))        # 仓库根（hedge_fund 的父目录）
sys.path.insert(0, _HERE)
sys.path.insert(0, _REPO_ROOT)

# 📝 自动加载 .env（否则 WECHAT_WEBHOOK / 视觉 Key 等为空导致不推送）
try:
    from _env import load_env
except ImportError:
    from hedge_fund.strategies._env import load_env
load_env()

import holding_analyzer as ha

HOST = os.environ.get("WX_UPLOAD_HOST", "127.0.0.1").strip()
PORT = int(os.environ.get("WX_UPLOAD_PORT", "8899"))

PAGE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>持仓录入（文字优先 · 推送企微群）</title>
<style>
  body { font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif; max-width: 820px; margin: 24px auto; padding: 0 16px; color: #1f2329; }
  h1 { font-size: 20px; margin-bottom: 4px; }
  .card { border: 1px solid #e5e6eb; border-radius: 10px; padding: 16px; margin: 16px 0; background: #fafbfc; }
  textarea { width: 100%; height: 130px; border: 1px solid #c9cdd4; border-radius: 8px; padding: 10px; font-size: 15px; font-family: ui-monospace, Menlo, Consolas, monospace; resize: vertical; box-sizing: border-box; }
  .drop { border: 2px dashed #c9cdd4; border-radius: 10px; padding: 24px; text-align: center; cursor: pointer; color: #4e5969; }
  .drop.hover { border-color: #3370ff; background: #f0f5ff; color: #3370ff; }
  button { background: #3370ff; color: #fff; border: 0; border-radius: 8px; padding: 10px 16px; font-size: 14px; cursor: pointer; margin: 6px 8px 0 0; }
  button.sec { background: #fff; color: #3370ff; border: 1px solid #3370ff; }
  button:disabled { opacity: .5; cursor: default; }
  pre { white-space: pre-wrap; word-break: break-word; background: #0f1419; color: #e6edf3; padding: 14px; border-radius: 8px; max-height: 460px; overflow: auto; font-size: 13px; line-height: 1.5; }
  .hint { color: #86909c; font-size: 13px; }
  .ok { color: #00b42a; } .warn { color: #ff7d00; }
</style>
</head>
<body>
  <h1>📊 持仓录入（文字优先 · 推送企微群）</h1>
  <p class="hint">无需企微自建应用 / 域名 / 备案，也<b>无需图片识别</b>。直接文字录入持仓 → 自动多维分析 + 多空议会 → 通过群机器人推送企微群，并开启盘中异动自动提醒。</p>

  <div class="card">
    <b>① 文字录入持仓</b>（每行一只，顺序无关；「单价」填你的持仓成本价）
    <div class="hint">格式：代码 数量 单价　例： 600519 100 1500　｜　或 数量 单价 代码　例： 100 1500 600519</div>
    <textarea id="text" placeholder="600519 100 1500&#10;300750 200 180&#10;000001 500 12.5"></textarea>
    <p style="margin-top:10px;">
      <button id="btnText">🔍 分析并推送企微</button>
      <button id="btnDaily" class="sec">📅 每日总结</button>
      <button id="btnMonitor" class="sec">📡 立即异动扫描</button>
      <button id="btnClear" class="sec">🗑 清空持仓</button>
    </p>
    <p class="hint">提示：未录入新数据前，上一次持仓一直沿用（默认持续持仓）。命令可用「总结 / 清空 / 异动」。</p>
  </div>

  <div class="card">
    <b>② 券商截图录入（可选）</b>
    <div class="drop" id="drop">点击或拖拽持仓截图到此（jpg / png）<input type="file" id="file" accept="image/*" hidden></div>
    <p style="margin-top:10px;">
      <button id="btnImg" disabled>🖼 截图分析并推送企微</button>
    </p>
    <p class="hint" id="fileInfo"></p>
  </div>

  <div class="card">
    <div id="status" class="hint">就绪。</div>
    <pre id="result" style="display:none;"></pre>
  </div>
<script>
const elText = document.getElementById('text');
const fileInput = document.getElementById('file');
const drop = document.getElementById('drop');
const btnText = document.getElementById('btnText');
const btnImg = document.getElementById('btnImg');
const btnDaily = document.getElementById('btnDaily');
const btnMonitor = document.getElementById('btnMonitor');
const btnClear = document.getElementById('btnClear');
const status = document.getElementById('status');
const result = document.getElementById('result');
const fileInfo = document.getElementById('fileInfo');
let currentFile = null;

function setBusy(b){ [btnText,btnImg,btnDaily,btnMonitor,btnClear].forEach(x=>x.disabled=b); }
function show(data) {
  result.style.display = 'block';
  if (data.pushed_to_wechat === true) status.innerHTML = '<span class="ok">✅ 完成，已推送到企微群。</span>';
  else if (data.pushed_to_wechat === false) status.innerHTML = '<span class="warn">⚠️ 完成（本地显示，未配置 WECHAT_WEBHOOK，未推送企微）。</span>';
  if (data.ok) result.textContent = data.text || '';
  else { status.innerHTML = '<span class="warn">出错：' + (data.error || '未知错误') + '</span>'; result.textContent = data.text || ''; }
}

// —— 文字录入 ——
btnText.onclick = async () => {
  const txt = elText.value.trim();
  if (!txt) { status.innerHTML = '<span class="warn">请先输入持仓（如 600519 100 1500）。</span>'; return; }
  setBusy(true); status.textContent = '⏳ 正在解析并分析（首次可能需数秒）…'; result.style.display='none';
  try {
    const resp = await fetch('/analyze_text', { method:'POST', headers:{'Content-Type':'text/plain'}, body: txt });
    show(await resp.json());
  } catch(e){ status.innerHTML = '<span class="warn">请求失败：'+e+'</span>'; }
  finally { setBusy(false); }
};
btnDaily.onclick = async () => {
  setBusy(true); status.textContent = '⏳ 正在生成每日总结…'; result.style.display='none';
  try { const r = await fetch('/daily', {method:'POST'}); show(await r.json()); }
  catch(e){ status.innerHTML = '<span class="warn">请求失败：'+e+'</span>'; } finally { setBusy(false); }
};
btnMonitor.onclick = async () => {
  setBusy(true); status.textContent = '⏳ 正在扫描持仓异动…'; result.style.display='none';
  try { const r = await fetch('/monitor', {method:'POST'}); show(await r.json()); }
  catch(e){ status.innerHTML = '<span class="warn">请求失败：'+e+'</span>'; } finally { setBusy(false); }
};
btnClear.onclick = async () => {
  setBusy(true); status.textContent = '⏳ 正在清空…'; result.style.display='none';
  try { const r = await fetch('/analyze_text', {method:'POST', headers:{'Content-Type':'text/plain'}, body:'清空'}); show(await r.json()); }
  catch(e){ status.innerHTML = '<span class="warn">请求失败：'+e+'</span>'; } finally { setBusy(false); }
};

// —— 截图录入（可选）——
drop.onclick = () => fileInput.click();
fileInput.onchange = e => { if (e.target.files[0]) setFile(e.target.files[0]); };
['dragover','dragenter'].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.add('hover'); }));
['dragleave','drop'].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.remove('hover'); }));
drop.ondrop = e => { const f = e.dataTransfer.files[0]; if (f) setFile(f); };
function setFile(f){ currentFile=f; fileInfo.textContent='已选：'+f.name+'（'+(f.size/1024).toFixed(0)+' KB）'; btnImg.disabled=false; }
btnImg.onclick = async () => {
  if (!currentFile) return;
  setBusy(true); status.textContent='⏳ 正在识别并分析…'; result.style.display='none';
  try {
    const resp = await fetch('/analyze?name='+encodeURIComponent(currentFile.name), { method:'POST', headers:{'Content-Type':currentFile.type||'application/octet-stream'}, body: currentFile });
    show(await resp.json());
  } catch(e){ status.innerHTML='<span class="warn">请求失败：'+e+'</span>'; } finally { setBusy(false); }
};
</script>
</body>
</html>
"""


class UploadHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", 0) or 0)
        return self.rfile.read(length) if length else b""

    def _send(self, code: int, body: str = "", ctype: str = "text/plain; charset=utf-8"):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if data:
            self.wfile.write(data)

    def _send_json(self, obj):
        self._send(200, json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8")

    def do_GET(self):
        if urlparse(self.path).path in ("/", "/index.html"):
            self._send(200, PAGE, "text/html; charset=utf-8")
        else:
            self._send(404, "not found")

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/analyze":
            self._handle_analyze()
        elif path == "/analyze_text":
            self._handle_analyze_text()
        elif path == "/monitor":
            self._handle_monitor()
        elif path == "/daily":
            self._handle_daily()
        else:
            self._send(404, "not found")

    def _handle_analyze_text(self):
        try:
            raw = self._read_body()
            text = raw.decode("utf-8", "ignore")
            if not text.strip():
                self._send_json({"ok": False, "error": "未收到文字内容", "pushed_to_wechat": bool(ha.WECHAT_WEBHOOK)})
                return
            out = ha.run_from_text(text)
            self._send_json({"ok": True, "text": out, "pushed_to_wechat": bool(ha.WECHAT_WEBHOOK)})
        except Exception as e:
            self._send_json({"ok": False, "error": str(e), "text": "", "pushed_to_wechat": bool(ha.WECHAT_WEBHOOK)})

    def _handle_monitor(self):
        try:
            alerts = ha.run_holding_monitor()
            self._send_json({"ok": True,
                             "text": ("📡 已执行一次持仓异动扫描，触发 "
                                      + str(len(alerts)) + " 只并推送。") if alerts else "📡 异动扫描完成，当前无标的触发异动。",
                             "pushed_to_wechat": bool(ha.WECHAT_WEBHOOK)})
        except Exception as e:
            self._send_json({"ok": False, "error": str(e), "text": "", "pushed_to_wechat": bool(ha.WECHAT_WEBHOOK)})

    def _handle_analyze(self):
        try:
            raw = self._read_body()
            mime = (self.headers.get("Content-Type") or "application/octet-stream").split(";")[0].strip()
            if not raw:
                self._send_json({"ok": False, "error": "未收到图片数据", "pushed_to_wechat": bool(ha.WECHAT_WEBHOOK)})
                return
            text = ha.run_from_image_bytes(raw, mime)
            self._send_json({"ok": True, "text": text, "pushed_to_wechat": bool(ha.WECHAT_WEBHOOK)})
        except Exception as e:
            self._send_json({"ok": False, "error": str(e), "text": "", "pushed_to_wechat": bool(ha.WECHAT_WEBHOOK)})

    def _handle_daily(self):
        try:
            text = ha.run_daily()
            self._send_json({"ok": True, "text": text, "pushed_to_wechat": bool(ha.WECHAT_WEBHOOK)})
        except Exception as e:
            self._send_json({"ok": False, "error": str(e), "text": "", "pushed_to_wechat": bool(ha.WECHAT_WEBHOOK)})

    def log_message(self, *args):  # 静默默认访问日志
        pass


def _auto_monitor_loop():
    """后台守护线程：交易时段每 5 分钟扫描持仓异动，命中则推送企微群。"""
    while True:
        try:
            if ha._is_trading_time():
                ha.run_holding_monitor()
            else:
                time.sleep(60)
                continue
        except Exception as e:
            print(f"⚠️ 后台异动监控异常: {e}")
        time.sleep(300)  # 每 5 分钟


def main():
    server = ThreadingHTTPServer((HOST, PORT), UploadHandler)
    print(f"📂 本地持仓分析页已启动： http://{HOST}:{PORT}")
    print(f"   · 群机器人推送：{'✅ 已配置 WECHAT_WEBHOOK' if ha.WECHAT_WEBHOOK else '⚠️ 未配置（仅本地显示，不推企微）'}")
    print(f"   · 异动自动提醒：✅ 交易时段每 5 分钟扫描持仓并推企微群")
    print(f"   · 按 Ctrl+C 停止。")
    # 启动后台异动监控守护线程
    threading.Thread(target=_auto_monitor_loop, daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n👋 已停止。")


if __name__ == "__main__":
    main()

"""
本地持仓截图上传页（方案 A：无需企微自建应用 / 域名 / 备案）
=================================================================
为什么需要它：
  未认证企业微信无法配置「接收消息」回调（需 ICP 备案域名），而【群机器人 webhook】
  可以正常发消息（已实测 errcode=0）。本服务在本地起一个网页：
      上传券商持仓截图 → 复用 holding_analyzer 的视觉识别 + 多维分析
      → 结果通过【群机器人 webhook】推送到企微群（无需任何公网/域名/备案）。

用法：
  python holding_upload_server.py
  浏览器打开 http://127.0.0.1:8899
    · 选/拖持仓截图 → 「分析并推送企微」：识别+分析并推群
    · 「生成每日总结」：用已存持仓做每日总结并推群

依赖：仅标准库 + requests + pillow（与项目其余部分一致，无需 Flask）。
环境变量（可选）：WX_UPLOAD_HOST（默认 127.0.0.1）、WX_UPLOAD_PORT（默认 8899）。
前置：.env 中需配置 WECHAT_WEBHOOK（群机器人地址）与视觉后端（VISION_BACKEND/VISION_API_KEY 等）。
"""
import base64
import json
import os
import sys
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
<title>持仓截图分析（本地）</title>
<style>
  body { font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif; max-width: 780px; margin: 24px auto; padding: 0 16px; color: #1f2329; }
  h1 { font-size: 20px; margin-bottom: 4px; }
  .card { border: 1px solid #e5e6eb; border-radius: 10px; padding: 16px; margin: 16px 0; background: #fafbfc; }
  .drop { border: 2px dashed #c9cdd4; border-radius: 10px; padding: 30px; text-align: center; cursor: pointer; color: #4e5969; }
  .drop.hover { border-color: #3370ff; background: #f0f5ff; color: #3370ff; }
  button { background: #3370ff; color: #fff; border: 0; border-radius: 8px; padding: 10px 18px; font-size: 14px; cursor: pointer; margin-right: 8px; }
  button.sec { background: #fff; color: #3370ff; border: 1px solid #3370ff; }
  button:disabled { opacity: .5; cursor: default; }
  pre { white-space: pre-wrap; word-break: break-word; background: #0f1419; color: #e6edf3; padding: 14px; border-radius: 8px; max-height: 460px; overflow: auto; font-size: 13px; line-height: 1.5; }
  .hint { color: #86909c; font-size: 13px; }
  .ok { color: #00b42a; } .warn { color: #ff7d00; }
</style>
</head>
<body>
  <h1>📊 持仓截图分析（本地上传 · 推送企微群）</h1>
  <p class="hint">无需企微自建应用 / 域名 / 备案。上传券商持仓截图 → 自动识别 + 多维分析 → 通过群机器人推送企微群。</p>
  <div class="card">
    <div class="drop" id="drop">点击或拖拽持仓截图到此（jpg / png）<input type="file" id="file" accept="image/*" hidden></div>
    <p style="margin-top:12px;">
      <button id="btnAnalyze" disabled>🔍 分析并推送企微</button>
      <button id="btnDaily" class="sec">📅 生成每日总结（用已存持仓）</button>
    </p>
    <p class="hint" id="fileInfo"></p>
  </div>
  <div class="card">
    <div id="status" class="hint">就绪。</div>
    <pre id="result" style="display:none;"></pre>
  </div>
<script>
const fileInput = document.getElementById('file');
const drop = document.getElementById('drop');
const btnA = document.getElementById('btnAnalyze');
const btnD = document.getElementById('btnDaily');
const status = document.getElementById('status');
const result = document.getElementById('result');
const fileInfo = document.getElementById('fileInfo');
let currentFile = null;

drop.onclick = () => fileInput.click();
fileInput.onchange = e => { if (e.target.files[0]) setFile(e.target.files[0]); };
['dragover','dragenter'].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.add('hover'); }));
['dragleave','drop'].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.remove('hover'); }));
drop.ondrop = e => { const f = e.dataTransfer.files[0]; if (f) setFile(f); };

function setFile(f) {
  currentFile = f;
  fileInfo.textContent = '已选：' + f.name + '（' + (f.size/1024).toFixed(0) + ' KB）';
  btnA.disabled = false;
}

async function analyze() {
  if (!currentFile) return;
  btnA.disabled = true; btnD.disabled = true;
  status.textContent = '⏳ 正在识别并分析（首次可能需数秒）…';
  result.style.display = 'none';
  try {
    const resp = await fetch('/analyze?name=' + encodeURIComponent(currentFile.name), {
      method: 'POST',
      headers: { 'Content-Type': currentFile.type || 'application/octet-stream' },
      body: currentFile
    });
    const data = await resp.json();
    show(data);
  } catch (err) {
    status.innerHTML = '<span class="warn">请求失败：' + err + '</span>';
  } finally {
    btnA.disabled = false; btnD.disabled = false;
  }
}

async function daily() {
  btnA.disabled = true; btnD.disabled = true;
  status.textContent = '⏳ 正在生成每日总结并推送…';
  result.style.display = 'none';
  try {
    const resp = await fetch('/daily', { method: 'POST' });
    const data = await resp.json();
    show(data);
  } catch (err) {
    status.innerHTML = '<span class="warn">请求失败：' + err + '</span>';
  } finally {
    btnA.disabled = false; btnD.disabled = false;
  }
}

function show(data) {
  result.style.display = 'block';
  if (data.pushed_to_wechat === true) status.innerHTML = '<span class="ok">✅ 完成，已推送到企微群。</span>';
  else if (data.pushed_to_wechat === false) status.innerHTML = '<span class="warn">⚠️ 完成（本地显示，未配置 WECHAT_WEBHOOK，未推送企微）。</span>';
  if (data.ok) result.textContent = data.text || '';
  else { status.innerHTML = '<span class="warn">分析出错：' + (data.error || '未知错误') + '</span>'; result.textContent = data.text || ''; }
}

btnA.onclick = analyze;
btnD.onclick = daily;
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
        elif path == "/daily":
            self._handle_daily()
        else:
            self._send(404, "not found")

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


def main():
    server = ThreadingHTTPServer((HOST, PORT), UploadHandler)
    print(f"📂 本地持仓分析页已启动： http://{HOST}:{PORT}")
    print(f"   · 群机器人推送：{'✅ 已配置 WECHAT_WEBHOOK' if ha.WECHAT_WEBHOOK else '⚠️ 未配置（仅本地显示，不推企微）'}")
    print(f"   · 按 Ctrl+C 停止。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n👋 已停止。")


if __name__ == "__main__":
    main()

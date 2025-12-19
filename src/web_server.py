# src/web_server.py
import asyncio
import re
import os
from typing import List

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
from contextlib import asynccontextmanager
import uvicorn
from creart import it

# 引入核心组件
from src.logger import GlobalLogger, RipLogger
from src.config import Config
from src.grpc.manager import WrapperManager

# --- 日志广播组件 (保持不变) ---

class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: str):
        clean_msg = self.remove_ansi(message)
        for connection in self.active_connections:
            try:
                await connection.send_text(clean_msg)
            except Exception:
                pass 
    
    @staticmethod
    def remove_ansi(text: str) -> str:
        ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
        return ansi_escape.sub('', text)

manager = ConnectionManager()

class WebSocketSink:
    def write(self, message):
        text = str(message)
        if manager.active_connections:
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(manager.broadcast(text))
            except RuntimeError:
                pass

ws_sink = WebSocketSink()

# --- 日志 Patch (保持不变) ---

def setup_web_logging():
    try:
        global_logger = it(GlobalLogger).logger
        global_logger.add(ws_sink.write, format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | <level>{message}</level>", level="INFO", colorize=True)
    except Exception: pass

    original_init = RipLogger.__init__
    original_set_fullname = RipLogger.set_fullname

    def patched_init(self, _type: str, item_id: str):
        original_init(self, _type, item_id)
        fmt = f"<green>{{time:HH:mm:ss}}</green> | <b>{_type.upper()}</b> | <level>{{message}}</level>"
        self.logger.add(ws_sink.write, format=fmt, level="INFO", colorize=True)

    def patched_set_fullname(self, artist: str, name: str = None):
        original_set_fullname(self, artist, name)
        full_name_clean = self.full_name
        item_type_clean = self.item_type.upper()
        fmt = (f"<green>{{time:HH:mm:ss}}</green> | <b>{item_type_clean}</b> | <b>{full_name_clean}</b> | <level>{{message}}</level>")
        self.logger.add(ws_sink.write, format=fmt, level="INFO", colorize=True)

    RipLogger.__init__ = patched_init
    RipLogger.set_fullname = patched_set_fullname

# --- 配置文件修改工具 ---

def update_config_file(url: str, secure: bool, file_path="config.toml"):
    """
    使用正则直接修改文件，避免引入额外的 TOML 写库依赖。
    只针对 [instance] 下面的 url 和 secure 进行修改。
    """
    if not os.path.exists(file_path):
        return

    with open(file_path, "r", encoding="utf-8") as f:
        content = f.read()

    # 1. 并没有完美的正则解析 TOML，但针对标准格式足够了
    # 逻辑：找到 [instance] 区块，替换下面的 url = "..." 和 secure = true/false
    
    # 替换 URL
    # 匹配：在 [instance] 之后，找到 url = "..."
    # 注意：这个正则假设 config.toml 格式比较规范
    pattern_url = r'(\[instance\][\s\S]*?url\s*=\s*)(["\'].*?["\'])'
    new_url_line = f'"{url}"'
    if re.search(pattern_url, content):
        content = re.sub(pattern_url, lambda m: f"{m.group(1)}{new_url_line}", content, count=1)
    
    # 替换 Secure
    # 匹配 secure = true 或 false
    pattern_secure = r'(\[instance\][\s\S]*?secure\s*=\s*)(true|false|True|False)'
    new_secure_line = "true" if secure else "false"
    if re.search(pattern_secure, content):
         content = re.sub(pattern_secure, lambda m: f"{m.group(1)}{new_secure_line}", content, count=1)

    with open(file_path, "w", encoding="utf-8") as f:
        f.write(content)

# --- Web API 定义 ---

_shell_instance = None
def set_shell_instance(shell):
    global _shell_instance
    _shell_instance = shell

class CommandRequest(BaseModel):
    cmd: str

class SettingsRequest(BaseModel):
    url: str
    secure: bool

@asynccontextmanager
async def lifespan(app: FastAPI):
    yield

app = FastAPI(lifespan=lifespan)

@app.websocket("/ws/log")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)

# API: 执行命令
@app.post("/api/run")
async def run_command(req: CommandRequest):
    global _shell_instance
    if not _shell_instance: return {"status": "error", "msg": "Not initialized"}
    
    command_str = req.cmd.strip()
    if not command_str: return {"status": "ignored"}
    
    it(GlobalLogger).logger.info(f"[WebAPI] Command received: {command_str}")
    try:
        await _shell_instance.command_parser(command_str)
        return {"status": "success", "msg": "Command started"}
    except Exception as e:
        return {"status": "error", "msg": str(e)}

# API: 获取设置
@app.get("/api/settings")
async def get_settings():
    cfg = it(Config)
    return {
        "url": cfg.instance.url,
        "secure": cfg.instance.secure
    }

# API: 保存设置
@app.post("/api/settings")
async def save_settings(req: SettingsRequest):
    cfg = it(Config)
    logger = it(GlobalLogger).logger
    
    old_url = cfg.instance.url
    old_secure = cfg.instance.secure
    
    # 1. 更新内存配置
    cfg.instance.url = req.url
    cfg.instance.secure = req.secure
    
    logger.info(f"[WebAPI] Updating Instance Config: {old_url} -> {req.url}, Secure: {req.secure}")
    
    # 2. 持久化到文件
    try:
        update_config_file(req.url, req.secure)
        logger.info("[WebAPI] config.toml updated.")
    except Exception as e:
        logger.error(f"[WebAPI] Failed to write config.toml: {e}")
        return {"status": "error", "msg": "Failed to save file"}

    # 3. 重新初始化连接 (Runtime Reload)
    try:
        # WrapperManager.init 是 async 的，会重新建立 gRPC channel
        logger.info("[WebAPI] Reconnecting WrapperManager...")
        await it(WrapperManager).init(cfg.instance.url, cfg.instance.secure)
        
        # 强制刷新状态缓存
        it(WrapperManager).status.cache_invalidate()
        # 尝试获取状态以验证连接
        st = await it(WrapperManager).status()
        logger.success(f"[WebAPI] Connected to {req.url}. Regions: {', '.join(st.regions)}")
        
        return {"status": "success", "msg": "Settings saved & reconnected"}
    except Exception as e:
        logger.error(f"[WebAPI] Reconnection Failed: {e}")
        # 回滚内存配置防止状态不一致 (可选)
        cfg.instance.url = old_url
        cfg.instance.secure = old_secure
        return {"status": "error", "msg": f"Connection failed: {str(e)}"}

@app.get("/")
async def index():
    from fastapi.responses import HTMLResponse
    html_content = """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <title>AMD Web Console</title>
        <style>
            body { font-family: 'Consolas', 'Menlo', monospace; background-color: #1e1e1e; color: #d4d4d4; padding: 20px; font-size: 13px; margin: 0;}
            h2 { color: #569cd6; border-bottom: 1px solid #333; padding-bottom: 5px; margin-top: 0; display: flex; justify-content: space-between;}
            .panel { background: #252526; padding: 15px; border-radius: 5px; margin-bottom: 15px; border: 1px solid #333; }
            
            /* Settings Panel */
            .settings-row { display: flex; gap: 10px; align-items: center; margin-bottom: 5px; }
            label { width: 60px; color: #9cdcfe; font-weight: bold; }
            input[type="text"] { flex-grow: 1; padding: 6px; border: 1px solid #3c3c3c; background: #333; color: white; outline: none; }
            button { padding: 6px 15px; background-color: #0e639c; color: white; border: none; cursor: pointer; border-radius: 3px;}
            button:hover { background-color: #1177bb; }
            button.secondary { background-color: #3a3d41; }
            
            #log-container {
                background-color: #101010; border: 1px solid #333; 
                height: calc(100vh - 250px); 
                overflow-y: auto; padding: 10px; 
                white-space: pre-wrap; word-break: break-all;
            }
            .log-line { border-bottom: 1px solid #1a1a1a; padding: 2px 0; }
        </style>
    </head>
    <body>
        <div style="max-width: 1200px; margin: 0 auto;">
            
            <!-- Config Panel -->
            <div class="panel">
                <div style="margin-bottom:10px; font-weight:bold; color: #ce9178;">[instance] Settings</div>
                <div class="settings-row">
                    <label>URL:</label>
                    <input type="text" id="cfg-url" placeholder="e.g. 192.168.1.10:32767">
                </div>
                <div class="settings-row">
                    <label>Secure:</label>
                    <input type="checkbox" id="cfg-secure"> 
                    <span style="font-size: 0.9em; color: gray;">(Use TLS)</span>
                    <div style="flex-grow:1"></div>
                    <button onclick="saveSettings()">Save & Reconnect</button>
                    <button class="secondary" onclick="document.getElementById('cmd').focus()">Cancel</button>
                </div>
            </div>

            <h2>Console</h2>

            <!-- Command Panel -->
            <div style="display: flex; gap: 10px; margin-bottom: 10px;">
                <input type="text" id="cmd" placeholder="dl https://music.apple.com/..." style="padding: 10px;" autofocus onkeydown="if(event.key==='Enter') sendCmd()">
                <button onclick="sendCmd()" style="padding: 0 25px;">RUN</button>
            </div>
            
            <div id="log-container"></div>
        </div>

        <script>
            const protocol = window.location.protocol === 'https:' ? 'wss' : 'ws';
            const logContainer = document.getElementById('log-container');
            
            // --- Logging ---
            const wsUrl = `${protocol}://${window.location.host}/ws/log`;
            let ws;

            function connect() {
                ws = new WebSocket(wsUrl);
                ws.onopen = () => appendLog(">>> [SYSTEM] Connected to server.");
                ws.onmessage = (e) => appendLog(e.data);
                ws.onclose = () => setTimeout(connect, 3000);
            }

            function appendLog(msg) {
                const div = document.createElement('div');
                div.className = 'log-line';
                div.textContent = msg;
                logContainer.appendChild(div);
                if (logContainer.scrollHeight - logContainer.scrollTop < logContainer.clientHeight + 200) {
                    logContainer.scrollTop = logContainer.scrollHeight;
                }
            }

            // --- Commands ---
            async function sendCmd() {
                const cmdInput = document.getElementById('cmd');
                const cmd = cmdInput.value;
                if (!cmd) return;
                cmdInput.value = ''; 
                try {
                    await fetch('/api/run', {
                        method: 'POST', 
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({cmd: cmd})
                    });
                } catch (e) { appendLog(">>> Network Error"); }
            }

            // --- Settings ---
            async function loadSettings() {
                try {
                    const res = await fetch('/api/settings');
                    const data = await res.json();
                    document.getElementById('cfg-url').value = data.url;
                    document.getElementById('cfg-secure').checked = data.secure;
                } catch (e) {
                    console.error("Failed to load settings", e);
                }
            }

            async function saveSettings() {
                const url = document.getElementById('cfg-url').value;
                const secure = document.getElementById('cfg-secure').checked;
                
                appendLog(`>>> Saving settings: URL=${url}, Secure=${secure}...`);
                
                try {
                    const res = await fetch('/api/settings', {
                        method: 'POST', 
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({url: url, secure: secure})
                    });
                    const data = await res.json();
                    if(data.status === 'success') {
                        appendLog(`>>> [SUCCESS] ${data.msg}`);
                    } else {
                        appendLog(`>>> [ERROR] ${data.msg}`);
                    }
                } catch (e) {
                    appendLog(`>>> [ERROR] Failed to save settings: ${e}`);
                }
            }

            // Init
            connect();
            loadSettings();
        </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)

async def start_web_server(host="0.0.0.0", port=8080):
    config = uvicorn.Config(app, host=host, port=port, log_level="critical") 
    server = uvicorn.Server(config)
    await server.serve()
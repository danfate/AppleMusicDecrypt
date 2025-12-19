# src/web_server.py
import asyncio
import re
from typing import List

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
from contextlib import asynccontextmanager
import uvicorn
from creart import it
from src.logger import GlobalLogger, RipLogger # 引入 RipLogger 以便 Patch

# --- 日志广播组件 ---

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
        # 移除 ANSI 颜色代码，让网页显示纯文本
        clean_msg = self.remove_ansi(message)
        # 并发发送给所有连接的网页
        for connection in self.active_connections:
            try:
                await connection.send_text(clean_msg)
            except Exception:
                pass 
    
    @staticmethod
    def remove_ansi(text: str) -> str:
        # 去除 \033[...m 颜色代码
        text = re.sub(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])', '', text)
        return text

manager = ConnectionManager()

class WebSocketSink:
    """Loguru Sink"""
    def write(self, message):
        text = str(message)
        if manager.active_connections:
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(manager.broadcast(text))
            except RuntimeError:
                pass

# 全局单例 Sink
ws_sink = WebSocketSink()

def setup_web_logging():
    """
    1. 挂载到全局 GlobalLogger
    2. 使用 Monkey Patch 挂载到动态创建的 RipLogger
    """
    
    # --- 1. 挂载 Global Logger (系统级日志) ---
    global_logger = it(GlobalLogger).logger
    global_logger.add(
        ws_sink.write,
        format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | <level>{message}</level>",
        level="INFO",
        colorize=True
    )
    global_logger.info("Web Log Sink attached to GlobalLogger.")

    # --- 2. 挂载 RipLogger (任务级日志 - 如下载进度) ---
    # 保存原始的 __init__ 方法
    original_init = RipLogger.__init__

    def patched_init(self, *args, **kwargs):
        # 1. 先执行原有的初始化 (它会创建 self.logger)
        original_init(self, *args, **kwargs)
        
        # 2. 【核心】在原有逻辑执行完后，强行给它的 logger 添加我们的 sink
        # 注意：RipLogger 内部使用了 copy.deepcopy，所以必须针对每个实例添加
        self.logger.add(
            ws_sink.write,
            # 使用稍简单的格式，因为 RipLogger 已经在 message 里包含了很多格式信息
            format="{message}", 
            level="INFO",
            colorize=True 
        )

    # 应用补丁：将 RipLogger 的类初始化方法替换为我们的版本
    RipLogger.__init__ = patched_init
    global_logger.info("Web Log Sink attached to RipLogger (Patched).")

# --- 核心 Web 逻辑 ---

_shell_instance = None

def set_shell_instance(shell):
    global _shell_instance
    _shell_instance = shell

class CommandRequest(BaseModel):
    cmd: str

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

@app.post("/api/run")
async def run_command(req: CommandRequest):
    global _shell_instance
    if not _shell_instance:
        raise HTTPException(status_code=503, detail="Shell instance not initialized")

    command_str = req.cmd.strip()
    if not command_str:
        return {"status": "ignored", "msg": "Empty command"}

    # 这条日志现在应该能立即在网页看到
    it(GlobalLogger).logger.info(f"[WebAPI] Command received: {command_str}")

    try:
        await _shell_instance.command_parser(command_str)
        return {"status": "success", "msg": "Command started"}
    except ValueError as e:
        it(GlobalLogger).logger.warning(f"[WebAPI] Arg Error: {e}")
        return {"status": "error", "msg": str(e)}
    except Exception as e:
        it(GlobalLogger).logger.error(f"[WebAPI] Exec Error: {e}")
        return {"status": "error", "msg": str(e)}

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
            body { font-family: 'Consolas', 'Menlo', monospace; background-color: #1e1e1e; color: #d4d4d4; padding: 20px; font-size: 13px; }
            h2 { color: #569cd6; border-bottom: 1px solid #333; padding-bottom: 5px; }
            .input-group { display: flex; gap: 10px; margin-bottom: 10px; }
            input { flex-grow: 1; padding: 10px; border: 1px solid #3c3c3c; background: #252526; color: #d4d4d4; outline: none; }
            button { padding: 10px 20px; background-color: #0e639c; color: white; border: none; cursor: pointer; }
            #log-container {
                background-color: #101010; border: 1px solid #333; 
                height: 70vh; overflow-y: auto; padding: 10px; 
                white-space: pre-wrap; word-break: break-all;
            }
            .log-line { border-bottom: 1px solid #1a1a1a; padding: 2px 0; }
        </style>
    </head>
    <body>
        <div style="max-width: 1000px; margin: 0 auto;">
            <h2>Music Downloader Web Console</h2>
            <div class="input-group">
                <input type="text" id="cmd" placeholder="dl https://music.apple.com/..." autofocus onkeydown="if(event.key==='Enter') sendCmd()">
                <button onclick="sendCmd()">RUN</button>
            </div>
            <div id="log-container"></div>
        </div>

        <script>
            const protocol = window.location.protocol === 'https:' ? 'wss' : 'ws';
            const wsUrl = `${protocol}://${window.location.host}/ws/log`;
            const logContainer = document.getElementById('log-container');
            let ws;

            function connect() {
                ws = new WebSocket(wsUrl);
                ws.onopen = () => appendLog(">>> Connected to server.");
                ws.onmessage = (e) => appendLog(e.data);
                ws.onclose = () => {
                    appendLog(">>> Connection lost. Reconnecting...");
                    setTimeout(connect, 3000);
                };
            }

            function appendLog(msg) {
                const div = document.createElement('div');
                div.className = 'log-line';
                div.textContent = msg;
                logContainer.appendChild(div);
                logContainer.scrollTop = logContainer.scrollHeight;
            }

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
                } catch (e) {
                    appendLog(">>> Network Error");
                }
            }
            connect();
        </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)

async def start_web_server(host="0.0.0.0", port=8080):
    config = uvicorn.Config(app, host=host, port=port, log_level="warning") 
    server = uvicorn.Server(config)
    await server.serve()
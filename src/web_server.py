# src/web_server.py
import asyncio
import logging
import re
from typing import List

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
from contextlib import asynccontextmanager
import uvicorn
from creart import it
from src.logger import GlobalLogger

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
                pass # 忽略发送失败的连接
    
    @staticmethod
    def remove_ansi(text: str) -> str:
        # 正则表达式去除 \033[...m 这种颜色代码
        ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
        return ansi_escape.sub('', text)

manager = ConnectionManager()

class WebSocketLogHandler(logging.Handler):
    """自定义日志处理器：拦截日志并扔给 WebSocket"""
    def emit(self, record):
        try:
            msg = self.format(record)
            # logging.emit 是同步方法，但 websocket 发送是异步的
            # 我们需要获取当前的 event loop 来调度发送任务
            if manager.active_connections:
                loop = asyncio.get_running_loop()
                # 使用 create_task 避免阻塞业务逻辑
                loop.create_task(manager.broadcast(msg))
        except (RuntimeError, Exception):
            # 如果 event loop 没运行或者其他错误，忽略
            pass

# 暴露给外部调用的挂载函数
def setup_web_logging():
    """将 WebSocket 处理器挂载到全局 Logger 上"""
    logger = it(GlobalLogger).logger
    ws_handler = WebSocketLogHandler()
    ws_handler.setLevel(logging.INFO) # 也可以设为 DEBUG
    
    # 设置日志格式 (包含时间)
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s', datefmt='%H:%M:%S')
    ws_handler.setFormatter(formatter)
    
    logger.add(ws_handler)
    logger.info("Web Log Handler attached.")

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
            # 保持连接，在这个示例中我们不需要从网页接收数据
            # 只需要发送，所以这里的 receive 可以用来检测断开
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

    # 日志会自动通过 WebSocket 广播出去
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
            body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background-color: #1e1e1e; color: #c0c0c0; padding: 20px; }
            h2 { color: #fff; }
            .container { max-width: 900px; margin: 0 auto; }
            .input-group { display: flex; gap: 10px; margin-bottom: 20px; }
            input { flex-grow: 1; padding: 10px; border-radius: 4px; border: 1px solid #444; background: #2d2d2d; color: white; outline: none; }
            button { padding: 10px 20px; background-color: #007acc; color: white; border: none; border-radius: 4px; cursor: pointer; }
            button:hover { background-color: #005f9e; }
            #log-container {
                background-color: #0d0d0d; border: 1px solid #333; border-radius: 4px;
                height: 500px; overflow-y: auto; padding: 10px; font-family: 'Consolas', 'Monaco', monospace; font-size: 0.9em;
            }
            .log-line { margin: 2px 0; border-bottom: 1px solid #1a1a1a; word-wrap: break-word; white-space: pre-wrap;}
        </style>
    </head>
    <body>
        <div class="container">
            <h2>Music Downloader Web Console</h2>
            <div class="input-group">
                <input type="text" id="cmd" placeholder="Enter command (e.g., dl https://music.apple.com/...)" onkeydown="if(event.key==='Enter') sendCmd()">
                <button onclick="sendCmd()">Execute</button>
            </div>
            <div id="log-container"></div>
        </div>

        <script>
            // 1. WebSocket 连接逻辑
            const protocol = window.location.protocol === 'https:' ? 'wss' : 'ws';
            const wsUrl = `${protocol}://${window.location.host}/ws/log`;
            const ws = new WebSocket(wsUrl);
            const logContainer = document.getElementById('log-container');

            function appendLog(msg) {
                const div = document.createElement('div');
                div.className = 'log-line';
                div.textContent = msg; // TextContent 防止 XSS
                logContainer.appendChild(div);
                // 自动滚动到底部
                logContainer.scrollTop = logContainer.scrollHeight;
            }

            ws.onmessage = function(event) {
                appendLog(event.data);
            };
            ws.onopen = () => appendLog(">>> Connected to Real-time Stream.");
            ws.onclose = () => appendLog(">>> Connection Lost.");

            // 2. 命令发送逻辑
            async function sendCmd() {
                const cmdInput = document.getElementById('cmd');
                const cmd = cmdInput.value;
                if (!cmd) return;

                cmdInput.value = ''; // 立即清空，提升体验
                // 不需要在前端 appendLog，因为后端收到请求后会打印日志，WS 会自动推回来
                
                try {
                    await fetch('/api/run', {
                        method: 'POST', 
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({cmd: cmd})
                    });
                } catch (e) {
                    appendLog(">>> Error sending command.");
                }
            }
        </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)

async def start_web_server(host="0.0.0.0", port=8080):
    config = uvicorn.Config(app, host=host, port=port, log_level="warning") # 减少 uvicorn 自身日志
    server = uvicorn.Server(config)
    await server.serve()
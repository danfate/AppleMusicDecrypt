# src/web_server.py
import asyncio
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

class WebSocketSink:
    """
    Loguru 的 Sink 类。
    Loguru 会调用 write 方法写入日志。
    """
    def write(self, message):
        # message 在 loguru 中是一个包含所有元数据的对象，可以直接转为 str
        text = str(message)
        
        # 因为 write 可能是从同步环境调用的，我们需要调度到 loop 中发送
        if manager.active_connections:
            try:
                # 尝试获取正在运行的 Loop
                loop = asyncio.get_running_loop()
                # 放入任务队列
                loop.create_task(manager.broadcast(text))
            except RuntimeError:
                # 如果没有运行的 loop (极少情况)，忽略
                pass

# 暴露给外部调用的挂载函数
def setup_web_logging():
    """将 WebSocket Sink 挂载到 loguru"""
    # 获取 loguru 实例
    # 注意：src.logger.GlobalLogger 把 loguru 实例存在 self.logger 中
    log_instance = it(GlobalLogger).logger
    
    # 实例化 Sink
    ws_sink = WebSocketSink()
    
    # 添加 Sink 到 Loguru
    # format 指定了发往 WebSocket 的日志格式
    log_instance.add(
        ws_sink.write,
        format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | <level>{message}</level>",
        level="INFO",
        colorize=True # Loguru 输出带颜色的字符，Web端再正则去除，或者设为False直接出纯文本
    )
    
    log_instance.info("Web Log Sink attached successfully.")

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
    # 这里不需要改动，JS 使用 window.location.host 会自动适配 Docker 映射的端口
    html_content = """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <title>AMD Web Console</title>
        <style>
            body { font-family: 'Consolas', 'Menlo', monospace; background-color: #1e1e1e; color: #d4d4d4; padding: 20px; font-size: 14px; }
            h2 { color: #569cd6; border-bottom: 1px solid #333; padding-bottom: 10px; }
            .container { max-width: 1000px; margin: 0 auto; }
            .input-group { display: flex; gap: 10px; margin-bottom: 20px; }
            input { flex-grow: 1; padding: 12px; border-radius: 4px; border: 1px solid #3c3c3c; background: #252526; color: #d4d4d4; outline: none; font-family: inherit; }
            input:focus { border-color: #007acc; }
            button { padding: 10px 24px; background-color: #0e639c; color: white; border: none; border-radius: 4px; cursor: pointer; font-family: inherit; font-weight: bold; }
            button:hover { background-color: #1177bb; }
            #log-container {
                background-color: #101010; border: 1px solid #333; border-radius: 4px;
                height: 600px; overflow-y: auto; padding: 15px; 
                white-space: pre-wrap; word-break: break-all;
            }
            .log-line { margin-bottom: 4px; line-height: 1.4; }
            /* 简单的日志颜色模拟 */
            .log-line:contains("ERROR") { color: #f48771; }
            .log-line:contains("WARNING") { color: #cca700; }
            .log-line:contains("SUCCESS") { color: #89d185; }
        </style>
    </head>
    <body>
        <div class="container">
            <h2>Music Downloader Web Console</h2>
            <div class="input-group">
                <input type="text" id="cmd" placeholder="dl https://music.apple.com/..." autofocus onkeydown="if(event.key==='Enter') sendCmd()">
                <button onclick="sendCmd()">RUN</button>
            </div>
            <div id="log-container"></div>
        </div>

        <script>
            const protocol = window.location.protocol === 'https:' ? 'wss' : 'ws';
            const logContainer = document.getElementById('log-container');
            
            // 自动使用当前浏览器地址栏的 Host (IP:Port)
            const wsUrl = `${protocol}://${window.location.host}/ws/log`;
            console.log("Connecting to WS:", wsUrl);

            let ws;
            
            function connect() {
                ws = new WebSocket(wsUrl);
                
                ws.onopen = () => appendLog(">>> [SYSTEM] Connected to server.");
                
                ws.onmessage = function(event) {
                    appendLog(event.data);
                };

                ws.onclose = function() {
                    appendLog(">>> [SYSTEM] Connection lost. Reconnecting in 3s...");
                    setTimeout(connect, 3000);
                };
                
                ws.onerror = function(err) {
                    console.error("WS Error:", err);
                    // 不要在 GUI 频繁显示错误，依靠 onclose 重连
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
                    const res = await fetch('/api/run', {
                        method: 'POST', 
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({cmd: cmd})
                    });
                    if (res.status !== 200) {
                        appendLog(`>>> [API ERROR] ${res.statusText}`);
                    }
                } catch (e) {
                    appendLog(">>> [NETWORK ERROR] Failed to send command.");
                }
            }

            // 初始化连接
            connect();
        </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)

async def start_web_server(host="0.0.0.0", port=8080):
    # host="0.0.0.0" 确保 Docker 能够映射端口
    config = uvicorn.Config(app, host=host, port=port, log_level="warning") 
    server = uvicorn.Server(config)
    await server.serve()
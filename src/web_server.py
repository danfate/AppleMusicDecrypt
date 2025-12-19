import asyncio
import re
from typing import List

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
from contextlib import asynccontextmanager
import uvicorn
from creart import it
# 引入 GlobalLogger 和 RipLogger 进行 Patch
from src.logger import GlobalLogger, RipLogger 

# --- WebSocket 核心管理 ---

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
        # 移除 Loguru 产生的 ANSI 颜色代码，保证网页整洁
        clean_msg = self.remove_ansi(message)
        for connection in self.active_connections:
            try:
                await connection.send_text(clean_msg)
            except Exception:
                pass 
    
    @staticmethod
    def remove_ansi(text: str) -> str:
        # 增强版正则，去除各类控制符
        ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
        return ansi_escape.sub('', text)

manager = ConnectionManager()

class WebSocketSink:
    """Loguru 的 Sink，负责写入 WebSocket"""
    def write(self, message):
        # Loguru 的 message 对象可以直接转 str
        text = str(message)
        if manager.active_connections:
            try:
                # 必须在一个 Event Loop 中执行
                loop = asyncio.get_running_loop()
                loop.create_task(manager.broadcast(text))
            except RuntimeError:
                pass

ws_sink = WebSocketSink()

# --- 核心：日志 Patch 逻辑 ---

def setup_web_logging():
    """
    不仅挂载 GlobalLogger，还要通过 Monkey Patch 
    死死咬住 RipLogger，无论它 reset 多少次。
    """
    
    # 1. 挂载全局 GlobalLogger (处理 WebAPI 自身的日志)
    try:
        global_logger = it(GlobalLogger).logger
        global_logger.add(
            ws_sink.write,
            format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | <level>{message}</level>",
            level="INFO",
            colorize=True
        )
    except Exception as e:
        print(f"Warning: Failed to attach GlobalLogger: {e}")

    # ==========================================
    # 2. Patch RipLogger (处理具体的下载日志)
    # ==========================================
    
    # 保存原始方法引用
    original_init = RipLogger.__init__
    original_set_fullname = RipLogger.set_fullname

    # 定义 Patch 后的 __init__
    def patched_init(self, _type: str, item_id: str):
        # 执行原始逻辑（它会 remove 所有 handler）
        original_init(self, _type, item_id)
        
        # 重新加入我们的 WS Sink
        # 此时还没有 fullname，格式稍微简单点
        fmt = f"<green>{{time:HH:mm:ss}}</green> | <b>{_type.upper()}</b> | <level>{{message}}</level>"
        self.logger.add(ws_sink.write, format=fmt, level="INFO", colorize=True)

    # 定义 Patch 后的 set_fullname
    # 这是最关键的一步，因为原代码在这里又 remove 了一次 handler
    def patched_set_fullname(self, artist: str, name: str = None):
        # 执行原始逻辑
        original_set_fullname(self, artist, name)
        
        # 构建与 RipLogger 类似的格式字符串，把歌名信息带上
        # 注意：这里我们利用 self.full_name 和 self.item_type
        # 它们在 original_set_fullname 执行后就已经被设置好了
        full_name_clean = self.full_name # 这里不需要转义，因为 loguru 会处理
        item_type_clean = self.item_type.upper()
        
        # 构造 Loguru 格式字符串
        fmt = (
            f"<green>{{time:HH:mm:ss}}</green> | "
            f"<b>{item_type_clean}</b> | "
            f"<b>{full_name_clean}</b> | "
            f"<level>{{message}}</level>"
        )
        
        # 再次强行插入 WS Sink
        self.logger.add(ws_sink.write, format=fmt, level="INFO", colorize=True)

    # 应用 Patch
    RipLogger.__init__ = patched_init
    RipLogger.set_fullname = patched_set_fullname
    
    print(">>> Web Logging Hooks Installed Successfully.")

# --- Web API 定义 ---

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

    it(GlobalLogger).logger.info(f"[WebAPI] Command received: {command_str}")

    try:
        # 这里需要由 create_task 来调用，或者直接 await
        # 原 InteractiveShell 逻辑
        await _shell_instance.command_parser(command_str)
        return {"status": "success", "msg": "Command started"}
    except ValueError as e:
        it(GlobalLogger).logger.warning(f"Args Error: {e}")
        return {"status": "error", "msg": str(e)}
    except Exception as e:
        it(GlobalLogger).logger.error(f"Exec Error: {e}")
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
            body { font-family: 'Consolas', 'Menlo', monospace; background-color: #1e1e1e; color: #d4d4d4; padding: 20px; font-size: 13px; margin: 0;}
            h2 { color: #569cd6; border-bottom: 1px solid #333; padding-bottom: 5px; margin-top: 0; }
            .input-group { display: flex; gap: 10px; margin-bottom: 10px; }
            input { flex-grow: 1; padding: 10px; border: 1px solid #3c3c3c; background: #252526; color: #d4d4d4; outline: none; }
            button { padding: 10px 20px; background-color: #0e639c; color: white; border: none; cursor: pointer; }
            
            #log-container {
                background-color: #101010; border: 1px solid #333; 
                height: calc(100vh - 120px); /* 自适应高度 */
                overflow-y: auto; padding: 10px; 
                white-space: pre-wrap; word-break: break-all;
            }
            .log-line { border-bottom: 1px solid #1a1a1a; padding: 2px 0; }
            
            /* 日志颜色高亮 */
            .log-line:nth-child(even) { background-color: #141414; }
        </style>
    </head>
    <body>
        <div style="max-width: 1200px; margin: 0 auto;">
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
            
            // 自动判断端口，适配 Docker 映射
            const wsUrl = `${protocol}://${window.location.host}/ws/log`;
            let ws;

            function connect() {
                ws = new WebSocket(wsUrl);
                ws.onopen = () => appendLog(">>> [SYSTEM] Connected to server.");
                ws.onmessage = (e) => appendLog(e.data);
                ws.onclose = () => {
                    // appendLog(">>> Connection lost. Reconnecting...");
                    setTimeout(connect, 3000);
                };
            }

            function appendLog(msg) {
                const div = document.createElement('div');
                div.className = 'log-line';
                div.textContent = msg;
                logContainer.appendChild(div);
                // 只有当用户在大致底部时才自动滚动，方便查看历史
                if (logContainer.scrollHeight - logContainer.scrollTop < logContainer.clientHeight + 200) {
                    logContainer.scrollTop = logContainer.scrollHeight;
                }
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
                    appendLog(">>> Network Error: " + e);
                }
            }
            connect();
        </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)

async def start_web_server(host="0.0.0.0", port=8080):
    # 关闭 uvicorn access log 以免干扰我们自己的日志
    config = uvicorn.Config(app, host=host, port=port, log_level="critical") 
    server = uvicorn.Server(config)
    await server.serve()
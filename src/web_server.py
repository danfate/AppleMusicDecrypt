# src/web_server.py
import asyncio
from fastapi import FastAPI, BackgroundTasks, HTTPException
from pydantic import BaseModel
from contextlib import asynccontextmanager
import uvicorn
from creart import it
from src.logger import GlobalLogger

# 用于接收 Web 请求的数据结构
class CommandRequest(BaseModel):
    cmd: str

# 全局变量，用于持有 main.py 传进来的 shell 实例
_shell_instance = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    # 可以在这里做一些清理工作
    yield

app = FastAPI(lifespan=lifespan)

def set_shell_instance(shell):
    """在该模块中保存 CLI shell 的实例引用"""
    global _shell_instance
    _shell_instance = shell

@app.post("/api/run")
async def run_command(req: CommandRequest):
    """
    接收命令并调用原有逻辑
    """
    global _shell_instance
    if not _shell_instance:
        raise HTTPException(status_code=503, detail="Shell instance not initialized")

    command_str = req.cmd.strip()
    if not command_str:
        return {"status": "ignored", "msg": "Empty command"}

    it(GlobalLogger).logger.info(f"[WebAPI] Received command: {command_str}")

    try:
        # 直接复用 src/cmd.py 中的 command_parser 方法
        # 注意：这里需要 await，因为 command_parser 是 async 的
        await _shell_instance.command_parser(command_str)
        return {"status": "success", "msg": "Command executed (check logs for details)"}
    except ValueError as e:
        # 下面我们会修改 argparse 让它抛出 ValueError 而不是退出
        it(GlobalLogger).logger.error(f"[WebAPI] Argument Error: {e}")
        return {"status": "error", "msg": str(e)}
    except Exception as e:
        it(GlobalLogger).logger.exception("[WebAPI] Execution error")
        return {"status": "error", "msg": str(e)}

@app.get("/")
async def index():
    """提供一个极其简易的 HTML 输入框"""
    from fastapi.responses import HTMLResponse
    html_content = """
    <!DOCTYPE html>
    <html>
        <head><title>Music Downloader Web Console</title></head>
        <body style="font-family: sans-serif; padding: 2rem;">
            <h2>Web Console</h2>
            <div style="display: flex; gap: 10px;">
                <input type="text" id="cmd" placeholder="e.g.: dl https://music.apple.com/..." style="width: 400px; padding: 8px;">
                <button onclick="sendCmd()" style="padding: 8px 16px;">Run</button>
            </div>
            <p id="status" style="margin-top: 10px; color: gray;"></p>
            <script>
                async function sendCmd() {
                    const cmdInput = document.getElementById('cmd');
                    const status = document.getElementById('status');
                    status.innerText = "Sending...";
                    try {
                        const res = await fetch('/api/run', {
                            method: 'POST', 
                            headers: {'Content-Type': 'application/json'},
                            body: JSON.stringify({cmd: cmdInput.value})
                        });
                        const data = await res.json();
                        status.innerText = `[${data.status}] ${data.msg}`;
                        if(data.status === 'success') cmdInput.value = '';
                    } catch (e) {
                        status.innerText = "Connection Error";
                    }
                }
            </script>
        </body>
    </html>
    """
    return HTMLResponse(content=html_content)

async def start_web_server(host="0.0.0.0", port=8080):
    """启动 uvicorn server"""
    config = uvicorn.Config(app, host=host, port=port, log_level="error")
    server = uvicorn.Server(config)
    await server.serve()
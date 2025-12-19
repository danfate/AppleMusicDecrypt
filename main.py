import asyncio
import argparse
from creart import add_creator

#loop = asyncio.new_event_loop()

from src.logger import LoggerCreator
add_creator(LoggerCreator)
from src.config import ConfigCreator
add_creator(ConfigCreator)
from src.api import APICreator
add_creator(APICreator)
from src.grpc.manager import WMCreator
add_creator(WMCreator)
from src.measurer import MeasurerCreator
add_creator(MeasurerCreator)

from src.cmd import InteractiveShell

# 引入我们新建的模块
from src.web_server import set_shell_instance, start_web_server

# 定义一个不需要改动 API 签名的方法来劫持 argparse 的 error
def patch_parser_error(parser_instance):
    def error_handler(message):
        # 抛出异常而不是 sys.exit
        raise ValueError(f"Invalid arguments: {message}")
    parser_instance.error = error_handler

if __name__ == '__main__':
    # 原代码的 loop 创建
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop) # 显式设置，防止部分库找不到 loop

    # 1. 实例化 Shell (它会在 __init__ 里做同步阻塞的初始化，这符合原逻辑)
    cmd = InteractiveShell(loop)
    
    # 【关键步骤】Patch 它的 parser 防止 crash
    #InteractiveShell 内部创建了 self.parser，我们直接修改该实例的方法
    patch_parser_error(cmd.parser)
    
    # 2. 将实例注入给 Web Server
    set_shell_instance(cmd)

    # 3. 定义并行任务
    async def main_entry():
        # 创建 Web Server 任务
        # 这里端口设为 8080，你可以改为其他的
        web_task = asyncio.create_task(start_web_server(port=8080))
        
        # 创建原有的 CLI Prompt 任务
        # cmd.start() 内部是 while True 循环，我们需要它和 web_task 并行
        cli_task = asyncio.create_task(cmd.start())
        
        print(">>> System Started.")
        print(">>> Web Interface available at: http://127.0.0.1:8080")
        print(">>> CLI is also active below:")

        try:
            # 等待两者之一结束 (通常 CLI 结束意味着用户通过 exit 命令退出)
            await asyncio.wait([web_task, cli_task], return_when=asyncio.FIRST_COMPLETED)
        except Exception as e:
            print(f"System error: {e}")
        finally:
            # 清理
            web_task.cancel()
            cli_task.cancel()
            loop.stop()

    try:
        loop.run_until_complete(main_entry())
    except KeyboardInterrupt:
        # 处理 Ctrl+C
        loop.stop()

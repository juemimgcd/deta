import argparse

from src.deta import __version__


def main() -> int:
    """作为命令入口解析参数，支持帮助和版本信息，并在无参数时显示用法。
    由 deta 命令或 python -m deta 调用，正常完成后返回退出码 0。
    """
    parser = argparse.ArgumentParser(prog="deta", description="Deta 本地 Coding Agent")
    parser.add_argument("--version", action="version", version=f"deta {__version__}")
    parser.parse_args()
    parser.print_help()
    return 0
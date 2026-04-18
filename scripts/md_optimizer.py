"""
======================

用法（多目录并发）:
    python scripts/md_optimizer.py \
        --input-dirs ./docs1 ./docs2 ./docs3 \
        --output-dir ./output_cn

用法（递归 + 自定义并发数）:
    python scripts/md_optimizer.py --input-dirs ./docs --output-dir ./out --recursive --concurrency 5

依赖:
    pip install -r scripts/requirements.txt
"""
import os
import argparse
import asyncio
import sys
from pathlib import Path

from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage, HumanMessage


# ── 处理 Prompt ────────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """你是一位专业的技术专家，擅长软件、技术类话题。

请读取以下内容，输出你的理解与总结。"""


def create_llm(
    base_url: str = "http://127.0.0.1:7860/v1",
    api_key: str = "change-me-now",
    model: str = "qwen3.6-plus",
    temperature: float = 0.01,
) -> ChatOpenAI:
    """创建 ChatOpenAI 实例，连接本地 doubao2API 服务。"""
    return ChatOpenAI(
        base_url=base_url,
        api_key=api_key,
        model=model,
        temperature=temperature,
    )


def read_markdown(file_path: Path) -> str:
    """读取 Markdown 文件内容。"""
    return file_path.read_text(encoding="utf-8")


def write_markdown(file_path: Path, content: str) -> None:
    """将处理后的内容写入文件。"""
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(content, encoding="utf-8")


def collect_markdown_files(input_dir: Path, recursive: bool = False) -> list[Path]:
    """收集目录下的 Markdown 文件。"""
    files: list[Path] = []
    for pattern in ("*.md", "*.markdown"):
        if recursive:
            files.extend(input_dir.rglob(pattern))
        else:
            files.extend(input_dir.glob(pattern))
    return sorted(set(files))


async def process_markdown(llm: ChatOpenAI, content: str, file_name: str = "") -> str:
    """异步调用 LLM 处理 Markdown 内容。"""
    messages = [
        SystemMessage(content=SYSTEM_PROMPT),
        HumanMessage(
            content=f"文件名：{file_name}\n\n{content}" if file_name else content
        ),
    ]
    response = await llm.ainvoke(messages)
    return response.content


async def process_file(
    llm: ChatOpenAI,
    input_file: Path,
    output_file: Path,
    semaphore: asyncio.Semaphore,
    retry: int = 3,
    retry_delay: float = 5.0,
) -> tuple[Path, bool]:
    """
    异步处理单个 Markdown 文件。
    使用 semaphore 控制并发数量，返回 (文件路径, 是否成功)。
    """
    async with semaphore:
        content = read_markdown(input_file)
        if not content.strip():
            print(f"  [跳过] 文件为空: {input_file}")
            return input_file, False

        for attempt in range(1, retry + 1):
            try:
                print(f"  [处理中] {input_file.name}  (尝试 {attempt}/{retry})")
                processed = await process_markdown(llm, content, input_file.name)
                write_markdown(output_file, processed)
                print(f"  [完成] → {output_file}")
                return input_file, True
            except Exception as exc:
                print(f"  [错误] {input_file.name}: {exc}")
                if attempt < retry:
                    print(f"         等待 {retry_delay}s 后重试...")
                    await asyncio.sleep(retry_delay)
                else:
                    print(f"  [放弃] {input_file.name}（已重试 {retry} 次）")
                    return input_file, False

    return input_file, False


async def process_directory(
    llm: ChatOpenAI,
    input_dir: Path,
    output_dir: Path,
    semaphore: asyncio.Semaphore,
    recursive: bool = False,
    retry: int = 3,
    retry_delay: float = 5.0,
) -> tuple[int, int]:
    """
    异步处理单个目录下的所有 Markdown 文件。
    返回 (成功数, 失败数)。
    """
    md_files = collect_markdown_files(input_dir, recursive)
    if not md_files:
        print(f"[目录] 未找到 Markdown 文件: {input_dir}")
        return 0, 0

    print(f"\n[目录] {input_dir}  ({len(md_files)} 个文件)")

    tasks = []
    for input_file in md_files:
        rel_path = input_file.relative_to(input_dir)
        output_file = output_dir / rel_path
        tasks.append(
            process_file(llm, input_file, output_file, semaphore, retry, retry_delay)
        )

    results = await asyncio.gather(*tasks)
    success = sum(1 for _, ok in results if ok)
    fail = len(results) - success
    return success, fail


def normalize_path(raw: str) -> Path:
    """
    将路径字符串统一转换为当前系统可用的 Path 对象。
    兼容以下场景：
    - 原生 Windows 路径：C:\\foo\\bar 或 C:/foo/bar
    - WSL 下传入 Windows 路径：自动转换为 /mnt/<drive>/...
    - 普通 POSIX 路径：直接使用
    """
    import re
    import platform

    # 匹配 Windows 绝对路径，如 E:\xxx 或 E:/xxx
    win_abs = re.match(r'^([A-Za-z]):[/\\](.*)', raw)
    if win_abs:
        drive, rest = win_abs.group(1).lower(), win_abs.group(2)
        if platform.system() != "Windows":
            # WSL / Linux 环境：转换为 /mnt/<drive>/...
            rest_posix = rest.replace("\\", "/")
            return Path(f"/mnt/{drive}/{rest_posix}").resolve()
        else:
            # 原生 Windows 环境：直接使用
            return Path(raw).resolve()

    return Path(raw).resolve()


async def async_main(args: argparse.Namespace) -> None:
    """异步主流程：并发处理多个目录。"""
    # 解析输入目录列表
    input_dirs: list[Path] = []
    for d in args.input_dirs:
        p = normalize_path(d)
        if not p.is_dir():
            print(f"错误: 输入目录不存在: {p}")
            sys.exit(1)
        input_dirs.append(p)

    output_base = Path(args.output_dir).resolve() if args.output_dir else None

    llm = create_llm(
        base_url=args.base_url,
        api_key=args.api_key,
        model=args.model,
        temperature=args.temperature,
    )

    semaphore = asyncio.Semaphore(args.concurrency)

    print("╔══════════════════════════════════════════╗")
    print("║  Markdown 处理脚本                  ║")
    print("╠══════════════════════════════════════════╣")
    print(f"║  输入目录数: {len(input_dirs):<28d}║")
    print(f"║  API 地址:   {args.base_url:<27s}║")
    print(f"║  模型:       {args.model:<27s}║")
    print(f"║  并发数:     {args.concurrency:<27d}║")
    print(f"║  递归模式:   {'是' if args.recursive else '否':<27s}║")
    print("╚══════════════════════════════════════════╝")

    dir_tasks = []
    for input_dir in input_dirs:
        # 多目录时：output_base/<input_dir_name>/；单目录时直接用 output_base
        if output_base:
            out_dir = output_base if len(input_dirs) == 1 else output_base / input_dir.name
        else:
            out_dir = input_dir / "_processed"

        dir_tasks.append(
            process_directory(
                llm, input_dir, out_dir, semaphore,
                args.recursive, args.retry, args.retry_delay,
            )
        )

    # 多目录并发执行
    dir_results = await asyncio.gather(*dir_tasks)

    total_success = sum(s for s, _ in dir_results)
    total_fail = sum(f for _, f in dir_results)

    print("\n═══ 处理完成 ═══")
    print(f"  成功: {total_success}")
    print(f"  失败: {total_fail}")
    print(f"  总计: {total_success + total_fail}")

    if total_fail > 0:
        sys.exit(1)


def main() -> None:
    FILE_DIR = "E:\\03-splendor\\weplat"
    parser = argparse.ArgumentParser(
        description="Markdown处理脚本 - 通过本地 OpenAI 兼容 API 批量处理 Markdown 文档"
    )
    parser.add_argument(
        "--input-dirs", "-i",
        nargs="+",
        default=[os.path.join(FILE_DIR, v) for v in ["20260420", "20260421", "20260422", "20260423"]],
        # default=[os.path.join(FILE_DIR, v) for v in ["20260420"]],
        help="一个或多个包含待处理 Markdown 文件的输入目录",
    )
    parser.add_argument(
        "--output-dir", "-o",
        type=str,
        default=None,
        help="处理后文件的输出目录（单目录时直接使用；多目录时作为父目录，默认: 各输入目录下的 _processed 子目录）",
    )
    parser.add_argument(
        "--base-url",
        type=str,
        default="http://127.0.0.1:7861/v1",
        help="OpenAI 兼容 API 地址（默认: http://127.0.0.1:7861/v1）",
    )
    parser.add_argument(
        "--api-key",
        type=str,
        default="admin",
        help="API 密钥（默认: admin）",
    )
    parser.add_argument(
        "--model", "-m",
        type=str,
        default="doubao",
        help="模型名称（默认: doubao）",
    )
    parser.add_argument(
        "--temperature", "-t",
        type=float,
        default=0.01,
        help="生成温度（默认: 0.01）",
    )
    parser.add_argument(
        "--recursive", "-r",
        action="store_true",
        help="递归处理子目录中的 Markdown 文件",
    )
    parser.add_argument(
        "--concurrency", "-c",
        type=int,
        default=3,
        help="最大并发请求数（默认: 3）",
    )
    parser.add_argument(
        "--retry",
        type=int,
        default=3,
        help="单文件失败重试次数（默认: 3）",
    )
    parser.add_argument(
        "--retry-delay",
        type=float,
        default=5.0,
        help="重试间隔秒数（默认: 5.0）",
    )

    args = parser.parse_args()

    asyncio.run(async_main(args))


if __name__ == "__main__":
    main()

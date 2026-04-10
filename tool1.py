from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Iterable

from PIL import Image
from rembg import remove, new_session


def iter_image_paths(images_dir: Path, exts: Iterable[str]) -> list[Path]:
    exts_lc = {e.lower() for e in exts}
    paths: list[Path] = []
    for p in images_dir.iterdir():
        if p.is_file() and p.suffix.lower() in exts_lc:
            paths.append(p)
    return sorted(paths)


_thread_local = threading.local()


def get_thread_session():
    """
    每个线程各自维护一个 rembg session，避免并发时的潜在线程安全问题，
    同时又不会像多进程那样重复加载太多次模型。
    """
    session = getattr(_thread_local, "session", None)
    if session is None:
        # 这里用默认模型；如需更快/更准可在 rembg 文档中调整模型名称参数。
        session = new_session()
        _thread_local.session = session
    return session


def process_one(img_path: Path, output_path: Path) -> str:
    """
    返回状态字符串：OK/FAIL:reason
    """
    try:
        # 不缩放：保持原始分辨率，符合“清晰度不变”
        with Image.open(img_path) as im:
            im = im.convert("RGBA")

            session = get_thread_session()
            # alpha_matting=False：更快，且通常更不容易改变边缘细节
            try:
                out = remove(im, session=session, alpha_matting=False)
            except TypeError:
                # 兼容不同 rembg 版本：若 alpha_matting 参数不被支持，则退回到默认行为。
                out = remove(im, session=session)

        output_path.parent.mkdir(parents=True, exist_ok=True)
        out.save(output_path, format="PNG")
        return "OK"
    except Exception as e:
        return f"FAIL:{type(e).__name__}:{e}"


def main() -> None:
    base_dir = Path(__file__).resolve().parent
    images_dir = base_dir / "./cache/inputImg"
    output_dir = base_dir / "./cache/outputImg"

    if not images_dir.exists():
        raise FileNotFoundError(f"找不到目录：{images_dir}")

    exts = [".jpg", ".jpeg", ".png", ".webp", ".bmp"]
    img_paths = iter_image_paths(images_dir, exts)
    if not img_paths:
        print(f"images 目录下没有图片：{images_dir}")
        return

    # 并发参数：默认限制并发线程数，避免同时加载多个模型导致更慢
    cpu_cnt = os.cpu_count() or 4
    max_workers = min(4, cpu_cnt)
    max_workers_env = os.getenv("REM_BG_WORKERS")
    if max_workers_env:
        try:
            max_workers = max(1, int(max_workers_env))
        except ValueError:
            pass

    futures = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        for p in img_paths:
            out_name = p.stem + ".png"  # 输出透明 PNG
            out_path = output_dir / out_name
            futures.append(executor.submit(process_one, p, out_path))

        ok = 0
        fail = 0
        for p, fut in zip(img_paths, futures):
            _ = fut  # 只是占位，下面统一 as_completed 处理

        for fut in as_completed(futures):
            res = fut.result()
            if res == "OK":
                ok += 1
            else:
                fail += 1
                print(res)

    print(f"完成：共 {len(img_paths)} 张，成功 {ok}，失败 {fail}。输出目录：{output_dir}")


if __name__ == "__main__":
    main()


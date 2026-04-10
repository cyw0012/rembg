from __future__ import annotations

import os
import shutil
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import BytesIO
from pathlib import Path
from typing import Dict, List

import uvicorn
from fastapi import BackgroundTasks, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse, Response
from fastapi.staticfiles import StaticFiles

from tool1 import process_one


BASE_DIR = Path(__file__).resolve().parent
CACHE_DIR = BASE_DIR / "cache"
INPUT_DIR = CACHE_DIR / "inputImg"
OUTPUT_DIR = CACHE_DIR / "outputImg"

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}


def _ensure_dirs() -> None:
    INPUT_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def _secure_join(base: Path, relative: str) -> Path:
    """
    防止路径穿越攻击，只允许在 base 目录下。
    """
    rel = os.path.normpath(relative).lstrip("/\\")
    drive, rel_path = os.path.splitdrive(rel)
    rel_path = rel_path.lstrip("/\\")
    full = (base / rel_path).resolve()
    if not str(full).startswith(str(base.resolve())):
        raise HTTPException(status_code=400, detail="非法路径")
    return full


def _iter_images_recursive(root: Path) -> List[Path]:
    result: List[Path] = []
    if not root.exists():
        return result
    for dirpath, _, filenames in os.walk(root):
        d = Path(dirpath)
        for name in filenames:
            p = d / name
            if p.suffix.lower() in IMAGE_EXTS:
                result.append(p)
    result.sort()
    return result


def _rel_to_output(p: Path) -> str:
    """
    返回相对于 cache 目录的路径字符串，例如 'outputImg/xxx.png'。
    """
    return str(p.resolve().relative_to(CACHE_DIR.resolve())).replace("\\", "/")


JobState = Dict[str, object]
JOBS: Dict[str, JobState] = {}
JOBS_LOCK = threading.Lock()
CACHE_LOCK = threading.Lock()


def _start_job(job_id: str, total: int) -> None:
    with JOBS_LOCK:
        JOBS[job_id] = {
            "status": "pending",
            "total": total,
            "done": 0,
            "ok": 0,
            "fail": 0,
            "error": None,
        }


def _update_job_progress(job_id: str, *, ok_inc: int = 0, fail_inc: int = 0) -> None:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return
        job["done"] = int(job.get("done", 0)) + ok_inc + fail_inc
        job["ok"] = int(job.get("ok", 0)) + ok_inc
        job["fail"] = int(job.get("fail", 0)) + fail_inc


def _set_job_status(job_id: str, status: str, error: str | None = None) -> None:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return
        job["status"] = status
        job["error"] = error


def _get_job(job_id: str) -> JobState | None:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return None
        return dict(job)


def _run_cutout_job(job_id: str, input_dir: Path, output_dir: Path) -> None:
    try:
        _set_job_status(job_id, "running")
        img_paths = _iter_images_recursive(input_dir)
        if not img_paths:
            _set_job_status(job_id, "done")
            return

        cpu_cnt = os.cpu_count() or 4
        max_workers = min(4, cpu_cnt)
        env_workers = os.getenv("REM_BG_WORKERS")
        if env_workers:
            try:
                max_workers = max(1, int(env_workers))
            except ValueError:
                pass

        futures = []
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            for src in img_paths:
                rel = src.relative_to(input_dir)
                dst = output_dir / rel
                dst = dst.with_suffix(".png")
                futures.append(executor.submit(process_one, src, dst))

            for fut in as_completed(futures):
                try:
                    res = fut.result()
                except Exception as e:  # noqa: BLE001
                    _update_job_progress(job_id, ok_inc=0, fail_inc=1)
                    _set_job_status(job_id, "error", error=str(e))
                    continue

                if res == "OK":
                    _update_job_progress(job_id, ok_inc=1, fail_inc=0)
                else:
                    _update_job_progress(job_id, ok_inc=0, fail_inc=1)

        job = _get_job(job_id)
        if job is not None and job.get("status") != "error":
            _set_job_status(job_id, "done")
    except Exception as e:  # noqa: BLE001
        _set_job_status(job_id, "error", error=str(e))


app = FastAPI()

_ensure_dirs()

app.mount("/cache", StaticFiles(directory=str(CACHE_DIR)), name="cache")


@app.get("/", response_class=HTMLResponse)
def read_index() -> HTMLResponse:
    index_path = BASE_DIR / "index.html"
    if not index_path.exists():
        raise HTTPException(status_code=404, detail="index.html 未找到，请先创建前端文件。")
    return HTMLResponse(index_path.read_text(encoding="utf-8"))


@app.post("/api/trim")
async def upload_and_start_trim(
    background_tasks: BackgroundTasks,
    files: List[UploadFile] = File(...),
) -> JSONResponse:
    if not files:
        raise HTTPException(status_code=400, detail="未上传任何文件")

    with CACHE_LOCK:
        if INPUT_DIR.exists():
            shutil.rmtree(INPUT_DIR)
        if OUTPUT_DIR.exists():
            shutil.rmtree(OUTPUT_DIR)
        _ensure_dirs()

        for file in files:
            filename = file.filename or "unnamed"
            dest_path = _secure_join(INPUT_DIR, filename)
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            content = await file.read()
            dest_path.write_bytes(content)

    img_paths = _iter_images_recursive(INPUT_DIR)
    total = len(img_paths)
    if total == 0:
        raise HTTPException(status_code=400, detail="上传内容中未找到可处理的图片")

    job_id = uuid.uuid4().hex
    _start_job(job_id, total=total)

    background_tasks.add_task(_run_cutout_job, job_id, INPUT_DIR, OUTPUT_DIR)

    return JSONResponse({"job_id": job_id, "total": total})


@app.get("/api/trim/{job_id}/progress")
def get_progress(job_id: str) -> JSONResponse:
    job = _get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在")

    total = int(job.get("total", 0))
    done = int(job.get("done", 0))
    percent = int(done * 100 / total) if total > 0 else 0

    data = {
        "status": job.get("status"),
        "total": total,
        "done": done,
        "percent": percent,
        "ok": int(job.get("ok", 0)),
        "fail": int(job.get("fail", 0)),
        "error": job.get("error"),
    }
    return JSONResponse(data)


@app.get("/api/trim/{job_id}/outputs")
def list_outputs(job_id: str) -> JSONResponse:
    job = _get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if job.get("status") not in {"done", "error", "running"}:
        raise HTTPException(status_code=400, detail="任务未开始")

    results: List[str] = []
    if OUTPUT_DIR.exists():
        for dirpath, _, filenames in os.walk(OUTPUT_DIR):
            d = Path(dirpath)
            for name in filenames:
                p = d / name
                if p.suffix.lower() in IMAGE_EXTS or p.suffix.lower() == ".png":
                    results.append(_rel_to_output(p))
    results.sort()
    return JSONResponse({"files": results})


@app.get("/api/trim/{job_id}/download", response_model=None)
def download_zip(job_id: str) -> Response:
    job = _get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if job.get("status") not in {"done", "error"}:
        raise HTTPException(status_code=400, detail="任务尚未完成")

    if not OUTPUT_DIR.exists():
        raise HTTPException(status_code=404, detail="没有输出结果")

    # 收集所有输出文件
    output_files: list[Path] = []
    for dirpath, _, filenames in os.walk(OUTPUT_DIR):
        d = Path(dirpath)
        for name in filenames:
            p = d / name
            if p.is_file():
                output_files.append(p)

    if not output_files:
        raise HTTPException(status_code=404, detail="没有输出结果")

    # 只有单张图片时，直接返回该图片文件，触发浏览器下载
    if len(output_files) == 1:
        img_path = output_files[0]
        filename = img_path.name
        return FileResponse(
            img_path,
            media_type="image/png",
            filename=filename,
        )

    # 多张图片时，打包为 ZIP 返回
    import zipfile

    mem_file = BytesIO()
    with zipfile.ZipFile(mem_file, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for p in output_files:
            arcname = p.relative_to(OUTPUT_DIR).as_posix()
            zf.write(p, arcname)

    mem_file.seek(0)
    filename = f"cutout_{job_id}.zip"
    headers = {"Content-Disposition": f'attachment; filename=\"{filename}\"'}
    return StreamingResponse(mem_file, media_type="application/zip", headers=headers)


# 前端界面：http://127.0.0.1:8000/
if __name__ == "__main__":
    uvicorn.run("fastapi1:app", host="0.0.0.0", port=8000, reload=True)


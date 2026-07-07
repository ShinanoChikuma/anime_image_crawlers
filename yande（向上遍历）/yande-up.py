import requests
import re
import os
import time
from PIL import Image, ExifTags
from requests.exceptions import RequestException
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock

SAVE_PATH = "./downloads"  # 请根据需要修改为实际保存路径
MAX_WORKERS = 5
CONSECUTIVE_404_LIMIT = 5

print_lock = Lock()
skipped_ids: set[int] = set()
skipped_lock = Lock()


def log(msg: str):
    with print_lock:
        print(msg)


def add_skipped(post_id: int):
    with skipped_lock:
        skipped_ids.add(post_id)


# ── 图片下载 ────────────────────────────────────────────────────────────────

def download_image(image_url: str) -> tuple[str | None, str]:
    """返回 (保存路径 | None, status)  status: exists | downloaded | error"""
    filename = image_url.split("/")[-1].split("?")[0]
    dest = os.path.join(SAVE_PATH, filename)

    if os.path.exists(dest):
        log(f"[跳过] 已存在：{dest}")
        return dest, "exists"

    for attempt in range(1, 6):
        try:
            r = requests.get(image_url, timeout=15)
            if r.status_code == 200:
                with open(dest, "wb") as f:
                    f.write(r.content)
                log(f"[下载] {image_url}")
                return dest, "downloaded"
            log(f"[失败] HTTP {r.status_code}，URL：{image_url}")
            return None, "error"
        except (RequestException, OSError) as e:
            log(f"[重试 {attempt}/5] {e}")
            if attempt < 5:
                time.sleep(5)

    log(f"[放弃] 多次下载失败：{image_url}")
    return None, "error"


# ── 文件重命名 ───────────────────────────────────────────────────────────────

def rename_by_id(path: str, post_id: int) -> str:
    """按图片ID重命名。"""
    try:
        ext = os.path.splitext(path)[1]
        base = str(post_id)

        new_path = os.path.join(os.path.dirname(path), f"{base}{ext}")

        if os.path.exists(new_path):
            i = 1
            while os.path.exists(
                os.path.join(os.path.dirname(path), f"{base}_{i}{ext}")
            ):
                i += 1
            new_path = os.path.join(
                os.path.dirname(path),
                f"{base}_{i}{ext}"
            )

        os.replace(path, new_path)
        log(f"[重命名] {os.path.basename(path)} -> {os.path.basename(new_path)}")
        return new_path

    except Exception as e:
        log(f"[错误] 重命名失败：{e}")
        return path


# ── 页面解析 ─────────────────────────────────────────────────────────────────

def is_suspended(html: str) -> bool:
    return "此图帖被上传者暂时挂起" in html or \
           "This post has been temporarily held from the index by the poster." in html


def extract_image_url(html: str) -> str:
    m = re.search(r'https://files\.yande\.re/image/[^\s"<>]+', html)
    return m.group(0) if m else ""


# ── 单 ID 处理（供线程池调用）────────────────────────────────────────────────

def process_id(post_id: int, session: requests.Session) -> str:
    """返回处理结果描述字符串，副作用：下载文件、更新 skipped_ids。"""
    url = f"https://yande.re/post/show/{post_id}"
    try:
        resp = session.get(url, timeout=10)
    except RequestException as e:
        log(f"[错误] ID={post_id} 网络异常：{e}")
        add_skipped(post_id)
        return "error"

    if resp.status_code == 404:
        log(f"[跳过] ID={post_id} HTTP 404")
        add_skipped(post_id)
        return "404"

    if resp.status_code != 200:
        log(f"[跳过] ID={post_id} HTTP {resp.status_code}")
        add_skipped(post_id)
        return "skip"

    img_url = extract_image_url(resp.text)
    if not img_url:
        log(f"[警告] ID={post_id} 未提取到图片 URL")
        add_skipped(post_id)
        return "no_url"

    saved, status = download_image(img_url)
    if saved:
        if status == "exists":
            add_skipped(post_id)
        rename_by_id(saved, post_id)
        return status
    else:
        add_skipped(post_id)
        return "error"


# ── 主逻辑 ────────────────────────────────────────────────────────────────────

def compress_ranges(ids: list[int]) -> str:
    if not ids:
        return ""
    parts, s, p = [], ids[0], ids[0]
    for n in ids[1:]:
        if n == p + 1:
            p = n
        else:
            parts.append(str(s) if s == p else f"{s}-{p}")
            s = p = n
    parts.append(str(s) if s == p else f"{s}-{p}")
    return ", ".join(parts)


def main():
    raw = input("请输入起始图片 ID（上次 1265153）：").strip()
    m = re.search(r'\d+', raw)
    if not m:
        print("无法解析 ID，退出。")
        return
    start_id = int(m.group(0))

    os.makedirs(SAVE_PATH, exist_ok=True)

    session = requests.Session()
    session.headers["User-Agent"] = "Mozilla/5.0 (compatible; yande-scraper/1.0)"

    consecutive_404 = 0
    first_404_id = None
    current_id = start_id

    try:
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = {}  # future -> post_id

            while True:
                # 补充任务直到线程池满
                while len(futures) < MAX_WORKERS:
                    fut = pool.submit(process_id, current_id, session)
                    futures[fut] = current_id
                    current_id += 1

                # 等待任意一个完成
                done = next(as_completed(futures))
                post_id = futures.pop(done)
                result = done.result()

                # 404 连续计数
                if result == "404":
                    consecutive_404 += 1
                    if first_404_id is None:
                        first_404_id = post_id
                    if consecutive_404 >= CONSECUTIVE_404_LIMIT:
                        print(f"\n连续 {CONSECUTIVE_404_LIMIT} 次 404，停止（首次：{first_404_id}）")
                        pool.shutdown(wait=False, cancel_futures=True)
                        break
                else:
                    consecutive_404 = 0

                time.sleep(0.2)  # 轻微限速，避免请求过密

    except KeyboardInterrupt:
        print("\n[中断] 用户手动中断。")
    finally:
        sorted_ids = sorted(skipped_ids)
        if sorted_ids:
            print(f"\n=== 已跳过 ID（共 {len(sorted_ids)} 个）===")
            print(compress_ranges(sorted_ids))
        else:
            print("\n程序结束 — 无跳过 ID。")


if __name__ == "__main__":
    main()
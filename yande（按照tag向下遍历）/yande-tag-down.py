import requests
import re
import os
import time
from datetime import datetime
from PIL import Image, ExifTags
from requests.exceptions import RequestException
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
from urllib.parse import unquote_plus
import html

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

def rename_by_time(path: str) -> str:
    """按 EXIF 拍摄时间或文件修改时间重命名，跳过已同名文件。"""
    base = None
    try:
        try:
            with Image.open(path) as img:
                exif = img.getexif()
            if exif:
                for tag_id, val in exif.items():
                    if ExifTags.TAGS.get(tag_id) in ("DateTimeOriginal", "DateTime"):
                        date_str, time_str = str(val).split(" ")
                        base = f"{date_str.replace(':', '')}_{time_str.replace(':', '')}"
                        break
        except Exception:
            pass

        if not base:
            dt = datetime.fromtimestamp(os.path.getmtime(path))
            base = dt.strftime("%Y%m%d_%H%M%S")

        ext = os.path.splitext(path)[1]
        new_path = os.path.join(os.path.dirname(path), f"{base}{ext}")

        if os.path.exists(new_path):
            i = 1
            while os.path.exists(os.path.join(os.path.dirname(path), f"{base}_{i}{ext}")):
                i += 1
            new_path = os.path.join(os.path.dirname(path), f"{base}_{i}{ext}")

        os.replace(path, new_path)
        log(f"[重命名] {os.path.basename(path)} -> {os.path.basename(new_path)}")
        return new_path
    except Exception as e:
        log(f"[错误] 重命名失败：{e}")
        return path


# ── 页面解析 ─────────────────────────────────────────────────────────────────

def extract_image_url(html_text: str) -> str:
    m = re.search(r'https://files\.yande\.re/image/[^\s"<>]+', html_text)
    return m.group(0) if m else ""


def page_has_tag(html_text: str, target_tag: str) -> bool:
    """
    从页面里提取 tag 链接，判断是否包含目标 tag。
    例如：/post?tags=takamiya_keika
    """
    target_tag = target_tag.strip().lower()

    found_tags = set()
    for m in re.finditer(r'href="/post\?tags=([^"#&]+)"', html_text):
        tag = unquote_plus(m.group(1)).strip().lower()
        found_tags.add(tag)

    if target_tag in found_tags:
        return True

    if re.search(rf'(?<![\w-]){re.escape(target_tag)}(?![\w-])', html.unescape(html_text), re.IGNORECASE):
        return True

    return False


# ── 单 ID 处理（供线程池调用）────────────────────────────────────────────────

def process_id(post_id: int, session: requests.Session, target_tag: str) -> str:
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

    html_text = resp.text

    # 先检查 tag，不符合就跳过
    if target_tag and not page_has_tag(html_text, target_tag):
        log(f"[跳过] ID={post_id} 不包含 tag：{target_tag}")
        add_skipped(post_id)
        return "tag_skip"

    # 挂起也下载，不做跳过处理

    img_url = extract_image_url(html_text)
    if not img_url:
        log(f"[警告] ID={post_id} 未提取到图片 URL")
        add_skipped(post_id)
        return "no_url"

    saved, status = download_image(img_url)
    if saved:
        if status == "exists":
            add_skipped(post_id)
        rename_by_time(saved)
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
    raw = input("请输入起始图片 ID（例如 1263409）：").strip()
    m = re.search(r'\d+', raw)
    if not m:
        print("无法解析 ID，退出。")
        return
    start_id = int(m.group(0))

    target_tag = input("请输入要匹配的 tag（例如 takamiya_keika）：").strip().lower()
    if not target_tag:
        print("未输入 tag，退出。")
        return

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
                    fut = pool.submit(process_id, current_id, session, target_tag)
                    futures[fut] = current_id
                    current_id -= 1  # 改为向下遍历

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
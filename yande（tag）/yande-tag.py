import os
import re
import sys
import time
import random
import threading
from dataclasses import dataclass
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


BASE_LIST_URL = "https://yande.re/post?page={page}&tags={tags}"

# 并发不要太大，避免请求过猛
MAX_POST_WORKERS = 4
MAX_DOWNLOAD_WORKERS = 4

# 超时与重试
CONNECT_TIMEOUT = 10
READ_TIMEOUT = 30
RETRY_TOTAL = 6
BACKOFF_FACTOR = 1.2

# 下载块大小
CHUNK_SIZE = 64 * 1024

POST_URL_PATTERN = re.compile(r"https://yande\.re/post/show/\d+")
POST_ID_PATTERN = re.compile(r"/post/show/(\d+)")
IMAGE_URL_PATTERN = re.compile(r"https://files\.yande\.re/image/[^\"'\s>]+")


@dataclass
class Stats:
    bytes_downloaded: int = 0
    files_downloaded: int = 0
    files_failed: int = 0
    total_files: int = 0
    start_time: float = time.monotonic()
    lock: threading.Lock = threading.Lock()


def create_session() -> requests.Session:
    session = requests.Session()

    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Referer": "https://yande.re/",
        "Connection": "keep-alive",
    })

    retry = Retry(
        total=RETRY_TOTAL,
        connect=RETRY_TOTAL,
        read=RETRY_TOTAL,
        status=RETRY_TOTAL,
        backoff_factor=BACKOFF_FACTOR,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=frozenset(["GET", "HEAD"]),
        raise_on_status=False,
    )

    adapter = HTTPAdapter(
        max_retries=retry,
        pool_connections=MAX_POST_WORKERS + MAX_DOWNLOAD_WORKERS + 4,
        pool_maxsize=MAX_POST_WORKERS + MAX_DOWNLOAD_WORKERS + 4,
    )
    session.mount("http://", adapter)
    session.mount("https://", adapter)

    return session


def request_text(session: requests.Session, url: str) -> str:
    resp = session.get(url, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT))
    resp.raise_for_status()
    return resp.text


def extract_post_urls(html: str) -> list[str]:
    # 保序去重
    return list(dict.fromkeys(POST_URL_PATTERN.findall(html)))


def extract_image_url(post_html: str) -> str | None:
    m = IMAGE_URL_PATTERN.search(post_html)
    if m:
        return m.group(0)
    return None


def unique_path(directory: str, filename: str) -> str:
    base, ext = os.path.splitext(filename)
    candidate = os.path.join(directory, filename)
    idx = 1

    while os.path.exists(candidate):
        candidate = os.path.join(directory, f"{base}_{idx}{ext}")
        idx += 1

    return candidate


def collect_all_post_urls(session: requests.Session, tags: str, start_page: int) -> list[str]:
    page = start_page
    all_posts: list[str] = []

    while True:
        url = BASE_LIST_URL.format(page=page, tags=tags)
        print(f"扫描第 {page} 页：{url}")

        try:
            html = request_text(session, url)
        except Exception as e:
            print(f"第 {page} 页请求失败：{e}，稍后重试...")
            time.sleep(2)
            continue

        posts = extract_post_urls(html)
        if not posts:
            print(f"第 {page} 页未找到帖子链接，扫描结束。")
            break

        all_posts.extend(posts)
        page += 1

        # 小幅抖动，降低请求节奏的机械性
        time.sleep(random.uniform(0.15, 0.35))

    # 全局去重，保序
    return list(dict.fromkeys(all_posts))


def resolve_image_urls(session: requests.Session, post_urls: list[str]) -> list[tuple[str, str]]:
    """返回 (image_url, post_id) 配对列表，按原序去重。"""
    results: list[tuple[str, str]] = []

    def worker(post_url: str) -> tuple[str, str] | None:
        # 从 post URL 中提取 post ID
        pid_match = POST_ID_PATTERN.search(post_url)
        post_id = pid_match.group(1) if pid_match else None

        try:
            post_html = request_text(session, post_url)
            img_url = extract_image_url(post_html)
            if img_url and post_id:
                return (img_url, post_id)
        except Exception as e:
            print(f"解析帖子失败：{post_url} -> {e}")
        return None

    with ThreadPoolExecutor(max_workers=MAX_POST_WORKERS) as executor:
        futures = [executor.submit(worker, url) for url in post_urls]

        for fut in as_completed(futures):
            result = fut.result()
            if result:
                results.append(result)

    # 按 image_url 去重，保序
    seen: set[str] = set()
    unique: list[tuple[str, str]] = []
    for img_url, post_id in results:
        if img_url not in seen:
            seen.add(img_url)
            unique.append((img_url, post_id))

    return unique


def download_one_image(session: requests.Session, image_url: str, post_id: str, save_dir: str, stats: Stats) -> None:
    # 从图片 URL 取扩展名
    parsed = urlparse(image_url)
    ext = os.path.splitext(parsed.path)[1]
    if not ext:
        ext = ".jpg"

    filename = f"{post_id}{ext}"
    save_path = unique_path(save_dir, filename)
    temp_path = save_path + ".part"

    try:
        with session.get(image_url, stream=True, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT)) as resp:
            resp.raise_for_status()

            with open(temp_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=CHUNK_SIZE):
                    if not chunk:
                        continue
                    f.write(chunk)
                    with stats.lock:
                        stats.bytes_downloaded += len(chunk)

        os.replace(temp_path, save_path)

        with stats.lock:
            stats.files_downloaded += 1

    except Exception:
        # 清理未完成的 .part 临时文件
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass
        with stats.lock:
            stats.files_failed += 1
        raise


def human_size(num_bytes: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(num_bytes)
    for unit in units:
        if size < 1024.0:
            return f"{size:.2f}{unit}"
        size /= 1024.0
    return f"{size:.2f}PB"


def render_progress(stats: Stats) -> str:
    with stats.lock:
        total = max(stats.total_files, 1)
        total_bytes = stats.bytes_downloaded
        elapsed = max(time.monotonic() - stats.start_time, 0.001)
        speed = total_bytes / elapsed
        done = stats.files_downloaded
        failed = stats.files_failed

    percent = done / total
    bar_len = 5
    filled = int(bar_len * percent)
    bar = "█" * filled + "░" * (bar_len - filled)

    line = f"下载进度 [{bar}] {percent * 100:6.2f}% | 速度 {human_size(int(speed))}/s"
    if failed:
        line += f" | 失败 {failed}"
    return line


def monitor_progress(stats: Stats, stop_event: threading.Event) -> None:
    last_len = 0
    while not stop_event.is_set():
        line = render_progress(stats)
        pad = max(last_len - len(line), 0)
        sys.stdout.write("\r" + line + (" " * pad))
        sys.stdout.flush()
        last_len = len(line)
        time.sleep(0.5)

    # 最后再刷新一次，避免停在旧值
    line = render_progress(stats)
    pad = max(last_len - len(line), 0)
    sys.stdout.write("\r" + line + (" " * pad) + "\n")
    sys.stdout.flush()


def main():
    tags_input = input("请输入需要搜索的 tags：").strip()
    start_page = int(input("请输入起始页数：").strip())

    # 自动生成保存路径：./downloads/{tag}
    save_dir = os.path.join("./downloads", tags_input)
    if not os.path.exists(save_dir):
        os.makedirs(save_dir)
        print(f"已创建文件夹：{save_dir}")
    else:
        print(f"文件夹已存在，直接使用：{save_dir}")

    session = create_session()

    print("\n开始扫描所有页，统计总图片数...\n")
    post_urls = collect_all_post_urls(session, tags_input, start_page)

    if not post_urls:
        print("没有找到任何帖子。")
        return

    print(f"\n帖子总数：{len(post_urls)}")
    print("开始解析图片链接...\n")
    image_pairs = resolve_image_urls(session, post_urls)

    if not image_pairs:
        print("没有解析到任何图片链接。")
        return

    total = len(image_pairs)
    print(f"\n共找到 {total} 张图片，开始下载...\n")

    stats = Stats(total_files=total)
    stop_event = threading.Event()
    monitor_thread = threading.Thread(target=monitor_progress, args=(stats, stop_event), daemon=True)
    monitor_thread.start()

    try:
        with ThreadPoolExecutor(max_workers=MAX_DOWNLOAD_WORKERS) as executor:
            futures = [
                executor.submit(download_one_image, session, image_url, post_id, save_dir, stats)
                for image_url, post_id in image_pairs
            ]

            for fut in as_completed(futures):
                try:
                    fut.result()
                except Exception as e:
                    print(f"\n下载任务出错：{e}")

    finally:
        stop_event.set()
        monitor_thread.join(timeout=1)

    with stats.lock:
        final_bytes = stats.bytes_downloaded
        final_done = stats.files_downloaded
        final_failed = stats.files_failed

    print("\n下载完成。")
    print(f"成功：{final_done} 张，失败：{final_failed} 张")
    print(f"累计下载文件大小：{human_size(final_bytes)}")
    print(f"保存目录：{save_dir}")


if __name__ == "__main__":
    main()
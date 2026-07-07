import os
import re
import json
import time
import mimetypes
from pathlib import Path
from urllib.parse import urlparse

import requests


BASE_URL = "https://danbooru.donmai.us"
TAG = "takamiya_keika"
OUT_DIR = Path("downloads") / TAG

# 如果你的环境里不需要认证，这两项可以留空
DANBOORU_USER = os.getenv("DANBOORU_USER", "")
DANBOORU_API_KEY = os.getenv("DANBOORU_API_KEY", "")

# 每页多少条；Danbooru 常见分页接口支持 page + tags 这种方式
LIMIT = 100

# 控制下载速度，别太猛
SLEEP_BETWEEN_PAGES = 0.8
SLEEP_BETWEEN_FILES = 0.2


def safe_filename(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|]+', "_", name)
    name = re.sub(r"\s+", "_", name).strip("_")
    return name[:180] if len(name) > 180 else name


def guess_ext_from_url(url: str) -> str:
    suffix = Path(urlparse(url).path).suffix
    if suffix:
        return suffix
    return ""


def request_posts(session: requests.Session, tag: str, page: int, limit: int = 100):
    params = {
        "tags": tag,
        "page": page,
        "limit": limit,
    }
    if DANBOORU_USER and DANBOORU_API_KEY:
        params["user"] = DANBOORU_USER
        params["api_key"] = DANBOORU_API_KEY

    resp = session.get(f"{BASE_URL}/posts.json", params=params, timeout=30)

    # 不是 JSON 时，通常是被限制、被重定向，或者接口行为发生了变化
    content_type = resp.headers.get("Content-Type", "")
    if "json" not in content_type.lower():
        raise RuntimeError(
            f"接口没有返回 JSON，状态码={resp.status_code}，Content-Type={content_type}\n"
            f"返回内容前 200 字符：{resp.text[:200]!r}"
        )

    resp.raise_for_status()
    data = resp.json()

    if not isinstance(data, list):
        raise RuntimeError(f"预期返回 list，但拿到的是 {type(data)}: {data!r}")

    return data


def download_file(session: requests.Session, url: str, out_path: Path):
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if out_path.exists() and out_path.stat().st_size > 0:
        print(f"跳过已存在：{out_path.name}")
        return

    with session.get(url, stream=True, timeout=60) as resp:
        resp.raise_for_status()

        # 如果 URL 没后缀，尝试用 Content-Type 猜一个
        if out_path.suffix == "":
            ctype = resp.headers.get("Content-Type", "").split(";")[0].strip()
            guessed = mimetypes.guess_extension(ctype) or ""
            if guessed:
                out_path = out_path.with_suffix(guessed)

        tmp_path = out_path.with_suffix(out_path.suffix + ".part")
        with open(tmp_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1024 * 256):
                if chunk:
                    f.write(chunk)

        tmp_path.replace(out_path)
        print(f"已下载：{out_path.name}")


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "Mozilla/5.0 (compatible; DanbooruDownloader/1.0)",
            "Accept": "application/json,text/plain,*/*",
            "Referer": BASE_URL + "/",
        }
    )

    seen_ids = set()
    page = 1
    total_downloaded = 0

    while True:
        print(f"\n抓取第 {page} 页...")
        posts = request_posts(session, TAG, page, LIMIT)

        if not posts:
            print("没有更多帖子了。")
            break

        new_posts = 0

        for post in posts:
            post_id = post.get("id")
            if not post_id or post_id in seen_ids:
                continue

            seen_ids.add(post_id)
            new_posts += 1

            file_url = post.get("file_url") or post.get("large_file_url") or post.get("preview_file_url")
            if not file_url:
                print(f"跳过 {post_id}：没有可下载链接")
                continue

            md5 = post.get("md5", "")
            ext = guess_ext_from_url(file_url)

            # 文件名：ID_MD5.扩展名
            base = safe_filename(f"{post_id}_{md5}" if md5 else str(post_id))
            filename = base + ext
            out_path = OUT_DIR / filename

            try:
                download_file(session, file_url, out_path)
                total_downloaded += 1

                # 可选：把每个帖子的 JSON 元数据也保存下来
                meta_path = OUT_DIR / f"{base}.json"
                with open(meta_path, "w", encoding="utf-8") as f:
                    json.dump(post, f, ensure_ascii=False, indent=2)

                time.sleep(SLEEP_BETWEEN_FILES)
            except Exception as e:
                print(f"下载失败 {post_id}: {e}")

        print(f"本页新增 {new_posts} 条，累计下载 {total_downloaded} 个文件")
        page += 1
        time.sleep(SLEEP_BETWEEN_PAGES)


if __name__ == "__main__":
    main()
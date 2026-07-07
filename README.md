# Anime Image Crawlers

针对 Danbooru / Yande.re 的动漫图片爬虫合集，覆盖按标签检索和按 ID 遍历两种策略。

## 项目结构

```
├── danbooru（tag）/          # Danbooru 标签爬虫
│   └── danbooru-tag.py      #   调用 REST API，分页拉取
├── yande（tag）/             # Yande.re 标签爬虫（翻页模式）
│   └── yande-tag.py         #   HTML 解析，两阶段流水线
├── yande（向上遍历）/        # Yande.re ID 递增遍历
│   └── yande-up.py          #   线程池并发，连续 404 自动终止
└── yande（按照tag向下遍历）/ # Yande.re ID 递减遍历 + 标签过滤
    └── yande-tag-down.py    #   先匹配 tag 再下载，按 EXIF 重命名
```

## 技术架构

### 两种爬取策略

| 策略 | 适用场景 | 代表脚本 |
|---|---|---|
| **API 分页** | 目标站提供结构化接口，按 tag + page 拉取 | `danbooru-tag.py` |
| **ID 遍历** | 无搜索接口或需要全量扫描时，按 post ID 逐一遍历 | `yande-up.py`、`yande-tag-down.py` |
| **页面解析** | 从列表页提取 post URL，再逐个解析详情页获取图片直链 | `yande-tag.py` |

### 并发模型

- **`ThreadPoolExecutor`** — I/O 密集型任务（HTTP 请求 + 文件写入），多线程足够且实现简单。`yande-tag.py` 将 *帖子解析* 和 *文件下载* 分为两个独立线程池，避免相互阻塞。
- **速率控制** — 页间/文件间 `time.sleep()` 配合随机抖动，降低请求特征。
- **重试机制** — `urllib3.Retry` + `HTTPAdapter` 实现透明重试，覆盖 429/5xx 和连接异常。

### ID 遍历的终止条件

`yande-up.py` 和 `yande-tag-down.py` 采用 **连续 404 计数** 作为终止信号：连续 N 次 404 说明已触及站点 ID 边界，自动停止，避免无意义请求。

### 去重 & 断点续爬

- API 模式通过内存 `set` 记录已见 `post_id`，翻页时跳过重复。
- 下载前检查本地文件是否存在 (`os.path.exists`)，已下载的跳过。

## 依赖

```
requests
Pillow          # 仅 yande-up.py、yande-tag-down.py（EXIF 读取）
urllib3         # yande-tag.py 重试机制
```

安装：

```bash
pip install requests Pillow urllib3
```

## 使用方式

每个脚本独立运行，启动后会交互式询问参数（tag、起始页/ID 等），下载目录默认为脚本所在目录下的 `./downloads/`，可按需修改脚本顶部的路径常量。

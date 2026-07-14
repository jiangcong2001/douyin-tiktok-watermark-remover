import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx
import re
import json
import urllib.parse
from fastapi import FastAPI, Request, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from urllib.parse import quote

app = FastAPI(title="Watermark Remover API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1"


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/proxy")
async def proxy_media(url: str, dl: str = ""):
    if not url:
        raise HTTPException(400, "url required")
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        resp = await client.get(url, headers={"User-Agent": UA})
    headers = {"Access-Control-Allow-Origin": "*", "Cache-Control": "public, max-age=3600"}
    ct = resp.headers.get("content-type", "application/octet-stream")
    headers["Content-Type"] = ct
    if dl:
        name = dl if "." in dl else f"{dl}.mp4"
        headers["Content-Disposition"] = f'attachment; filename="{quote(name)}"'
    return Response(content=resp.content, headers=headers, status_code=resp.status_code)


@app.post("/api/parse")
async def parse(request: Request):
    try:
        body = await request.json()
        url = body.get("url", "").strip()
        if not url:
            raise HTTPException(400, "请提供URL链接")
        hostname = (urllib.parse.urlparse(url).hostname or "").replace("www.", "")

        if "douyin.com" in hostname or "iesdouyin.com" in hostname:
            result = await _parse_douyin(url)
        elif "tiktok.com" in hostname:
            result = await _parse_tiktok(url)
        elif "kuaishou.com" in hostname or "gifshow.com" in hostname:
            result = await _parse_kuaishou(url)
        elif "bilibili.com" in hostname or "b23.tv" in hostname:
            result = await _parse_bilibili(url)
        elif "instagram.com" in hostname:
            result = await _parse_instagram(url)
        elif "youtube.com" in hostname or "youtu.be" in hostname:
            result = await _parse_youtube(url)
        else:
            result = await _parse_generic(url)

        return {"success": True, "data": result}
    except HTTPException:
        raise
    except Exception as e:
        return {"success": False, "error": str(e)}


async def _parse_douyin(url: str) -> dict:
    vid = _extract_pattern(url, [r'/video/(\d+)', r'modal_id=(\d+)', r'note_id=(\d+)', r'/note/(\d+)'])
    if not vid:
        vid = await _resolve_short(url)
        if vid:
            vid = _extract_pattern(vid, [r'/video/(\d+)', r'modal_id=(\d+)'])

    if not vid:
        return _empty("douyin", "无法解析抖音链接，请使用APP内的分享链接")

    api = f"https://www.iesdouyin.com/web/api/v2/aweme/iteminfo/?item_ids={vid}"
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(api, headers={"User-Agent": UA, "Referer": "https://www.douyin.com/"})
        if resp.status_code != 200:
            return _empty("douyin", "抖音API请求失败，风控拦截，需要配置Cookie")

        data = resp.json()
        item = (data.get("item_list") or [None])[0]
        if not item:
            return _empty("douyin", "未找到该视频，链接可能已失效")

        video = item.get("video", {})
        author = item.get("author", {})
        nwm = ""
        for url_entry in video.get("play_addr", {}).get("url_list", []):
            nwm = url_entry.replace("playwm", "play")
            break

        return {
            "platform": "douyin",
            "title": item.get("desc", "抖音视频"),
            "cover": (video.get("cover", {}).get("url_list") or [""])[0],
            "video_url": nwm,
            "images": [],
            "author": author.get("nickname", ""),
        }


async def _parse_tiktok(url: str) -> dict:
    resolved = url
    if "tiktok.com/t/" in url:
        resolved = await _resolve_short(url) or url

    vid = _extract_pattern(resolved, [r'/video/(\d+)', r'/photo/(\d+)'])
    if not vid:
        return _empty("tiktok", "无法解析TikTok链接")

    try:
        api = f"https://www.tiktok.com/oembed?url=https://www.tiktok.com/@t/video/{vid}"
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(api, headers={"User-Agent": UA})
            if resp.status_code == 200:
                d = resp.json()
                return {
                    "platform": "tiktok",
                    "title": d.get("title", "TikTok"),
                    "cover": d.get("thumbnail_url", ""),
                    "video_url": "",
                    "images": [],
                    "author": d.get("author_name", ""),
                }
    except Exception:
        pass

    return _empty("tiktok", "TikTok解析失败，需要Cookie")


async def _parse_kuaishou(url: str) -> dict:
    resolved = await _resolve_short(url) or url
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        resp = await client.get(resolved, headers={"User-Agent": UA})
        html = resp.text

    title = _extract_meta(html, "og:title") or "快手视频"
    cover = _extract_meta(html, "og:image") or ""

    video_url = ""
    for p in [r'"srcNoMark":"([^"]+)"', r'"src_url":"([^"]+)"', r'"video_url":"([^"]+)"']:
        m = re.search(p, html)
        if m:
            video_url = m.group(1).replace("\\u002F", "/")
            break

    return {
        "platform": "kuaishou",
        "title": title,
        "cover": cover,
        "video_url": video_url,
        "images": [],
        "author": "",
    }


async def _parse_bilibili(url: str) -> dict:
    vid = _extract_pattern(url, [r'/video/(BV[A-Za-z0-9]+)', r'/BV([A-Za-z0-9]+)'])
    if not vid:
        if "b23.tv" in url:
            resolved = await _resolve_short(url) or ""
            vid = _extract_pattern(resolved, [r'/video/(BV[A-Za-z0-9]+)'])
    if not vid:
        return _empty("bilibili", "无法解析B站链接")

    api = f"https://api.bilibili.com/x/web-interface/view?bvid={vid}"
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(api, headers={"User-Agent": UA, "Referer": "https://www.bilibili.com/"})
        if resp.status_code != 200:
            return _empty("bilibili", "B站API请求失败")
        d = resp.json().get("data", {})

    owner = d.get("owner", {})
    return {
        "platform": "bilibili",
        "title": d.get("title", ""),
        "cover": d.get("pic", ""),
        "video_url": f"https://www.bilibili.com/video/{vid}",
        "images": [],
        "author": owner.get("name", ""),
    }


async def _parse_instagram(url: str) -> dict:
    code = _extract_pattern(url, [r'/p/([^/?]+)', r'/reel/([^/?]+)', r'/tv/([^/?]+)'])
    if not code:
        return _empty("instagram", "无法解析Instagram链接")

    title = "Instagram"
    cover = ""
    video_url = ""
    images = []
    author = ""

    result = await _try_instagram_html(code)
    if result:
        return result

    result = await _try_instagram_graphql(code)
    if result:
        return result

    return _empty("instagram", "无法获取内容，可能触发反爬机制")


async def _try_instagram_html(code: str) -> dict | None:
    try:
        post_url = f"https://www.instagram.com/p/{code}/"
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = await client.get(post_url, headers={
                "User-Agent": UA,
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "en-US,en;q=0.9",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
            })
            html = resp.text

        if "login" in html.lower() and ("instagram" in html.lower()):
            return None

        title = _extract_meta(html, "og:title") or "Instagram"
        cover = _extract_meta(html, "og:image") or ""
        video_url = _extract_meta(html, "og:video") or ""

        author = title.split(" on Instagram:")[0].strip() if "on Instagram:" in title else ""
        if not author:
            author = title.split(" \u2022 ")[0].strip()

        images = []
        if cover:
            images.append(cover)

        scripts = re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.DOTALL)
        for s in scripts:
            try:
                import json
                data = json.loads(s)
                items = data if isinstance(data, list) else [data]
                for item in items:
                    if item.get("@type") == "VideoObject":
                        if not video_url:
                            video_url = item.get("contentUrl", "")
                    if item.get("image"):
                        imgs = item["image"] if isinstance(item["image"], list) else [item["image"]]
                        for img in imgs:
                            if isinstance(img, str) and img not in images:
                                images.append(img)
                    elif isinstance(item, str) and item not in images:
                        images.append(item)
            except Exception:
                pass

        if not images and cover:
            images = [cover]

        return {
            "platform": "instagram",
            "title": title,
            "cover": cover,
            "video_url": video_url,
            "images": images,
            "author": author,
        }
    except Exception:
        return None


async def _try_instagram_graphql(code: str) -> dict | None:
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"https://www.instagram.com/p/{code}/?__a=1&__d=1",
                headers={"User-Agent": UA, "Accept": "application/json"}
            )
            if resp.status_code != 200:
                return None
            data = resp.json()

        items = data.get("items", [data]) if isinstance(data, dict) else []
        if not items:
            items = [data]

        item = items[0] if items else {}
        caption = item.get("caption", "")
        if isinstance(caption, dict):
            caption = caption.get("text", "")

        carousel = item.get("carousel_media", [])
        is_video = item.get("is_video", False)
        video_url = item.get("video_url", "") if is_video else ""

        images = []
        if carousel:
            for m in carousel:
                if m.get("is_video"):
                    if not video_url:
                        video_url = m.get("video_url", "")
                imgs = m.get("image_versions2", {}).get("candidates", [])
                if imgs:
                    images.append(imgs[0].get("url", ""))
        else:
            imgs = item.get("image_versions2", {}).get("candidates", [])
            if imgs:
                images.append(imgs[0].get("url", ""))

        user = item.get("user", {})
        cover = images[0] if images else ""

        return {
            "platform": "instagram",
            "title": caption[:200] if caption else "Instagram",
            "cover": cover,
            "video_url": video_url,
            "images": images,
            "author": user.get("username", ""),
        }
    except Exception:
        return None


async def _parse_youtube(url: str) -> dict:
    vid = _extract_pattern(url, [
        r'(?:youtu\.be/|watch\?v=|embed/|v/|shorts/|live/)([a-zA-Z0-9_-]{11})',
    ])
    if not vid:
        return _empty("youtube", "无法解析YouTube链接")

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={vid}&format=json"
        )
        if resp.status_code == 200:
            d = resp.json()
            return {
                "platform": "youtube",
                "title": d.get("title", ""),
                "cover": f"https://img.youtube.com/vi/{vid}/maxresdefault.jpg",
                "video_url": f"https://www.youtube.com/watch?v={vid}",
                "images": [],
                "author": d.get("author_name", ""),
                "embed": d.get("html", ""),
            }

    return {
        "platform": "youtube",
        "title": "YouTube",
        "cover": f"https://img.youtube.com/vi/{vid}/maxresdefault.jpg",
        "video_url": f"https://www.youtube.com/watch?v={vid}",
        "images": [],
        "author": "",
        "embed": f'<iframe width="100%" height="400" src="https://www.youtube.com/embed/{vid}" frameborder="0" allowfullscreen></iframe>',
    }


async def _parse_generic(url: str) -> dict:
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        resp = await client.get(url, headers={"User-Agent": UA})
        html = resp.text

    title = _extract_meta(html, "og:title") or ""
    cover = _extract_meta(html, "og:image") or ""
    video_url = _extract_meta(html, "og:video") or _extract_meta(html, "og:video:url") or ""
    author = _extract_meta(html, "og:site_name") or ""

    images = []
    if cover:
        images.append(cover)

    return {
        "platform": "generic",
        "title": title,
        "cover": cover,
        "video_url": video_url,
        "images": images,
        "author": author,
    }


def _extract_pattern(text: str, patterns: list) -> str:
    for p in patterns:
        m = re.search(p, text)
        if m:
            return m.group(1)
    return ""


async def _resolve_short(url: str) -> str:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.head(url, follow_redirects=True, headers={"User-Agent": UA})
            return str(resp.url)
    except Exception:
        return ""


def _extract_meta(html: str, prop: str) -> str:
    for pat in [
        f'<meta[^>]+property="{prop}"[^>]+content="([^"]*)"',
        f'<meta[^>]+content="([^"]*)"[^>]+property="{prop}"',
        f'<meta[^>]+name="{prop}"[^>]+content="([^"]*)"',
    ]:
        m = re.search(pat, html, re.IGNORECASE)
        if m:
            return m.group(1)
    return ""


def _empty(platform: str, error: str) -> dict:
    return {
        "platform": platform,
        "title": "",
        "cover": "",
        "video_url": "",
        "images": [],
        "author": "",
        "error": error,
    }


static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "static")
if os.path.exists(static_dir):
    app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")

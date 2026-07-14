import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx
import re
from urllib.parse import urlparse

from fastapi import FastAPI, Request, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, Response
from urllib.parse import quote

app = FastAPI(title="Watermark Remover API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1"

_SUPPORTED = {
    "douyin": "抖音",
    "tiktok": "TikTok",
    "kuaishou": "快手",
    "bilibili": "哔哩哔哩",
    "instagram": "Instagram",
    "youtube": "YouTube",
    "xiaohongshu": "小红书",
    "weibo": "微博",
    "twitter": "Twitter/X",
    "facebook": "Facebook",
}


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/proxy")
async def proxy_media(url: str, dl: str = ""):
    if not url:
        raise HTTPException(400, "url required")
    try:
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            resp = await client.get(url, headers={
                "User-Agent": UA,
                "Referer": "https://www.instagram.com/",
            })
        headers = {
            "Access-Control-Allow-Origin": "*",
            "Cache-Control": "public, max-age=3600",
        }
        content_type = resp.headers.get("content-type", "application/octet-stream")
        headers["Content-Type"] = content_type
        if dl:
            filename = dl if "." in dl else f"{dl}.mp4"
            headers["Content-Disposition"] = f'attachment; filename="{quote(filename)}"'
        return Response(content=resp.content, headers=headers, status_code=resp.status_code)
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/api/parse")
async def parse(request: Request):
    try:
        body = await request.json()
        url = body.get("url", "").strip()
        if not url:
            raise HTTPException(400, "请提供URL链接")

        hostname = (urlparse(url).hostname or "").replace("www.", "")

        if "douyin.com" in hostname or "iesdouyin.com" in hostname:
            result = await _parse_hybrid(url)
        elif "tiktok.com" in hostname:
            result = await _parse_hybrid(url)
        elif "kuaishou.com" in hostname or "gifshow.com" in hostname:
            result = await _parse_hybrid(url)
        elif "bilibili.com" in hostname or "b23.tv" in hostname:
            result = await _parse_hybrid(url)
        elif "instagram.com" in hostname or "cdninstagram.com" in hostname:
            result = await _parse_instagram(url)
        elif "youtube.com" in hostname or "youtu.be" in hostname:
            result = await _parse_youtube(url)
        elif "xiaohongshu.com" in hostname or "xhslink.com" in hostname:
            result = await _parse_generic_page(url, "xiaohongshu")
        elif "weibo.com" in hostname or "weibo.cn" in hostname:
            result = await _parse_generic_page(url, "weibo")
        elif "twitter.com" in hostname or "x.com" in hostname:
            result = await _parse_generic_page(url, "twitter")
        elif "facebook.com" in hostname or "fb.com" in hostname:
            result = await _parse_generic_page(url, "facebook")
        else:
            result = await _parse_generic_page(url, "generic")

        return {"success": True, "data": result}

    except HTTPException:
        raise
    except Exception as e:
        return {"success": False, "error": str(e)}


async def _parse_hybrid(url: str) -> dict:
    from crawlers.hybrid.hybrid_crawler import HybridCrawler

    crawler = HybridCrawler()
    raw = await crawler.hybrid_parsing_single_video(url, minimal=True)

    result = {
        "platform": raw.get("platform", ""),
        "title": raw.get("desc", ""),
        "cover": raw.get("cover_data", {}).get("cover", ""),
        "video_url": "",
        "images": [],
        "author": "",
    }

    author = raw.get("author", {})
    if isinstance(author, dict):
        result["author"] = author.get("nickname", "") or author.get("unique_id", "") or author.get("name", "")

    video_data = raw.get("video_data", {})
    if video_data:
        result["video_url"] = video_data.get("nwm_video_url") or video_data.get("nwm_video_url_HQ") or ""

    image_data = raw.get("image_data", {})
    if image_data:
        result["images"] = image_data.get("no_watermark_image_list", [])

    return result


async def _parse_instagram(url: str) -> dict:
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        resp = await client.get(url, headers={"User-Agent": UA})
        html = resp.text
        final_url = str(resp.url)

    title = _extract_meta(html, "og:title") or "Instagram Post"
    cover = _extract_meta(html, "og:image") or ""
    author = title.split(" on ")[0].strip() if " on " in title else ""

    video_url = _extract_meta(html, "og:video") or ""

    images = []
    if cover and cover not in images:
        images.append(cover)

    scripts = re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.DOTALL)
    for s in scripts:
        try:
            import json
            data = json.loads(s)
            items = data if isinstance(data, list) else [data]
            for item in items:
                if item.get("@type") == "VideoObject":
                    video_url = item.get("contentUrl", video_url)
                imgs = item.get("image")
                if isinstance(imgs, list):
                    for img in imgs:
                        if img not in images:
                            images.append(img)
                elif isinstance(imgs, str) and imgs not in images:
                    images.append(imgs)
        except Exception:
            pass

    return {
        "platform": "instagram",
        "title": title,
        "cover": cover,
        "video_url": video_url,
        "images": images,
        "author": author,
    }


async def _parse_youtube(url: str) -> dict:
    vid = None
    for p in [
        r'(?:youtu\.be/|watch\?v=|embed/|v/|shorts/)([a-zA-Z0-9_-]{11})',
        r'youtube\.com/live/([a-zA-Z0-9_-]{11})',
    ]:
        m = re.search(p, url)
        if m:
            vid = m.group(1)
            break
    if not vid:
        return {"platform": "youtube", "title": "无法解析YouTube链接", "cover": "", "video_url": "", "images": [], "author": ""}

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(f"https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={vid}&format=json")
        if resp.status_code == 200:
            data = resp.json()
            return {
                "platform": "youtube",
                "title": data.get("title", ""),
                "cover": f"https://img.youtube.com/vi/{vid}/maxresdefault.jpg",
                "video_url": f"https://www.youtube.com/watch?v={vid}",
                "images": [],
                "author": data.get("author_name", ""),
                "embed": f'<iframe width="100%" height="400" src="https://www.youtube.com/embed/{vid}" frameborder="0" allowfullscreen></iframe>',
            }

    return {
        "platform": "youtube",
        "title": "YouTube Video",
        "cover": f"https://img.youtube.com/vi/{vid}/maxresdefault.jpg",
        "video_url": f"https://www.youtube.com/watch?v={vid}",
        "images": [],
        "author": "",
    }


async def _parse_generic_page(url: str, platform: str) -> dict:
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        resp = await client.get(url, headers={"User-Agent": UA})
        html = resp.text

    title = _extract_meta(html, "og:title") or _extract_meta(html, "twitter:title") or ""
    cover = _extract_meta(html, "og:image") or ""
    description = _extract_meta(html, "og:description") or ""
    video_url = _extract_meta(html, "og:video") or _extract_meta(html, "og:video:url") or ""
    author = _extract_meta(html, "og:site_name") or ""

    images = []
    if cover:
        images.append(cover)
    og_images = re.findall(r'<meta[^>]+property="og:image"[^>]+content="([^"]+)"', html)
    for img in og_images:
        if img not in images:
            images.append(img)

    return {
        "platform": platform,
        "title": title,
        "cover": cover,
        "description": description,
        "video_url": video_url,
        "images": images,
        "author": author,
    }


def _extract_meta(html: str, name: str) -> str:
    patterns = [
        f'<meta[^>]+property="{name}"[^>]+content="([^"]+)"',
        f'<meta[^>]+content="([^"]+)"[^>]+property="{name}"',
        f'<meta[^>]+name="{name}"[^>]+content="([^"]+)"',
        f'<meta[^>]+content="([^"]+)"[^>]+name="{name}"',
    ]
    for p in patterns:
        m = re.search(p, html, re.IGNORECASE)
        if m:
            return m.group(1)
    return ""


static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "static")
if os.path.exists(static_dir):
    app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI, Request, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="Watermark Remover API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.post("/api/parse")
async def parse(request: Request):
    try:
        body = await request.json()
        url = body.get("url", "").strip()
        if not url:
            raise HTTPException(400, "请提供URL链接")

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

        return {"success": True, "data": result}

    except HTTPException:
        raise
    except Exception as e:
        return {"success": False, "error": str(e)}


static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "static")
if os.path.exists(static_dir):
    app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")

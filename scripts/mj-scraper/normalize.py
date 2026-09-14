"""Item normalization functions — shared between browser-based and proxy-based scrapers.
This module has NO playwright dependency."""
import datetime
from pathlib import Path

CDN_SIZES = [384, 1024, 2048]
CDN_FLAGS = ["N"]

# ---- Item extraction ----
def build_image_urls(job_id, num_variants=4):
    """Build CDN URLs for all variants of a job at multiple sizes.
    Pattern: https://cdn.midjourney.com/<job_id>/0_<variant>_<size>_<flag>.<ext>
    The SPA loads .webp at size 384 for thumbnails; we include common sizes."""
    urls = []
    for variant in range(num_variants):
        for size in CDN_SIZES:
            for flag in CDN_FLAGS:
                urls.append(f"https://cdn.midjourney.com/{job_id}/0_{variant}_{size}_{flag}.webp")
    return urls


def build_image_urls_primary(job_id, num_variants=4):
    """Just the primary thumbnail URL per variant (matches what SPA loads)."""
    return [f"https://cdn.midjourney.com/{job_id}/0_{v}_384_N.webp" for v in range(num_variants)]


def build_video_url(job_id):
    """Build the video CDN URL.
    Pattern discovered: https://cdn.midjourney.com/video/<job_id>/0.mp4
    (Single file per video job — no variants/sizes.)"""
    return f"https://cdn.midjourney.com/video/{job_id}/0.mp4"


def extract_prompt_text(prompt_obj):
    """Concatenate all decodedPrompt contents into a single string."""
    if not prompt_obj: return ""
    parts = prompt_obj.get("decodedPrompt") or []
    return " ".join(p.get("content", "") for p in parts if p.get("content"))


def extract_aspect_ratio(prompt_obj):
    if not prompt_obj: return None
    ar = prompt_obj.get("ar")
    if ar and isinstance(ar, dict) and ar.get("w") and ar.get("h"):
        return f"{ar['w']}:{ar['h']}"
    return None


def extract_tags(item):
    """Extract any tag-like fields. MJ doesn't have explicit 'tags' but
    personalize codes, styleRef, and depthRef serve a similar purpose.
    Preserves the discriminator `t` field on styleRef/depthRef items."""
    tags = []
    prompt = item.get("prompt") or {}
    for p in (prompt.get("personalize") or []):
        if isinstance(p, dict) and p.get("content"):
            tags.append({"type": "personalize", "content": p["content"], "weight": p.get("weight")})
    for s in (prompt.get("styleRef") or []):
        if isinstance(s, dict) and s.get("content"):
            tags.append({"type": "styleRef", "t": s.get("t"), "content": s["content"], "weight": s.get("weight")})
    for d in (prompt.get("depthRef") or []):
        if isinstance(d, dict) and d.get("content"):
            tags.append({"type": "depthRef", "t": d.get("t"), "content": d["content"], "weight": d.get("weight")})
    return tags


def gcd(a, b):
    while b: a, b = b, a % b
    return a


def normalize_style_item(raw_item, feed, page):
    """Normalize a style item from /api/explore-srefs.
    Style items have a different schema:
    - id: "0_<sref_number>" (not a UUID)
    - formatted_sref: the sref code (e.g. "3925209156") — what users pass to --sref
    - type: "style"
    - prompt: empty string (styles are pure visual references)
    - items: array with 1 entry (not 4 like image grids)
    - display_name: always "Midjourney" (curated)

    NOTE: Style CDN image URLs use a DIFFERENT pattern from regular images.
    The SPA resolves sref → image UUID via an internal mechanism we haven't
    cracked (the UUIDs appear in background-image CSS but not in the API
    response). We save the sref code (the primary identifier) and dimensions
    but leave image_urls empty. To get the actual image, use the Midjourney
    web UI with --sref <code>, or decompile the SPA's JS bundle to find the
    sref→UUID resolution endpoint.
    """
    job_id = raw_item.get("id", "")
    # Extract sref number from id (format: "0_<number>")
    sref = raw_item.get("formatted_sref") or ""
    if not sref and "_" in job_id:
        sref = job_id.split("_", 1)[1]

    # Dimensions
    w = raw_item.get("width")
    h = raw_item.get("height")
    aspect_ratio = None
    if w and h:
        g = gcd(w, h)
        aspect_ratio = f"{w//g}:{h//g}"

    return {
        "id": job_id,
        "type": "style",
        "sref": sref,
        "formatted_sref": sref,
        "prompt": raw_item.get("prompt", ""),
        "prompt_text": "",
        "aspect_ratio": aspect_ratio,
        "tags": [],
        "user": {
            "username": raw_item.get("username_v2"),
            "display_name": raw_item.get("display_name"),
            "user_id": raw_item.get("user_id"),
        },
        "dimensions": {"width": w, "height": h},
        "job_metadata": {
            "job_type": raw_item.get("job_type"),
            "event_type": raw_item.get("event_type"),
            "enqueue_time": raw_item.get("enqueue_time"),
            "enqueue_time_iso": None,
            "parent_grid": raw_item.get("parent_grid"),
            "parent_id": raw_item.get("parent_id"),
            "published": raw_item.get("published"),
            "isStyleJob": raw_item.get("isStyleJob"),
            "liked_by_user": raw_item.get("liked_by_user"),
            "selected": raw_item.get("selected"),
        },
        "video_metadata": None,
        # Style image URLs are UNKNOWN — the SPA resolves sref→UUID via an
        # internal mechanism not visible in the API response. Left empty.
        "image_urls": [],
        "image_urls_all_sizes": [],
        "image_urls_original": [],
        "video_urls": [],
        "video_thumbnail_url": None,
        "items": raw_item.get("items") or [],
        "owner_profile": raw_item.get("owner_profile"),
        "raw": raw_item,
        "feed": feed,
        "page": page,
        "scraped_at": datetime.datetime.now(tz=datetime.timezone.utc).isoformat(),
    }


def normalize_item(raw_item, feed, page):
    """Convert raw API item into our normalized schema."""
    prompt = raw_item.get("prompt") or {}
    job_id = raw_item.get("id")
    items_arr = raw_item.get("items") or []
    num_variants = len(items_arr) if items_arr else 4
    item_type = raw_item.get("type")  # "image" or "video"

    # Build CDN URLs based on type. Verified URL patterns (see verify_urls_dom.py):
    #   Image:  https://cdn.midjourney.com/<job_id>/0_<variant>_<size>_N.webp  (sizes 384/1024/2048)
    #           Also: <job_id>/0_0.png and <job_id>/0_0.webp (original-resolution)
    #   Video:  https://cdn.midjourney.com/video/<job_id>/0.mp4  (single file, ~5.2s, 24fps)
    #           Video thumbnails come from the PARENT image (use parent_id), NOT the video job_id.
    image_urls = []
    image_urls_all_sizes = []
    image_urls_original = []
    video_urls = []
    video_thumbnail_url = None
    if job_id:
        if item_type == "video":
            # Videos: single .mp4 file per job.
            # Both 0.mp4 and 0_0.mp4 work (verified); we use the canonical 0.mp4.
            video_urls = [build_video_url(job_id)]
            # Video thumbnail = parent image's variant thumbnail (if parent_id is known)
            parent_id = raw_item.get("parent_id")
            parent_grid = raw_item.get("parent_grid")
            if parent_id:
                # parent_grid is the variant index of the parent image (0-3 typically)
                variant = parent_grid if isinstance(parent_grid, int) else 0
                video_thumbnail_url = f"https://cdn.midjourney.com/{parent_id}/0_{variant}_384_N.webp"
                image_urls = [video_thumbnail_url]
                image_urls_all_sizes = [f"https://cdn.midjourney.com/{parent_id}/0_{variant}_{s}_N.webp" for s in CDN_SIZES]
                image_urls_original = [
                    f"https://cdn.midjourney.com/{parent_id}/0_{variant}.png",
                    f"https://cdn.midjourney.com/{parent_id}/0_{variant}.webp",
                ]
        else:
            # Images: 4 variants × multiple sizes + original-resolution png/webp
            image_urls = build_image_urls_primary(job_id, num_variants)
            image_urls_all_sizes = build_image_urls(job_id, num_variants)
            image_urls_original = []
            for v in range(num_variants):
                image_urls_original.append(f"https://cdn.midjourney.com/{job_id}/0_{v}.png")
                image_urls_original.append(f"https://cdn.midjourney.com/{job_id}/0_{v}.webp")

    # video_segments is a list of frame counts at 24fps (verified: [125] = 5.2s)
    video_segments = raw_item.get("video_segments")
    video_duration_seconds = None
    if isinstance(video_segments, list) and video_segments:
        try:
            total_frames = sum(int(v) for v in video_segments if isinstance(v, (int, float)))
            video_duration_seconds = round(total_frames / 24.0, 3)
        except Exception:
            pass

    # Aspect ratio: prefer prompt.ar; fall back to gcd-reduced width/height
    aspect_ratio = extract_aspect_ratio(prompt)
    if not aspect_ratio:
        w = raw_item.get("width") or 0
        h = raw_item.get("height") or 0
        if w and h:
            g = gcd(w, h)
            aspect_ratio = f"{w//g}:{h//g}"

    # Safe enqueue_time conversion
    enqueue_time = raw_item.get("enqueue_time")
    enqueue_time_iso = None
    if isinstance(enqueue_time, (int, float)):
        try:
            enqueue_time_iso = datetime.datetime.fromtimestamp(enqueue_time/1000, tz=datetime.timezone.utc).isoformat()
        except Exception:
            pass

    return {
        "id": job_id,
        "type": item_type,  # "image" or "video"
        "prompt": prompt,  # full prompt object
        "prompt_text": extract_prompt_text(prompt),
        "aspect_ratio": aspect_ratio,
        "tags": extract_tags(raw_item),
        "user": {
            "username": raw_item.get("username_v2"),
            "display_name": raw_item.get("display_name"),
            "user_id": raw_item.get("user_id"),
        },
        "dimensions": {
            "width": raw_item.get("width"),
            "height": raw_item.get("height"),
        },
        "job_metadata": {
            "job_type": raw_item.get("job_type"),
            "event_type": raw_item.get("event_type"),
            "enqueue_time": enqueue_time,
            "enqueue_time_iso": enqueue_time_iso,
            "parent_grid": raw_item.get("parent_grid"),
            "parent_id": raw_item.get("parent_id"),
            "published": raw_item.get("published"),
        },
        "video_metadata": {
            "motion": prompt.get("motion") if item_type == "video" else None,
            "length": prompt.get("length") if item_type == "video" else None,
            "end": prompt.get("end") if item_type == "video" else None,
            "video_segments": video_segments,
            "duration_seconds": video_duration_seconds,
            "fps": 24,
        } if item_type == "video" else None,
        "image_urls": image_urls,                  # primary thumbnails (one per variant) OR video poster
        "image_urls_all_sizes": image_urls_all_sizes,  # all sizes × variants × flags (384/1024/2048 × N)
        "image_urls_original": image_urls_original,    # original-resolution .png + .webp per variant
        "video_urls": video_urls,                   # video .mp4 URLs (empty for image items)
        "video_thumbnail_url": video_thumbnail_url,  # parent image's thumbnail for video items
        "items": items_arr,                          # raw per-variant filter flags
        "owner_profile": raw_item.get("owner_profile"),
        "raw": raw_item,                             # full original
        "feed": feed,
        "page": page,
        "scraped_at": datetime.datetime.now(tz=datetime.timezone.utc).isoformat(),
    }



"""Two-sided Numista image identification. No image or result persistence."""
import base64
import binascii
import io
import os
import threading

import requests
from flask import Blueprint, jsonify, request

photo_api = Blueprint("coin_photo", __name__)
_slot = threading.BoundedSemaphore(1)
MAX_BODY = 4 * 1024 * 1024


def normalize_image(value):
    from PIL import Image
    if not isinstance(value, dict) or value.get("mime_type") not in ("image/jpeg", "image/png"):
        raise ValueError("Use JPEG or PNG photos.")
    encoded = value.get("image_data")
    if not isinstance(encoded, str) or len(encoded) > 2 * 1024 * 1024:
        raise ValueError("Photo is too large.")
    raw = base64.b64decode(encoded, validate=True)
    with Image.open(io.BytesIO(raw)) as image:
        if image.format not in ("JPEG", "PNG") or max(image.size) > 1024:
            raise ValueError("Photos must be JPEG or PNG, at most 1024 pixels per side.")
        image.load()
        clean = io.BytesIO()
        image.convert("RGB").save(clean, format="JPEG", quality=90)
    return {"mime_type": "image/jpeg", "image_data": base64.b64encode(clean.getvalue()).decode("ascii")}


@photo_api.post("/api/coin-identify-images")
def identify_images():
    # Explicit operator opt-in: the Numista image endpoint is a paid feature.
    if os.environ.get("NUMISTA_IMAGE_SEARCH_ENABLED", "").lower() != "true":
        return jsonify(error="Photo recognition has not been activated yet. You can still search by text."), 503
    key = os.environ.get("NUMISTA_API_KEY", "")
    if not key:
        return jsonify(error="Photo recognition is not configured."), 503
    if request.content_length and request.content_length > MAX_BODY:
        return jsonify(error="Photos are too large."), 413
    # Bound reads even when Content-Length is absent.
    raw = request.stream.read(MAX_BODY + 1)
    if len(raw) > MAX_BODY:
        return jsonify(error="Photos are too large."), 413
    try:
        import json
        payload = json.loads(raw)
        images = payload.get("images") if isinstance(payload, dict) else None
        if not isinstance(images, list) or len(images) != 2:
            raise ValueError("Upload both sides of the same coin.")
        images = [normalize_image(value) for value in images]
    except (ValueError, TypeError, binascii.Error, OSError):
        return jsonify(error="Upload two valid JPEG or PNG photos, at most 1024 pixels per side."), 400
    if not _slot.acquire(blocking=False):
        return jsonify(error="Photo recognition is busy. Please try again shortly."), 429
    try:
        response = requests.post(
            "https://api.numista.com/v3/search_by_image",
            headers={"Numista-API-Key": key},
            json={"category": "coin", "images": images, "max_results": 5},
            timeout=(5, 35),
        )
        if response.status_code != 200:
            errors = {401: "The recognition service credentials need attention.",
                      403: "Numista image recognition is not activated for this API key.",
                      429: "The recognition service has reached its request limit. Please try later."}
            return jsonify(error=errors.get(response.status_code, "Photo recognition is temporarily unavailable.")), (429 if response.status_code == 429 else 503)
        data = response.json()
        if not isinstance(data, dict) or not isinstance(data.get("types"), list):
            raise ValueError("Invalid response")
        candidates = []
        for item in data["types"][:5]:
            if not isinstance(item, dict) or not isinstance(item.get("id"), int) or not isinstance(item.get("title"), str):
                continue
            issuer = item.get("issuer") or {}
            candidates.append({"id": item["id"], "title": item["title"][:250],
                               "country": issuer.get("name", "") if isinstance(issuer, dict) else "",
                               "min_year": item.get("min_year"), "max_year": item.get("max_year"),
                               "url": f"https://en.numista.com/catalogue/pieces{item['id']}.html"})
        return jsonify(provider="numista", status="review" if candidates else "no_match", candidates=candidates)
    except (requests.RequestException, ValueError):
        return jsonify(error="Photo recognition could not finish. Please try again later."), 503
    finally:
        _slot.release()


@photo_api.after_request
def no_cache(response):
    response.headers["Cache-Control"] = "no-store"
    return response

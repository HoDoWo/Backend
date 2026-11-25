import pathlib

import fastapi
from starlette import datastructures

import ar_scan as scan_module
import config
import logging_config
import request_helpers

logger = logging_config.get_logger(__name__)

ar_scan_router = fastapi.APIRouter()


@ar_scan_router.post("/api/ar/scan")
@config.ai_rate_limit
async def ar_scan(request: fastapi.Request) -> dict[str, object]:
    """Name the device in a camera frame and its main visible controls (AR tab).

    multipart/form-data: ``image`` (the camera frame, JPEG) and optional
    ``language`` (BCP-47, for the labels). Returns
    ``{"device": {"name", "brand", "model"} | null, "controls": [{"label", "description", "x", "y"}]}``.
    """
    if not request.headers.get("content-type", "").startswith("multipart/form-data"):
        raise fastapi.HTTPException(status_code=415, detail="send the frame as multipart/form-data")
    form = await request.form()
    frame = form.get("image")
    if not isinstance(frame, datastructures.UploadFile) or not (frame.content_type or "").startswith("image/"):
        raise fastapi.HTTPException(status_code=422, detail="an image upload is required")
    language = str(form.get("language") or "").strip() or "en"

    frame_path = await request_helpers.save_uploaded_image(frame, "frame.jpg")
    try:
        scan = scan_module.scan_frame(frame_path, language)
        return scan.model_dump(mode="json")
    except (scan_module.ArScanError, FileNotFoundError) as exc:
        logger.error("AR scan failed: %s", exc)
        raise fastapi.HTTPException(status_code=502, detail=str(exc)) from exc
    finally:
        pathlib.Path(frame_path).unlink(missing_ok=True)

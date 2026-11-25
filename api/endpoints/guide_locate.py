import pathlib

import fastapi
from starlette import datastructures

import config
import guide_locate as guide_module
import logging_config
import request_helpers

logger = logging_config.get_logger(__name__)

guide_locate_router = fastapi.APIRouter()


def _image_field(form: datastructures.FormData, name: str) -> datastructures.UploadFile | None:
    value = form.get(name)
    if isinstance(value, datastructures.UploadFile) and (value.content_type or "").startswith("image/"):
        return value
    return None


@guide_locate_router.post("/api/guide/locate")
@config.ai_rate_limit
async def guide_locate(request: fastapi.Request) -> dict[str, list[dict[str, float | int]]]:
    """Find the controls of guide steps in a live camera frame (AR auto-alignment).

    multipart/form-data:
    - ``image``: the camera frame (JPEG);
    - ``steps``: JSON array of ``{"step", "title", "description", "pin_x", "pin_y"}``;
    - ``targets`` (optional): step numbers to find, e.g. ``"1,3"`` (default: all, at most 4);
    - ``reference`` (optional): the photo the guide was made from, where ``pin_x/pin_y`` are;
    - ``appliance`` (optional): e.g. "Bosch Serie 6 washing machine".

    Returns ``{"points": [{"step", "x", "y", "confidence"}]}`` with only the steps
    found confidently; the app asks the user for the others.
    """
    if not request.headers.get("content-type", "").startswith("multipart/form-data"):
        raise fastapi.HTTPException(status_code=415, detail="send the frame as multipart/form-data")
    form = await request.form()
    frame = _image_field(form, "image")
    if frame is None:
        raise fastapi.HTTPException(status_code=422, detail="an image upload is required")
    try:
        steps = guide_module.parse_steps(str(form.get("steps") or ""))
        targets = guide_module.parse_targets(str(form.get("targets") or ""), steps)
    except ValueError as exc:
        raise fastapi.HTTPException(status_code=422, detail=str(exc)) from exc
    appliance = str(form.get("appliance") or "").strip()

    frame_path = await request_helpers.save_uploaded_image(frame, "frame.jpg")
    reference = _image_field(form, "reference")
    reference_path = await request_helpers.save_uploaded_image(reference, "reference.jpg") if reference else None
    try:
        points = guide_module.locate_steps(frame_path, steps, targets, reference_path, appliance)
        return {"points": [point.model_dump(mode="json") for point in points]}
    except (guide_module.GuideLocateError, FileNotFoundError) as exc:
        logger.error("Guide locate failed: %s", exc)
        raise fastapi.HTTPException(status_code=502, detail=str(exc)) from exc
    finally:
        pathlib.Path(frame_path).unlink(missing_ok=True)
        if reference_path:
            pathlib.Path(reference_path).unlink(missing_ok=True)

import pathlib

import fastapi
import config
import logging_config
import panel_analyze as panel_module
import request_helpers

logger = logging_config.get_logger(__name__)

locate_router = fastapi.APIRouter()

@locate_router.post("/api/panel/locate")
@config.ai_rate_limit
async def panel_locate(request: fastapi.Request) -> dict[str, dict[str, float] | None]:
    """Locate the control panel in a full appliance photo (S1-16).

    Returns its normalised box, or ``null`` when there is no panel, so the
    client can crop before calling ``/api/panel/analyze``.
    """
    if not request.headers.get("content-type", "").startswith("multipart/form-data"):
        raise fastapi.HTTPException(
            status_code=415,
            detail="send the photo as multipart/form-data",
        )

    form = await request.form()
    image = request_helpers.find_uploaded_image(form)
    if image is None:
        logger.warning("Rejected panel location without an image")
        raise fastapi.HTTPException(status_code=422, detail="an image upload is required")

    brand = str(form.get("brand") or "").strip()
    model = str(form.get("model") or "").strip()

    image_path = await request_helpers.save_uploaded_image(image, "appliance.jpg")
    try:
        box = panel_module.locate_panel(image_path, brand=brand, model=model)
        logger.info("Panel location found a panel: %s", box is not None)
        return {"box": box.model_dump(mode="json") if box else None}
    except (panel_module.PanelControlError, FileNotFoundError) as exc:
        logger.error("Panel location failed: %s", exc)
        raise fastapi.HTTPException(status_code=502, detail=str(exc)) from exc
    finally:
        pathlib.Path(image_path).unlink(missing_ok=True)



@locate_router.post("/api/panel/analyze")
@config.ai_rate_limit
async def panel_analyze(request: fastapi.Request) -> dict[str, list[dict[str, object]]]:
    """Detect the buttons, dials and other controls in a cropped panel photo."""
    if not request.headers.get("content-type", "").startswith("multipart/form-data"):
        raise fastapi.HTTPException(
            status_code=415,
            detail="send the panel image as multipart/form-data",
        )

    form = await request.form()
    image = request_helpers.find_uploaded_image(form)
    if image is None:
        logger.warning("Rejected panel analysis without an image")
        raise fastapi.HTTPException(status_code=422, detail="an image upload is required")

    brand = str(form.get("brand") or "").strip()
    model = str(form.get("model") or "").strip()
    language = str(form.get("language") or "").strip() or "en"

    image_path = await request_helpers.save_uploaded_image(image, "panel.jpg")
    try:
        controls = panel_module.analyze_panel(image_path, brand=brand, model=model, language=language)
        logger.info("Panel analysis detected %s controls", len(controls))
        return {"controls": [control.model_dump(mode="json") for control in controls]}
    except (panel_module.PanelControlError, FileNotFoundError) as exc:
        logger.error("Panel analysis failed: %s", exc)
        raise fastapi.HTTPException(status_code=502, detail=str(exc)) from exc
    finally:
        pathlib.Path(image_path).unlink(missing_ok=True)
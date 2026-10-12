from pathlib import Path
import tempfile

from fastapi import FastAPI, HTTPException, Request
from starlette.datastructures import UploadFile

from .langchain_setup import build_agent
from .logging_config import get_logger

app = FastAPI()
logger = get_logger(__name__)

MAX_IMAGE_BYTES = 15 * 1024 * 1024


@app.post("/api/chat")
async def chat(request: Request) -> dict[str, str]:
    """Run every text and/or image request through the LangChain agent."""
    image: UploadFile | None = None
    image_path: str | None = None

    if request.headers.get("content-type", "").startswith("multipart/form-data"):
        form = await request.form()
        message = form.get("message") or form.get("text") or ""
        form_image = form.get("image")
        if isinstance(form_image, UploadFile):
            image = form_image
    else:
        body = await request.json()
        message = body.get("message") or body.get("text") or ""

    if not isinstance(message, str):
        raise HTTPException(status_code=422, detail="message must be text")

    if image is None and not message.strip():
        logger.warning("Rejected chat request without text or image")
        raise HTTPException(
            status_code=422,
            detail="provide at least one of image or message",
        )

    if image is not None:
        if not image.content_type or not image.content_type.startswith("image/"):
            logger.warning("Rejected chat request with invalid image content type")
            raise HTTPException(status_code=415, detail="image must be an image upload")

        content = await image.read()
        if len(content) > MAX_IMAGE_BYTES:
            logger.warning("Rejected chat request because image exceeds size limit")
            raise HTTPException(status_code=413, detail="image is larger than 15 MB")

        suffix = Path(image.filename or "appliance.jpg").suffix or ".jpg"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as temporary:
            temporary.write(content)
            image_path = temporary.name

    try:
        prompt = message or "Identify, describe and provide instructions for the appliance in the uploaded image."
        if image_path is not None:
            prompt += f"\nUploaded image path: {image_path}"

        logger.info("Invoking LangChain agent (has_image=%s)", image_path is not None)
        result = build_agent(image_path).invoke(
            {"messages": [{"role": "user", "content": prompt}]}
        )
        logger.info("LangChain agent completed successfully")
        return {"reply": result["messages"][-1].content}
    except RuntimeError as exc:
        logger.error("LangChain agent failed: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    finally:
        if image_path is not None:
            Path(image_path).unlink(missing_ok=True)
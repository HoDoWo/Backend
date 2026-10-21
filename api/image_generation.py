"""Kie Nano Banana image-overlay generation tool."""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import pathlib
import time
from typing import Any

import logging_config
import requests
import time_budget


logger = logging_config.get_logger(__name__)

KIE_CREATE_URL = "https://api.kie.ai/api/v1/jobs/createTask"
KIE_STATUS_URL = "https://api.kie.ai/api/v1/jobs/recordInfo"
KIE_UPLOAD_URL = "https://kieai.redpandaai.co/api/file-base64-upload"

# Longest wait for one Kie image (it usually takes 30-90 s).
DEFAULT_KIE_TIMEOUT_SECONDS = 150
# Timeout of each single HTTP call to Kie.
HTTP_TIMEOUT_SECONDS = 30
# Below this many seconds left in the request, the overlay is not even started.
MIN_OVERLAY_SECONDS = 45
# Seconds kept free after the overlay, to finish and send the answer.
FINISH_MARGIN_SECONDS = 10


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is not configured")
    return value


def _image_data_url(image_path: str) -> str:
    path = pathlib.Path(image_path)
    if not path.is_file():
        raise FileNotFoundError(f"Appliance image does not exist: {image_path}")
    mime_type = mimetypes.guess_type(path.name)[0] or "image/jpeg"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_required_env('KIE_API_KEY')}",
        "Content-Type": "application/json",
    }


def _response_json(response: requests.Response, operation: str) -> dict[str, Any]:
    try:
        response.raise_for_status()
    except requests.RequestException as exc:
        raise RuntimeError(f"Kie {operation} request failed: {exc}") from exc
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError(f"Kie {operation} returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"Kie {operation} returned an invalid response")
    return payload


def _upload_image(image_path: str) -> str:
    path = pathlib.Path(image_path)
    payload = {
        "base64Data": _image_data_url(image_path),
        "uploadPath": "images",
        "fileName": path.name,
    }
    result = _response_json(
        requests.post(KIE_UPLOAD_URL, json=payload, headers=_headers(), timeout=HTTP_TIMEOUT_SECONDS),
        "image upload",
    )
    data = result.get("data") or {}
    image_url = data.get("fileUrl") or data.get("downloadUrl")
    if result.get("code") != 200 or not image_url:
        raise RuntimeError(f"Kie image upload failed: {result.get('msg', result)}")
    return str(image_url)


def _create_task(image_url: str, instructions: str, steps: list[str]) -> str:
    numbered_steps = "\n".join(
        f"{index}. {step}" for index, step in enumerate(steps, start=1)
    )
    prompt = (
        "Just generate same image but with overlay step by step instructions. PRESERVE exact same base image. Just add the instructions as an overlay, point to the exact buttons or dials to use. Do not invent or alter controls. The base image should remain unchanged.\n\n"
        f"Instructions:\n{instructions}\n\n"
        f"Steps:\n{numbered_steps}"
    )
    logger.info("Creating Kie task for image overlay generation with prompt: %s", prompt)
    payload = {
        #"model": "nano-banana-2",
        "model": "gpt-image-2-5-sunburst-image-to-image",
        "input": {
            "prompt": prompt,
            "input_urls": [image_url],
            "aspect_ratio": "auto",
            "resolution": "1K",
            "output_format": "png",
        },
    }
    result = _response_json(
        requests.post(KIE_CREATE_URL, json=payload, headers=_headers(), timeout=HTTP_TIMEOUT_SECONDS),
        "task creation",
    )
    if result.get("code") != 200:
        raise RuntimeError(f"Kie task creation failed: {result.get('msg', result)}")
    task_id = (result.get("data") or {}).get("taskId")
    if not task_id:
        raise RuntimeError("Kie task creation did not return a task id")
    return str(task_id)


def _result_url(payload: dict[str, Any]) -> str | None:
    data = payload.get("data") or {}
    result_json = data.get("resultJson", {})
    if isinstance(result_json, str):
        try:
            result_json = json.loads(result_json)
        except json.JSONDecodeError as exc:
            raise RuntimeError("Kie task returned invalid result JSON") from exc
    if not isinstance(result_json, dict):
        raise RuntimeError("Kie task returned an invalid result")
    urls = result_json.get("resultUrls") or []
    if not isinstance(urls, list) or not urls:
        return None
    return str(urls[0])


def _wait_for_task(task_id: str) -> str:
    timeout = int(os.getenv("KIE_TIMEOUT_SECONDS", str(DEFAULT_KIE_TIMEOUT_SECONDS)))
    # Never wait past the request budget: Vercel would kill the whole answer.
    timeout = min(timeout, time_budget.remaining() - FINISH_MARGIN_SECONDS)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            response = requests.get(
                KIE_STATUS_URL,
                params={"taskId": task_id},
                headers={"Authorization": _headers()["Authorization"]},
                timeout=HTTP_TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:
            raise RuntimeError(f"Kie task status request failed: {exc}") from exc
        payload = _response_json(response, "task status")
        data = payload.get("data") or {}
        state = data.get("state")
        if state == "success":
            image_url = _result_url(payload)
            if image_url:
                return image_url
            raise RuntimeError("Kie task completed without returning an image URL")
        if state == "fail":
            raise RuntimeError(f"Kie image generation failed: {data.get('failMsg', 'unknown error')}")
        time.sleep(3)
    raise RuntimeError(f"Timed out waiting for Kie task {task_id}")


def generate_overlay(
    image_path: str | None,
    instructions: str,
    steps: list[str],
) -> dict[str, Any]:
    """Generate an annotated copy of the original image with Kie Nano Banana."""
    if not image_path:
        raise ValueError("image_path is required to generate an appliance overlay")
    if not isinstance(instructions, str) or not instructions.strip():
        raise ValueError("instructions must not be empty")
    if not isinstance(steps, list) or not steps or not all(
        isinstance(step, str) and step.strip() for step in steps
    ):
        raise ValueError("steps must be a non-empty list of strings")
    if time_budget.remaining() < MIN_OVERLAY_SECONDS:
        raise RuntimeError("Not enough time left in the request for the image overlay")

    uploaded_url = _upload_image(image_path)
    task_id = _create_task(uploaded_url, instructions, steps)
    image_url = _wait_for_task(task_id)
    logger.info("Kie image overlay generated successfully: task_id=%s", task_id)
    result = {
        "task_id": task_id,
        "image_url": image_url,
        "message": "Generated an annotated step-by-step guide from the original appliance image.",
        "steps": steps,
    }
    return result

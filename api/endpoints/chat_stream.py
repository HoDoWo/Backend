import json
import pathlib

import types
from typing import Any

import config
import fastapi
import langchain_setup
import logging_config
import reinforcement
import request_helpers
import response_formatting
import time_budget
from fastapi import HTTPException, Request, datastructures, responses
from endpoints import chat

logger = logging_config.get_logger(__name__)

chat_stream_router = fastapi.APIRouter()

@chat_stream_router.post("/api/chat/stream")
@config.ai_rate_limit
async def chat_stream(request: Request):
    """Stream tool execution steps dynamically and stream only the final agent response via SSE."""
    image: datastructures.UploadFile | None = None
    image_path: str | None = None
    history_raw: Any = None
    appliance_raw: Any = None

    content_type = request.headers.get("content-type", "")
    if content_type.startswith("multipart/form-data") or content_type.startswith("application/x-www-form-urlencoded"):
        form = await request.form()
        message = form.get("message") or form.get("text") or ""
        history_raw = form.get("history")
        appliance_raw = form.get("appliance")
        image = request_helpers.find_uploaded_image(form)
    else:
        body = await request.json()
        message = body.get("message") or body.get("text") or ""
        history_raw = body.get("history")
        appliance_raw = body.get("appliance")

    if not isinstance(message, str):
        raise HTTPException(status_code=422, detail="message must be text")

    if image is None and not message.strip():
        raise HTTPException(
            status_code=422,
            detail="provide at least one of image or message",
        )

    if image is not None:
        image_path = await request_helpers.save_uploaded_image(image, "appliance.jpg")

    async def event_generator():
        # Vercel stops the function after 300 s: the slow steps share this budget.
        time_budget.start()
        try:
            if image_path:
                yield f"event: step\ndata: {json.dumps({'step': 'identifying', 'title': 'Identifying appliance...', 'detail': 'Analyzing image features'})}\n\n"
            else:
                yield f"event: step\ndata: {json.dumps({'step': 'analyzing', 'title': 'Analyzing query...', 'detail': 'Interpreting question'})}\n\n"

            prompt = message or "Identify, describe and provide instructions for the appliance in the uploaded image."
            if image_path is not None:
                prompt += f"\nUploaded image path: {image_path}"

            # Rated guides of the same appliance (#40).
            prompt = reinforcement.add_to_prompt(
                prompt, reinforcement.appliance_name_from(appliance_raw)
            )

            conversation_messages = request_helpers.parse_history(history_raw)
            conversation_messages.append({"role": "user", "content": prompt})

            agent = langchain_setup.build_agent(image_path)

            messages = []
            in_tool = False
            saw_tool_activity = False
            buffered_tokens: list[str] = []

            async for event in agent.astream_events({"messages": conversation_messages}, version="v2"):
                kind = event["event"]

                if kind == "on_tool_start":
                    saw_tool_activity = True
                    in_tool = True
                    tool_name = event.get("name", "tool")
                    friendly_title = f"Running {tool_name.replace('_', ' ').capitalize()}..."
                    yield f"event: step\ndata: {json.dumps({'step': 'tool', 'title': friendly_title, 'detail': f'Executing tool: {tool_name}'})}\n\n"

                elif kind == "on_tool_end":
                    in_tool = False
                    output = event.get("data", {}).get("output")
                    if isinstance(output, dict) and (
                        output.get("image_url") or output.get("summary")
                    ):
                        messages.append(
                            types.SimpleNamespace(content=json.dumps(output))
                        )

                # 3. Buffer model tokens until we know whether this is a tool
                # workflow. Otherwise provisional Markdown appears before the
                # final manual-card response and the client renders both.
                elif kind == "on_chat_model_stream" and not in_tool:
                    chunk = event["data"]["chunk"]
                    content = getattr(chunk, "content", "")
                    if content:
                        buffered_tokens.append(content)

                # 4. Capturamos los mensajes finales de la ejecución
                elif kind == "on_chain_end":
                    output = event["data"].get("output")
                    if isinstance(output, dict) and "messages" in output:
                        messages = output["messages"]
                    elif isinstance(output, dict) and (
                        output.get("image_url") or output.get("summary")
                    ):
                        # Some LangGraph versions expose tool output events but
                        # omit the final Agent node output.
                        messages.append(
                            types.SimpleNamespace(content=json.dumps(output))
                        )

            # Re-invoking the agent after a streamed tool call can repeat
            # side effects (for example, image generation). Only use the
            # fallback when the stream produced no agent/tool events at all.
            if not messages and not saw_tool_activity:
                result = await agent.ainvoke({"messages": conversation_messages})
                messages = result["messages"]

            if image_path and not in_tool:
                yield f"event: step\ndata: {json.dumps({'step': 'generating', 'title': 'Finalizing visual guide...', 'detail': 'Preparing interactive layout'})}\n\n"

            generated_image = langchain_setup.ensure_image_overlay(messages, image_path, message)
            formatted = response_formatting.format_chat_response(messages, image_path, message, generated_image)
            formatted = chat._persist_generated_manual(request, formatted)

            # Token streaming is retained for ordinary conversations only.
            # Manual/image workflows expose one authoritative final response.
            if not saw_tool_activity:
                for token in buffered_tokens:
                    yield f"event: token\ndata: {json.dumps({'token': token})}\n\n"

            yield f"event: response\ndata: {json.dumps(formatted)}\n\n"
            yield "event: done\ndata: [DONE]\n\n"
        except Exception as exc:
            logger.error("Error in chat stream: %s", exc)
            yield f"event: error\ndata: {json.dumps({'error': str(exc)})}\n\n"
        finally:
            if image_path is not None:
                pathlib.Path(image_path).unlink(missing_ok=True)

    return responses.StreamingResponse(event_generator(), media_type="text/event-stream")

import json

import auth
import config
import fastapi
import index
from fastapi import HTTPException, params


environmental_tip_router = fastapi.APIRouter()

@environmental_tip_router.get("/api/appliances/environmental-tip")
async def get_environmental_tip(user: dict = params.Depends(auth.get_current_user)):
    """
    Generates an electricity-saving and environmental home appliance tip
    dynamically using OpenRouter LLM integration.
    """
    try:
        completion = config.get_openrouter_llm().complete(
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are an expert in home appliance energy efficiency and environmental sustainability. "
                        "Provide a unique, practical electricity-saving or environmental tip/curiosity about home appliances. Just string "
                    )
                },
                {
                    "role": "user",
                    "content": "Generate an energy-saving home appliance tip or curiosity."
                }
            ],
        )

        raw_content = completion.choices[0].message.content
        if not raw_content:
            raise HTTPException(status_code=500, detail="Empty response received from OpenRouter.")
        return raw_content if isinstance(raw_content, dict) else json.loads(raw_content)

    except json.JSONDecodeError as json_exc:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to parse LLM JSON response: {str(json_exc)}"
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"OpenRouter integration error: {str(exc)}"
        )
        


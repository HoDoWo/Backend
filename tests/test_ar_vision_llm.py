"""The AR camera calls can use Gemini directly, without touching the rest of the app."""

import config


def _env(monkeypatch, **values):
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    for name in ("GEMINI_API_KEY", "GEMINI_MODEL", "OPENROUTER_MODEL"):
        monkeypatch.delenv(name, raising=False)
    for name, value in values.items():
        monkeypatch.setenv(name, value)


def test_without_a_gemini_key_ar_uses_openrouter(monkeypatch):
    _env(monkeypatch)
    llm = config.get_ar_vision_llm()
    assert llm.model_name == config.DEFAULT_OPENROUTER_MODEL
    assert "openrouter.ai" in str(llm.openai_api_base)


def test_with_a_gemini_key_ar_uses_gemini_and_falls_back_to_openrouter(monkeypatch):
    _env(monkeypatch, GEMINI_API_KEY=" gm-key \n")
    llm = config.get_ar_vision_llm()
    assert llm.runnable.model_name == config.DEFAULT_GEMINI_MODEL
    assert llm.runnable.openai_api_base == config.GEMINI_OPENAI_BASE_URL
    assert llm.runnable.openai_api_key.get_secret_value() == "gm-key"
    assert llm.runnable.max_retries == 0
    [fallback] = llm.fallbacks
    assert "openrouter.ai" in str(fallback.openai_api_base)


def test_the_gemini_model_can_be_chosen(monkeypatch):
    _env(monkeypatch, GEMINI_API_KEY="gm-key", GEMINI_MODEL="gemini-2.5-flash")
    assert config.get_ar_vision_llm().runnable.model_name == "gemini-2.5-flash"


def test_the_gemini_key_does_not_change_the_chat_model(monkeypatch):
    _env(monkeypatch, GEMINI_API_KEY="gm-key")
    llm = config.get_openrouter_llm()
    assert llm.model_name == config.DEFAULT_OPENROUTER_MODEL
    assert "openrouter.ai" in str(llm.openai_api_base)

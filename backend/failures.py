"""Safe provider failures suitable for display and persistence; no provider bodies."""

MESSAGES = {
    "missing_key": "Add an OpenRouter API key in Setup before sending a message.",
    "authentication": "OpenRouter rejected the API key. Update it in Setup, then retry.",
    "credits": "OpenRouter reports insufficient credits. Add credits to your OpenRouter account, then retry.",
    "access": "OpenRouter denied access to this model or request. Check the model and key permissions in Setup.",
    "model": "The selected model is unavailable. Choose another model in Setup, then retry.",
    "request": "The model rejected this request. Check its settings in Setup or shorten the question.",
    "reasoning": "This model rejected the reasoning setting. Choose Default or a supported effort in Setup, then retry.",
    "rate_limit": "OpenRouter is rate limiting requests. Wait a moment, then retry.",
    "timeout": "The model took too long to respond. Please retry.",
    "network": "Could not reach OpenRouter. Check the connection, then retry.",
    "unavailable": "OpenRouter or the model provider is temporarily unavailable. Please retry shortly.",
    "empty": "The model returned an empty answer. Please retry.",
    "invalid_response": "The model returned an invalid response. Please retry.",
}


class ProviderError(Exception):
    def __init__(self, code: str):
        self.code = code
        self.setup_required = code in {
            "missing_key",
            "authentication",
            "access",
            "model",
            "request",
            "reasoning",
        }
        super().__init__(MESSAGES[code])

    def as_dict(self) -> dict:
        return {"code": self.code, "message": str(self), "setup_required": self.setup_required}

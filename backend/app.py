import os
from typing import Any

from flask import Flask
from flask_cors import CORS

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:
    load_dotenv = None

from routes import _summarize_job_metrics, api_bp
from services.agent_chat_runtime import AgentChatRuntime
from services.personality_rag import PersonalityRagService
from services.storage import Storage


storage = None
agent_chat = None
personality_rag = None


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return bool(default)
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _parse_cors_origins(raw: str) -> Any:
    value = str(raw or "").strip()
    if not value:
        return []
    if value == "*":
        return "*"
    return [item.strip() for item in value.split(",") if item.strip()]


def _configure_cors(flask_app: Flask) -> None:
    raw_origins = os.getenv(
        "BACKEND_CORS_ORIGINS",
        os.getenv("RENTAL_CORS_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000"),
    )
    origins = _parse_cors_origins(raw_origins)
    if origins:
        CORS(flask_app, resources={r"/*": {"origins": origins}})


def create_app() -> Flask:
    global storage, agent_chat, personality_rag
    flask_app = Flask(__name__)
    _configure_cors(flask_app)
    storage = Storage()
    agent_chat = AgentChatRuntime(storage=storage)
    personality_rag = PersonalityRagService(storage=storage)
    flask_app.config["storage"] = storage
    flask_app.config["agent_chat"] = agent_chat
    flask_app.config["personality_rag"] = personality_rag
    flask_app.register_blueprint(api_bp)
    return flask_app


app = create_app()


if __name__ == "__main__":
    app.run(
        host=os.getenv("BACKEND_HOST", "0.0.0.0"),
        port=int(os.getenv("BACKEND_PORT", "5002")),
        debug=_env_bool("BACKEND_DEBUG", _env_bool("FLASK_DEBUG", False)),
    )

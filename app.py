"""
Spark — Flask Backend
=====================
Auth (JWT) + Chat (Socket.IO) + Payments (Stripe)
"""

import os
from dotenv import load_dotenv

load_dotenv()

from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_bcrypt import Bcrypt
from flask_jwt_extended import JWTManager
from flask_socketio import SocketIO
from flask_cors import CORS
from sqlalchemy import inspect, text

# ── Extension instances (imported by other modules) ──────────────────────────
db       = SQLAlchemy()
bcrypt   = Bcrypt()
jwt      = JWTManager()
socketio = SocketIO()


def _ensure_runtime_schema(app):
    """Small SQLite-friendly updater for new columns while this project has no migrations."""
    with app.app_context():
        inspector = inspect(db.engine)
        if "users" in inspector.get_table_names():
            user_columns = {col["name"] for col in inspector.get_columns("users")}
            additions = {
                "cash_balance_cents": "INTEGER NOT NULL DEFAULT 0",
                "total_earned_cents": "INTEGER NOT NULL DEFAULT 0",
                "total_paid_out_cents": "INTEGER NOT NULL DEFAULT 0",
            }
            with db.engine.begin() as conn:
                for column, ddl in additions.items():
                    if column not in user_columns:
                        conn.execute(text(f"ALTER TABLE users ADD COLUMN {column} {ddl}"))


def create_app():
    app = Flask(__name__, static_folder="static", template_folder="templates")

    # ── Config ───────────────────────────────────────────────────────────────
    app.config["SECRET_KEY"]                  = os.getenv("SECRET_KEY", "dev-secret-change-me")
    app.config["JWT_SECRET_KEY"]              = os.getenv("JWT_SECRET_KEY", "jwt-secret-change-me")
    app.config["JWT_ACCESS_TOKEN_EXPIRES"]    = False   # long-lived for demo; tighten in prod
    app.config["SQLALCHEMY_DATABASE_URI"]     = os.getenv("DATABASE_URL", "sqlite:///spark.db")
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["STRIPE_SECRET_KEY"]           = os.getenv("STRIPE_SECRET_KEY", "")
    app.config["STRIPE_PUBLISHABLE_KEY"]      = os.getenv("STRIPE_PUBLISHABLE_KEY", "")
    app.config["STRIPE_WEBHOOK_SECRET"]       = os.getenv("STRIPE_WEBHOOK_SECRET", "")

    # ── Extensions ───────────────────────────────────────────────────────────
    db.init_app(app)
    bcrypt.init_app(app)
    jwt.init_app(app)
    # Allow all origins — needed for ngrok, mobile, and cross-device access
    CORS(app, resources={r"/*": {"origins": "*"}},
         supports_credentials=True,
         allow_headers=["Authorization", "Content-Type"])

    socketio.init_app(
        app,
        cors_allowed_origins="*",
        async_mode="threading",   # eventlet breaks on Python 3.12+; threading works everywhere
        logger=False,
        engineio_logger=False,
        ping_timeout=60,
        ping_interval=25,
    )

    # ── Blueprints ───────────────────────────────────────────────────────────
    from routes.auth     import auth_bp
    from routes.payments import payments_bp
    from routes.users    import users_bp
    from routes.chat     import chat_bp
    from routes.friends  import friends_bp
    from routes.admin    import admin_bp

    app.register_blueprint(auth_bp,     url_prefix="/api/auth")
    app.register_blueprint(payments_bp, url_prefix="/api/payments")
    app.register_blueprint(users_bp,    url_prefix="/api/users")
    app.register_blueprint(chat_bp,     url_prefix="/api/chat")
    app.register_blueprint(friends_bp,  url_prefix="/api/friends")
    app.register_blueprint(admin_bp,    url_prefix="/api/admin")

    # ── Socket.IO events ─────────────────────────────────────────────────────
    from sockets import events   # noqa: F401  (registers handlers as side-effect)

    # ── DB init ──────────────────────────────────────────────────────────────
    with app.app_context():
        db.create_all()
    _ensure_runtime_schema(app)

    # ── Security headers — required for getUserMedia on non-localhost ────────
    from flask import send_from_directory

    @app.after_request
    def add_security_headers(response):
        # Allow camera/mic access via Permissions-Policy
        response.headers["Permissions-Policy"] = "camera=*, microphone=*"
        # DO NOT set Cross-Origin-Embedder-Policy — it blocks CDN scripts and
        # cross-origin fetch calls (socket.io CDN, API calls from ngrok URLs)
        return response

    @app.route("/favicon.ico")
    def favicon():
        return "", 204   # no favicon — return empty 204 to stop 404 noise

    @app.errorhandler(404)
    def not_found(_error):
        from flask import request
        if request.path.startswith(("/api/", "/socket.io", "/.well-known")):
            return {"error": "Not found"}, 404
        return send_from_directory("static", "404.html"), 404

    @app.route("/")
    def index():
        return send_from_directory("static", "strangerdate.html")

    @app.route("/<path:filename>")
    def serve_static(filename):
        # Never intercept API, socket or well-known paths
        if filename.startswith(("api/", "socket.io", ".well-known")):
            from flask import abort
            abort(404)
        return send_from_directory("static", filename)

    return app


if __name__ == "__main__":
    app = create_app()
    socketio.run(
        app,
        host="0.0.0.0",
        port=int(os.getenv("PORT", "5000")),
        debug=os.getenv("FLASK_DEBUG", "0") == "1",
        allow_unsafe_werkzeug=True,
        use_reloader=False,
    )

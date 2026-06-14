"""
routes/chat.py — Chat session management & message history
"""

import os
import secrets
import time
from datetime import datetime, timezone
from flask import Blueprint, request, jsonify, current_app
from flask_jwt_extended import jwt_required, get_jwt_identity
from werkzeug.utils import secure_filename
from app import db
from models import User, ChatSession, ChatMessage

chat_bp = Blueprint("chat", __name__)
ALLOWED_IMAGE_EXTS = {"jpg", "jpeg", "png", "gif", "webp"}
MAX_CHAT_PHOTO_BYTES = 5 * 1024 * 1024

def utcnow():
    return datetime.now(timezone.utc)


def _allowed_image(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_IMAGE_EXTS


def _is_session_member(session, user_id):
    return session.user1_id == user_id or session.user2_id == user_id


# ── Start a new chat session ──────────────────────────────────────────────────

@chat_bp.route("/session/start", methods=["POST"])
@jwt_required()
def start_session():
    user_id = int(get_jwt_identity())
    user    = User.query.get_or_404(user_id)
    data    = request.get_json(silent=True) or {}

    session_type = data.get("type", "video")  # video | audio

    session = ChatSession(
        user1_id=user_id,
        session_type=session_type,
        tokens_spent=0,
    )
    db.session.add(session)
    db.session.commit()

    return jsonify({
        "session_id": session.id,
        "tokens":     user.tokens,
        "message":    "Session started.",
    }), 201


# ── End a chat session ────────────────────────────────────────────────────────

@chat_bp.route("/session/<int:session_id>/end", methods=["POST"])
@jwt_required()
def end_session(session_id):
    user_id = int(get_jwt_identity())
    session = ChatSession.query.get_or_404(session_id)

    if session.user1_id != user_id:
        return jsonify({"error": "Unauthorized."}), 403

    if session.ended_at:
        return jsonify({"error": "Session already ended."}), 400

    session.ended_at = utcnow()
    db.session.commit()

    return jsonify({
        "message":          "Session ended.",
        "duration_seconds": session.duration_seconds(),
    }), 200


# ── Save a chat message ───────────────────────────────────────────────────────

@chat_bp.route("/session/<int:session_id>/message", methods=["POST"])
@jwt_required()
def save_message(session_id):
    user_id = int(get_jwt_identity())
    session = ChatSession.query.get_or_404(session_id)

    if not _is_session_member(session, user_id):
        return jsonify({"error": "Unauthorized."}), 403

    data     = request.get_json(silent=True) or {}
    content  = (data.get("content") or "").strip()
    msg_type = data.get("type", "text")

    if msg_type not in ("text", "image"):
        return jsonify({"error": "Invalid message type."}), 422
    if not content:
        return jsonify({"error": "Message content is required."}), 422
    if msg_type == "image" and not content.startswith("/static/chat_uploads/"):
        return jsonify({"error": "Invalid photo."}), 422

    msg = ChatMessage(
        session_id=session_id,
        sender_id=user_id,
        content=content,
        msg_type=msg_type,
    )
    db.session.add(msg)
    db.session.commit()

    return jsonify({"message": msg.to_dict()}), 201


@chat_bp.route("/session/<int:session_id>/photo", methods=["POST"])
@jwt_required()
def upload_session_photo(session_id):
    user_id = int(get_jwt_identity())
    session = ChatSession.query.get_or_404(session_id)
    if not _is_session_member(session, user_id):
        return jsonify({"error": "Unauthorized."}), 403

    file = request.files.get("photo")
    if not file or not file.filename:
        return jsonify({"error": "Photo is required."}), 400
    if not _allowed_image(file.filename):
        return jsonify({"error": "Only JPG, PNG, GIF, or WebP images are allowed."}), 422

    file.seek(0, os.SEEK_END)
    size = file.tell()
    file.seek(0)
    if size > MAX_CHAT_PHOTO_BYTES:
        return jsonify({"error": "Photo must be 5MB or smaller."}), 413

    ext = secure_filename(file.filename).rsplit(".", 1)[1].lower()
    filename = f"chat_{session_id}_{user_id}_{int(time.time())}_{secrets.token_hex(8)}.{ext}"
    upload_dir = os.path.join(current_app.static_folder, "chat_uploads")
    os.makedirs(upload_dir, exist_ok=True)
    file.save(os.path.join(upload_dir, filename))

    return jsonify({"url": f"/static/chat_uploads/{filename}"}), 201


# ── Get message history for a session ────────────────────────────────────────

@chat_bp.route("/session/<int:session_id>/messages", methods=["GET"])
@jwt_required()
def get_messages(session_id):
    user_id = int(get_jwt_identity())
    session = ChatSession.query.get_or_404(session_id)

    if not _is_session_member(session, user_id):
        return jsonify({"error": "Unauthorized."}), 403

    messages = (
        ChatMessage.query
        .filter_by(session_id=session_id)
        .order_by(ChatMessage.created_at.asc())
        .all()
    )
    return jsonify({"messages": [m.to_dict() for m in messages]}), 200


# ── My session history ────────────────────────────────────────────────────────

@chat_bp.route("/sessions", methods=["GET"])
@jwt_required()
def my_sessions():
    user_id  = int(get_jwt_identity())
    page     = request.args.get("page", 1, type=int)
    per_page = min(request.args.get("per_page", 20, type=int), 100)

    pagination = (
        ChatSession.query
        .filter_by(user1_id=user_id)
        .order_by(ChatSession.started_at.desc())
        .paginate(page=page, per_page=per_page, error_out=False)
    )
    sessions = [
        {
            "id":             s.id,
            "type":           s.session_type,
            "started_at":     s.started_at.isoformat(),
            "ended_at":       s.ended_at.isoformat() if s.ended_at else None,
            "duration_secs":  s.duration_seconds(),
            "tokens_spent":   s.tokens_spent,
            "message_count":  s.messages.count(),
        }
        for s in pagination.items
    ]
    return jsonify({
        "sessions": sessions,
        "total":    pagination.total,
        "page":     page,
        "pages":    pagination.pages,
    }), 200

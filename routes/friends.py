"""
routes/friends.py — Friend requests, friend list, DM history
"""
import os
import secrets
import time
from flask import Blueprint, request, jsonify, current_app
from flask_jwt_extended import jwt_required, get_jwt_identity
from werkzeug.utils import secure_filename
from app import db
from models import User, Friendship, DirectMessage
from sqlalchemy import or_, and_

friends_bp = Blueprint("friends", __name__)
ALLOWED_IMAGE_EXTS = {"jpg", "jpeg", "png", "gif", "webp"}
MAX_DM_PHOTO_BYTES = 5 * 1024 * 1024


def _get_friendship(user_id, other_id):
    return Friendship.query.filter(
        or_(
            and_(Friendship.requester_id == user_id,  Friendship.receiver_id == other_id),
            and_(Friendship.requester_id == other_id, Friendship.receiver_id == user_id),
        )
    ).first()


def _allowed_image(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_IMAGE_EXTS


# ── Send friend request ───────────────────────────────────────────────────────

@friends_bp.route("/request/<int:other_id>", methods=["POST"])
@jwt_required()
def send_request(other_id):
    user_id = int(get_jwt_identity())
    if user_id == other_id:
        return jsonify({"error": "You cannot add yourself."}), 400

    User.query.get_or_404(other_id)
    existing = _get_friendship(user_id, other_id)

    if existing:
        if existing.status == "accepted":
            return jsonify({"error": "Already friends."}), 409
        if existing.status == "pending":
            return jsonify({"error": "Request already sent."}), 409
        if existing.status == "blocked":
            return jsonify({"error": "Cannot send request."}), 403

    f = Friendship(requester_id=user_id, receiver_id=other_id, status="pending")
    db.session.add(f)
    db.session.commit()
    return jsonify({"message": "Friend request sent.", "friendship": f.to_dict(user_id)}), 201


# ── Accept / decline request ──────────────────────────────────────────────────

@friends_bp.route("/request/<int:friendship_id>/accept", methods=["POST"])
@jwt_required()
def accept_request(friendship_id):
    user_id = int(get_jwt_identity())
    f = Friendship.query.get_or_404(friendship_id)
    if f.receiver_id != user_id:
        return jsonify({"error": "Not authorized."}), 403
    if f.status != "pending":
        return jsonify({"error": "Request is not pending."}), 400
    f.status = "accepted"
    db.session.commit()
    return jsonify({"message": "Friend request accepted.", "friendship": f.to_dict(user_id)}), 200


@friends_bp.route("/request/<int:friendship_id>/decline", methods=["POST"])
@jwt_required()
def decline_request(friendship_id):
    user_id = int(get_jwt_identity())
    f = Friendship.query.get_or_404(friendship_id)
    if f.receiver_id != user_id:
        return jsonify({"error": "Not authorized."}), 403
    db.session.delete(f)
    db.session.commit()
    return jsonify({"message": "Request declined."}), 200


# ── Remove friend ─────────────────────────────────────────────────────────────

@friends_bp.route("/<int:other_id>", methods=["DELETE"])
@jwt_required()
def remove_friend(other_id):
    user_id = int(get_jwt_identity())
    f = _get_friendship(user_id, other_id)
    if not f:
        return jsonify({"error": "Not friends."}), 404
    db.session.delete(f)
    db.session.commit()
    return jsonify({"message": "Friend removed."}), 200


# ── List accepted friends ─────────────────────────────────────────────────────

@friends_bp.route("/", methods=["GET"])
@jwt_required()
def list_friends():
    user_id = int(get_jwt_identity())
    friendships = Friendship.query.filter(
        Friendship.status == "accepted",
        or_(Friendship.requester_id == user_id, Friendship.receiver_id == user_id)
    ).all()
    return jsonify({"friends": [f.to_dict(user_id) for f in friendships]}), 200


# ── Pending incoming requests ─────────────────────────────────────────────────

@friends_bp.route("/pending", methods=["GET"])
@jwt_required()
def pending_requests():
    user_id = int(get_jwt_identity())
    pending = Friendship.query.filter_by(receiver_id=user_id, status="pending").all()
    return jsonify({"pending": [f.to_dict(user_id) for f in pending]}), 200


# ── Sent (outgoing) requests ──────────────────────────────────────────────────

@friends_bp.route("/sent", methods=["GET"])
@jwt_required()
def sent_requests():
    user_id = int(get_jwt_identity())
    sent = Friendship.query.filter_by(requester_id=user_id, status="pending").all()
    return jsonify({"sent": [f.to_dict(user_id) for f in sent]}), 200


# ── Search users by name/email ────────────────────────────────────────────────

@friends_bp.route("/search", methods=["GET"])
@jwt_required()
def search_users():
    user_id = int(get_jwt_identity())
    q = (request.args.get("q") or "").strip()

    query = User.query.filter(User.id != user_id)

    if q:
        for term in q.split():
            search_term = f"{term}%" if len(term) == 1 else f"%{term}%"
            query = query.filter(or_(
                User.first_name.ilike(search_term),
                User.last_name.ilike(search_term),
                User.email.ilike(search_term)
            ))

    users = query.limit(100).all()

    results = []
    for u in users:
        f = _get_friendship(user_id, u.id)
        results.append({
            "id":           u.id,
            "first_name":   u.first_name,
            "last_name":    u.last_name,
            "avatar_url":   u.avatar_url,
            "is_premium":   getattr(u, "is_premium", False),
            "friendship":   f.status if f else None,
            "friendship_id": f.id if f else None,
        })

    return jsonify({"users": results}), 200


# ── DM: send message ──────────────────────────────────────────────────────────

@friends_bp.route("/dm/photo", methods=["POST"])
@jwt_required()
def upload_dm_photo():
    file = request.files.get("photo")
    if not file or not file.filename:
        return jsonify({"error": "Photo is required."}), 400
    if not _allowed_image(file.filename):
        return jsonify({"error": "Only JPG, PNG, GIF, or WebP images are allowed."}), 422

    file.seek(0, os.SEEK_END)
    size = file.tell()
    file.seek(0)
    if size > MAX_DM_PHOTO_BYTES:
        return jsonify({"error": "Photo must be 5MB or smaller."}), 413

    ext = secure_filename(file.filename).rsplit(".", 1)[1].lower()
    filename = f"dm_{int(time.time())}_{secrets.token_hex(8)}.{ext}"
    upload_dir = os.path.join(current_app.static_folder, "dm_uploads")
    os.makedirs(upload_dir, exist_ok=True)
    file.save(os.path.join(upload_dir, filename))

    return jsonify({"url": f"/static/dm_uploads/{filename}"}), 201

@friends_bp.route("/dm/<int:other_id>", methods=["POST"])
@jwt_required()
def send_dm(other_id):
    user_id = int(get_jwt_identity())
    User.query.get_or_404(other_id)

    # Must be friends
    f = _get_friendship(user_id, other_id)
    if not f or f.status != "accepted":
        return jsonify({"error": "You can only message friends."}), 403

    data    = request.get_json(silent=True) or {}
    content = (data.get("content") or "").strip()
    msg_type = data.get("type", "text")
    if msg_type not in ("text", "image"):
        return jsonify({"error": "Invalid message type."}), 422
    if not content:
        return jsonify({"error": "Message cannot be empty."}), 422
    if msg_type == "image" and not content.startswith("/static/dm_uploads/"):
        return jsonify({"error": "Invalid photo."}), 422

    msg = DirectMessage(
        sender_id=user_id,
        receiver_id=other_id,
        content=content,
        msg_type=msg_type,
    )
    db.session.add(msg)
    db.session.commit()
    return jsonify({"message": msg.to_dict()}), 201


# ── DM: get conversation ──────────────────────────────────────────────────────

@friends_bp.route("/dm/<int:other_id>", methods=["GET"])
@jwt_required()
def get_dm(other_id):
    user_id = int(get_jwt_identity())
    page     = request.args.get("page", 1, type=int)
    per_page = min(request.args.get("per_page", 50, type=int), 100)

    msgs = DirectMessage.query.filter(
        or_(
            and_(DirectMessage.sender_id == user_id,  DirectMessage.receiver_id == other_id),
            and_(DirectMessage.sender_id == other_id, DirectMessage.receiver_id == user_id),
        )
    ).order_by(DirectMessage.created_at.desc()).paginate(
        page=page, per_page=per_page, error_out=False
    )

    # Mark received messages as read
    DirectMessage.query.filter_by(
        sender_id=other_id, receiver_id=user_id, is_read=False
    ).update({"is_read": True})
    db.session.commit()

    return jsonify({
        "messages": [m.to_dict() for m in reversed(msgs.items)],
        "total":    msgs.total,
        "page":     page,
        "pages":    msgs.pages,
    }), 200


# ── DM: unread count ──────────────────────────────────────────────────────────

@friends_bp.route("/dm/unread", methods=["GET"])
@jwt_required()
def unread_count():
    user_id = int(get_jwt_identity())
    count   = DirectMessage.query.filter_by(receiver_id=user_id, is_read=False).count()
    return jsonify({"unread": count}), 200

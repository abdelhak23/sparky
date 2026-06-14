"""
routes/users.py — User profiles, stats, reports
"""

from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity
from app import db
from models import User, Report, ChatSession, Gift

users_bp = Blueprint("users", __name__)


# ── Public profile ────────────────────────────────────────────────────────────

@users_bp.route("/<int:user_id>", methods=["GET"])
@jwt_required()
def get_user(user_id):
    user = User.query.get_or_404(user_id)
    return jsonify({"user": user.to_dict()}), 200


# ── My stats ─────────────────────────────────────────────────────────────────

@users_bp.route("/me/stats", methods=["GET"])
@jwt_required()
def my_stats():
    user_id = int(get_jwt_identity())

    total_sessions = ChatSession.query.filter_by(user1_id=user_id).count()
    total_seconds  = sum(
        s.duration_seconds()
        for s in ChatSession.query.filter_by(user1_id=user_id).all()
    )
    gifts_sent  = Gift.query.filter_by(sender_id=user_id).count()
    tokens_spent_on_gifts = db.session.query(
        db.func.sum(Gift.tokens_cost)
    ).filter_by(sender_id=user_id).scalar() or 0

    return jsonify({
        "total_sessions":       total_sessions,
        "total_minutes_chatted": round(total_seconds / 60, 1),
        "gifts_sent":           gifts_sent,
        "tokens_spent_on_gifts": tokens_spent_on_gifts,
    }), 200


# ── Submit a report ───────────────────────────────────────────────────────────

@users_bp.route("/report", methods=["POST"])
@jwt_required()
def submit_report():
    reporter_id = int(get_jwt_identity())
    data        = request.get_json(silent=True) or {}

    reason     = data.get("reason", "").strip()
    session_id = data.get("session_id")
    notes      = data.get("notes", "").strip()

    if not reason:
        return jsonify({"error": "Reason is required."}), 422

    reported_id = None
    if session_id:
        session = ChatSession.query.get(session_id)
        if session:
            if session.user1_id == reporter_id:
                reported_id = session.user2_id
            elif session.user2_id == reporter_id:
                reported_id = session.user1_id

    report = Report(
        reporter_id=reporter_id,
        reported_id=reported_id,
        session_id=session_id,
        reason=reason,
        notes=notes,
    )
    db.session.add(report)
    db.session.commit()

    return jsonify({"message": "Report submitted. Our team will review it shortly."}), 201


# ── List my reports ───────────────────────────────────────────────────────────

@users_bp.route("/me/reports", methods=["GET"])
@jwt_required()
def my_reports():
    user_id = int(get_jwt_identity())
    reports = Report.query.filter_by(reporter_id=user_id).order_by(
        Report.created_at.desc()
    ).all()
    return jsonify({
        "reports": [
            {
                "id":         r.id,
                "reason":     r.reason,
                "status":     r.status,
                "created_at": r.created_at.isoformat(),
            }
            for r in reports
        ]
    }), 200


# ── TURN credentials (called by frontend before WebRTC) ───────────────────────

@users_bp.route("/turn-credentials", methods=["GET"])
@jwt_required()
def turn_credentials():
    """
    Returns ICE server config to the frontend.
    In production, you can configure your own TURN server or a service
    like Twilio or Xirsys.
    """
    import os
    turn_url  = os.getenv("TURN_URL",  "").strip()
    turn_user = os.getenv("TURN_USER", "").strip()
    turn_pass = os.getenv("TURN_PASS", "").strip()

    # Accept either "eu-turn3.xirsys.com" or copied values like
    # "turn:eu-turn3.xirsys.com:3478?transport=udp" from provider dashboards.
    if turn_url.startswith("turns:"):
        turn_url = turn_url.removeprefix("turns:")
    elif turn_url.startswith("turn:"):
        turn_url = turn_url.removeprefix("turn:")
    turn_url = turn_url.split("?", 1)[0].split(":", 1)[0].strip("/")

    ice_servers = [
        {"urls": "stun:stun.l.google.com:19302"},
    ]
    if turn_url:
        ice_servers.extend([
            {"urls": f"stun:{turn_url}"},
            {
                "username": turn_user,
                "credential": turn_pass,
                "urls": [
                    f"turn:{turn_url}:80?transport=udp",
                    f"turn:{turn_url}:3478?transport=udp",
                    f"turn:{turn_url}:80?transport=tcp",
                    f"turn:{turn_url}:3478?transport=tcp",
                    f"turns:{turn_url}:443?transport=tcp",
                    f"turns:{turn_url}:5349?transport=tcp",
                ],
            },
        ])
    return jsonify({"iceServers": ice_servers}), 200

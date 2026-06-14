"""
routes/admin.py — Owner-only site dashboard
"""

import os
from functools import wraps
from flask import Blueprint, abort, jsonify, request
from flask_jwt_extended import jwt_required, get_jwt_identity
from app import db
from models import (
    User,
    Report,
    ChatSession,
    ChatMessage,
    DirectMessage,
    Friendship,
    Gift,
    TokenPurchase,
    WithdrawalRequest,
)
from sqlalchemy import or_

admin_bp = Blueprint("admin", __name__)
OWNER_EMAILS = {"abdelhaqnidahmed@gmail.com"}


def _admin_emails():
    raw = os.getenv("ADMIN_EMAILS") or os.getenv("ADMIN_EMAIL") or ""
    return OWNER_EMAILS | {email.strip().lower() for email in raw.split(",") if email.strip()}


def _require_admin(fn):
    @wraps(fn)
    @jwt_required()
    def wrapper(*args, **kwargs):
        user = User.query.get_or_404(int(get_jwt_identity()))
        allowed = _admin_emails()
        if not allowed:
            abort(404)
        if user.email.lower() not in allowed:
            abort(404)
        return fn(*args, **kwargs)
    return wrapper


def _session_seconds_for(user_id):
    sessions = ChatSession.query.filter(
        or_(ChatSession.user1_id == user_id, ChatSession.user2_id == user_id)
    ).all()
    return sum(s.duration_seconds() for s in sessions)


def _user_row(user):
    seconds = _session_seconds_for(user.id)
    gifts_sent = Gift.query.filter_by(sender_id=user.id).count()
    gifts_received = Gift.query.filter_by(receiver_id=user.id).count()
    purchases = TokenPurchase.query.filter_by(user_id=user.id).all()
    spent_cents = sum(p.amount_cents for p in purchases if p.status == "completed")

    return {
        "id": user.id,
        "email": user.email,
        "first_name": user.first_name,
        "last_name": user.last_name,
        "age": user.age,
        "gender": user.gender,
        "country": user.country,
        "tokens": user.tokens,
        "is_premium": user.is_premium,
        "is_verified": user.is_verified,
        "is_active": user.is_active,
        "avatar_url": user.avatar_url,
        "created_at": user.created_at.isoformat() if user.created_at else None,
        "last_login": user.last_login.isoformat() if user.last_login else None,
        "password": "Not available. Passwords are stored as hashes only.",
        "password_hash_prefix": (user.password_hash or "")[:18],
        "password_hash_length": len(user.password_hash or ""),
        "sessions": ChatSession.query.filter(
            or_(ChatSession.user1_id == user.id, ChatSession.user2_id == user.id)
        ).count(),
        "minutes_spent": round(seconds / 60, 1),
        "dm_sent": DirectMessage.query.filter_by(sender_id=user.id).count(),
        "dm_received": DirectMessage.query.filter_by(receiver_id=user.id).count(),
        "reports_made": Report.query.filter_by(reporter_id=user.id).count(),
        "gifts_sent": gifts_sent,
        "gifts_received": gifts_received,
        "purchase_count": len(purchases),
        "spent_cents": spent_cents,
        "cash_balance_cents": user.cash_balance_cents or 0,
        "total_earned_cents": user.total_earned_cents or 0,
        "total_paid_out_cents": user.total_paid_out_cents or 0,
    }


def _compact_user(user):
    if not user:
        return None
    return {
        "id": user.id,
        "email": user.email,
        "first_name": user.first_name,
        "last_name": user.last_name,
        "age": user.age,
        "gender": user.gender,
        "country": user.country,
        "is_active": user.is_active,
        "is_verified": user.is_verified,
    }


def _delete_user_account(user):
    user1_session_ids = [
        sid for (sid,) in ChatSession.query.with_entities(ChatSession.id).filter_by(user1_id=user.id).all()
    ]
    if user1_session_ids:
        ChatMessage.query.filter(ChatMessage.session_id.in_(user1_session_ids)).delete(synchronize_session=False)
        ChatSession.query.filter(ChatSession.id.in_(user1_session_ids)).delete(synchronize_session=False)

    ChatMessage.query.filter_by(sender_id=user.id).delete(synchronize_session=False)
    ChatSession.query.filter_by(user2_id=user.id).update({"user2_id": None}, synchronize_session=False)

    DirectMessage.query.filter(
        or_(DirectMessage.sender_id == user.id, DirectMessage.receiver_id == user.id)
    ).delete(synchronize_session=False)
    Friendship.query.filter(
        or_(Friendship.requester_id == user.id, Friendship.receiver_id == user.id)
    ).delete(synchronize_session=False)
    TokenPurchase.query.filter_by(user_id=user.id).delete(synchronize_session=False)
    WithdrawalRequest.query.filter_by(user_id=user.id).delete(synchronize_session=False)
    Gift.query.filter_by(sender_id=user.id).delete(synchronize_session=False)
    Gift.query.filter_by(receiver_id=user.id).update({"receiver_id": None}, synchronize_session=False)
    Report.query.filter_by(reporter_id=user.id).delete(synchronize_session=False)
    Report.query.filter_by(reported_id=user.id).update({"reported_id": None}, synchronize_session=False)
    for verification in list(user.email_verifications):
        db.session.delete(verification)

    db.session.delete(user)


@admin_bp.route("/overview", methods=["GET"])
@_require_admin
def overview():
    users = User.query.order_by(User.created_at.desc()).all()
    users_by_id = {u.id: u for u in users}
    sessions = ChatSession.query.order_by(ChatSession.started_at.desc()).all()
    purchases = TokenPurchase.query.order_by(TokenPurchase.created_at.desc()).all()
    reports = Report.query.order_by(Report.created_at.desc()).all()
    withdrawals = WithdrawalRequest.query.order_by(WithdrawalRequest.created_at.desc()).all()

    total_seconds = sum(s.duration_seconds() for s in sessions)
    completed_purchases = [p for p in purchases if p.status == "completed"]

    return jsonify({
        "summary": {
            "users": len(users),
            "active_users": sum(1 for u in users if u.is_active),
            "premium_users": sum(1 for u in users if u.is_premium),
            "tokens_in_circulation": sum(u.tokens for u in users),
            "sessions": len(sessions),
            "minutes_spent": round(total_seconds / 60, 1),
            "direct_messages": DirectMessage.query.count(),
            "chat_messages": ChatMessage.query.count(),
            "friendships": Friendship.query.count(),
            "reports": len(reports),
            "pending_reports": sum(1 for r in reports if r.status == "pending"),
            "gifts": Gift.query.count(),
            "gift_tokens": db.session.query(db.func.sum(Gift.tokens_cost)).scalar() or 0,
            "revenue_cents": sum(p.amount_cents for p in completed_purchases),
            "purchases": len(purchases),
            "pending_cashouts": sum(1 for w in withdrawals if w.status == "pending"),
            "cashout_cents": sum(w.amount_cents for w in withdrawals if w.status == "paid"),
            "creator_balance_cents": sum((u.cash_balance_cents or 0) for u in users),
        },
        "users": [_user_row(u) for u in users],
        "reports": [
            {
                "id": r.id,
                "reporter_id": r.reporter_id,
                "reporter_email": users_by_id[r.reporter_id].email if r.reporter_id in users_by_id else None,
                "reporter": _compact_user(users_by_id.get(r.reporter_id)),
                "reported_id": r.reported_id,
                "reported_email": users_by_id[r.reported_id].email if r.reported_id in users_by_id else None,
                "reported": _compact_user(users_by_id.get(r.reported_id)),
                "session_id": r.session_id,
                "reason": r.reason,
                "notes": r.notes,
                "status": r.status,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in reports
        ],
        "sessions": [
            {
                "id": s.id,
                "user1_id": s.user1_id,
                "user2_id": s.user2_id,
                "type": s.session_type,
                "started_at": s.started_at.isoformat() if s.started_at else None,
                "ended_at": s.ended_at.isoformat() if s.ended_at else None,
                "duration_seconds": s.duration_seconds(),
                "tokens_spent": s.tokens_spent,
                "message_count": s.messages.count(),
            }
            for s in sessions[:250]
        ],
        "purchases": [
            {
                "id": p.id,
                "user_id": p.user_id,
                "email": users_by_id[p.user_id].email if p.user_id in users_by_id else None,
                "tokens_purchased": p.tokens_purchased,
                "bonus_tokens": p.bonus_tokens,
                "total_tokens": p.tokens_purchased + p.bonus_tokens,
                "amount_cents": p.amount_cents,
                "currency": p.currency,
                "status": p.status,
                "created_at": p.created_at.isoformat() if p.created_at else None,
                "completed_at": p.completed_at.isoformat() if p.completed_at else None,
            }
            for p in purchases[:250]
        ],
        "withdrawals": [
            {
                "id": w.id,
                "user_id": w.user_id,
                "email": users_by_id[w.user_id].email if w.user_id in users_by_id else None,
                "name": (
                    f"{users_by_id[w.user_id].first_name} {users_by_id[w.user_id].last_name or ''}".strip()
                    if w.user_id in users_by_id else None
                ),
                "amount_cents": w.amount_cents,
                "method": w.method,
                "destination": w.destination,
                "status": w.status,
                "admin_note": w.admin_note,
                "created_at": w.created_at.isoformat() if w.created_at else None,
                "processed_at": w.processed_at.isoformat() if w.processed_at else None,
            }
            for w in withdrawals[:250]
        ],
    }), 200


@admin_bp.route("/withdrawals/<int:withdrawal_id>", methods=["PATCH"])
@_require_admin
def update_withdrawal(withdrawal_id):
    from datetime import datetime, timezone

    data = request.get_json(silent=True) or {}
    status = (data.get("status") or "").strip().lower()
    note = (data.get("admin_note") or "").strip()
    if status not in ("paid", "rejected"):
        return jsonify({"error": "Status must be paid or rejected."}), 422

    withdrawal = WithdrawalRequest.query.get_or_404(withdrawal_id)
    if withdrawal.status != "pending":
        return jsonify({"error": "Only pending withdrawals can be updated."}), 422

    user = User.query.get(withdrawal.user_id)
    withdrawal.status = status
    withdrawal.admin_note = note or None
    withdrawal.processed_at = datetime.now(timezone.utc)

    if status == "paid" and user:
        user.total_paid_out_cents = (user.total_paid_out_cents or 0) + withdrawal.amount_cents
    elif status == "rejected" and user:
        user.cash_balance_cents = (user.cash_balance_cents or 0) + withdrawal.amount_cents

    db.session.commit()
    return jsonify({"message": f"Withdrawal marked {status}.", "withdrawal": withdrawal.to_dict()}), 200


@admin_bp.route("/users/<int:user_id>", methods=["DELETE"])
@_require_admin
def delete_user(user_id):
    admin_id = int(get_jwt_identity())
    if user_id == admin_id:
        return jsonify({"error": "You cannot delete your own admin account here."}), 400

    user = User.query.get_or_404(user_id)
    email = user.email
    _delete_user_account(user)
    db.session.commit()
    return jsonify({"message": f"Account deleted: {email}", "deleted_user_id": user_id}), 200

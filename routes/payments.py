"""
routes/payments.py — Stripe Checkout (card only), Webhooks, Purchase History

PAYMENT FLOW:
  1. POST /api/payments/create-checkout  → returns Stripe checkout URL
  2. User is redirected to Stripe hosted page (card only)
  3. Stripe calls POST /api/payments/webhook on success
  4. Webhook verifies signature, marks purchase completed, THEN adds tokens
  5. Client polls GET /api/payments/verify-session/:id to confirm new balance

Tokens are NEVER added before the webhook confirms payment.
"""

import os
import stripe
from datetime import datetime, timezone
from flask import Blueprint, request, jsonify, current_app
from flask_jwt_extended import jwt_required, get_jwt_identity
from app import db
from models import User, TokenPurchase, Gift, ChatSession, WithdrawalRequest

payments_bp = Blueprint("payments", __name__)

def utcnow():
    return datetime.now(timezone.utc)

def get_stripe():
    key = current_app.config.get("STRIPE_SECRET_KEY", "")
    if not key:
        raise RuntimeError("STRIPE_SECRET_KEY is not set in .env")
    stripe.api_key = key
    return stripe

# ── Token Packs ───────────────────────────────────────────────────────────────

TOKEN_PACKS = [
    {"id": "pack_50",   "tokens": 50,   "bonus": 0,   "price_cents": 399,  "label": "Starter"},
    {"id": "pack_150",  "tokens": 150,  "bonus": 20,  "price_cents": 999,  "label": "Popular"},
    {"id": "pack_300",  "tokens": 300,  "bonus": 50,  "price_cents": 1799, "label": "Value"},
    {"id": "pack_600",  "tokens": 600,  "bonus": 100, "price_cents": 2999, "label": "Pro"},
    {"id": "pack_1200", "tokens": 1200, "bonus": 300, "price_cents": 4999, "label": "Elite"},
    {"id": "pack_2500", "tokens": 2500, "bonus": 700, "price_cents": 8999, "label": "Ultimate"},
]

PACK_BY_ID = {p["id"]: p for p in TOKEN_PACKS}

GIFT_CATALOG = {
    "rose":    {"tokens": 5,   "usd": 0.25},
    "heart":   {"tokens": 10,  "usd": 0.50},
    "fire":    {"tokens": 20,  "usd": 1.00},
    "diamond": {"tokens": 50,  "usd": 2.50},
    "crown":   {"tokens": 100, "usd": 5.00},
    "rocket":  {"tokens": 150, "usd": 7.50},
    "star":    {"tokens": 30,  "usd": 1.50},
    "trophy":  {"tokens": 200, "usd": 10.00},
}

MIN_WITHDRAWAL_CENTS = int(os.getenv("MIN_WITHDRAWAL_CENTS", "1000"))


def _gift_value_cents(tokens):
    return int(tokens) * 5


def _credit_gift_receiver(receiver_id, amount_cents):
    if not receiver_id:
        return
    receiver = User.query.get(receiver_id)
    if not receiver:
        return
    receiver.cash_balance_cents = (receiver.cash_balance_cents or 0) + amount_cents
    receiver.total_earned_cents = (receiver.total_earned_cents or 0) + amount_cents


def _other_session_user(session_id, sender_id):
    if not session_id:
        return None
    session = ChatSession.query.get(session_id)
    if not session:
        return None
    if session.user1_id == sender_id:
        return session.user2_id
    if session.user2_id == sender_id:
        return session.user1_id
    return None

# ── List packs ────────────────────────────────────────────────────────────────

@payments_bp.route("/packs", methods=["GET"])
def list_packs():
    return jsonify({"packs": TOKEN_PACKS}), 200

@payments_bp.route("/gifts", methods=["GET"])
def gift_catalog():
    return jsonify({"gifts": GIFT_CATALOG}), 200

# ── Create Stripe Checkout (card only — NO token grant here) ──────────────────

@payments_bp.route("/create-checkout", methods=["POST"])
@jwt_required()
def create_checkout():
    user_id = int(get_jwt_identity())
    user    = User.query.get_or_404(user_id)
    data    = request.get_json(silent=True) or {}
    pack_id = data.get("pack_id", "")

    if pack_id not in PACK_BY_ID:
        return jsonify({"error": "Invalid pack selected."}), 400

    pack = PACK_BY_ID[pack_id]

    try:
        s = get_stripe()
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 503

    frontend_url  = os.getenv("FRONTEND_URL", "http://localhost:5000")
    total_tokens  = pack["tokens"] + pack["bonus"]

    try:
        checkout_session = s.checkout.Session.create(
            # Card is the ONLY accepted payment method
            payment_method_types=["card"],
            line_items=[{
                "price_data": {
                    "currency": "usd",
                    "unit_amount": pack["price_cents"],
                    "product_data": {
                        "name": f"Spark — {total_tokens} Tokens ({pack['label']})",
                        "description": (
                            f"{pack['tokens']} tokens"
                            + (f" + {pack['bonus']} bonus" if pack["bonus"] else "")
                        ),
                    },
                },
                "quantity": 1,
            }],
            mode="payment",
            # {CHECKOUT_SESSION_ID} replaced automatically by Stripe
            success_url=(
                f"{frontend_url}/payment-success.html"
                f"?session_id={{CHECKOUT_SESSION_ID}}"
            ),
            cancel_url=f"{frontend_url}/videochat.html?payment=cancelled",
            metadata={
                "user_id": str(user_id),
                "pack_id": pack_id,
                "tokens":  str(pack["tokens"]),
                "bonus":   str(pack["bonus"]),
            },
            customer_email=user.email,
            expires_at=int(utcnow().timestamp()) + 1800,  # 30 min expiry
        )
    except stripe.error.StripeError as e:
        return jsonify({"error": e.user_message or str(e)}), 502

    # Save PENDING record — tokens NOT added yet
    purchase = TokenPurchase(
        user_id=user_id,
        stripe_session_id=checkout_session.id,
        tokens_purchased=pack["tokens"],
        bonus_tokens=pack["bonus"],
        amount_cents=pack["price_cents"],
        status="pending",
    )
    db.session.add(purchase)
    db.session.commit()

    return jsonify({
        "checkout_url": checkout_session.url,
        "session_id":   checkout_session.id,
    }), 200

# ── Stripe Webhook — ONLY place tokens are ever credited ─────────────────────

@payments_bp.route("/webhook", methods=["POST"])
def stripe_webhook():
    payload        = request.get_data()
    sig_header     = request.headers.get("Stripe-Signature", "")
    webhook_secret = current_app.config.get("STRIPE_WEBHOOK_SECRET", "")

    try:
        s = get_stripe()
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 503

    # Verify webhook signature
    try:
        if webhook_secret:
            event = s.Webhook.construct_event(payload, sig_header, webhook_secret)
        else:
            if os.getenv("FLASK_ENV") == "production":
                return jsonify({"error": "Webhook secret not configured."}), 400
            import json
            event = stripe.Event.construct_from(json.loads(payload), s.api_key)
    except ValueError:
        return jsonify({"error": "Invalid payload."}), 400
    except stripe.error.SignatureVerificationError:
        return jsonify({"error": "Invalid signature."}), 400

    # Payment succeeded
    if event["type"] == "checkout.session.completed":
        session        = event["data"]["object"]
        payment_status = session.get("payment_status")

        # Only credit if actually paid (not e.g. bank transfer pending)
        if payment_status != "paid":
            return jsonify({"received": True, "action": "awaiting_payment"}), 200

        meta    = session.get("metadata", {})
        user_id = int(meta.get("user_id", 0))
        tokens  = int(meta.get("tokens", 0))
        bonus   = int(meta.get("bonus", 0))
        total   = tokens + bonus

        if user_id == 0 or total == 0:
            return jsonify({"error": "Bad metadata."}), 400

        # Idempotency guard — never credit the same session twice
        purchase = TokenPurchase.query.filter_by(
            stripe_session_id=session["id"]
        ).first()

        if purchase and purchase.status == "completed":
            return jsonify({"received": True, "action": "already_processed"}), 200

        # Credit tokens NOW (only after confirmed payment)
        user = User.query.get(user_id)
        if user:
            user.tokens += total

        if purchase:
            purchase.status            = "completed"
            purchase.stripe_payment_id = session.get("payment_intent")
            purchase.completed_at      = utcnow()

        db.session.commit()

    # Payment failed
    elif event["type"] == "payment_intent.payment_failed":
        pi = event["data"]["object"]
        purchase = TokenPurchase.query.filter_by(
            stripe_payment_id=pi["id"]
        ).first()
        if purchase and purchase.status == "pending":
            purchase.status = "failed"
            db.session.commit()

    # Refund issued — reverse the token credit
    elif event["type"] == "charge.refunded":
        charge     = event["data"]["object"]
        payment_id = charge.get("payment_intent")
        purchase   = TokenPurchase.query.filter_by(
            stripe_payment_id=payment_id, status="completed"
        ).first()
        if purchase:
            user  = User.query.get(purchase.user_id)
            total = purchase.tokens_purchased + purchase.bonus_tokens
            if user:
                user.tokens = max(0, user.tokens - total)
            purchase.status = "refunded"
            db.session.commit()

    # Dispute opened — freeze tokens
    elif event["type"] == "charge.dispute.created":
        charge     = event["data"]["object"]
        payment_id = charge.get("payment_intent")
        purchase   = TokenPurchase.query.filter_by(
            stripe_payment_id=payment_id, status="completed"
        ).first()
        if purchase:
            user  = User.query.get(purchase.user_id)
            total = purchase.tokens_purchased + purchase.bonus_tokens
            if user:
                user.tokens = max(0, user.tokens - total)
            purchase.status = "disputed"
            db.session.commit()

    return jsonify({"received": True}), 200

# ── Poll endpoint — success page calls this to check if webhook fired ─────────

@payments_bp.route("/verify-session/<stripe_session_id>", methods=["GET"])
@jwt_required()
def verify_session(stripe_session_id):
    """
    The payment-success page polls this every 2s after the Stripe redirect.
    Returns status + new token balance once the webhook has fired.
    Typically resolves within 1-3 seconds.
    """
    user_id  = int(get_jwt_identity())
    purchase = TokenPurchase.query.filter_by(
        stripe_session_id=stripe_session_id,
        user_id=user_id,
    ).first()

    if not purchase:
        return jsonify({"error": "Purchase not found."}), 404

    user = User.query.get(user_id)
    return jsonify({
        "status":       purchase.status,
        "tokens":       user.tokens if user else 0,
        "tokens_added": purchase.tokens_purchased + purchase.bonus_tokens,
        "amount_cents": purchase.amount_cents,
    }), 200

# ── Purchase history ──────────────────────────────────────────────────────────

@payments_bp.route("/history", methods=["GET"])
@jwt_required()
def purchase_history():
    user_id   = int(get_jwt_identity())
    purchases = (
        TokenPurchase.query
        .filter_by(user_id=user_id)
        .order_by(TokenPurchase.created_at.desc())
        .limit(50).all()
    )
    return jsonify({"purchases": [p.to_dict() for p in purchases]}), 200


# ── Cashout wallet ────────────────────────────────────────────────────────────

@payments_bp.route("/cashout/summary", methods=["GET"])
@jwt_required()
def cashout_summary():
    user_id = int(get_jwt_identity())
    user = User.query.get_or_404(user_id)
    requests = (
        WithdrawalRequest.query
        .filter_by(user_id=user_id)
        .order_by(WithdrawalRequest.created_at.desc())
        .limit(50).all()
    )
    gifts = (
        Gift.query
        .filter_by(receiver_id=user_id)
        .order_by(Gift.created_at.desc())
        .limit(50).all()
    )
    return jsonify({
        "cash_balance_cents": user.cash_balance_cents or 0,
        "total_earned_cents": user.total_earned_cents or 0,
        "total_paid_out_cents": user.total_paid_out_cents or 0,
        "minimum_withdrawal_cents": MIN_WITHDRAWAL_CENTS,
        "withdrawals": [w.to_dict() for w in requests],
        "received_gifts": [g.to_dict() for g in gifts],
    }), 200


@payments_bp.route("/cashout/request", methods=["POST"])
@jwt_required()
def request_cashout():
    user_id = int(get_jwt_identity())
    user = User.query.get_or_404(user_id)
    data = request.get_json(silent=True) or {}
    method = (data.get("method") or "").strip().lower()
    destination = (data.get("destination") or "").strip()
    try:
        amount_cents = int(data.get("amount_cents") or user.cash_balance_cents or 0)
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid cashout amount."}), 422

    if method not in ("paypal", "stripe"):
        return jsonify({"error": "Choose PayPal or Stripe."}), 422
    if not destination:
        return jsonify({"error": "Payout destination is required."}), 422
    if amount_cents < MIN_WITHDRAWAL_CENTS:
        return jsonify({"error": f"Minimum cashout is ${MIN_WITHDRAWAL_CENTS / 100:.2f}."}), 422
    if amount_cents > (user.cash_balance_cents or 0):
        return jsonify({"error": "Amount is higher than your available balance."}), 422

    user.cash_balance_cents -= amount_cents
    withdrawal = WithdrawalRequest(
        user_id=user_id,
        amount_cents=amount_cents,
        method=method,
        destination=destination,
        status="pending",
    )
    db.session.add(withdrawal)
    db.session.commit()
    return jsonify({
        "message": "Cashout request sent. Admin will review and pay it.",
        "cash_balance_cents": user.cash_balance_cents,
        "withdrawal": withdrawal.to_dict(),
    }), 201

# ── Deduct tokens ─────────────────────────────────────────────────────────────

@payments_bp.route("/deduct", methods=["POST"])
@jwt_required()
def deduct_tokens():
    user_id = int(get_jwt_identity())
    user    = User.query.get_or_404(user_id)
    data    = request.get_json(silent=True) or {}
    amount  = int(data.get("amount", 1))
    if user.tokens < amount:
        return jsonify({"error": "Insufficient tokens.", "tokens": user.tokens}), 402
    user.tokens -= amount
    db.session.commit()
    return jsonify({"message": "Tokens deducted.", "tokens": user.tokens}), 200

# ── Send gift ─────────────────────────────────────────────────────────────────

@payments_bp.route("/send-gift", methods=["POST"])
@jwt_required()
def send_gift():
    from models import Gift
    user_id    = int(get_jwt_identity())
    user       = User.query.get_or_404(user_id)
    data       = request.get_json(silent=True) or {}
    gift_type  = data.get("gift_type", "")
    session_id = data.get("session_id")

    if gift_type not in GIFT_CATALOG:
        return jsonify({"error": "Unknown gift type."}), 400

    cost = GIFT_CATALOG[gift_type]["tokens"]
    if user.tokens < cost:
        return jsonify({"error": "Insufficient tokens.", "tokens": user.tokens}), 402

    receiver_id = _other_session_user(session_id, user_id)
    amount_cents = _gift_value_cents(cost)
    user.tokens -= cost
    gift = Gift(
        sender_id=user_id,
        receiver_id=receiver_id,
        session_id=session_id,
        gift_type=gift_type,
        tokens_cost=cost,
        usd_value=amount_cents / 100,
    )
    db.session.add(gift)
    _credit_gift_receiver(receiver_id, amount_cents)
    db.session.commit()
    return jsonify({"message": f"Gift sent!", "gift": gift.to_dict(), "tokens": user.tokens}), 200

# ── Dev only: grant free tokens ───────────────────────────────────────────────

@payments_bp.route("/grant-tokens", methods=["POST"])
@jwt_required()
def grant_tokens():
    if os.getenv("FLASK_ENV") == "production":
        return jsonify({"error": "Not available in production."}), 403
    user_id = int(get_jwt_identity())
    user    = User.query.get_or_404(user_id)
    data    = request.get_json(silent=True) or {}
    amount  = max(1, min(int(data.get("amount", 50)), 1000))
    user.tokens += amount
    purchase = TokenPurchase(
        user_id=user_id, tokens_purchased=amount, bonus_tokens=0,
        amount_cents=0, status="completed", completed_at=utcnow(),
    )
    db.session.add(purchase)
    db.session.commit()
    return jsonify({"message": f"{amount} tokens granted (dev).", "tokens": user.tokens}), 200

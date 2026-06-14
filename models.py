"""
models.py — SQLAlchemy models for Spark
"""

from datetime import datetime, timezone
from app import db


def utcnow():
    return datetime.now(timezone.utc)


# ── User ─────────────────────────────────────────────────────────────────────

class User(db.Model):
    __tablename__ = "users"

    id            = db.Column(db.Integer, primary_key=True)
    email         = db.Column(db.String(255), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    first_name    = db.Column(db.String(100), nullable=False)
    last_name     = db.Column(db.String(100), nullable=True, default="")
    age           = db.Column(db.Integer, nullable=True)
    country       = db.Column(db.String(100), nullable=True)
    gender        = db.Column(db.String(20), nullable=True)
    avatar_url    = db.Column(db.String(255), nullable=True)
    tokens        = db.Column(db.Integer, nullable=False, default=0)
    cash_balance_cents = db.Column(db.Integer, nullable=False, default=0)
    total_earned_cents = db.Column(db.Integer, nullable=False, default=0)
    total_paid_out_cents = db.Column(db.Integer, nullable=False, default=0)
    is_premium    = db.Column(db.Boolean, default=False)
    is_verified   = db.Column(db.Boolean, default=False)
    is_active     = db.Column(db.Boolean, default=True)
    created_at    = db.Column(db.DateTime(timezone=True), default=utcnow)
    last_login    = db.Column(db.DateTime(timezone=True), nullable=True)

    # relationships
    sent_messages    = db.relationship("ChatMessage", foreign_keys="ChatMessage.sender_id",    backref="sender",    lazy="dynamic")
    token_purchases  = db.relationship("TokenPurchase", backref="user", lazy="dynamic")
    gift_sent        = db.relationship("Gift", foreign_keys="Gift.sender_id",   backref="sender",   lazy="dynamic")
    gift_received    = db.relationship("Gift", foreign_keys="Gift.receiver_id", backref="receiver", lazy="dynamic")
    reports_made     = db.relationship("Report", foreign_keys="Report.reporter_id", backref="reporter", lazy="dynamic")

    def to_dict(self, include_private=False):
        data = {
            "id":         self.id,
            "first_name": self.first_name,
            "last_name":  self.last_name,
            "avatar_url": self.avatar_url,
            "country":    self.country,
            "is_premium": self.is_premium,
            "created_at": self.created_at.isoformat(),
        }
        if include_private:
            data.update({
                "email":      self.email,
                "tokens":     self.tokens,
                "cash_balance_cents": self.cash_balance_cents,
                "total_earned_cents": self.total_earned_cents,
                "total_paid_out_cents": self.total_paid_out_cents,
                "is_verified":self.is_verified,
                "age":        self.age,
                "gender":     self.gender,
                "last_login": self.last_login.isoformat() if self.last_login else None,
            })
        return data


# ── Chat Session ─────────────────────────────────────────────────────────────

class EmailVerification(db.Model):
    __tablename__ = "email_verifications"

    id         = db.Column(db.Integer, primary_key=True)
    user_id    = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    code_hash  = db.Column(db.String(255), nullable=False)
    expires_at = db.Column(db.DateTime(timezone=True), nullable=False)
    used_at    = db.Column(db.DateTime(timezone=True), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow)

    user = db.relationship("User", backref="email_verifications")


# ── Chat Session ─────────────────────────────────────────────────────────────

class ChatSession(db.Model):
    __tablename__ = "chat_sessions"

    id           = db.Column(db.Integer, primary_key=True)
    user1_id     = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    user2_id     = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)   # null = stranger/anon
    session_type = db.Column(db.String(20), default="video")  # video | audio
    started_at   = db.Column(db.DateTime(timezone=True), default=utcnow)
    ended_at     = db.Column(db.DateTime(timezone=True), nullable=True)
    tokens_spent = db.Column(db.Integer, default=0)

    messages = db.relationship("ChatMessage", backref="session", lazy="dynamic")

    def duration_seconds(self):
        if self.ended_at:
            return int((self.ended_at - self.started_at).total_seconds())
        return 0


# ── Chat Message ─────────────────────────────────────────────────────────────

class ChatMessage(db.Model):
    __tablename__ = "chat_messages"

    id         = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.Integer, db.ForeignKey("chat_sessions.id"), nullable=False)
    sender_id  = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    content    = db.Column(db.Text, nullable=False)
    msg_type   = db.Column(db.String(20), default="text")  # text | gift | reaction | system
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow)

    def to_dict(self):
        return {
            "id":         self.id,
            "session_id": self.session_id,
            "sender_id":  self.sender_id,
            "content":    self.content,
            "msg_type":   self.msg_type,
            "created_at": self.created_at.isoformat(),
        }


# ── Token Purchase ────────────────────────────────────────────────────────────

class TokenPurchase(db.Model):
    __tablename__ = "token_purchases"

    id                 = db.Column(db.Integer, primary_key=True)
    user_id            = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    stripe_payment_id  = db.Column(db.String(255), nullable=True)
    stripe_session_id  = db.Column(db.String(255), nullable=True)
    tokens_purchased   = db.Column(db.Integer, nullable=False)
    bonus_tokens       = db.Column(db.Integer, default=0)
    amount_cents       = db.Column(db.Integer, nullable=False)   # in cents
    currency           = db.Column(db.String(10), default="usd")
    status             = db.Column(db.String(30), default="pending")  # pending | completed | failed | refunded
    created_at         = db.Column(db.DateTime(timezone=True), default=utcnow)
    completed_at       = db.Column(db.DateTime(timezone=True), nullable=True)

    def to_dict(self):
        return {
            "id":               self.id,
            "tokens_purchased": self.tokens_purchased,
            "bonus_tokens":     self.bonus_tokens,
            "total_tokens":     self.tokens_purchased + self.bonus_tokens,
            "amount_cents":     self.amount_cents,
            "status":           self.status,
            "created_at":       self.created_at.isoformat(),
        }


# ── Gift ─────────────────────────────────────────────────────────────────────

class Gift(db.Model):
    __tablename__ = "gifts"

    id          = db.Column(db.Integer, primary_key=True)
    sender_id   = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    receiver_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)  # null = anon stranger
    session_id  = db.Column(db.Integer, db.ForeignKey("chat_sessions.id"), nullable=True)
    gift_type   = db.Column(db.String(50), nullable=False)   # rose | heart | fire | diamond | crown | rocket | star | trophy
    tokens_cost = db.Column(db.Integer, nullable=False)
    usd_value   = db.Column(db.Float, nullable=False)        # tokens_cost * 0.05
    created_at  = db.Column(db.DateTime(timezone=True), default=utcnow)

    def to_dict(self):
        return {
            "id":          self.id,
            "sender_id":   self.sender_id,
            "receiver_id": self.receiver_id,
            "gift_type":   self.gift_type,
            "tokens_cost": self.tokens_cost,
            "usd_value":   self.usd_value,
            "created_at":  self.created_at.isoformat(),
        }


# ── Withdrawal Request ───────────────────────────────────────────────────────

class WithdrawalRequest(db.Model):
    __tablename__ = "withdrawal_requests"

    id            = db.Column(db.Integer, primary_key=True)
    user_id       = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    amount_cents  = db.Column(db.Integer, nullable=False)
    method        = db.Column(db.String(20), nullable=False)  # paypal | stripe
    destination   = db.Column(db.String(255), nullable=False)
    status        = db.Column(db.String(30), default="pending")  # pending | paid | rejected
    admin_note    = db.Column(db.Text, nullable=True)
    created_at    = db.Column(db.DateTime(timezone=True), default=utcnow)
    processed_at  = db.Column(db.DateTime(timezone=True), nullable=True)

    user = db.relationship("User", backref="withdrawal_requests")

    def to_dict(self):
        return {
            "id": self.id,
            "user_id": self.user_id,
            "amount_cents": self.amount_cents,
            "method": self.method,
            "destination": self.destination,
            "status": self.status,
            "admin_note": self.admin_note,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "processed_at": self.processed_at.isoformat() if self.processed_at else None,
        }


# ── Report ────────────────────────────────────────────────────────────────────

class Report(db.Model):
    __tablename__ = "reports"

    id          = db.Column(db.Integer, primary_key=True)
    reporter_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    reported_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)   # null = anon
    session_id  = db.Column(db.Integer, db.ForeignKey("chat_sessions.id"), nullable=True)
    reason      = db.Column(db.String(100), nullable=False)
    notes       = db.Column(db.Text, nullable=True)
    status      = db.Column(db.String(30), default="pending")   # pending | reviewed | resolved
    created_at  = db.Column(db.DateTime(timezone=True), default=utcnow)


# ── Friendship ────────────────────────────────────────────────────────────────

class Friendship(db.Model):
    __tablename__ = "friendships"

    id          = db.Column(db.Integer, primary_key=True)
    requester_id= db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    receiver_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    status      = db.Column(db.String(20), default="pending")  # pending | accepted | blocked
    created_at  = db.Column(db.DateTime(timezone=True), default=utcnow)
    updated_at  = db.Column(db.DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    requester   = db.relationship("User", foreign_keys=[requester_id], backref="sent_requests")
    receiver    = db.relationship("User", foreign_keys=[receiver_id],  backref="received_requests")

    __table_args__ = (
        db.UniqueConstraint("requester_id", "receiver_id", name="uq_friendship"),
    )

    def to_dict(self, for_user_id=None):
        other = self.receiver if self.requester_id == for_user_id else self.requester
        return {
            "id":         self.id,
            "status":     self.status,
            "friend": {
                "id":         other.id,
                "first_name": other.first_name,
                "last_name":  other.last_name,
                "is_premium": other.is_premium,
            "avatar_url": other.avatar_url,
            },
            "created_at": self.created_at.isoformat(),
        }


# ── Direct Message ────────────────────────────────────────────────────────────

class DirectMessage(db.Model):
    __tablename__ = "direct_messages"

    id          = db.Column(db.Integer, primary_key=True)
    sender_id   = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    receiver_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    content     = db.Column(db.Text, nullable=False)
    msg_type    = db.Column(db.String(20), default="text")   # text | image | gift
    is_read     = db.Column(db.Boolean, default=False)
    created_at  = db.Column(db.DateTime(timezone=True), default=utcnow)

    sender   = db.relationship("User", foreign_keys=[sender_id],   backref="dm_sent")
    receiver = db.relationship("User", foreign_keys=[receiver_id], backref="dm_received")

    def to_dict(self):
        return {
            "id":          self.id,
            "sender_id":   self.sender_id,
            "receiver_id": self.receiver_id,
            "content":     self.content,
            "msg_type":    self.msg_type,
            "is_read":     self.is_read,
            "created_at":  self.created_at.isoformat(),
        }

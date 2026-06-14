"""
routes/auth.py — Registration, Login, JWT, Profile
"""

from datetime import datetime, timezone, timedelta
import ipaddress
import html
import json
import os
import secrets
import smtplib
from email.utils import formataddr
from email.message import EmailMessage
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from flask import Blueprint, request, jsonify, current_app
from flask_jwt_extended import (
    create_access_token, jwt_required, get_jwt_identity
)
from app import db, bcrypt
from models import User, EmailVerification

auth_bp = Blueprint("auth", __name__)


def utcnow():
    return datetime.now(timezone.utc)


def _send_email_resend(to_email, subject, body, html_body=None):
    api_key = os.getenv("RESEND_API_KEY", "").strip()
    if not api_key:
        return False

    sender_name = os.getenv("SMTP_FROM_NAME", "Spark").strip() or "Spark"
    sender = os.getenv("RESEND_FROM", os.getenv("SMTP_FROM", "onboarding@resend.dev")).strip()
    payload = {
        "from": formataddr((sender_name, sender)),
        "to": [to_email],
        "subject": subject,
        "text": body,
    }
    if html_body:
        payload["html"] = html_body

    req = Request(
        "https://api.resend.com/emails",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(req, timeout=12) as resp:
            if resp.status >= 400:
                raise RuntimeError(f"Resend email failed with status {resp.status}")
    except HTTPError as exc:
        details = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Resend email failed with status {exc.code}: {details}") from exc
    current_app.logger.info("Verification email sent to %s via Resend HTTPS", to_email)
    return True


def _send_email(to_email, subject, body, html_body=None):
    provider = os.getenv("EMAIL_PROVIDER", "").strip().lower()
    if provider != "smtp" and os.getenv("RESEND_API_KEY", "").strip():
        return _send_email_resend(to_email, subject, body, html_body)

    host = os.getenv("SMTP_HOST", "").strip()
    port = int(os.getenv("SMTP_PORT", "587"))
    username = os.getenv("SMTP_USERNAME", "").strip()
    password = os.getenv("SMTP_PASSWORD", "").replace(" ", "").strip()
    sender = os.getenv("SMTP_FROM", username or "no-reply@spark.local").strip()
    sender_name = os.getenv("SMTP_FROM_NAME", "Spark").strip() or "Spark"

    if not host:
        current_app.logger.warning("SMTP_HOST not set. Email for %s:\n%s", to_email, body)
        return False

    msg = EmailMessage()
    msg["From"] = formataddr((sender_name, sender))
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.set_content(body)
    if html_body:
        msg.add_alternative(html_body, subtype="html")

    ports = [port]
    if host == "smtp.gmail.com" and port == 587:
        ports.append(465)

    errors = []
    for smtp_port in ports:
        try:
            if smtp_port == 465:
                with smtplib.SMTP_SSL(host, smtp_port, timeout=12) as smtp:
                    if username:
                        smtp.login(username, password)
                    smtp.send_message(msg)
            else:
                with smtplib.SMTP(host, smtp_port, timeout=12) as smtp:
                    smtp.starttls()
                    if username:
                        smtp.login(username, password)
                    smtp.send_message(msg)
            current_app.logger.info("Verification email sent to %s via %s:%s", to_email, host, smtp_port)
            return True
        except OSError as exc:
            errors.append(f"{host}:{smtp_port} {exc}")
            current_app.logger.warning("SMTP attempt failed for %s via %s:%s: %s", to_email, host, smtp_port, exc)

    raise RuntimeError("All SMTP attempts failed: " + " | ".join(errors))


def _send_verification_email(user):
    code = f"{secrets.randbelow(1000000):06d}"
    EmailVerification.query.filter_by(user_id=user.id, used_at=None).update({"used_at": utcnow()})
    verification = EmailVerification(
        user_id=user.id,
        code_hash=bcrypt.generate_password_hash(code).decode("utf-8"),
        expires_at=utcnow() + timedelta(minutes=15),
    )
    db.session.add(verification)
    db.session.commit()

    safe_name = (user.first_name or "there").strip()
    safe_name_html = html.escape(safe_name)
    body = (
        f"Hi {safe_name},\n\n"
        "Your Spark verification code is:\n\n"
        f"{code}\n\n"
        "Enter this code in Spark to verify your email. It expires in 15 minutes.\n"
    )
    html_body = f"""\
<!doctype html>
<html>
  <body style="margin:0;padding:0;background:#0b0a10;color:#f0ecff;font-family:Arial,Helvetica,sans-serif;">
    <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#0b0a10;background-image:radial-gradient(circle at 18% 8%,rgba(255,78,106,.28),transparent 30%),radial-gradient(circle at 88% 18%,rgba(199,125,255,.22),transparent 34%),linear-gradient(135deg,#0b0a10 0%,#160d1f 48%,#0d1020 100%);padding:36px 14px;">
      <tr>
        <td align="center">
          <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="max-width:560px;">
            <tr>
              <td align="center" style="padding:0 8px 18px;">
                <div style="font-family:Georgia,'Times New Roman',serif;font-size:38px;font-weight:900;line-height:1;color:#ff4e6a;text-shadow:0 0 24px rgba(255,78,106,.38);">Spark</div>
                <div style="margin-top:10px;color:#c9c3e8;font-size:14px;letter-spacing:.2px;">One click away from something real.</div>
              </td>
            </tr>
            <tr>
              <td style="background:#171625;border:1px solid rgba(255,255,255,.10);border-radius:22px;overflow:hidden;box-shadow:0 28px 80px rgba(0,0,0,.55);">
                <table role="presentation" width="100%" cellspacing="0" cellpadding="0">
                  <tr>
                    <td style="padding:34px 30px 22px;background:#171625;background-image:radial-gradient(circle at 20% 20%,rgba(255,78,106,.20),transparent 28%),radial-gradient(circle at 85% 5%,rgba(199,125,255,.16),transparent 32%),linear-gradient(135deg,#181523 0%,#201329 58%,#121421 100%);">
                      <div style="display:inline-block;padding:7px 12px;border:1px solid rgba(255,78,106,.28);border-radius:999px;background:rgba(255,78,106,.10);color:#ff9aaa;font-size:11px;font-weight:700;letter-spacing:1.1px;text-transform:uppercase;">Email verification</div>
                      <h1 style="margin:18px 0 10px;font-family:Georgia,'Times New Roman',serif;font-size:32px;line-height:1.15;color:#f0ecff;font-weight:700;">Your Spark code is ready</h1>
                      <p style="margin:0;color:#b8b1d8;font-size:15px;line-height:1.7;">Hi {safe_name_html}, use this OTP to verify your account and start meeting people worldwide.</p>
                    </td>
                  </tr>
                  <tr>
                    <td style="padding:26px 30px 8px;background:#171625;">
                      <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#0f0e18;border:1px solid rgba(255,78,106,.38);border-radius:18px;box-shadow:0 0 36px rgba(255,78,106,.12);">
                        <tr>
                          <td align="center" style="padding:26px 18px 8px;">
                            <div style="color:#8581aa;font-size:11px;text-transform:uppercase;letter-spacing:1.4px;font-weight:700;">One-time password</div>
                          </td>
                        </tr>
                        <tr>
                          <td align="center" style="padding:0 18px 26px;">
                            <div style="display:inline-block;background:linear-gradient(135deg,#ff4e6a,#ff8c42);border-radius:14px;padding:3px;">
                              <div style="background:#12101b;border-radius:12px;padding:16px 18px;">
                                <span style="font-family:'Courier New',Courier,monospace;font-size:40px;line-height:1;font-weight:800;letter-spacing:8px;color:#ffffff;text-shadow:0 0 18px rgba(255,78,106,.35);">{code}</span>
                              </div>
                            </div>
                          </td>
                        </tr>
                      </table>
                    </td>
                  </tr>
                  <tr>
                    <td style="padding:18px 30px 30px;background:#171625;">
                      <table role="presentation" width="100%" cellspacing="0" cellpadding="0">
                        <tr>
                          <td style="padding:14px 16px;background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.08);border-radius:14px;color:#8581aa;font-size:13px;line-height:1.65;">
                            This code expires in <strong style="color:#f0ecff;">15 minutes</strong>. Spark will never ask you to share this code with another person.
                          </td>
                        </tr>
                      </table>
                    </td>
                  </tr>
                  <tr>
                    <td style="padding:18px 30px;background:#11101b;border-top:1px solid rgba(255,255,255,.08);">
                      <table role="presentation" width="100%" cellspacing="0" cellpadding="0">
                        <tr>
                          <td style="color:#6f6a91;font-size:12px;line-height:1.6;">If you did not create a Spark account, you can ignore this email.</td>
                          <td align="right" style="color:#ff4e6a;font-family:Georgia,'Times New Roman',serif;font-size:18px;font-weight:700;">Spark</td>
                        </tr>
                      </table>
                    </td>
                  </tr>
                </table>
              </td>
            </tr>
          </table>
        </td>
      </tr>
    </table>
  </body>
</html>
"""
    try:
        sent = _send_email(user.email, "Your Spark verification code", body, html_body)
    except Exception as exc:
        current_app.logger.exception("Failed to send verification OTP to %s: %s", user.email, exc)
        if os.getenv("FLASK_ENV") != "production":
            current_app.logger.warning("Verification OTP for %s: %s", user.email, code)
        sent = False
    return sent, code


def _client_ip():
    forwarded_for = request.headers.get("X-Forwarded-For", "")
    if forwarded_for:
        return forwarded_for.split(",", 1)[0].strip()
    return request.headers.get("X-Real-IP") or request.remote_addr


def _is_public_ip(ip):
    try:
        parsed = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return not (
        parsed.is_private
        or parsed.is_loopback
        or parsed.is_link_local
        or parsed.is_multicast
        or parsed.is_reserved
        or parsed.is_unspecified
    )


def _geo_lookup_urls(ip=None):
    if ip:
        return [
            f"https://ipwho.is/{ip}?fields=success,country",
            f"https://ipapi.co/{ip}/json/",
            f"http://ip-api.com/json/{ip}?fields=status,country,query",
        ]
    return [
        "https://ipwho.is/?fields=success,country",
        "https://ipapi.co/json/",
        "http://ip-api.com/json?fields=status,country,query",
    ]


def _country_from_geo_payload(data):
    if data.get("success") is False:
        return None
    if data.get("status") and data.get("status") != "success":
        return None
    return (data.get("country") or data.get("country_name") or "").strip() or None


def _lookup_country(ip=None):
    label = ip or "current public IP"
    for url in _geo_lookup_urls(ip):
        try:
            with urlopen(url, timeout=3) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except (OSError, URLError, TimeoutError, json.JSONDecodeError) as exc:
            current_app.logger.info("Country lookup failed for %s via %s: %s", label, url, exc)
            continue

        country = _country_from_geo_payload(data)
        if country:
            return country
    return None


def country_from_request_ip():
    ip = _client_ip()
    if ip and _is_public_ip(ip):
        return _lookup_country(ip)

    # Localhost, Wi-Fi hotspot, and LAN addresses cannot be geolocated directly.
    # In development, fall back to the machine's public outbound IP.
    return _lookup_country()


# ── Register ─────────────────────────────────────────────────────────────────

@auth_bp.route("/register", methods=["POST"])
def register():
    data = request.get_json(silent=True) or {}

    email      = (data.get("email") or "").strip().lower()
    password   = data.get("password") or ""
    first_name = (data.get("first_name") or data.get("name") or "").strip()
    last_name  = (data.get("last_name") or "").strip()
    age        = data.get("age")
    country    = country_from_request_ip()
    gender     = (data.get("gender") or "").strip()

    # ── Validation ────────────────────────────────────────────────────────────
    errors = {}
    if not email or "@" not in email:
        errors["email"] = "A valid email is required."
    if len(password) < 8:
        errors["password"] = "Password must be at least 8 characters."
    if not first_name:
        errors["first_name"] = "First name is required."
    if not age or not str(age).isdigit() or int(age) < 18:
        errors["age"] = "You must be at least 18 years old."
    if not gender:
        errors["gender"] = "Sex is required."
    if errors:
        return jsonify({"error": "Validation failed", "fields": errors}), 422

    if User.query.filter_by(email=email).first():
        return jsonify({"error": "An account with that email already exists."}), 409

    # ── Create user ───────────────────────────────────────────────────────────
    pw_hash = bcrypt.generate_password_hash(password).decode("utf-8")
    user = User(
        email=email,
        password_hash=pw_hash,
        first_name=first_name,
        last_name=last_name,
        age=int(age),
        country=country,
        gender=gender,
        tokens=0,
        last_login=utcnow(),
    )
    db.session.add(user)
    db.session.commit()
    sent, dev_code = _send_verification_email(user)

    return jsonify({
        "message": "Account created! Please enter the OTP sent to your email before signing in.",
        "email_sent": sent,
        "user":    user.to_dict(include_private=True),
    }), 201


# ── Login ─────────────────────────────────────────────────────────────────────

@auth_bp.route("/login", methods=["POST"])
def login():
    data = request.get_json(silent=True) or {}

    email    = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""

    if not email or not password:
        return jsonify({"error": "Email and password are required."}), 400

    user = User.query.filter_by(email=email).first()
    if not user or not bcrypt.check_password_hash(user.password_hash, password):
        return jsonify({"error": "Invalid email or password."}), 401

    if not user.is_active:
        return jsonify({"error": "This account has been suspended."}), 403
    if not user.is_verified:
        return jsonify({
            "error": "Please verify your email before signing in.",
            "needs_verification": True,
            "email": user.email,
        }), 403

    user.last_login = utcnow()
    if not user.country:
        user.country = country_from_request_ip()
    db.session.commit()

    token = create_access_token(identity=str(user.id))
    return jsonify({
        "message": "Welcome back!",
        "token":   token,
        "user":    user.to_dict(include_private=True),
    }), 200


# ── Email verification ───────────────────────────────────────────────────────

@auth_bp.route("/verify-email", methods=["POST"])
def verify_email():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    code = (data.get("code") or "").strip()
    if not email or not code:
        return jsonify({"error": "Email and OTP code are required."}), 400
    if not code.isdigit() or len(code) != 6:
        return jsonify({"error": "OTP code must be 6 digits."}), 400

    user = User.query.filter_by(email=email).first()
    if not user:
        return jsonify({"error": "Account not found."}), 404
    if user.is_verified:
        return jsonify({"message": "Email is already verified."}), 200

    verification = EmailVerification.query.filter_by(user_id=user.id, used_at=None).order_by(
        EmailVerification.created_at.desc()
    ).first()
    if not verification:
        return jsonify({"error": "No active OTP found. Please request a new code."}), 400
    expires_at = verification.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at < utcnow():
        verification.used_at = utcnow()
        db.session.commit()
        return jsonify({"error": "OTP expired. Please request a new code."}), 400
    if not bcrypt.check_password_hash(verification.code_hash, code):
        return jsonify({"error": "Invalid OTP code."}), 400

    user.is_verified = True
    verification.used_at = utcnow()
    db.session.commit()
    return jsonify({"message": "Email verified. You can sign in now."}), 200


@auth_bp.route("/resend-verification", methods=["POST"])
def resend_verification():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    if not email:
        return jsonify({"error": "Email is required."}), 400

    user = User.query.filter_by(email=email).first()
    if not user:
        return jsonify({"message": "If that account exists, a verification email has been sent."}), 200
    if user.is_verified:
        return jsonify({"message": "Email is already verified."}), 200

    sent, dev_code = _send_verification_email(user)
    return jsonify({
        "message": "Verification OTP sent.",
        "email_sent": sent,
    }), 200


# ── Get current user profile ──────────────────────────────────────────────────

@auth_bp.route("/me", methods=["GET"])
@jwt_required()
def me():
    user_id = int(get_jwt_identity())
    user = User.query.get_or_404(user_id)
    if not user.country:
        user.country = country_from_request_ip()
        db.session.commit()
    return jsonify({"user": user.to_dict(include_private=True)}), 200


# ── Update profile ────────────────────────────────────────────────────────────

@auth_bp.route("/me", methods=["PATCH"])
@jwt_required()
def update_profile():
    user_id = int(get_jwt_identity())
    user = User.query.get_or_404(user_id)
    data = request.get_json(silent=True) or {}

    if "first_name" in data and data["first_name"].strip():
        user.first_name = data["first_name"].strip()
    if "last_name" in data:
        user.last_name = data["last_name"].strip()

    if "avatar_base64" in data and data["avatar_base64"]:
        try:
            import base64, os, time
            header, encoded = data["avatar_base64"].split(",", 1)
            ext = "jpg"
            if "png" in header: ext = "png"
            elif "gif" in header: ext = "gif"
            
            avatars_dir = os.path.join(current_app.static_folder, "avatars")
            os.makedirs(avatars_dir, exist_ok=True)
            filename = f"avatar_{user_id}_{int(time.time())}.{ext}"
            filepath = os.path.join(avatars_dir, filename)
            
            with open(filepath, "wb") as f:
                f.write(base64.b64decode(encoded))
            
            user.avatar_url = f"/static/avatars/{filename}"
        except Exception as e:
            return jsonify({"error": f"Failed to process image: {str(e)}"}), 400

    db.session.commit()
    return jsonify({"message": "Profile updated.", "user": user.to_dict(include_private=True)}), 200


# ── Change password ───────────────────────────────────────────────────────────

@auth_bp.route("/change-password", methods=["POST"])
@jwt_required()
def change_password():
    user_id = int(get_jwt_identity())
    user = User.query.get_or_404(user_id)
    data = request.get_json(silent=True) or {}

    current = data.get("current_password") or ""
    new_pw  = data.get("new_password") or ""

    if not bcrypt.check_password_hash(user.password_hash, current):
        return jsonify({"error": "Current password is incorrect."}), 401
    if len(new_pw) < 8:
        return jsonify({"error": "New password must be at least 8 characters."}), 422

    user.password_hash = bcrypt.generate_password_hash(new_pw).decode("utf-8")
    db.session.commit()
    return jsonify({"message": "Password changed successfully."}), 200


# ── Token balance ─────────────────────────────────────────────────────────────

@auth_bp.route("/tokens", methods=["GET"])
@jwt_required()
def token_balance():
    user_id = int(get_jwt_identity())
    user = User.query.get_or_404(user_id)
    return jsonify({"tokens": user.tokens}), 200

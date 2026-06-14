"""
sockets/events.py
Threading-mode Socket.IO — auth via first event, not connect handshake.
"""

import uuid
import threading
from flask import request
from flask_jwt_extended import decode_token
from flask_socketio import emit
from app import db, socketio
from models import User, ChatSession, ChatMessage
from datetime import datetime, timezone

# ── Shared state (all access guarded by _lock) ───────────────────────────────
_lock         = threading.Lock()
waiting_video = []          # [[user_id, sid, filters, filter_cost], ...]
active_rooms  = {}          # room_id -> user ids, socket ids, session_id, type
user_rooms    = {}          # user_id -> room_id
sid_rooms     = {}          # sid     -> room_id
sid_to_user   = {}          # sid     -> user_id


def utcnow():
    return datetime.now(timezone.utc)


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


def _user_from_token(token):
    try:
        data = decode_token(token)
        return User.query.get(int(data["sub"]))
    except Exception:
        return None


def _make_room():
    return f"room_{uuid.uuid4().hex[:12]}"


def _sid_for(user_id):
    """Return socket-id for a user_id, or None."""
    with _lock:
        for s, u in sid_to_user.items():
            if u == user_id:
                return s
    return None


def _premium_filter_cost(filters):
    filters = filters if isinstance(filters, dict) else {}
    cost = 0
    if filters.get("gender", "any") != "any":
        cost += 2
    if filters.get("age", "any") != "any":
        cost += 2
    if filters.get("region", "any") != "any":
        cost += 3
    if filters.get("lang", "any") != "any":
        cost += 2
    if filters.get("mode", "any") != "any":
        cost += 5
    return cost


def _normalise_gender(value):
    value = (value or "").strip().lower()
    aliases = {
        "m": "male",
        "man": "male",
        "men": "male",
        "boy": "male",
        "f": "female",
        "woman": "female",
        "women": "female",
        "girl": "female",
        "non-binary": "nonbinary",
        "non binary": "nonbinary",
        "nb": "nonbinary",
    }
    return aliases.get(value, value)


def _normalise_filters(filters):
    filters = filters if isinstance(filters, dict) else {}
    return {
        "gender": _normalise_gender(filters.get("gender") or "any") or "any",
        "age": (filters.get("age") or "any").strip().lower(),
        "region": (filters.get("region") or "any").strip().lower(),
        "lang": (filters.get("lang") or "any").strip().lower(),
        "mode": (filters.get("mode") or "any").strip().lower(),
        "interests": filters.get("interests") if isinstance(filters.get("interests"), list) else [],
    }


def _user_matches_filters(user, filters):
    if not user:
        return False

    filters = _normalise_filters(filters)
    wanted_gender = filters.get("gender", "any")
    if wanted_gender != "any" and _normalise_gender(user.gender) != wanted_gender:
        return False

    wanted_age = filters.get("age", "any")
    if wanted_age != "any":
        if not user.age:
            return False
        if wanted_age == "18-24" and not (18 <= user.age <= 24):
            return False
        if wanted_age == "25-34" and not (25 <= user.age <= 34):
            return False
        if wanted_age == "35-44" and not (35 <= user.age <= 44):
            return False
        if wanted_age == "45+" and user.age < 45:
            return False

    wanted_mode = filters.get("mode", "any")
    if wanted_mode == "verified" and not user.is_verified:
        return False
    if wanted_mode == "premium" and not user.is_premium:
        return False
    if wanted_mode == "new":
        created_at = user.created_at
        if not created_at:
            return False
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        if (utcnow() - created_at).days > 7:
            return False

    return True


def _users_are_match_compatible(user, user_filters, candidate, candidate_filters):
    return (
        _user_matches_filters(candidate, user_filters)
        and _user_matches_filters(user, candidate_filters)
    )


# ─────────────────────────────────────────────────────────────────────────────
# CONNECT / DISCONNECT  (no auth here — auth happens in "authenticate" event)
# ─────────────────────────────────────────────────────────────────────────────

@socketio.on("connect")
def on_connect():
    sid = request.sid
    with _lock:
        sid_to_user[sid] = None          # placeholder — replaced in authenticate
    print(f"[Socket] connect  sid={sid[:8]}")


@socketio.on("disconnect")
def on_disconnect():
    sid = request.sid
    with _lock:
        user_id = sid_to_user.pop(sid, None)
        for q in (waiting_video,):
            for e in [x for x in q if x[1] == sid]:
                q.remove(e)
        room_id = sid_rooms.pop(sid, None)
        if not room_id and user_id:
            room_id = user_rooms.pop(user_id, None)
        room    = active_rooms.pop(room_id, None) if room_id else None
        if room:
            partner_id = room["user2_id"] if room["user1_id"] == user_id else room["user1_id"]
            partner_sid = room["user2_sid"] if room["user1_sid"] == sid else room["user1_sid"]
            user_rooms.pop(partner_id, None)
            sid_rooms.pop(partner_sid, None)

    if room:
        partner_id  = room["user2_id"] if room["user1_id"] == user_id else room["user1_id"]
        partner_sid = room["user2_sid"] if room["user1_sid"] == sid else room["user1_sid"]
        if partner_sid not in sid_to_user:
            partner_sid = _sid_for(partner_id)
        if partner_sid:
            socketio.emit("partner_disconnected",
                          {"message": "Your partner left."}, to=partner_sid)
        _end_session(room.get("session_id"))
        print(f"[Socket] disconnect uid={user_id} ended room={room_id}")
    else:
        print(f"[Socket] disconnect sid={sid[:8]} uid={user_id}")


# ─────────────────────────────────────────────────────────────────────────────
# AUTHENTICATE  — client sends token right after connect
# ─────────────────────────────────────────────────────────────────────────────

@socketio.on("authenticate")
def on_authenticate(data):
    """
    Client emits: socket.emit('authenticate', { token: '...' })
    Server replies: 'connected' event with authenticated:true/false
    """
    sid   = request.sid
    token = (data or {}).get("token", "")

    print(f"[Auth] authenticate sid={sid[:8]} token={'yes' if token else 'MISSING'}")

    user = _user_from_token(token) if token else None

    if user:
        with _lock:
            sid_to_user[sid] = user.id
        emit("connected", {
            "authenticated": True,
            "user_id":       user.id,
            "name":          user.first_name,
            "tokens":        user.tokens,
        })
        print(f"[Auth] ✅ uid={user.id} name={user.first_name}")
    else:
        emit("connected", {"authenticated": False,
                           "message": "Invalid or missing token."})
        print(f"[Auth] ❌ bad token")


# ─────────────────────────────────────────────────────────────────────────────
# FIND MATCH
# ─────────────────────────────────────────────────────────────────────────────

@socketio.on("find_match")
def on_find_match(data):
    sid = request.sid

    with _lock:
        user_id = sid_to_user.get(sid)

    if not user_id:
        emit("error", {"message": "Not authenticated — please reload and log in."})
        print(f"[Match] ❌ not authenticated sid={sid[:8]}")
        return

    user = User.query.get(user_id)
    if not user:
        emit("error", {"message": "User not found."})
        return

    chat_type = data.get("type", "video")
    filters   = _normalise_filters(data.get("filters"))
    filter_cost = _premium_filter_cost(filters)
    queue     = waiting_video

    if user.tokens < filter_cost:
        emit("error", {"message": "Not enough tokens for those premium filters."})
        return

    # Leave any current room first
    _leave_room(user_id, sid)

    partner_entry = None
    partner_filter_cost = 0

    with _lock:
        # Purge stale (disconnected) entries
        stale = [e for e in queue if e[1] not in sid_to_user]
        for e in stale:
            queue.remove(e)
        if stale:
            print(f"[Match] purged {len(stale)} stale entries")

        print(f"[Match] uid={user_id} filters={filters} queue={[(e[0]) for e in queue]}")

        # Find partner
        for entry in list(queue):
            cuid, csid = entry[0], entry[1]
            if cuid != user_id and csid in sid_to_user:
                candidate = User.query.get(cuid)
                candidate_filters = _normalise_filters(entry[2] if len(entry) > 2 else {})
                candidate_cost = entry[3] if len(entry) > 3 else 0
                if not candidate:
                    queue.remove(entry)
                    continue
                if not _users_are_match_compatible(user, filters, candidate, candidate_filters):
                    continue
                if candidate and candidate.tokens < candidate_cost:
                    queue.remove(entry)
                    socketio.emit("error", {"message": "Not enough tokens for those premium filters."}, to=csid)
                    continue
                partner_entry = (cuid, csid)
                partner_filter_cost = candidate_cost
                queue.remove(entry)
                break

        if not partner_entry:
            # Remove any old entry for this user then add fresh
            for e in [x for x in queue if x[0] == user_id]:
                queue.remove(e)
            queue.append([user_id, sid, filters, filter_cost])
            print(f"[Match] uid={user_id} queued with filters={filters}. queue size={len(queue)}")

    if partner_entry:
        partner_id, partner_sid = partner_entry
        partner = User.query.get(partner_id)

        user.tokens -= filter_cost
        if partner:
            partner.tokens -= partner_filter_cost
        try:    db.session.commit()
        except: db.session.rollback()

        session_id = None
        try:
            db_sess = ChatSession(user1_id=user_id, user2_id=partner_id,
                                  session_type=chat_type,
                                  tokens_spent=filter_cost + partner_filter_cost)
            db.session.add(db_sess)
            db.session.commit()
            session_id = db_sess.id
        except: db.session.rollback()

        room_id = _make_room()
        with _lock:
            active_rooms[room_id] = {"user1_id": user_id, "user2_id": partner_id,
                                     "user1_sid": sid, "user2_sid": partner_sid,
                                     "session_id": session_id, "type": chat_type}
            user_rooms[user_id]    = room_id
            user_rooms[partner_id] = room_id
            sid_rooms[sid]         = room_id
            sid_rooms[partner_sid] = room_id

        print(f"[Match] ✅ PAIRED uid={user_id}(init) <-> uid={partner_id} room={room_id} cost={filter_cost}+{partner_filter_cost}")

        # Notify both directly by sid
        emit("match_found", {
            "room_id": room_id, "session_id": session_id, "is_initiator": True,
            "partner": {"name": partner.first_name if partner else "Stranger",
                        "location": f"🌍 {partner.country}" if partner and partner.country else "🌍 Somewhere",
                        "is_premium": partner.is_premium if partner else False,
                        "avatar_url": partner.avatar_url if partner else None},
            "tokens": user.tokens,
        })

        socketio.emit("match_found", {
            "room_id": room_id, "session_id": session_id, "is_initiator": False,
            "partner": {"name": user.first_name, "location": f"🌍 {user.country}" if user.country else "🌍 Somewhere",
                        "is_premium": user.is_premium,
                        "avatar_url": user.avatar_url},
            "tokens": partner.tokens if partner else 0,
        }, to=partner_sid)

    else:
        if filter_cost:
            emit("searching", {"message": "Searching for someone who matches your filters…"})
        else:
            emit("searching", {"message": "Searching for someone…"})


# ─────────────────────────────────────────────────────────────────────────────
# CANCEL SEARCH
# ─────────────────────────────────────────────────────────────────────────────

@socketio.on("cancel_search")
def on_cancel_search():
    sid = request.sid
    with _lock:
        user_id = sid_to_user.get(sid)
        for q in (waiting_video,):
            for e in [x for x in q if x[1] == sid]:
                q.remove(e)
    print(f"[Match] cancel uid={user_id}")
    emit("search_cancelled", {"message": "Search cancelled."})


# ─────────────────────────────────────────────────────────────────────────────
# CHAT
# ─────────────────────────────────────────────────────────────────────────────

@socketio.on("send_message")
def on_send_message(data):
    sid = request.sid
    with _lock:
        user_id = sid_to_user.get(sid)
        room_id = sid_rooms.get(sid) or (user_rooms.get(user_id) if user_id else None)
        room    = active_rooms.get(room_id, {}) if room_id else {}
    if not room_id: return

    msg_type = data.get("type", "text")
    content = (data.get("content") or "").strip()
    if msg_type not in ("text", "image"):
        emit("error", {"message": "Invalid message type."})
        return
    if not content: return
    if msg_type == "image" and not content.startswith("/static/chat_uploads/"):
        emit("error", {"message": "Invalid photo."})
        return

    msg_id = None
    session_id = room.get("session_id")
    if session_id:
        try:
            msg = ChatMessage(session_id=session_id, sender_id=user_id,
                              content=content, msg_type=msg_type)
            db.session.add(msg); db.session.commit()
            msg_id = msg.id
        except: db.session.rollback()

    partner_id  = room["user2_id"] if room.get("user1_sid") == sid else room.get("user1_id")
    partner_sid = room.get("user2_sid") if room.get("user1_sid") == sid else room.get("user1_sid")
    if partner_sid not in sid_to_user:
        partner_sid = _sid_for(partner_id) if partner_id else None
    payload = {"id": msg_id, "sender_id": user_id, "content": content,
               "type": msg_type, "timestamp": utcnow().isoformat()}
    emit("new_message", payload)
    if partner_sid:
        socketio.emit("new_message", payload, to=partner_sid)


# ─────────────────────────────────────────────────────────────────────────────
# REACTION
# ─────────────────────────────────────────────────────────────────────────────

@socketio.on("send_reaction")
def on_send_reaction(data):
    sid = request.sid
    with _lock:
        user_id = sid_to_user.get(sid)
        room_id = sid_rooms.get(sid) or (user_rooms.get(user_id) if user_id else None)
        room    = active_rooms.get(room_id, {}) if room_id else {}
    if not room_id: return
    partner_id  = room["user2_id"] if room.get("user1_sid") == sid else room.get("user1_id")
    partner_sid = room.get("user2_sid") if room.get("user1_sid") == sid else room.get("user1_sid")
    if partner_sid not in sid_to_user:
        partner_sid = _sid_for(partner_id) if partner_id else None
    if partner_sid:
        socketio.emit("reaction_received",
                      {"sender_id": user_id, "reaction": data.get("reaction", "👋")},
                      to=partner_sid)


# ─────────────────────────────────────────────────────────────────────────────
# GIFT
# ─────────────────────────────────────────────────────────────────────────────

@socketio.on("send_gift")
def on_send_gift(data):
    from models import Gift
    sid = request.sid
    with _lock:
        user_id = sid_to_user.get(sid)
        room_id = sid_rooms.get(sid) or (user_rooms.get(user_id) if user_id else None)
        room    = active_rooms.get(room_id, {}) if room_id else {}
    if not room_id:
        emit("error", {"message": "You must be in a call to send a gift."}); return

    COSTS = {"rose":5,"heart":10,"fire":20,"diamond":50,
             "crown":100,"rocket":150,"star":30,"trophy":200}
    gift_type = data.get("gift_type","")
    cost      = COSTS.get(gift_type)
    if not cost:
        emit("error", {"message": "Invalid gift type."}); return

    user = User.query.get(user_id)
    if not user or user.tokens < cost:
        emit("error", {"message": "Not enough tokens."}); return

    partner_id = room["user2_id"] if room["user1_sid"] == sid else room["user1_id"]
    amount_cents = _gift_value_cents(cost)
    user.tokens -= cost
    try:
        gift = Gift(sender_id=user_id, receiver_id=partner_id,
                    session_id=room.get("session_id"), gift_type=gift_type,
                    tokens_cost=cost, usd_value=amount_cents / 100)
        db.session.add(gift)
        _credit_gift_receiver(partner_id, amount_cents)
        db.session.commit()
    except: db.session.rollback()

    partner_sid = room["user2_sid"] if room["user1_sid"] == sid else room["user1_sid"]
    if partner_sid not in sid_to_user:
        partner_sid = _sid_for(partner_id)
    if partner_sid:
        socketio.emit("gift_received",
                      {"sender_id":user_id,"gift_type":gift_type,
                       "tokens_cost":cost,"usd_value":amount_cents / 100},
                      to=partner_sid)
    emit("tokens_updated", {"tokens": user.tokens})


# ─────────────────────────────────────────────────────────────────────────────
# SKIP / LEAVE
# ─────────────────────────────────────────────────────────────────────────────

@socketio.on("skip")
def on_skip():
    sid = request.sid
    with _lock:
        user_id = sid_to_user.get(sid)
    _leave_room(user_id, sid)
    emit("skipped", {"message": "Finding next match…"})


@socketio.on("leave_room")
def on_leave_room_event():
    sid = request.sid
    with _lock:
        user_id = sid_to_user.get(sid)
    _leave_room(user_id, sid)
    emit("left_room", {"message": "You left the room."})


# ─────────────────────────────────────────────────────────────────────────────
# WEBRTC RELAY
# ─────────────────────────────────────────────────────────────────────────────

@socketio.on("webrtc_offer")
def on_webrtc_offer(data):     _relay("webrtc_offer", data)

@socketio.on("webrtc_answer")
def on_webrtc_answer(data):    _relay("webrtc_answer", data)

@socketio.on("webrtc_ice_candidate")
def on_ice(data):              _relay("webrtc_ice_candidate", data)


def _relay(event, data):
    sid = request.sid
    with _lock:
        user_id = sid_to_user.get(sid)
        room_id = sid_rooms.get(sid) or (user_rooms.get(user_id) if user_id else None)
        room    = active_rooms.get(room_id, {}) if room_id else {}
    if not room_id:
        print(f"[Relay] {event}: uid={user_id} not in room — dropped"); return
    partner_id  = room["user2_id"] if room["user1_sid"] == sid else room["user1_id"]
    partner_sid = room["user2_sid"] if room["user1_sid"] == sid else room["user1_sid"]
    if partner_sid not in sid_to_user:
        partner_sid = _sid_for(partner_id)
    if not partner_sid:
        print(f"[Relay] {event}: partner not connected — dropped"); return
    print(f"[Relay] {event}: uid={user_id} → uid={partner_id}")
    socketio.emit(event, data, to=partner_sid)


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _leave_room(user_id, sid):
    if not user_id: return
    with _lock:
        room_id = sid_rooms.pop(sid, None) or user_rooms.pop(user_id, None)
        room    = active_rooms.pop(room_id, None) if room_id else None
        if room:
            partner_id = room["user2_id"] if room["user1_sid"] == sid else room["user1_id"]
            partner_sid = room["user2_sid"] if room["user1_sid"] == sid else room["user1_sid"]
            user_rooms.pop(partner_id, None)
            sid_rooms.pop(partner_sid, None)
    if room:
        partner_id  = room["user2_id"] if room["user1_sid"] == sid else room["user1_id"]
        partner_sid = room["user2_sid"] if room["user1_sid"] == sid else room["user1_sid"]
        if partner_sid not in sid_to_user:
            partner_sid = _sid_for(partner_id)
        if partner_sid:
            socketio.emit("partner_disconnected",
                          {"message": "Your partner left."}, to=partner_sid)
        _end_session(room.get("session_id"))


def _end_session(session_id):
    if not session_id: return
    try:
        s = ChatSession.query.get(session_id)
        if s and not s.ended_at:
            s.ended_at = utcnow()
            db.session.commit()
    except: db.session.rollback()


# ─────────────────────────────────────────────────────────────────────────────
# DIRECT MESSAGES
# ─────────────────────────────────────────────────────────────────────────────

@socketio.on("dm_send")
def on_dm_send(data):
    from models import DirectMessage, Friendship
    from sqlalchemy import or_, and_
    sid = request.sid
    with _lock:
        user_id = sid_to_user.get(sid)
    if not user_id:
        emit("error", {"message": "Not authenticated."}); return
    other_id = int(data.get("to_user_id", 0))
    content  = (data.get("content") or "").strip()
    msg_type = data.get("type", "text")
    if msg_type not in ("text", "image"):
        emit("error", {"message": "Invalid message type."}); return
    if not content or not other_id:
        emit("error", {"message": "Invalid message."}); return
    if msg_type == "image" and not content.startswith("/static/dm_uploads/"):
        emit("error", {"message": "Invalid photo."}); return
    f = Friendship.query.filter(
        Friendship.status == "accepted",
        or_(and_(Friendship.requester_id==user_id, Friendship.receiver_id==other_id),
            and_(Friendship.requester_id==other_id, Friendship.receiver_id==user_id))
    ).first()
    if not f:
        emit("error", {"message": "You can only message friends."}); return
    try:
        msg = DirectMessage(sender_id=user_id, receiver_id=other_id,
                            content=content, msg_type=msg_type)
        db.session.add(msg); db.session.commit()
    except: db.session.rollback(); return
    payload = msg.to_dict()
    recip_sid = _sid_for(other_id)
    if recip_sid:
        socketio.emit("dm_received", payload, to=recip_sid)
    emit("dm_sent", payload)


@socketio.on("dm_typing")
def on_dm_typing(data):
    sid = request.sid
    with _lock:
        user_id = sid_to_user.get(sid)
    if not user_id: return
    rsid = _sid_for(int(data.get("to_user_id", 0)))
    if rsid:
        socketio.emit("dm_typing", {"from_user_id": user_id}, to=rsid)


@socketio.on("friend_online_check")
def on_friend_online_check(data):
    ids    = data.get("friend_ids", [])
    online = [uid for uid in ids if _sid_for(uid)]
    emit("friends_online", {"online_ids": online})

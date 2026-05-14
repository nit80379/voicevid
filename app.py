"""
VoiceVid — Pure Python Single-File Re-creation
Voice & Video calling app using Flask + SocketIO + WebRTC signaling

Local:  python app.py
Render: gunicorn --worker-class eventlet -w 1 --bind 0.0.0.0:$PORT app:app
"""
# ── MONKEY PATCH MUST BE FIRST — before ANY other import ──────────────────────
# eventlet.monkey_patch() must run before os, threading, flask, etc.
# This is the fix for "monkey_patch called too late" errors on Render.
import sys

def _get_async_mode():
    try:
        import eventlet  # noqa
        return 'eventlet'
    except ImportError:
        pass
    try:
        import gevent  # noqa
        return 'gevent'
    except ImportError:
        pass
    return 'threading'

_ASYNC_MODE = _get_async_mode()

if _ASYNC_MODE == 'eventlet':
    import eventlet
    eventlet.monkey_patch()
elif _ASYNC_MODE == 'gevent':
    from gevent import monkey as _gmonkey
    _gmonkey.patch_all()

# ── Now safe to import everything else ────────────────────────────────────────
import os
import re
import uuid
import bcrypt
import threading
from datetime import datetime, timezone, timedelta

from flask import Flask, request, jsonify, session, redirect, url_for, render_template_string
from flask_socketio import SocketIO, emit, join_room, leave_room
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user

# ── App setup ──────────────────────────────────────────────────────────────────
app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-secret-voicevid-2024')

# DATABASE — use DATABASE_URL env var (PostgreSQL on Render) or fall back to SQLite locally
_db_url = os.environ.get('DATABASE_URL', 'sqlite:///voicevid.db')
# Render uses postgres:// but SQLAlchemy 1.4+ requires postgresql://
if _db_url.startswith('postgres://'):
    _db_url = _db_url.replace('postgres://', 'postgresql://', 1)
app.config['SQLALCHEMY_DATABASE_URI'] = _db_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# SESSION COOKIES — secure cookies in production (HTTPS on Render)
_is_prod = os.environ.get('RENDER') or os.environ.get('PRODUCTION')
app.config['SESSION_COOKIE_SECURE']   = bool(_is_prod)
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_HTTPONLY'] = True

db = SQLAlchemy(app)
socketio = SocketIO(
    app,
    cors_allowed_origins='*',
    async_mode=_ASYNC_MODE,
    manage_session=False,
    # Ping settings tuned for Render's 30-second idle timeout
    ping_timeout=25,
    ping_interval=10,
)
login_manager = LoginManager(app)
login_manager.login_view = 'index'

# ── Models ─────────────────────────────────────────────────────────────────────
class User(UserMixin, db.Model):
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(32), unique=True, nullable=False, index=True)
    display_name = db.Column(db.String(64), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(128), nullable=False)
    is_online = db.Column(db.Boolean, default=False)
    last_seen = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    socket_id = db.Column(db.String(64), nullable=True)
    status = db.Column(db.String(20), default='available')
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    is_active = db.Column(db.Boolean, default=True)

    friendships_sent = db.relationship('Friendship', foreign_keys='Friendship.sender_id', backref='sender', lazy='dynamic')
    friendships_received = db.relationship('Friendship', foreign_keys='Friendship.receiver_id', backref='receiver', lazy='dynamic')
    calls_made = db.relationship('CallLog', foreign_keys='CallLog.caller_id', backref='caller', lazy='dynamic')
    calls_received = db.relationship('CallLog', foreign_keys='CallLog.callee_id', backref='callee', lazy='dynamic')

    def to_dict(self, include_private=False):
        data = {
            'id': self.id,
            'username': self.username,
            'display_name': self.display_name,
            'avatar_url': f'/api/avatar/{self.username}',
            'is_online': self.is_online,
            'status': self.status,
            'last_seen': self.last_seen.isoformat() if self.last_seen else None,
        }
        if include_private:
            data['email'] = self.email
        return data

    def get_friends(self):
        accepted = Friendship.query.filter(
            ((Friendship.sender_id == self.id) | (Friendship.receiver_id == self.id)),
            Friendship.status == 'accepted'
        ).all()
        friends = []
        for f in accepted:
            friends.append(f.receiver if f.sender_id == self.id else f.sender)
        return friends


class Friendship(db.Model):
    __tablename__ = 'friendships'
    id = db.Column(db.Integer, primary_key=True)
    sender_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    receiver_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    status = db.Column(db.String(20), default='pending')
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    __table_args__ = (db.UniqueConstraint('sender_id', 'receiver_id', name='unique_friendship'),)


class CallLog(db.Model):
    __tablename__ = 'call_logs'
    id = db.Column(db.Integer, primary_key=True)
    call_id = db.Column(db.String(64), unique=True, nullable=False, index=True)
    caller_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    callee_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    call_type = db.Column(db.String(10), nullable=False)
    status = db.Column(db.String(20), nullable=False, default='initiated')
    started_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    answered_at = db.Column(db.DateTime, nullable=True)
    ended_at = db.Column(db.DateTime, nullable=True)
    duration_seconds = db.Column(db.Integer, nullable=True)

    def to_dict(self):
        return {
            'id': self.id, 'call_id': self.call_id,
            'caller': self.caller.to_dict() if self.caller else None,
            'callee': self.callee.to_dict() if self.callee else None,
            'call_type': self.call_type, 'status': self.status,
            'started_at': self.started_at.isoformat() if self.started_at else None,
            'answered_at': self.answered_at.isoformat() if self.answered_at else None,
            'ended_at': self.ended_at.isoformat() if self.ended_at else None,
            'duration_seconds': self.duration_seconds,
        }


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))

# ── In-memory call manager ─────────────────────────────────────────────────────
_active_calls = {}
_call_lock = threading.Lock()

def create_call(caller_id, callee_id, call_type):
    call_id = str(uuid.uuid4())
    with _call_lock:
        _active_calls[call_id] = {
            'call_id': call_id, 'caller_id': caller_id, 'callee_id': callee_id,
            'call_type': call_type, 'status': 'ringing',
            'started_at': datetime.now(timezone.utc).isoformat(),
        }
    try:
        log = CallLog(call_id=call_id, caller_id=caller_id, callee_id=callee_id, call_type=call_type, status='ringing')
        db.session.add(log)
        db.session.commit()
    except Exception as e:
        db.session.rollback()
    return call_id

def get_call(call_id):
    with _call_lock:
        return _active_calls.get(call_id)

def accept_call(call_id):
    with _call_lock:
        if call_id in _active_calls:
            _active_calls[call_id]['status'] = 'active'
            _active_calls[call_id]['answered_at'] = datetime.now(timezone.utc).isoformat()
    try:
        log = CallLog.query.filter_by(call_id=call_id).first()
        if log:
            log.status = 'accepted'
            log.answered_at = datetime.now(timezone.utc)
            db.session.commit()
    except Exception:
        db.session.rollback()

def end_call(call_id, reason='ended'):
    with _call_lock:
        call = _active_calls.pop(call_id, None)
    if call:
        now = datetime.now(timezone.utc)
        answered_at = call.get('answered_at')
        duration = None
        if answered_at:
            try:
                ans = datetime.fromisoformat(answered_at)
                if ans.tzinfo is None:
                    ans = ans.replace(tzinfo=timezone.utc)
                duration = int((now - ans).total_seconds())
            except Exception:
                pass
        try:
            log = CallLog.query.filter_by(call_id=call_id).first()
            if log:
                log.status = reason
                log.ended_at = now
                if duration is not None:
                    log.duration_seconds = duration
                db.session.commit()
        except Exception:
            db.session.rollback()
    return call

def is_user_in_call(user_id):
    with _call_lock:
        for c in _active_calls.values():
            if (c['caller_id'] == user_id or c['callee_id'] == user_id) and c['status'] in ('ringing', 'active'):
                return True
    return False

def get_user_active_call(user_id):
    with _call_lock:
        for c in _active_calls.values():
            if (c['caller_id'] == user_id or c['callee_id'] == user_id) and c['status'] in ('ringing', 'active'):
                return dict(c)
    return None

def set_user_online(user_id, socket_id):
    user = db.session.get(User, user_id)
    if user:
        user.is_online = True
        user.socket_id = socket_id
        user.last_seen = datetime.now(timezone.utc)
        db.session.commit()

def set_user_offline(user_id):
    user = db.session.get(User, user_id)
    if user:
        user.is_online = False
        user.socket_id = None
        user.last_seen = datetime.now(timezone.utc)
        db.session.commit()

def update_user_status(user_id, status):
    user = db.session.get(User, user_id)
    if user and status in ('available', 'busy', 'away'):
        user.status = status
        db.session.commit()

# ── Auth Routes ────────────────────────────────────────────────────────────────
USERNAME_RE = re.compile(r'^[a-zA-Z0-9_]{3,32}$')

@app.route('/auth/signup', methods=['POST'])
def signup():
    data = request.get_json(silent=True) or {}
    username = (data.get('username') or '').strip().lower()
    display_name = (data.get('display_name') or '').strip()
    email = (data.get('email') or '').strip().lower()
    password = data.get('password') or ''

    errors = {}
    if not USERNAME_RE.match(username):
        errors['username'] = 'Username must be 3-32 alphanumeric characters or underscores.'
    if not display_name or len(display_name) < 2:
        errors['display_name'] = 'Display name must be at least 2 characters.'
    if not email or '@' not in email:
        errors['email'] = 'Valid email required.'
    if len(password) < 8:
        errors['password'] = 'Password must be at least 8 characters.'
    if errors:
        return jsonify({'ok': False, 'errors': errors}), 422

    if User.query.filter_by(username=username).first():
        return jsonify({'ok': False, 'errors': {'username': 'Username already taken.'}}), 409
    if User.query.filter_by(email=email).first():
        return jsonify({'ok': False, 'errors': {'email': 'Email already registered.'}}), 409

    pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    user = User(username=username, display_name=display_name, email=email, password_hash=pw_hash)
    db.session.add(user)
    db.session.commit()
    login_user(user, remember=True)
    return jsonify({'ok': True, 'user': user.to_dict(include_private=True)}), 201

@app.route('/auth/login', methods=['POST'])
def login():
    data = request.get_json(silent=True) or {}
    identifier = (data.get('username') or data.get('email') or '').strip().lower()
    password = data.get('password') or ''
    user = User.query.filter((User.username == identifier) | (User.email == identifier)).first()
    if not user or not bcrypt.checkpw(password.encode(), user.password_hash.encode()):
        return jsonify({'ok': False, 'error': 'Invalid credentials.'}), 401
    login_user(user, remember=True)
    return jsonify({'ok': True, 'user': user.to_dict(include_private=True)})

@app.route('/auth/logout', methods=['POST'])
@login_required
def logout():
    set_user_offline(current_user.id)
    logout_user()
    return jsonify({'ok': True})

@app.route('/auth/me')
@login_required
def me():
    return jsonify({'ok': True, 'user': current_user.to_dict(include_private=True)})

# ── API Routes ─────────────────────────────────────────────────────────────────
@app.route('/api/ice-config')
@login_required
def ice_config():
    # Base STUN servers (free, public)
    ice_servers = [
        {'urls': 'stun:stun.l.google.com:19302'},
        {'urls': 'stun:stun1.l.google.com:19302'},
    ]
    # Optional: add a TURN server for production (required when both peers are behind NAT)
    # Set TURN_URL, TURN_USERNAME, TURN_CREDENTIAL in Render environment variables
    turn_url  = os.environ.get('TURN_URL')
    turn_user = os.environ.get('TURN_USERNAME')
    turn_cred = os.environ.get('TURN_CREDENTIAL')
    if turn_url and turn_user and turn_cred:
        ice_servers.append({
            'urls': turn_url,
            'username': turn_user,
            'credential': turn_cred,
        })
    return jsonify({'iceServers': ice_servers})

@app.route('/api/avatar/<username>')
def avatar(username):
    user = User.query.filter_by(username=username).first()
    if not user:
        return '', 404
    initials = user.display_name[:2].upper()
    colors = ['#6366f1', '#8b5cf6', '#ec4899', '#14b8a6', '#f59e0b', '#ef4444']
    color = colors[user.id % len(colors)]
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="80" height="80" viewBox="0 0 80 80">
  <rect width="80" height="80" rx="40" fill="{color}"/>
  <text x="40" y="48" font-family="Arial,sans-serif" font-size="28" font-weight="bold"
        fill="white" text-anchor="middle" dominant-baseline="middle">{initials}</text>
</svg>'''
    return svg, 200, {'Content-Type': 'image/svg+xml', 'Cache-Control': 'public, max-age=86400'}

@app.route('/api/users/search')
@login_required
def search_users():
    q = request.args.get('q', '').strip()
    if len(q) < 2:
        return jsonify([])
    users = User.query.filter(
        (User.username.ilike(f'%{q}%') | User.display_name.ilike(f'%{q}%')),
        User.id != current_user.id, User.is_active == True
    ).limit(20).all()
    return jsonify([u.to_dict() for u in users])

@app.route('/api/users/<username>')
@login_required
def get_user(username):
    user = User.query.filter_by(username=username).first_or_404()
    return jsonify(user.to_dict())

@app.route('/api/friends')
@login_required
def list_friends():
    friends = current_user.get_friends()
    return jsonify([f.to_dict() for f in friends])

@app.route('/api/friends/requests')
@login_required
def friend_requests():
    pending = Friendship.query.filter_by(receiver_id=current_user.id, status='pending').all()
    return jsonify([{'id': f.id, 'sender': f.sender.to_dict(), 'created_at': f.created_at.isoformat()} for f in pending])

@app.route('/api/friends/add', methods=['POST'])
@login_required
def add_friend():
    data = request.get_json(silent=True) or {}
    username = data.get('username', '').strip().lower()
    target = User.query.filter_by(username=username).first()
    if not target:
        return jsonify({'ok': False, 'error': 'User not found.'}), 404
    if target.id == current_user.id:
        return jsonify({'ok': False, 'error': 'Cannot add yourself.'}), 400
    existing = Friendship.query.filter(
        ((Friendship.sender_id == current_user.id) & (Friendship.receiver_id == target.id)) |
        ((Friendship.sender_id == target.id) & (Friendship.receiver_id == current_user.id))
    ).first()
    if existing:
        return jsonify({'ok': False, 'error': 'Friendship already exists.'}), 409
    f = Friendship(sender_id=current_user.id, receiver_id=target.id)
    db.session.add(f)
    db.session.commit()
    if target.socket_id:
        socketio.emit('friend_request', {'sender': current_user.to_dict()}, room=f'user_{target.id}')
    return jsonify({'ok': True})

@app.route('/api/friends/respond', methods=['POST'])
@login_required
def respond_friend():
    data = request.get_json(silent=True) or {}
    friendship_id = data.get('friendship_id')
    action = data.get('action')
    f = Friendship.query.filter_by(id=friendship_id, receiver_id=current_user.id).first()
    if not f:
        return jsonify({'ok': False, 'error': 'Not found.'}), 404
    if action == 'accept':
        f.status = 'accepted'
        db.session.commit()
        if f.sender.socket_id:
            socketio.emit('friend_accepted', {'user': current_user.to_dict()}, room=f'user_{f.sender_id}')
    elif action == 'reject':
        db.session.delete(f)
        db.session.commit()
    return jsonify({'ok': True})

@app.route('/api/calls/history')
@login_required
def call_history():
    page = request.args.get('page', 1, type=int)
    calls = CallLog.query.filter(
        (CallLog.caller_id == current_user.id) | (CallLog.callee_id == current_user.id)
    ).order_by(CallLog.started_at.desc()).paginate(page=page, per_page=30, error_out=False)
    return jsonify({'calls': [c.to_dict() for c in calls.items], 'total': calls.total, 'pages': calls.pages, 'page': page})

@app.route('/api/calls/missed-count')
@login_required
def missed_count():
    count = CallLog.query.filter_by(callee_id=current_user.id, status='missed').count()
    return jsonify({'count': count})

# ── Page Routes ────────────────────────────────────────────────────────────────
@app.route('/')
def index():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    return render_template_string(INDEX_HTML)

@app.route('/dashboard')
@login_required
def dashboard():
    return render_template_string(DASHBOARD_HTML, user=current_user)

@app.route('/call/<call_id>')
@login_required
def call_room(call_id):
    return render_template_string(CALL_HTML, user=current_user, call_id=call_id)

# ── Socket Handlers ────────────────────────────────────────────────────────────
def _get_user():
    if current_user and current_user.is_authenticated:
        return current_user
    return None

@socketio.on('connect')
def on_connect():
    user = _get_user()
    if not user:
        return False
    sid = request.sid
    set_user_online(user.id, sid)
    join_room(f'user_{user.id}')
    friends = user.get_friends()
    for friend in friends:
        emit('presence_update', {'user_id': user.id, 'username': user.username, 'is_online': True, 'status': user.status}, room=f'user_{friend.id}')
    emit('connected', {'user_id': user.id, 'sid': sid})

@socketio.on('disconnect')
def on_disconnect():
    user = _get_user()
    if not user:
        return
    active = get_user_active_call(user.id)
    if active:
        _handle_call_end(active['call_id'], user.id, 'disconnected')
    set_user_offline(user.id)
    leave_room(f'user_{user.id}')
    friends = user.get_friends()
    for friend in friends:
        emit('presence_update', {'user_id': user.id, 'username': user.username, 'is_online': False, 'status': 'offline'}, room=f'user_{friend.id}')

@socketio.on('heartbeat')
def on_heartbeat():
    user = _get_user()
    if user:
        u = db.session.get(User, user.id)
        if u:
            u.last_seen = datetime.now(timezone.utc)
            db.session.commit()
        emit('heartbeat_ack', {'ts': datetime.now(timezone.utc).isoformat()})

@socketio.on('set_status')
def on_set_status(data):
    user = _get_user()
    if not user:
        return
    status = data.get('status', 'available')
    update_user_status(user.id, status)
    friends = user.get_friends()
    for friend in friends:
        emit('presence_update', {'user_id': user.id, 'username': user.username, 'is_online': True, 'status': status}, room=f'user_{friend.id}')

@socketio.on('call_initiate')
def on_call_initiate(data):
    caller = _get_user()
    if not caller:
        return
    callee_username = data.get('callee_username', '').strip()
    call_type = data.get('call_type', 'video')
    callee = User.query.filter_by(username=callee_username).first()
    if not callee:
        emit('call_error', {'code': 'NOT_FOUND', 'message': 'User not found.'})
        return
    if callee.id == caller.id:
        emit('call_error', {'code': 'SELF_CALL', 'message': 'Cannot call yourself.'})
        return
    if is_user_in_call(caller.id):
        emit('call_error', {'code': 'ALREADY_IN_CALL', 'message': 'You are already in a call.'})
        return
    if not callee.is_online:
        emit('call_error', {'code': 'OFFLINE', 'message': f'{callee.display_name} is offline.'})
        return
    if callee.status == 'busy' or is_user_in_call(callee.id):
        emit('call_error', {'code': 'BUSY', 'message': f'{callee.display_name} is busy.'})
        return
    call_id = create_call(caller.id, callee.id, call_type)
    update_user_status(caller.id, 'busy')
    emit('incoming_call', {'call_id': call_id, 'caller': caller.to_dict(), 'call_type': call_type}, room=f'user_{callee.id}')
    emit('call_ringing', {'call_id': call_id, 'callee': callee.to_dict(), 'call_type': call_type})

@socketio.on('call_accept')
def on_call_accept(data):
    callee = _get_user()
    if not callee:
        return
    call_id = data.get('call_id')
    call = get_call(call_id)
    if not call or call['callee_id'] != callee.id:
        emit('call_error', {'code': 'INVALID_CALL', 'message': 'Call not found.'})
        return
    accept_call(call_id)
    update_user_status(callee.id, 'busy')
    emit('call_accepted', {'call_id': call_id, 'callee': callee.to_dict()}, room=f'user_{call["caller_id"]}')
    emit('call_accepted_ack', {'call_id': call_id})

@socketio.on('call_reject')
def on_call_reject(data):
    callee = _get_user()
    if not callee:
        return
    call_id = data.get('call_id')
    reason = data.get('reason', 'rejected')
    call = get_call(call_id)
    if not call:
        return
    ended = end_call(call_id, reason)
    update_user_status(callee.id, 'available')
    if ended:
        emit('call_rejected', {'call_id': call_id, 'reason': reason, 'callee': callee.to_dict()}, room=f'user_{call["caller_id"]}')

@socketio.on('call_cancel')
def on_call_cancel(data):
    caller = _get_user()
    if not caller:
        return
    call_id = data.get('call_id')
    call = get_call(call_id)
    if not call or call['caller_id'] != caller.id:
        return
    ended = end_call(call_id, 'cancelled')
    update_user_status(caller.id, 'available')
    if ended:
        emit('call_cancelled', {'call_id': call_id, 'caller': caller.to_dict()}, room=f'user_{call["callee_id"]}')

@socketio.on('call_end')
def on_call_end(data):
    user = _get_user()
    if not user:
        return
    _handle_call_end(data.get('call_id'), user.id, 'ended')

def _handle_call_end(call_id, user_id, reason='ended'):
    call = get_call(call_id)
    if not call:
        return
    ended = end_call(call_id, reason)
    if not ended:
        return
    other_id = call['callee_id'] if call['caller_id'] == user_id else call['caller_id']
    update_user_status(user_id, 'available')
    update_user_status(other_id, 'available')
    emit('call_ended', {'call_id': call_id, 'reason': reason, 'ended_by': user_id}, room=f'user_{other_id}')

@socketio.on('webrtc_offer')
def on_webrtc_offer(data):
    user = _get_user()
    if not user:
        return
    call = get_call(data.get('call_id'))
    if not call:
        return
    other_id = call['callee_id'] if call['caller_id'] == user.id else call['caller_id']
    emit('webrtc_offer', {'call_id': data.get('call_id'), 'offer': data.get('offer'), 'from_user_id': user.id}, room=f'user_{other_id}')

@socketio.on('webrtc_answer')
def on_webrtc_answer(data):
    user = _get_user()
    if not user:
        return
    call = get_call(data.get('call_id'))
    if not call:
        return
    other_id = call['caller_id'] if call['callee_id'] == user.id else call['callee_id']
    emit('webrtc_answer', {'call_id': data.get('call_id'), 'answer': data.get('answer'), 'from_user_id': user.id}, room=f'user_{other_id}')

@socketio.on('webrtc_ice')
def on_webrtc_ice(data):
    user = _get_user()
    if not user:
        return
    call = get_call(data.get('call_id'))
    if not call:
        return
    other_id = call['callee_id'] if call['caller_id'] == user.id else call['caller_id']
    emit('webrtc_ice', {'call_id': data.get('call_id'), 'candidate': data.get('candidate'), 'from_user_id': user.id}, room=f'user_{other_id}')

@socketio.on('webrtc_ice_restart')
def on_ice_restart(data):
    user = _get_user()
    if not user:
        return
    call = get_call(data.get('call_id'))
    if not call:
        return
    other_id = call['callee_id'] if call['caller_id'] == user.id else call['caller_id']
    emit('webrtc_ice_restart', {'call_id': data.get('call_id'), 'from_user_id': user.id}, room=f'user_{other_id}')

# ── HTML Templates ─────────────────────────────────────────────────────────────
CSS = """
<link href="https://fonts.googleapis.com/css2?family=Space+Mono:wght@400;700&family=DM+Sans:opsz,wght@9..40,300;400;500;600;700&display=swap" rel="stylesheet">
<style>
:root{--bg:#080c14;--bg2:#0d1220;--bg3:#131927;--bg4:#1a2235;--border:rgba(255,255,255,.07);--border-2:rgba(255,255,255,.12);--text:#e8eaf0;--text-2:#8892a4;--text-3:#5a6475;--accent:#6366f1;--accent-2:#818cf8;--accent-glow:rgba(99,102,241,.35);--violet:#8b5cf6;--green:#10b981;--red:#ef4444;--amber:#f59e0b;--radius:12px;--radius-lg:20px;--shadow:0 4px 24px rgba(0,0,0,.4);--shadow-lg:0 8px 48px rgba(0,0,0,.6);--font-mono:'Space Mono',monospace;--font:'DM Sans',system-ui,sans-serif;--transition:.18s cubic-bezier(.4,0,.2,1)}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}html{height:100%}
body{font-family:var(--font);background:var(--bg);color:var(--text);min-height:100%;-webkit-font-smoothing:antialiased}
img{display:block}button{cursor:pointer;border:none;background:none;font-family:inherit}input,select{font-family:inherit}a{color:inherit;text-decoration:none}
.hidden{display:none!important}
::-webkit-scrollbar{width:6px}::-webkit-scrollbar-track{background:transparent}::-webkit-scrollbar-thumb{background:var(--bg4);border-radius:3px}
@keyframes spin{to{transform:rotate(360deg)}}@keyframes slideUp{from{opacity:0;transform:translateY(16px)}to{opacity:1;transform:none}}@keyframes fadeIn{from{opacity:0}to{opacity:1}}@keyframes callRing{0%{transform:scale(1);opacity:.7}100%{transform:scale(1.8);opacity:0}}@keyframes incomingRing{0%{transform:scale(1);opacity:.8}100%{transform:scale(2);opacity:0}}@keyframes toastIn{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}
/* Auth */
.auth-layout{display:grid;grid-template-columns:1fr 1fr;min-height:100vh;min-height:100dvh}
@media(max-width:768px){.auth-layout{grid-template-columns:1fr}.auth-brand{display:none}}
.auth-brand{position:relative;background:linear-gradient(135deg,#0d1628 0%,#0a0f1e 100%);display:flex;align-items:center;justify-content:center;overflow:hidden;border-right:1px solid var(--border)}
.brand-content{position:relative;z-index:2;padding:48px}
.brand-logo{display:flex;align-items:center;gap:12px;margin-bottom:48px}.brand-logo svg{width:48px;height:48px}.brand-logo span{font-family:var(--font-mono);font-size:1.25rem;font-weight:700;letter-spacing:-.5px}
.brand-headline{font-size:clamp(2rem,3.5vw,3rem);font-weight:700;line-height:1.1;letter-spacing:-1.5px;margin-bottom:20px;color:#f0f2ff}.brand-headline em{font-style:normal;color:var(--accent-2)}
.brand-sub{color:var(--text-2);font-size:.95rem;line-height:1.7;margin-bottom:40px}
.brand-features{display:flex;flex-direction:column;gap:10px}.feature-pill{display:flex;align-items:center;gap:10px;font-size:.875rem;color:var(--text-2)}.pill-dot{width:8px;height:8px;border-radius:50%;background:var(--accent);box-shadow:0 0 8px var(--accent);flex-shrink:0}
.brand-orb{position:absolute;border-radius:50%;filter:blur(80px);pointer-events:none;opacity:.15}.brand-orb-1{width:400px;height:400px;background:var(--accent);top:-100px;right:-100px}.brand-orb-2{width:300px;height:300px;background:var(--violet);bottom:-80px;left:-80px}
.auth-panel{display:flex;flex-direction:column;justify-content:center;padding:48px 64px;background:var(--bg2)}
@media(max-width:900px){.auth-panel{padding:32px 28px}}
.auth-tabs{display:flex;gap:4px;background:var(--bg3);border-radius:10px;padding:4px;margin-bottom:32px;width:fit-content}.auth-tab{padding:8px 20px;border-radius:7px;font-size:.875rem;font-weight:500;color:var(--text-2);transition:var(--transition)}.auth-tab.active{background:var(--bg4);color:var(--text)}
.auth-form{display:none;flex-direction:column;gap:18px}.auth-form.active{display:flex}
.form-row{display:grid;grid-template-columns:1fr 1fr;gap:14px}@media(max-width:500px){.form-row{grid-template-columns:1fr}}
.form-group{display:flex;flex-direction:column;gap:7px}label{font-size:.8rem;font-weight:500;color:var(--text-2);text-transform:uppercase;letter-spacing:.5px}
input[type="text"],input[type="email"],input[type="password"]{background:var(--bg3);border:1.5px solid var(--border-2);border-radius:9px;color:var(--text);font-size:.95rem;padding:11px 14px;outline:none;transition:var(--transition);width:100%}
input:focus{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-glow)}input::placeholder{color:var(--text-3)}
.password-wrap{position:relative}.password-wrap input{padding-right:44px}.toggle-pw{position:absolute;right:12px;top:50%;transform:translateY(-50%);color:var(--text-3)}.toggle-pw svg{width:20px;height:20px;fill:currentColor}
.field-error{font-size:.78rem;color:var(--red);min-height:16px}.form-error{background:rgba(239,68,68,.1);border:1px solid rgba(239,68,68,.25);border-radius:8px;padding:10px 12px;font-size:.875rem;color:var(--red)}
.btn-primary{background:linear-gradient(135deg,var(--accent),var(--violet));color:white;font-weight:600;font-size:.95rem;padding:13px;border-radius:10px;transition:var(--transition);position:relative;overflow:hidden}
.btn-primary:hover:not(:disabled){transform:translateY(-1px);box-shadow:0 6px 20px var(--accent-glow)}.btn-primary:disabled{opacity:.6;cursor:not-allowed}
.btn-auth{display:flex;align-items:center;justify-content:center;gap:8px;min-height:48px}
.btn-spinner{width:18px;height:18px;border:2.5px solid rgba(255,255,255,.3);border-top-color:white;border-radius:50%;animation:spin .7s linear infinite}
.auth-demo-hint{font-size:.8rem;color:var(--text-3);text-align:center}.auth-demo-hint code{font-family:var(--font-mono);font-size:.75rem;color:var(--accent-2)}
/* App layout */
.app-layout{display:grid;grid-template-columns:280px 1fr;height:100vh;height:100dvh;overflow:hidden}
@media(max-width:700px){.app-layout{grid-template-columns:1fr}}
/* Mobile menu toggle */
.mobile-menu-btn{display:none;position:fixed;top:14px;left:14px;z-index:60;width:40px;height:40px;border-radius:10px;background:var(--bg2);border:1px solid var(--border-2);align-items:center;justify-content:center;color:var(--text)}
.mobile-menu-btn svg{width:22px;height:22px;fill:currentColor}
@media(max-width:700px){.mobile-menu-btn{display:flex}}
.sidebar-overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:55;backdrop-filter:blur(2px)}
@media(max-width:700px){.sidebar-overlay.show{display:block}}
/* Sidebar */
.sidebar{background:var(--bg2);border-right:1px solid var(--border);display:flex;flex-direction:column;overflow:hidden}
@media(max-width:700px){.sidebar{position:fixed;left:0;top:0;bottom:0;width:280px;z-index:56;transform:translateX(-100%);transition:transform .28s cubic-bezier(.4,0,.2,1)}.sidebar.open{transform:translateX(0)}}
.sidebar-header{padding:20px;border-bottom:1px solid var(--border)}.sidebar-logo{display:flex;align-items:center;gap:10px;font-family:var(--font-mono);font-weight:700;font-size:1rem}.sidebar-logo svg{width:32px;height:32px}
.sidebar-user{display:flex;align-items:center;gap:12px;padding:16px 20px;border-bottom:1px solid var(--border)}.user-avatar-wrap{position:relative;flex-shrink:0}.user-avatar{width:40px;height:40px;border-radius:50%;object-fit:cover}
.status-dot{position:absolute;bottom:1px;right:1px;width:10px;height:10px;border-radius:50%;border:2px solid var(--bg2)}.status-dot.available{background:var(--green)}.status-dot.busy{background:var(--red)}.status-dot.away{background:var(--amber)}.status-dot.offline{background:var(--text-3)}
.user-info{flex:1;min-width:0}.user-display-name{font-size:.9rem;font-weight:600;color:var(--text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.user-username{font-size:.75rem;color:var(--text-3);font-family:var(--font-mono)}
.status-select{background:var(--bg3);border:1px solid var(--border-2);border-radius:7px;color:var(--text-2);font-size:.75rem;padding:4px 6px;cursor:pointer;outline:none}
.sidebar-nav{display:flex;flex-direction:column;gap:4px;padding:12px;flex:1}
.nav-btn{display:flex;align-items:center;gap:10px;padding:10px 12px;border-radius:9px;color:var(--text-2);font-size:.875rem;font-weight:500;transition:var(--transition);position:relative}.nav-btn svg{width:20px;height:20px;fill:currentColor;flex-shrink:0}.nav-btn:hover{background:var(--bg3);color:var(--text)}.nav-btn.active{background:rgba(99,102,241,.12);color:var(--accent-2)}
.nav-badge{margin-left:auto;background:var(--red);color:white;font-size:.65rem;font-weight:700;min-width:18px;height:18px;border-radius:9px;display:flex;align-items:center;justify-content:center;padding:0 5px}
.logout-btn{display:flex;align-items:center;gap:10px;padding:14px 20px;border-top:1px solid var(--border);color:var(--text-3);font-size:.875rem;transition:var(--transition)}.logout-btn svg{width:18px;height:18px;fill:currentColor}.logout-btn:hover{color:var(--red);background:rgba(239,68,68,.06)}
/* Main */
.main-content{background:var(--bg);overflow:hidden;position:relative}.panel{position:absolute;inset:0;display:none;flex-direction:column;overflow:hidden}.panel.active{display:flex}
@media(max-width:700px){.panel-header{padding-left:64px!important}}
.panel-header{display:flex;align-items:center;justify-content:space-between;padding:24px 28px 16px;border-bottom:1px solid var(--border)}.panel-header h2{font-size:1.2rem;font-weight:700;letter-spacing:-.3px}
.btn-icon{width:36px;height:36px;display:flex;align-items:center;justify-content:center;border-radius:8px;color:var(--text-2);transition:var(--transition)}.btn-icon svg{width:20px;height:20px;fill:currentColor}.btn-icon:hover{background:var(--bg3);color:var(--text)}
.search-bar{display:flex;align-items:center;gap:10px;background:var(--bg3);border:1.5px solid var(--border-2);border-radius:10px;padding:10px 14px;margin:12px 20px;transition:var(--transition)}.search-bar:focus-within{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-glow)}.search-bar svg{width:18px;height:18px;fill:var(--text-3);flex-shrink:0}.search-bar input{flex:1;background:none;border:none;outline:none;color:var(--text);font-size:.9rem}.search-bar input::placeholder{color:var(--text-3)}.search-bar.large{margin:20px;padding:12px 16px}
.contacts-list,.contact-search-results,.call-history-list{flex:1;overflow-y:auto;padding:8px 12px}.contact-search-results{max-height:320px;overflow-y:auto;border-top:1px solid var(--border)}
.contact-card{display:flex;align-items:center;gap:14px;padding:12px 14px;border-radius:10px;transition:var(--transition)}.contact-card:hover{background:var(--bg3)}.contact-avatar{width:44px;height:44px;border-radius:50%;object-fit:cover;flex-shrink:0}
.contact-info{flex:1;min-width:0}.contact-name{font-size:.9rem;font-weight:600}.contact-username{font-size:.75rem;color:var(--text-3);font-family:var(--font-mono)}.contact-status{font-size:.72rem;color:var(--text-3);margin-top:2px}.contact-status.available,.contact-status.online{color:var(--green)}.contact-status.busy{color:var(--red)}.contact-status.away{color:var(--amber)}
.contact-actions{display:flex;gap:6px}.contact-btn{width:34px;height:34px;border-radius:8px;display:flex;align-items:center;justify-content:center;transition:var(--transition);color:var(--text-2)}.contact-btn svg{width:18px;height:18px;fill:currentColor}.contact-btn.voice:hover{background:rgba(16,185,129,.15);color:var(--green)}.contact-btn.video:hover{background:rgba(99,102,241,.15);color:var(--accent-2)}.contact-btn.add:hover{background:rgba(99,102,241,.15);color:var(--accent-2)}
.add-contact-form{border-bottom:1px solid var(--border);padding-bottom:8px}
.friend-requests-section{padding:12px 20px;border-bottom:1px solid var(--border)}.section-title{font-size:.75rem;font-weight:600;color:var(--text-3);text-transform:uppercase;letter-spacing:.8px;margin-bottom:10px}
.friend-request-card{display:flex;align-items:center;gap:10px;padding:8px 0}.fr-actions{display:flex;gap:6px;margin-left:auto}.fr-btn{padding:5px 14px;border-radius:6px;font-size:.8rem;font-weight:600;transition:var(--transition)}.fr-btn.accept{background:rgba(16,185,129,.15);color:var(--green)}.fr-btn.accept:hover{background:var(--green);color:white}.fr-btn.reject{background:rgba(239,68,68,.1);color:var(--red)}.fr-btn.reject:hover{background:var(--red);color:white}
.history-card{display:flex;align-items:center;gap:12px;padding:12px 14px;border-radius:10px;transition:var(--transition)}.history-card:hover{background:var(--bg3)}.history-icon{width:38px;height:38px;border-radius:10px;display:flex;align-items:center;justify-content:center;flex-shrink:0}.history-icon svg{width:20px;height:20px;fill:currentColor}.history-icon.outgoing{background:rgba(99,102,241,.12);color:var(--accent-2)}.history-icon.incoming{background:rgba(16,185,129,.12);color:var(--green)}.history-icon.missed{background:rgba(239,68,68,.12);color:var(--red)}.history-info{flex:1;min-width:0}.history-name{font-size:.9rem;font-weight:600}.history-meta{font-size:.75rem;color:var(--text-3)}
.loading-state{display:flex;flex-direction:column;align-items:center;justify-content:center;gap:16px;padding:48px;color:var(--text-3)}.spinner{width:28px;height:28px;border:2.5px solid var(--border-2);border-top-color:var(--accent);border-radius:50%;animation:spin .8s linear infinite}.spinner.large{width:44px;height:44px;border-width:3px}.empty-state{display:flex;flex-direction:column;align-items:center;justify-content:center;gap:12px;padding:48px;color:var(--text-3);text-align:center}.empty-state svg{width:48px;height:48px;fill:var(--text-3);opacity:.4}.empty-state p{font-size:.9rem}
/* Call modal */
.modal{position:fixed;inset:0;z-index:100;display:flex;align-items:center;justify-content:center}.modal-backdrop{position:absolute;inset:0;background:rgba(0,0,0,.7);backdrop-filter:blur(8px)}.modal-card{position:relative;z-index:1;background:var(--bg2);border:1px solid var(--border-2);border-radius:var(--radius-lg);padding:40px;text-align:center;min-width:280px;animation:slideUp .25s ease}
.modal-avatar-wrap{position:relative;display:inline-block;margin-bottom:20px}.modal-avatar{width:84px;height:84px;border-radius:50%;margin:0 auto}.calling-rings{position:absolute;inset:-20px}.calling-ring{position:absolute;inset:0;border-radius:50%;border:2px solid var(--accent);animation:callRing 2s ease-out infinite}.calling-ring.r2{animation-delay:.7s}
.modal-label{font-size:.8rem;color:var(--text-3);text-transform:uppercase;letter-spacing:1px;margin-bottom:6px}.modal-card h3{font-size:1.4rem;font-weight:700;letter-spacing:-.3px}.modal-username{font-size:.85rem;color:var(--text-3);font-family:var(--font-mono);margin-top:4px;margin-bottom:28px}.modal-actions{display:flex;justify-content:center;gap:16px}
/* Incoming overlay */
.incoming-call-overlay{position:fixed;inset:0;z-index:200;display:flex;align-items:center;justify-content:center;background:rgba(0,0,0,.75);backdrop-filter:blur(12px);animation:fadeIn .2s ease}
.incoming-call-card{background:var(--bg2);border:1px solid var(--border-2);border-radius:var(--radius-lg);padding:40px 48px;text-align:center;box-shadow:var(--shadow-lg);animation:slideUp .25s ease}
.incoming-avatar-ring{position:relative;display:inline-block;margin-bottom:20px}.incoming-avatar{width:96px;height:96px;border-radius:50%;margin:0 auto}.ring{position:absolute;inset:0;border-radius:50%;border:2.5px solid var(--green);animation:incomingRing 2s ease-out infinite;transform-origin:center}.ring2{animation-delay:.5s}.ring3{animation-delay:1s}
.incoming-info{margin-bottom:28px}.incoming-label{font-size:.8rem;color:var(--text-3);text-transform:uppercase;letter-spacing:1px;margin-bottom:8px}.incoming-info h2{font-size:1.6rem;font-weight:700;letter-spacing:-.4px}.incoming-username{font-size:.875rem;color:var(--text-3);font-family:var(--font-mono);margin-top:4px}.incoming-actions{display:flex;justify-content:center;gap:24px}
/* Call buttons */
.btn-call-action{width:60px;height:60px;border-radius:50%;display:flex;align-items:center;justify-content:center;transition:var(--transition)}.btn-call-action svg{width:26px;height:26px;fill:white}
.btn-call-action.reject{background:var(--red);transform:rotate(135deg)}.btn-call-action.reject:hover{background:#dc2626;box-shadow:0 4px 16px rgba(239,68,68,.5);transform:rotate(135deg) scale(1.1)}.btn-call-action.accept{background:var(--green)}.btn-call-action.accept:hover{background:#059669;box-shadow:0 4px 16px rgba(16,185,129,.5);transform:scale(1.1)}.btn-call-action.large{width:68px;height:68px}.btn-call-action.large svg{width:30px;height:30px}
/* Call layout */
.call-layout{width:100vw;height:100vh;height:100dvh;position:relative;overflow:hidden;background:#000}.remote-video{width:100%;height:100%;object-fit:cover;display:block}
.remote-audio-only{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;background:linear-gradient(160deg,#0d1220 0%,#080c14 100%);gap:20px}.audio-avatar-wrap{position:relative}.audio-avatar{width:120px;height:120px;border-radius:50%}.audio-rings{position:absolute;inset:-24px}.audio-ring{position:absolute;inset:0;border-radius:50%;border:2px solid var(--accent);animation:incomingRing 2.5s ease-out infinite}.audio-ring.ar2{animation-delay:.8s}.audio-ring.ar3{animation-delay:1.6s}.remote-audio-only p{font-size:1.3rem;font-weight:600;color:var(--text-2)}
.call-status-overlay{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;background:rgba(8,12,20,.85);backdrop-filter:blur(4px);z-index:10;transition:opacity .4s}.status-content{display:flex;flex-direction:column;align-items:center;gap:16px}#call-status-text{color:var(--text-2);font-size:1rem}
.call-topbar{position:absolute;top:0;left:0;right:0;padding:20px 24px;background:linear-gradient(to bottom,rgba(0,0,0,.6) 0%,transparent 100%);display:flex;align-items:center;justify-content:space-between;z-index:20}
.call-info{display:flex;align-items:center;gap:14px}.call-peer-avatar{width:44px;height:44px;border-radius:50%}.call-peer-name{font-size:1rem;font-weight:600}.call-timer{font-family:var(--font-mono);font-size:.85rem;color:var(--text-2);margin-top:2px}
.call-quality{display:flex;align-items:flex-end;gap:2px;padding:8px}.quality-bar{display:block;width:4px;border-radius:2px;background:var(--green);opacity:.3;transition:var(--transition)}.quality-bar.q1{height:6px}.quality-bar.q2{height:10px}.quality-bar.q3{height:14px}.quality-bar.q4{height:18px}.quality-bar.active{opacity:1}
.local-pip{position:absolute;bottom:100px;right:20px;width:140px;height:100px;border-radius:12px;overflow:hidden;border:2px solid rgba(255,255,255,.15);box-shadow:var(--shadow);z-index:20;cursor:move;background:#1a1a2e}.local-video{width:100%;height:100%;object-fit:cover;transform:scaleX(-1)}.pip-cam-off{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;background:var(--bg3);color:var(--text-3)}.pip-cam-off svg{width:28px;height:28px;fill:currentColor}
.call-controls{position:absolute;bottom:0;left:0;right:0;padding:20px 24px 32px;background:linear-gradient(to top,rgba(0,0,0,.7) 0%,transparent 100%);display:flex;align-items:center;justify-content:center;gap:16px;z-index:20}
@media(max-width:500px){.call-controls{gap:8px;padding:16px 12px 24px}.ctrl-btn{min-width:56px;padding:10px 12px;font-size:.65rem}.local-pip{width:90px;height:66px;bottom:90px}.ctrl-btn svg{width:20px;height:20px}}
.ctrl-btn{display:flex;flex-direction:column;align-items:center;gap:6px;padding:12px 20px;border-radius:14px;background:rgba(255,255,255,.1);color:white;font-size:.72rem;font-weight:500;transition:var(--transition);min-width:72px;backdrop-filter:blur(8px)}.ctrl-btn svg{width:24px;height:24px;fill:currentColor}.ctrl-btn:hover{background:rgba(255,255,255,.2)}.ctrl-btn.active{background:rgba(99,102,241,.3)}.ctrl-btn.muted{background:rgba(239,68,68,.25)}.end-btn{background:var(--red)!important}.end-btn:hover{background:#dc2626!important}
.reconnecting-overlay{position:absolute;inset:0;z-index:30;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:16px;background:rgba(0,0,0,.7);backdrop-filter:blur(4px)}.reconnecting-overlay p{color:var(--amber);font-size:.95rem}
#toast-container{position:fixed;bottom:24px;right:24px;z-index:300;display:flex;flex-direction:column;gap:8px}.toast{background:var(--bg2);border:1px solid var(--border-2);border-radius:10px;padding:12px 18px;font-size:.875rem;max-width:320px;animation:toastIn .25s ease;display:flex;align-items:center;gap:10px;box-shadow:var(--shadow)}.toast.success{border-color:rgba(16,185,129,.3)}.toast.error{border-color:rgba(239,68,68,.3)}.toast.info{border-color:rgba(99,102,241,.3)}

/* ═══════════════════════════════════════════════════════
   ANDROID / MOBILE RESPONSIVE — complete rewrite
   Fixes: overflow, overlap, sidebar, fonts, call UI
════════════════════════════════════════════════════════ */

/* 1. ROOT FIXES — stop horizontal scroll on ALL pages */
html, body {
  overflow-x: hidden;
  width: 100%;
  max-width: 100%;
  -webkit-text-size-adjust: 100%;
  text-size-adjust: 100%;
}
* { box-sizing: border-box; }

/* 2. ANDROID INPUT ZOOM FIX — font-size must be >=16px */
input, select, textarea {
  font-size: 16px !important;
}

/* ── AUTH PAGE ─────────────────────────────────────── */
@media (max-width: 768px) {
  .auth-layout {
    display: flex;
    flex-direction: column;
    min-height: 100vh;
    min-height: 100dvh;
    overflow-x: hidden;
  }
  .auth-brand { display: none !important; }
  .auth-panel {
    flex: 1;
    padding: 36px 20px 32px;
    justify-content: flex-start;
    width: 100%;
    max-width: 100%;
    overflow-x: hidden;
  }
  .auth-tabs {
    display: flex;
    width: 100%;
    margin-bottom: 28px;
  }
  .auth-tab { flex: 1; text-align: center; padding: 10px 8px; }
  .auth-form { gap: 16px; }
  .form-row { grid-template-columns: 1fr !important; gap: 12px; }
  .form-group { gap: 6px; }
  input[type="text"],
  input[type="email"],
  input[type="password"] {
    padding: 13px 14px;
    width: 100%;
  }
  .password-wrap { width: 100%; }
  .btn-primary { padding: 14px; width: 100%; }
}

/* ── APP LAYOUT — sidebar drawer ──────────────────── */
@media (max-width: 768px) {
  /* Grid collapses to single column */
  .app-layout {
    display: block;          /* simpler than grid on Android */
    width: 100vw;
    height: 100vh;
    height: 100dvh;
    overflow: hidden;
    position: relative;
  }

  /* Main content fills full screen */
  .main-content {
    width: 100vw;
    height: 100vh;
    height: 100dvh;
    overflow: hidden;
    position: absolute;
    top: 0; left: 0; right: 0; bottom: 0;
  }

  /* Sidebar: hidden off-screen, slides in */
  .sidebar {
    position: fixed !important;
    top: 0; left: 0; bottom: 0;
    width: 82vw !important;
    max-width: 300px !important;
    z-index: 200 !important;
    transform: translateX(-110%) !important;
    transition: transform 0.28s cubic-bezier(0.4,0,0.2,1) !important;
    overflow-y: auto;
    -webkit-overflow-scrolling: touch;
  }
  .sidebar.open {
    transform: translateX(0) !important;
    box-shadow: 4px 0 32px rgba(0,0,0,0.6);
  }

  /* Overlay behind open sidebar */
  .sidebar-overlay {
    display: none;
    position: fixed;
    inset: 0;
    background: rgba(0,0,0,0.65);
    z-index: 199;
    backdrop-filter: blur(2px);
    -webkit-backdrop-filter: blur(2px);
  }
  .sidebar-overlay.show { display: block; }

  /* Hamburger button */
  .mobile-menu-btn {
    display: flex !important;
    position: fixed;
    top: 12px; left: 12px;
    z-index: 201;
    width: 42px; height: 42px;
    border-radius: 10px;
    background: var(--bg2);
    border: 1px solid var(--border-2);
    align-items: center;
    justify-content: center;
    color: var(--text);
    box-shadow: 0 2px 8px rgba(0,0,0,0.4);
  }
  .mobile-menu-btn svg { width: 22px; height: 22px; fill: currentColor; }
}

/* ── PANELS & HEADERS ──────────────────────────────── */
@media (max-width: 768px) {
  .panel { position: absolute; inset: 0; overflow: hidden; }
  .panel-header {
    padding: 14px 14px 12px 62px !important;
    border-bottom: 1px solid var(--border);
    flex-shrink: 0;
  }
  .panel-header h2 { font-size: 1rem; }

  .search-bar { margin: 8px 10px; }
  .search-bar.large { margin: 10px; }

  .contacts-list,
  .contact-search-results,
  .call-history-list {
    padding: 4px 6px;
    overflow-y: auto;
    -webkit-overflow-scrolling: touch;
    flex: 1;
  }
}

/* ── CONTACT CARDS ─────────────────────────────────── */
@media (max-width: 480px) {
  .contact-card { padding: 10px 8px; gap: 10px; }
  .contact-avatar { width: 40px; height: 40px; flex-shrink: 0; }
  .contact-info { min-width: 0; flex: 1; }
  .contact-name { font-size: .85rem; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .contact-username, .contact-status { font-size: .7rem; }
  .contact-actions { flex-shrink: 0; gap: 4px; }
  .contact-btn { width: 32px; height: 32px; }
  .contact-btn svg { width: 16px; height: 16px; }
}

/* ── HISTORY CARDS ─────────────────────────────────── */
@media (max-width: 480px) {
  .history-card { padding: 10px 8px; gap: 10px; }
  .history-icon { width: 34px; height: 34px; border-radius: 8px; flex-shrink: 0; }
  .history-icon svg { width: 17px; height: 17px; }
  .history-info { min-width: 0; flex: 1; }
  .history-name { font-size: .85rem; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .history-meta { font-size: .7rem; }
}

/* ── FRIEND REQUESTS ───────────────────────────────── */
@media (max-width: 480px) {
  .friend-requests-section { padding: 10px 10px; }
  .friend-request-card { gap: 8px; flex-wrap: wrap; }
  .fr-actions { flex-shrink: 0; }
  .fr-btn { padding: 5px 12px; font-size: .78rem; }
}

/* ── INCOMING CALL OVERLAY ─────────────────────────── */
@media (max-width: 600px) {
  .incoming-call-overlay {
    padding: 16px;
    align-items: center;
  }
  .incoming-call-card {
    width: 100%;
    max-width: 340px;
    padding: 28px 20px;
    box-sizing: border-box;
  }
  .incoming-avatar { width: 80px; height: 80px; }
  .incoming-info h2 { font-size: 1.3rem; }
  .incoming-username { font-size: .8rem; }
  .incoming-actions { gap: 36px; }
  .btn-call-action { width: 58px; height: 58px; }
  .btn-call-action svg { width: 24px; height: 24px; }
}

/* ── OUTGOING CALL MODAL ───────────────────────────── */
@media (max-width: 600px) {
  .modal-card {
    width: calc(100vw - 32px);
    max-width: 320px;
    padding: 28px 20px;
  }
  .modal-avatar { width: 72px; height: 72px; }
  .modal-card h3 { font-size: 1.2rem; }
}

/* ── CALL SCREEN ───────────────────────────────────── */
@media (max-width: 600px) {
  /* Use dvh so Android chrome toolbar doesn't overlap */
  .call-layout {
    height: 100vh;
    height: 100dvh;
  }

  /* Top bar */
  .call-topbar { padding: 12px 14px; }
  .call-peer-avatar { width: 36px; height: 36px; }
  .call-peer-name { font-size: .88rem; }
  .call-timer { font-size: .75rem; }

  /* PiP local video */
  .local-pip {
    width: 90px; height: 66px;
    bottom: 90px; right: 10px;
    border-radius: 8px;
  }

  /* Controls */
  .call-controls {
    padding: 12px 8px 24px;
    gap: 8px;
    flex-wrap: nowrap;
    overflow-x: auto;
    -webkit-overflow-scrolling: touch;
    justify-content: center;
  }
  .ctrl-btn {
    min-width: 54px;
    max-width: 70px;
    padding: 10px 6px;
    font-size: .62rem;
    border-radius: 12px;
    flex-shrink: 0;
  }
  .ctrl-btn svg { width: 20px; height: 20px; }

  /* Audio-only avatar */
  .audio-avatar { width: 88px; height: 88px; }
  .remote-audio-only p { font-size: 1.1rem; }
}

@media (max-width: 380px) {
  .ctrl-btn { min-width: 46px; padding: 9px 4px; font-size: .58rem; }
  .ctrl-btn svg { width: 18px; height: 18px; }
  .local-pip { width: 76px; height: 56px; bottom: 82px; }
}

/* ── TOASTS ────────────────────────────────────────── */
@media (max-width: 600px) {
  #toast-container {
    bottom: 12px; right: 12px; left: 12px;
    align-items: stretch;
  }
  .toast { max-width: 100%; }
}

/* ── SCROLLBARS ────────────────────────────────────── */
@media (max-width: 768px) {
  ::-webkit-scrollbar { width: 2px; height: 2px; }
}

/* ── SAFE AREA (notch/gesture bar phones) ──────────── */
@supports (padding: env(safe-area-inset-bottom)) {
  .call-controls {
    padding-bottom: calc(20px + env(safe-area-inset-bottom));
  }
  @media (max-width: 600px) {
    .call-controls {
      padding-bottom: calc(16px + env(safe-area-inset-bottom));
    }
  }
}

</style>
"""

SOUNDS_JS = """
<script>
const Sounds = (() => {
  let ctx = null, ringingInterval = null, callingInterval = null;
  function getCtx() {
    if (!ctx) ctx = new (window.AudioContext || window.webkitAudioContext)();
    if (ctx.state === 'suspended') ctx.resume();
    return ctx;
  }
  function beep(freq, duration, type = 'sine', volume = 0.3, delay = 0) {
    try {
      const c = getCtx(), osc = c.createOscillator(), gain = c.createGain();
      osc.connect(gain); gain.connect(c.destination); osc.type = type; osc.frequency.value = freq;
      const s = c.currentTime + delay;
      gain.gain.setValueAtTime(0, s); gain.gain.linearRampToValueAtTime(volume, s + 0.01);
      gain.gain.setValueAtTime(volume, s + duration - 0.05); gain.gain.linearRampToValueAtTime(0, s + duration);
      osc.start(s); osc.stop(s + duration);
    } catch(e) {}
  }
  function playRingTone() { beep(480,.4,'sine',.25,0); beep(620,.4,'sine',.2,0); beep(480,.4,'sine',.25,.6); beep(620,.4,'sine',.2,.6); }
  function playCallingTone() { beep(440,.6,'sine',.15,0); }
  return {
    startRinging() { this.stopRinging(); playRingTone(); ringingInterval = setInterval(playRingTone, 3000); },
    stopRinging() { if(ringingInterval) { clearInterval(ringingInterval); ringingInterval = null; } },
    startCalling() { this.stopCalling(); playCallingTone(); callingInterval = setInterval(playCallingTone, 3000); },
    stopCalling() { if(callingInterval) { clearInterval(callingInterval); callingInterval = null; } },
    accepted() { beep(523,.1,'sine',.3,0); beep(659,.1,'sine',.3,.12); beep(784,.25,'sine',.3,.24); },
    rejected() { beep(400,.15,'sine',.25,0); beep(320,.3,'sine',.2,.18); },
    ended() { beep(440,.1,'sine',.2,0); beep(350,.25,'sine',.15,.12); },
    notification() { beep(880,.07,'sine',.2,0); beep(1100,.12,'sine',.15,.09); },
    unlock() { try { getCtx(); } catch(e) {} },
  };
})();
['click','touchstart','keydown'].forEach(ev => document.addEventListener(ev, ()=>Sounds.unlock(), {once:true}));
</script>
"""

SOCKET_CLIENT_JS = """
<script src="https://cdnjs.cloudflare.com/ajax/libs/socket.io/4.7.5/socket.io.min.js"></script>
<script>
const SocketClient = (() => {
  let socket = null, heartbeatTimer = null;
  const listeners = {};
  function connect() {
    if (socket && socket.connected) return socket;
    socket = io({ transports: ['websocket','polling'], reconnection: true, reconnectionAttempts: 10, reconnectionDelay: 1000, reconnectionDelayMax: 8000, withCredentials: true });
    socket.on('connect', () => { startHeartbeat(); emit('connected', socket.id); });
    socket.on('disconnect', (reason) => { stopHeartbeat(); emit('disconnected', reason); });
    socket.on('reconnect', (attempt) => emit('reconnected', attempt));
    ['presence_update','incoming_call','call_ringing','call_accepted','call_rejected','call_cancelled','call_ended','call_error','call_accepted_ack','webrtc_offer','webrtc_answer','webrtc_ice','webrtc_ice_restart','friend_request','friend_accepted','heartbeat_ack'].forEach(ev => socket.on(ev, d => emit(ev, d)));
    return socket;
  }
  function startHeartbeat() { stopHeartbeat(); heartbeatTimer = setInterval(() => { if(socket && socket.connected) socket.emit('heartbeat'); }, 25000); }
  function stopHeartbeat() { if(heartbeatTimer) { clearInterval(heartbeatTimer); heartbeatTimer = null; } }
  function on(event, cb) { if(!listeners[event]) listeners[event] = []; listeners[event].push(cb); return () => off(event, cb); }
  function off(event, cb) { if(listeners[event]) listeners[event] = listeners[event].filter(l => l !== cb); }
  function emit(event, data) { (listeners[event] || []).forEach(cb => { try { cb(data); } catch(e) {} }); }
  function send(event, data) { if(socket && socket.connected) socket.emit(event, data); }
  function isConnected() { return socket && socket.connected; }
  return { connect, on, off, send, isConnected };
})();
window.addEventListener('DOMContentLoaded', () => { if(window.CURRENT_USER) SocketClient.connect(); });
</script>
"""

INCOMING_CALL_JS = """
<script>
const IncomingCall = (() => {
  let currentCall = null, autoRejectTimer = null;
  const overlay = document.getElementById('incoming-call-overlay');
  const avatar = document.getElementById('incoming-avatar');
  const nameEl = document.getElementById('incoming-name');
  const usernameEl = document.getElementById('incoming-username');
  const typeEl = document.getElementById('incoming-type');
  const btnAccept = document.getElementById('btn-accept-call');
  const btnReject = document.getElementById('btn-reject-call');

  function show(callData) {
    if (currentCall) { SocketClient.send('call_reject', { call_id: callData.call_id, reason: 'busy' }); return; }
    currentCall = callData;
    avatar.src = callData.caller.avatar_url;
    nameEl.textContent = callData.caller.display_name;
    usernameEl.textContent = '@' + callData.caller.username;
    typeEl.textContent = callData.call_type.charAt(0).toUpperCase() + callData.call_type.slice(1);
    overlay.classList.remove('hidden');
    Sounds.startRinging();
    autoRejectTimer = setTimeout(() => reject('timeout'), 45000);
  }
  function hide() {
    overlay.classList.add('hidden'); Sounds.stopRinging();
    if (autoRejectTimer) { clearTimeout(autoRejectTimer); autoRejectTimer = null; }
    currentCall = null;
  }
  function accept() {
    if (!currentCall) return;
    const call = currentCall;
    SocketClient.send('call_accept', { call_id: call.call_id });
    hide(); Sounds.accepted();
    window.location.href = `/call/${call.call_id}?type=${call.call_type}&peer=${call.caller.username}&role=callee`;
  }
  function reject(reason='rejected') {
    if (!currentCall) return;
    SocketClient.send('call_reject', { call_id: currentCall.call_id, reason });
    hide(); Sounds.rejected();
  }
  btnAccept && btnAccept.addEventListener('click', accept);
  btnReject && btnReject.addEventListener('click', () => reject('rejected'));
  SocketClient.on('incoming_call', show);
  SocketClient.on('call_cancelled', (data) => {
    if (currentCall && data.call_id === currentCall.call_id) { hide(); showToast(data.caller.display_name + ' cancelled the call.', 'info'); }
  });
  return { show, hide, accept, reject };
})();
</script>
"""

TOAST_JS = """
<script>
function showToast(message, type='info', duration=4000) {
  const container = document.getElementById('toast-container');
  if (!container) return;
  const el = document.createElement('div');
  el.className = 'toast ' + type; el.textContent = message;
  container.appendChild(el);
  setTimeout(() => { el.style.opacity='0'; el.style.transform='translateY(8px)'; el.style.transition='all .25s'; setTimeout(() => el.remove(), 280); }, duration);
}
</script>
"""

BASE_INCOMING_HTML = """
<div id="incoming-call-overlay" class="incoming-call-overlay hidden">
  <div class="incoming-call-card">
    <div class="incoming-avatar-ring">
      <img id="incoming-avatar" src="" alt="" class="incoming-avatar">
      <div class="ring ring1"></div><div class="ring ring2"></div><div class="ring ring3"></div>
    </div>
    <div class="incoming-info">
      <p class="incoming-label">Incoming <span id="incoming-type">Video</span> Call</p>
      <h2 id="incoming-name">Caller Name</h2>
      <p id="incoming-username" class="incoming-username">@username</p>
    </div>
    <div class="incoming-actions">
      <button id="btn-reject-call" class="btn-call-action reject" title="Reject">
        <svg viewBox="0 0 24 24"><path d="M20.01 15.38c-1.23 0-2.42-.2-3.53-.56a.977.977 0 00-1.01.24l-1.57 1.97c-2.83-1.35-5.48-3.9-6.89-6.83l1.95-1.66c.27-.28.35-.67.24-1.02-.37-1.12-.56-2.32-.56-3.55 0-.54-.45-.99-.99-.99H4.19C3.65 3 3 3.24 3 3.99 3 13.28 10.73 21 20.01 21c.71 0 .99-.63.99-1.18v-3.45c0-.54-.45-.99-.99-.99z"/></svg>
      </button>
      <button id="btn-accept-call" class="btn-call-action accept" title="Accept">
        <svg viewBox="0 0 24 24"><path d="M20.01 15.38c-1.23 0-2.42-.2-3.53-.56a.977.977 0 00-1.01.24l-1.57 1.97c-2.83-1.35-5.48-3.9-6.89-6.83l1.95-1.66c.27-.28.35-.67.24-1.02-.37-1.12-.56-2.32-.56-3.55 0-.54-.45-.99-.99-.99H4.19C3.65 3 3 3.24 3 3.99 3 13.28 10.73 21 20.01 21c.71 0 .99-.63.99-1.18v-3.45c0-.54-.45-.99-.99-.99z"/></svg>
      </button>
    </div>
  </div>
</div>
<div id="toast-container"></div>
"""

INDEX_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, viewport-fit=cover">
  <title>VoiceVid — Crystal-clear calls</title>
""" + CSS + """
</head>
<body>
<div class="auth-layout">
  <div class="auth-brand">
    <div class="brand-content">
      <div class="brand-logo">
        <svg viewBox="0 0 48 48" fill="none"><circle cx="24" cy="24" r="24" fill="url(#g1)"/><path d="M32 16H16a2 2 0 00-2 2v12a2 2 0 002 2h12l4 4V18a2 2 0 00-2-2z" fill="white" fill-opacity=".9"/><circle cx="19" cy="22" r="1.5" fill="#6366f1"/><circle cx="24" cy="22" r="1.5" fill="#6366f1"/><circle cx="29" cy="22" r="1.5" fill="#6366f1"/><defs><linearGradient id="g1" x1="0" y1="0" x2="48" y2="48" gradientUnits="userSpaceOnUse"><stop stop-color="#6366f1"/><stop offset="1" stop-color="#8b5cf6"/></linearGradient></defs></svg>
        <span>VoiceVid</span>
      </div>
      <h1 class="brand-headline">Crystal-clear<br><em>voice &amp; video</em><br>calls.</h1>
      <p class="brand-sub">Secure. Low-latency. Built on WebRTC.<br>Connect with anyone, anywhere.</p>
      <div class="brand-features">
        <div class="feature-pill"><span class="pill-dot"></span>End-to-end encrypted</div>
        <div class="feature-pill"><span class="pill-dot"></span>HD video &amp; audio</div>
        <div class="feature-pill"><span class="pill-dot"></span>Instant notifications</div>
      </div>
    </div>
    <div class="brand-orb brand-orb-1"></div>
    <div class="brand-orb brand-orb-2"></div>
  </div>
  <div class="auth-panel">
    <div class="auth-tabs">
      <button class="auth-tab active" data-tab="login">Sign in</button>
      <button class="auth-tab" data-tab="signup">Create account</button>
    </div>
    <form id="login-form" class="auth-form active">
      <div class="form-group">
        <label>Username or email</label>
        <input type="text" id="login-id" placeholder="yourname or email@example.com" required>
        <span class="field-error" id="login-id-err"></span>
      </div>
      <div class="form-group">
        <label>Password</label>
        <div class="password-wrap">
          <input type="password" id="login-password" placeholder="••••••••" required>
          <button type="button" class="toggle-pw"><svg class="eye-on" viewBox="0 0 24 24"><path d="M12 4.5C7 4.5 2.73 7.61 1 12c1.73 4.39 6 7.5 11 7.5s9.27-3.11 11-7.5c-1.73-4.39-6-7.5-11-7.5zM12 17c-2.76 0-5-2.24-5-5s2.24-5 5-5 5 2.24 5 5-2.24 5-5 5zm0-8c-1.66 0-3 1.34-3 3s1.34 3 3 3 3-1.34 3-3-1.34-3-3-3z"/></svg></button>
        </div>
      </div>
      <div class="form-error hidden" id="login-error"></div>
      <button type="submit" class="btn-primary btn-auth" id="login-btn">
        <span class="btn-text">Sign in</span><span class="btn-spinner hidden"></span>
      </button>
      <p class="auth-demo-hint">Demo: <code>alice / demo1234</code> or <code>bob / demo1234</code></p>
    </form>
    <form id="signup-form" class="auth-form">
      <div class="form-row">
        <div class="form-group">
          <label>Username</label>
          <input type="text" id="su-username" placeholder="cool_username" required>
          <span class="field-error" id="su-username-err"></span>
        </div>
        <div class="form-group">
          <label>Display name</label>
          <input type="text" id="su-display" placeholder="Your Name" required>
          <span class="field-error" id="su-display-err"></span>
        </div>
      </div>
      <div class="form-group">
        <label>Email</label>
        <input type="email" id="su-email" placeholder="you@example.com" required>
        <span class="field-error" id="su-email-err"></span>
      </div>
      <div class="form-group">
        <label>Password</label>
        <div class="password-wrap">
          <input type="password" id="su-password" placeholder="Min. 8 characters" required>
          <button type="button" class="toggle-pw"><svg viewBox="0 0 24 24"><path d="M12 4.5C7 4.5 2.73 7.61 1 12c1.73 4.39 6 7.5 11 7.5s9.27-3.11 11-7.5c-1.73-4.39-6-7.5-11-7.5zM12 17c-2.76 0-5-2.24-5-5s2.24-5 5-5 5 2.24 5 5-2.24 5-5 5zm0-8c-1.66 0-3 1.34-3 3s1.34 3 3 3 3-1.34 3-3-1.34-3-3-3z"/></svg></button>
        </div>
        <span class="field-error" id="su-pw-err"></span>
      </div>
      <div class="form-error hidden" id="signup-error"></div>
      <button type="submit" class="btn-primary btn-auth" id="signup-btn">
        <span class="btn-text">Create account</span><span class="btn-spinner hidden"></span>
      </button>
    </form>
  </div>
</div>
<script>
document.querySelectorAll('.auth-tab').forEach(tab => {
  tab.addEventListener('click', () => {
    document.querySelectorAll('.auth-tab').forEach(t=>t.classList.remove('active'));
    document.querySelectorAll('.auth-form').forEach(f=>f.classList.remove('active'));
    tab.classList.add('active');
    document.getElementById(tab.dataset.tab+'-form').classList.add('active');
  });
});
document.querySelectorAll('.toggle-pw').forEach(btn => {
  btn.addEventListener('click', () => { const inp=btn.previousElementSibling; inp.type = inp.type==='password'?'text':'password'; });
});
function setLoading(btn, loading) {
  btn.querySelector('.btn-text').classList.toggle('hidden', loading);
  btn.querySelector('.btn-spinner').classList.toggle('hidden', !loading);
  btn.disabled = loading;
}
function clearErrors(prefix) {
  document.querySelectorAll(`[id^="${prefix}-"][id$="-err"]`).forEach(el=>el.textContent='');
  const ge=document.getElementById(`${prefix}-error`); if(ge){ge.textContent='';ge.classList.add('hidden');}
}
document.getElementById('login-form').addEventListener('submit', async e => {
  e.preventDefault(); clearErrors('login');
  const btn=document.getElementById('login-btn'); setLoading(btn,true);
  const res=await fetch('/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},credentials:'include',body:JSON.stringify({username:document.getElementById('login-id').value.trim(),password:document.getElementById('login-password').value})});
  const data=await res.json(); setLoading(btn,false);
  if(data.ok) window.location.href='/dashboard';
  else { const err=document.getElementById('login-error'); err.textContent=data.error||'Login failed.'; err.classList.remove('hidden'); }
});
document.getElementById('signup-form').addEventListener('submit', async e => {
  e.preventDefault(); clearErrors('su');
  const btn=document.getElementById('signup-btn'); setLoading(btn,true);
  const res=await fetch('/auth/signup',{method:'POST',headers:{'Content-Type':'application/json'},credentials:'include',body:JSON.stringify({username:document.getElementById('su-username').value.trim(),display_name:document.getElementById('su-display').value.trim(),email:document.getElementById('su-email').value.trim(),password:document.getElementById('su-password').value})});
  const data=await res.json(); setLoading(btn,false);
  if(data.ok) window.location.href='/dashboard';
  else if(data.errors){const m={'username':'su-username-err','display_name':'su-display-err','email':'su-email-err','password':'su-pw-err'};Object.entries(data.errors).forEach(([k,v])=>{const el=document.getElementById(m[k]);if(el)el.textContent=v;});}
  else { const err=document.getElementById('signup-error'); err.textContent=data.error||'Signup failed.'; err.classList.remove('hidden'); }
});
</script>
</body></html>"""

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, viewport-fit=cover">
  <title>Dashboard — VoiceVid</title>
""" + CSS + """
</head>
<body>
<div class="app-layout">
  <!-- Mobile menu button -->
  <button class="mobile-menu-btn" id="mobile-menu-btn" aria-label="Open menu">
    <svg viewBox="0 0 24 24"><path d="M3 18h18v-2H3v2zm0-5h18v-2H3v2zm0-7v2h18V6H3z"/></svg>
  </button>
  <div class="sidebar-overlay" id="sidebar-overlay"></div>
  <aside class="sidebar" id="sidebar">
    <div class="sidebar-header">
      <div class="sidebar-logo">
        <svg viewBox="0 0 32 32" fill="none"><circle cx="16" cy="16" r="16" fill="url(#sg)"/><path d="M21 10H11a1.5 1.5 0 00-1.5 1.5v9l3-3H21a1.5 1.5 0 001.5-1.5v-6A1.5 1.5 0 0021 10z" fill="white" fill-opacity=".9"/><defs><linearGradient id="sg" x1="0" y1="0" x2="32" y2="32"><stop stop-color="#6366f1"/><stop offset="1" stop-color="#8b5cf6"/></linearGradient></defs></svg>
        <span>VoiceVid</span>
      </div>
    </div>
    <div class="sidebar-user">
      <div class="user-avatar-wrap">
        <img src="/api/avatar/{{ user.username }}" alt="{{ user.display_name }}" class="user-avatar">
        <span class="status-dot available" id="my-status-dot"></span>
      </div>
      <div class="user-info">
        <p class="user-display-name">{{ user.display_name }}</p>
        <p class="user-username">@{{ user.username }}</p>
      </div>
      <div class="user-actions">
        <select id="status-select" class="status-select">
          <option value="available">🟢 Available</option>
          <option value="away">🟡 Away</option>
          <option value="busy">🔴 Busy</option>
        </select>
      </div>
    </div>
    <nav class="sidebar-nav">
      <button class="nav-btn active" data-panel="contacts">
        <svg viewBox="0 0 24 24"><path d="M16 11c1.66 0 2.99-1.34 2.99-3S17.66 5 16 5c-1.66 0-3 1.34-3 3s1.34 3 3 3zm-8 0c1.66 0 2.99-1.34 2.99-3S9.66 5 8 5C6.34 5 5 6.34 5 8s1.34 3 3 3zm0 2c-2.33 0-7 1.17-7 3.5V19h14v-2.5c0-2.33-4.67-3.5-7-3.5zm8 0c-.29 0-.62.02-.97.05 1.16.84 1.97 1.97 1.97 3.45V19h6v-2.5c0-2.33-4.67-3.5-7-3.5z"/></svg>
        Contacts
      </button>
      <button class="nav-btn" data-panel="history">
        <svg viewBox="0 0 24 24"><path d="M13 3a9 9 0 00-9 9H1l3.89 3.89.07.14L9 12H6c0-3.87 3.13-7 7-7s7 3.13 7 7-3.13 7-7 7c-1.93 0-3.68-.79-4.94-2.06l-1.42 1.42A8.954 8.954 0 0013 21a9 9 0 000-18zm-1 5v5l4.28 2.54.72-1.21-3.5-2.08V8H12z"/></svg>
        Call History
        <span class="nav-badge hidden" id="missed-badge">0</span>
      </button>
      <button class="nav-btn" data-panel="search">
        <svg viewBox="0 0 24 24"><path d="M15.5 14h-.79l-.28-.27A6.471 6.471 0 0016 9.5 6.5 6.5 0 109.5 16c1.61 0 3.09-.59 4.23-1.57l.27.28v.79l5 4.99L20.49 19l-4.99-5zm-6 0C7.01 14 5 11.99 5 9.5S7.01 5 9.5 5 14 7.01 14 9.5 11.99 14 9.5 14z"/></svg>
        Find People
      </button>
    </nav>
    <button id="logout-btn" class="logout-btn">
      <svg viewBox="0 0 24 24"><path d="M17 7l-1.41 1.41L18.17 11H8v2h10.17l-2.58 2.58L17 17l5-5zM4 5h8V3H4c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h8v-2H4V5z"/></svg>
      Sign out
    </button>
  </aside>
  <main class="main-content">
    <div id="panel-contacts" class="panel active">
      <div class="panel-header">
        <h2>Contacts</h2>
        <button id="add-contact-btn" class="btn-icon" title="Add contact">
          <svg viewBox="0 0 24 24"><path d="M19 13h-6v6h-2v-6H5v-2h6V5h2v6h6v2z"/></svg>
        </button>
      </div>
      <div id="add-contact-form" class="add-contact-form hidden">
        <div class="search-bar">
          <svg viewBox="0 0 24 24"><path d="M15.5 14h-.79l-.28-.27A6.471 6.471 0 0016 9.5 6.5 6.5 0 109.5 16c1.61 0 3.09-.59 4.23-1.57l.27.28v.79l5 4.99L20.49 19l-4.99-5zm-6 0C7.01 14 5 11.99 5 9.5S7.01 5 9.5 5 14 7.01 14 9.5 11.99 14 9.5 14z"/></svg>
          <input type="text" id="contact-search-input" placeholder="Search by username or name…" autocomplete="off">
        </div>
        <div id="contact-search-results" class="contact-search-results"></div>
      </div>
      <div id="friend-requests-section" class="friend-requests-section hidden">
        <h3 class="section-title">Pending Requests</h3>
        <div id="friend-requests-list"></div>
      </div>
      <div id="contacts-list" class="contacts-list"><div class="loading-state"><div class="spinner"></div><p>Loading contacts…</p></div></div>
    </div>
    <div id="panel-history" class="panel">
      <div class="panel-header"><h2>Call History</h2></div>
      <div id="call-history-list" class="call-history-list"><div class="loading-state"><div class="spinner"></div><p>Loading…</p></div></div>
    </div>
    <div id="panel-search" class="panel">
      <div class="panel-header"><h2>Find People</h2></div>
      <div class="search-bar large">
        <svg viewBox="0 0 24 24"><path d="M15.5 14h-.79l-.28-.27A6.471 6.471 0 0016 9.5 6.5 6.5 0 109.5 16c1.61 0 3.09-.59 4.23-1.57l.27.28v.79l5 4.99L20.49 19l-4.99-5zm-6 0C7.01 14 5 11.99 5 9.5S7.01 5 9.5 5 14 7.01 14 9.5 11.99 14 9.5 14z"/></svg>
        <input type="text" id="global-search-input" placeholder="Search users…" autocomplete="off">
      </div>
      <div id="global-search-results" class="contact-search-results"></div>
    </div>
  </main>
</div>
<!-- Call modal -->
<div id="call-modal" class="modal hidden">
  <div class="modal-backdrop"></div>
  <div class="modal-card">
    <div class="modal-avatar-wrap">
      <img id="modal-avatar" src="" alt="" class="modal-avatar">
      <div class="calling-rings"><div class="calling-ring r1"></div><div class="calling-ring r2"></div></div>
    </div>
    <p class="modal-label" id="modal-status-label">Calling…</p>
    <h3 id="modal-name">Name</h3>
    <p id="modal-username" class="modal-username">@username</p>
    <div class="modal-actions">
      <button id="modal-end-btn" class="btn-call-action reject large">
        <svg viewBox="0 0 24 24"><path d="M20.01 15.38c-1.23 0-2.42-.2-3.53-.56a.977.977 0 00-1.01.24l-1.57 1.97c-2.83-1.35-5.48-3.9-6.89-6.83l1.95-1.66c.27-.28.35-.67.24-1.02-.37-1.12-.56-2.32-.56-3.55 0-.54-.45-.99-.99-.99H4.19C3.65 3 3 3.24 3 3.99 3 13.28 10.73 21 20.01 21c.71 0 .99-.63.99-1.18v-3.45c0-.54-.45-.99-.99-.99z"/></svg>
      </button>
    </div>
  </div>
</div>
""" + BASE_INCOMING_HTML + """
<script>
window.CURRENT_USER = { id: {{ user.id }}, username: "{{ user.username }}", display_name: "{{ user.display_name }}" };
</script>
""" + SOUNDS_JS + SOCKET_CLIENT_JS + TOAST_JS + INCOMING_CALL_JS + """
<script>
let contacts = [], currentCallId = null, currentCallee = null;

// Mobile sidebar
const sidebar = document.getElementById('sidebar');
const overlay = document.getElementById('sidebar-overlay');
document.getElementById('mobile-menu-btn').addEventListener('click', () => {
  sidebar.classList.add('open'); overlay.classList.add('show');
});
overlay.addEventListener('click', () => { sidebar.classList.remove('open'); overlay.classList.remove('show'); });
function closeSidebar() { sidebar.classList.remove('open'); overlay.classList.remove('show'); }

// Panel switching
document.querySelectorAll('.nav-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.nav-btn').forEach(b=>b.classList.remove('active'));
    document.querySelectorAll('.panel').forEach(p=>p.classList.remove('active'));
    btn.classList.add('active');
    const panel = document.getElementById('panel-' + btn.dataset.panel);
    if (panel) panel.classList.add('active');
    if (btn.dataset.panel === 'history') loadCallHistory();
    closeSidebar();
  });
});

// Status
document.getElementById('status-select').addEventListener('change', e => {
  SocketClient.send('set_status', { status: e.target.value });
  const dot = document.getElementById('my-status-dot');
  dot.className = 'status-dot ' + e.target.value;
});

// Logout
document.getElementById('logout-btn').addEventListener('click', async () => {
  await fetch('/auth/logout', { method: 'POST', credentials: 'include' });
  window.location.href = '/';
});

function escHtml(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

// Contacts
async function loadContacts() {
  const list = document.getElementById('contacts-list');
  try {
    const res = await fetch('/api/friends', { credentials: 'include' });
    contacts = await res.json();
    renderContacts(contacts);
    loadFriendRequests();
  } catch(e) {
    list.innerHTML = '<div class="empty-state"><p>Failed to load contacts.</p></div>';
  }
}

function renderContacts(users) {
  const list = document.getElementById('contacts-list');
  if (!users.length) {
    list.innerHTML = '<div class="empty-state"><svg viewBox="0 0 24 24"><path d="M16 11c1.66 0 2.99-1.34 2.99-3S17.66 5 16 5c-1.66 0-3 1.34-3 3s1.34 3 3 3zm-8 0c1.66 0 2.99-1.34 2.99-3S9.66 5 8 5C6.34 5 5 6.34 5 8s1.34 3 3 3zm0 2c-2.33 0-7 1.17-7 3.5V19h14v-2.5c0-2.33-4.67-3.5-7-3.5zm8 0c-.29 0-.62.02-.97.05 1.16.84 1.97 1.97 1.97 3.45V19h6v-2.5c0-2.33-4.67-3.5-7-3.5z"/></svg><p>No contacts yet. Search for people to add!</p></div>';
    return;
  }
  list.innerHTML = users.map(u => contactCardHTML(u)).join('');
}

function contactCardHTML(u) {
  const statusLabel = u.is_online ? (u.status || 'available') : 'offline';
  const canCall = u.is_online && u.status !== 'busy';
  const disabledAttr = canCall ? '' : 'disabled style="opacity:.35;cursor:not-allowed"';
  return `<div class="contact-card" id="contact-${u.username}">
    <img src="${u.avatar_url}" alt="${escHtml(u.display_name)}" class="contact-avatar">
    <div class="contact-info">
      <p class="contact-name">${escHtml(u.display_name)}</p>
      <p class="contact-username">@${escHtml(u.username)}</p>
      <p class="contact-status ${statusLabel}">${statusLabel}</p>
    </div>
    <div class="contact-actions">
      <button class="contact-btn voice" onclick="initiateCall('${u.username}','voice')" title="Voice call" ${disabledAttr}>
        <svg viewBox="0 0 24 24"><path d="M20.01 15.38c-1.23 0-2.42-.2-3.53-.56a.977.977 0 00-1.01.24l-1.57 1.97c-2.83-1.35-5.48-3.9-6.89-6.83l1.95-1.66c.27-.28.35-.67.24-1.02-.37-1.12-.56-2.32-.56-3.55 0-.54-.45-.99-.99-.99H4.19C3.65 3 3 3.24 3 3.99 3 13.28 10.73 21 20.01 21c.71 0 .99-.63.99-1.18v-3.45c0-.54-.45-.99-.99-.99z"/></svg>
      </button>
      <button class="contact-btn video" onclick="initiateCall('${u.username}','video')" title="Video call" ${disabledAttr}>
        <svg viewBox="0 0 24 24"><path d="M17 10.5V7c0-.55-.45-1-1-1H4c-.55 0-1 .45-1 1v10c0 .55.45 1 1 1h12c.55 0 1-.45 1-1v-3.5l4 4v-11l-4 4z"/></svg>
      </button>
    </div>
  </div>`;
}

// Presence updates
SocketClient.on('presence_update', (data) => {
  const card = document.getElementById('contact-' + data.username);
  if (card) {
    const statusEl = card.querySelector('.contact-status');
    const status = data.is_online ? (data.status || 'available') : 'offline';
    statusEl.textContent = status; statusEl.className = 'contact-status ' + status;
    const canCall = data.is_online && data.status !== 'busy';
    card.querySelectorAll('.contact-btn').forEach(btn => {
      btn.disabled = !canCall; btn.style.opacity = canCall ? '1' : '.35'; btn.style.cursor = canCall ? 'pointer' : 'not-allowed';
    });
    const c = contacts.find(x => x.username === data.username);
    if (c) { c.is_online = data.is_online; c.status = data.status; }
  }
});

// Friend requests
async function loadFriendRequests() {
  const res = await fetch('/api/friends/requests', { credentials: 'include' });
  const requests = await res.json();
  const section = document.getElementById('friend-requests-section');
  const list = document.getElementById('friend-requests-list');
  if (!requests.length) { section.classList.add('hidden'); return; }
  section.classList.remove('hidden');
  list.innerHTML = requests.map(r => `
    <div class="friend-request-card" id="fr-${r.id}">
      <img src="${r.sender.avatar_url}" alt="" class="contact-avatar" style="width:36px;height:36px;">
      <div style="flex:1"><p style="font-size:.875rem;font-weight:600">${escHtml(r.sender.display_name)}</p><p style="font-size:.75rem;color:var(--text-3)">@${escHtml(r.sender.username)}</p></div>
      <div class="fr-actions">
        <button class="fr-btn accept" onclick="respondFriend(${r.id},'accept')">Accept</button>
        <button class="fr-btn reject" onclick="respondFriend(${r.id},'reject')">Decline</button>
      </div>
    </div>`).join('');
}

async function respondFriend(id, action) {
  await fetch('/api/friends/respond', { method: 'POST', headers: {'Content-Type':'application/json'}, credentials: 'include', body: JSON.stringify({ friendship_id: id, action }) });
  document.getElementById('fr-' + id)?.remove();
  if (!document.querySelector('.friend-request-card')) document.getElementById('friend-requests-section').classList.add('hidden');
  if (action === 'accept') loadContacts();
}

SocketClient.on('friend_request', (data) => { showToast(data.sender.display_name + ' sent you a friend request!', 'info'); loadFriendRequests(); Sounds.notification(); });
SocketClient.on('friend_accepted', (data) => { showToast(data.user.display_name + ' accepted your friend request!', 'success'); loadContacts(); });

// Add contact
document.getElementById('add-contact-btn').addEventListener('click', () => {
  document.getElementById('add-contact-form').classList.toggle('hidden');
});

let searchTimeout = null;
document.getElementById('contact-search-input').addEventListener('input', e => {
  clearTimeout(searchTimeout); const q = e.target.value.trim();
  if (q.length < 2) { document.getElementById('contact-search-results').innerHTML = ''; return; }
  searchTimeout = setTimeout(() => searchContacts(q, 'contact-search-results'), 300);
});
document.getElementById('global-search-input').addEventListener('input', e => {
  clearTimeout(searchTimeout); const q = e.target.value.trim();
  if (q.length < 2) { document.getElementById('global-search-results').innerHTML = ''; return; }
  searchTimeout = setTimeout(() => searchContacts(q, 'global-search-results'), 300);
});

async function searchContacts(q, containerId) {
  const container = document.getElementById(containerId);
  container.innerHTML = '<div class="loading-state" style="padding:16px"><div class="spinner"></div></div>';
  const res = await fetch('/api/users/search?q=' + encodeURIComponent(q), { credentials: 'include' });
  const users = await res.json();
  if (!users.length) { container.innerHTML = '<div class="empty-state" style="padding:16px"><p>No users found.</p></div>'; return; }
  container.innerHTML = users.map(u => `
    <div class="contact-card">
      <img src="${u.avatar_url}" alt="" class="contact-avatar">
      <div class="contact-info"><p class="contact-name">${escHtml(u.display_name)}</p><p class="contact-username">@${escHtml(u.username)}</p></div>
      <div class="contact-actions">
        <button class="contact-btn add" onclick="addContact('${u.username}')" title="Add contact">
          <svg viewBox="0 0 24 24"><path d="M19 13h-6v6h-2v-6H5v-2h6V5h2v6h6v2z"/></svg>
        </button>
      </div>
    </div>`).join('');
}

async function addContact(username) {
  const res = await fetch('/api/friends/add', { method: 'POST', headers: {'Content-Type':'application/json'}, credentials: 'include', body: JSON.stringify({ username }) });
  const data = await res.json();
  if (data.ok) showToast('Friend request sent!', 'success');
  else showToast(data.error || 'Could not send request.', 'error');
}

// Call history
async function loadCallHistory() {
  const list = document.getElementById('call-history-list');
  try {
    const res = await fetch('/api/calls/history', { credentials: 'include' });
    const data = await res.json();
    if (!data.calls.length) { list.innerHTML = '<div class="empty-state"><p>No calls yet.</p></div>'; return; }
    list.innerHTML = data.calls.map(c => {
      const isCaller = c.caller && c.caller.id === window.CURRENT_USER.id;
      const peer = isCaller ? c.callee : c.caller;
      const iconClass = c.status === 'missed' ? 'missed' : (isCaller ? 'outgoing' : 'incoming');
      const date = c.started_at ? new Date(c.started_at).toLocaleString() : '';
      const dur = c.duration_seconds ? Math.floor(c.duration_seconds/60)+'m '+Math.floor(c.duration_seconds%60)+'s' : c.status;
      return `<div class="history-card">
        <div class="history-icon ${iconClass}">
          <svg viewBox="0 0 24 24"><path d="M20.01 15.38c-1.23 0-2.42-.2-3.53-.56a.977.977 0 00-1.01.24l-1.57 1.97c-2.83-1.35-5.48-3.9-6.89-6.83l1.95-1.66c.27-.28.35-.67.24-1.02-.37-1.12-.56-2.32-.56-3.55 0-.54-.45-.99-.99-.99H4.19C3.65 3 3 3.24 3 3.99 3 13.28 10.73 21 20.01 21c.71 0 .99-.63.99-1.18v-3.45c0-.54-.45-.99-.99-.99z"/></svg>
        </div>
        <div class="history-info">
          <p class="history-name">${escHtml(peer ? peer.display_name : '?')}</p>
          <p class="history-meta">${date} · ${dur}</p>
        </div>
        <div class="history-type">${c.call_type}</div>
      </div>`;
    }).join('');
  } catch(e) { list.innerHTML = '<div class="empty-state"><p>Failed to load history.</p></div>'; }
}

// Initiate call
function initiateCall(username, callType) {
  currentCallee = username;
  const contact = contacts.find(c => c.username === username) || { display_name: username, avatar_url: '/api/avatar/' + username };
  document.getElementById('modal-avatar').src = contact.avatar_url;
  document.getElementById('modal-name').textContent = contact.display_name;
  document.getElementById('modal-username').textContent = '@' + username;
  document.getElementById('modal-status-label').textContent = 'Calling…';
  document.getElementById('call-modal').classList.remove('hidden');
  Sounds.startCalling();
  SocketClient.send('call_initiate', { callee_username: username, call_type: callType });
}

document.getElementById('modal-end-btn').addEventListener('click', () => {
  if (currentCallId) SocketClient.send('call_cancel', { call_id: currentCallId });
  document.getElementById('call-modal').classList.add('hidden');
  Sounds.stopCalling(); currentCallId = null;
});

SocketClient.on('call_ringing', (data) => {
  currentCallId = data.call_id;
  document.getElementById('modal-status-label').textContent = 'Ringing…';
});

SocketClient.on('call_accepted', (data) => {
  Sounds.stopCalling(); Sounds.accepted();
  document.getElementById('call-modal').classList.add('hidden');
  const callData = data;
  const call = { call_id: data.call_id };
  // Get call type from modal context — find from current state
  const callType = 'video'; // default; ideally track it
  window.location.href = '/call/' + data.call_id + '?type=video&peer=' + (data.callee ? data.callee.username : currentCallee) + '&role=caller';
});

SocketClient.on('call_rejected', (data) => {
  Sounds.stopCalling(); Sounds.rejected();
  document.getElementById('call-modal').classList.add('hidden');
  showToast('Call was declined.', 'error'); currentCallId = null;
});

SocketClient.on('call_error', (data) => {
  Sounds.stopCalling();
  document.getElementById('call-modal').classList.add('hidden');
  showToast(data.message || 'Call failed.', 'error'); currentCallId = null;
});

// Load on start
loadContacts();
fetch('/api/calls/missed-count', { credentials: 'include' }).then(r=>r.json()).then(d => {
  if (d.count > 0) { const b = document.getElementById('missed-badge'); b.textContent = d.count; b.classList.remove('hidden'); }
});
</script>
</body></html>"""

CALL_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, viewport-fit=cover">
  <title>Call — VoiceVid</title>
""" + CSS + """
</head>
<body>
<div class="call-layout" id="call-layout" data-call-id="{{ call_id }}">
  <video id="remote-video" class="remote-video" autoplay playsinline></video>
  <div id="remote-audio-only" class="remote-audio-only hidden">
    <div class="audio-avatar-wrap">
      <img id="remote-avatar-img" src="" alt="" class="audio-avatar">
      <div class="audio-rings"><div class="audio-ring ar1"></div><div class="audio-ring ar2"></div><div class="audio-ring ar3"></div></div>
    </div>
    <p id="remote-user-name">Connecting…</p>
  </div>
  <div id="call-status-overlay" class="call-status-overlay">
    <div class="status-content"><div class="spinner large"></div><p id="call-status-text">Connecting…</p></div>
  </div>
  <div class="call-topbar">
    <div class="call-info">
      <img id="call-peer-avatar" src="" alt="" class="call-peer-avatar">
      <div><p id="call-peer-name" class="call-peer-name">—</p><p id="call-timer" class="call-timer">00:00</p></div>
    </div>
    <div class="call-quality" id="call-quality">
      <span class="quality-bar q1"></span><span class="quality-bar q2"></span><span class="quality-bar q3"></span><span class="quality-bar q4"></span>
    </div>
  </div>
  <div class="local-pip" id="local-pip">
    <video id="local-video" class="local-video" autoplay playsinline muted></video>
    <div id="local-cam-off" class="pip-cam-off hidden">
      <svg viewBox="0 0 24 24"><path d="M21 6.5l-4-4-9.83 9.83 4 4L21 6.5z"/></svg>
    </div>
  </div>
  <div class="call-controls" id="call-controls">
    <button id="ctrl-mute" class="ctrl-btn">
      <svg id="icon-mic-on" viewBox="0 0 24 24"><path d="M12 14c1.66 0 2.99-1.34 2.99-3L15 5c0-1.66-1.34-3-3-3S9 3.34 9 5v6c0 1.66 1.34 3 3 3zm5.3-3c0 3-2.54 5.1-5.3 5.1S6.7 14 6.7 11H5c0 3.41 2.72 6.23 6 6.72V21h2v-3.28c3.28-.48 6-3.3 6-6.72h-1.7z"/></svg>
      <svg id="icon-mic-off" class="hidden" viewBox="0 0 24 24"><path d="M19 11h-1.7c0 .74-.16 1.43-.43 2.05l1.23 1.23c.56-.98.9-2.09.9-3.28zm-4.02.17c0-.06.02-.11.02-.17V5c0-1.66-1.34-3-3-3S9 3.34 9 5v.18l5.98 5.99zM4.27 3L3 4.27l6.01 6.01V11c0 1.66 1.33 3 2.99 3 .22 0 .44-.03.65-.08l1.66 1.66c-.71.33-1.5.52-2.31.52-2.76 0-5.3-2.1-5.3-5.1H5c0 3.41 2.72 6.23 6 6.72V21h2v-3.28c.91-.13 1.77-.45 2.54-.9L19.73 21 21 19.73 4.27 3z"/></svg>
      <span id="mute-label">Mute</span>
    </button>
    <button id="ctrl-camera" class="ctrl-btn">
      <svg id="icon-cam-on" viewBox="0 0 24 24"><path d="M17 10.5V7c0-.55-.45-1-1-1H4c-.55 0-1 .45-1 1v10c0 .55.45 1 1 1h12c.55 0 1-.45 1-1v-3.5l4 4v-11l-4 4z"/></svg>
      <svg id="icon-cam-off" class="hidden" viewBox="0 0 24 24"><path d="M21 6.5l-4-4-9.83 9.83 4 4L21 6.5z"/></svg>
      <span id="cam-label">Camera</span>
    </button>
    <button id="ctrl-end" class="ctrl-btn end-btn">
      <svg viewBox="0 0 24 24"><path d="M20.01 15.38c-1.23 0-2.42-.2-3.53-.56a.977.977 0 00-1.01.24l-1.57 1.97c-2.83-1.35-5.48-3.9-6.89-6.83l1.95-1.66c.27-.28.35-.67.24-1.02-.37-1.12-.56-2.32-.56-3.55 0-.54-.45-.99-.99-.99H4.19C3.65 3 3 3.24 3 3.99 3 13.28 10.73 21 20.01 21c.71 0 .99-.63.99-1.18v-3.45c0-.54-.45-.99-.99-.99z"/></svg>
      <span>End</span>
    </button>
  </div>
  <div id="reconnecting-overlay" class="reconnecting-overlay hidden"><div class="spinner large"></div><p>Reconnecting…</p></div>
</div>
""" + BASE_INCOMING_HTML + """
<script>
window.CURRENT_USER = { id: {{ user.id }}, username: "{{ user.username }}", display_name: "{{ user.display_name }}" };
window.CALL_ID = "{{ call_id }}";
</script>
""" + SOUNDS_JS + SOCKET_CLIENT_JS + TOAST_JS + INCOMING_CALL_JS + """
<script>
// WebRTC Engine
const WebRTCEngine = (() => {
  let pc=null, localStream=null, remoteStream=null, callId=null, callType=null, role=null;
  let iceConfig=null, makingOffer=false, ignoreOffer=false, polite=false;
  let iceRestartTimer=null, reconnectAttempts=0;
  const MAX_RECONNECT=5;

  async function fetchIceConfig() {
    try { const r=await fetch('/api/ice-config',{credentials:'include'}); return await r.json(); }
    catch { return {iceServers:[{urls:'stun:stun.l.google.com:19302'}]}; }
  }
  function createPC() {
    if(pc){pc.close();pc=null;}
    pc=new RTCPeerConnection(iceConfig);
    pc.ontrack = evt => {
      if(!remoteStream){remoteStream=new MediaStream(); setRemoteStream(remoteStream);}
      evt.streams[0].getTracks().forEach(t=>remoteStream.addTrack(t));
    };
    pc.onicecandidate = ({candidate}) => { if(candidate) SocketClient.send('webrtc_ice',{call_id:callId,candidate}); };
    pc.oniceconnectionstatechange = () => {
      const s=pc.iceConnectionState;
      if(s==='connected'||s==='completed'){reconnectAttempts=0;clearTimer();updateState('connected');}
      else if(s==='disconnected'){updateState('reconnecting');scheduleRestart();}
      else if(s==='failed'){updateState('failed');handleFailed();}
    };
    pc.onnegotiationneeded = async () => {
      if(role!=='caller')return;
      try { makingOffer=true; const o=await pc.createOffer(); if(pc.signalingState!=='stable')return; await pc.setLocalDescription(o); SocketClient.send('webrtc_offer',{call_id:callId,offer:pc.localDescription}); }
      catch(e){console.error(e);}finally{makingOffer=false;}
    };
    return pc;
  }
  async function acquireMedia() {
    // getUserMedia sirf HTTPS ya localhost pe kaam karta hai
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      const msg = location.protocol !== 'https:' && location.hostname !== 'localhost' && location.hostname !== '127.0.0.1'
        ? 'Camera/mic requires HTTPS. Please open via https:// or localhost.'
        : 'Your browser does not support getUserMedia.';
      document.getElementById('call-status-text').textContent = msg;
      document.getElementById('call-status-overlay').classList.remove('hidden');
      document.getElementById('call-status-overlay').style.opacity = '1';
      return false;
    }
    try {
      localStream=await navigator.mediaDevices.getUserMedia({
        audio:{echoCancellation:true,noiseSuppression:true,autoGainControl:true},
        video: callType==='video'?{width:{ideal:1280},height:{ideal:720},frameRate:{ideal:30},facingMode:'user'}:false
      });
      setLocalStream(localStream); return true;
    } catch(e) {
      let msg = 'Media error: ' + e.message;
      if (e.name==='NotAllowedError') msg = 'Camera/microphone permission denied.';
      else if (e.name==='NotFoundError') {
        // video nahi mila, sirf audio try karo
        if (callType==='video') {
          try {
            localStream = await navigator.mediaDevices.getUserMedia({ audio: {echoCancellation:true,noiseSuppression:true}, video: false });
            setLocalStream(localStream); return true;
          } catch {}
        }
        msg = 'No camera or microphone found.';
      }
      document.getElementById('call-status-text').textContent = msg;
      return false;
    }
  }
  async function initAsCaller(cId,cType){callId=cId;callType=cType;role='caller';polite=false;iceConfig=await fetchIceConfig();const ok=await acquireMedia();if(!ok)return;createPC();localStream.getTracks().forEach(t=>pc.addTrack(t,localStream));}
  async function initAsCallee(cId,cType){callId=cId;callType=cType;role='callee';polite=true;iceConfig=await fetchIceConfig();const ok=await acquireMedia();if(!ok)return;createPC();localStream.getTracks().forEach(t=>pc.addTrack(t,localStream));}

  async function handleOffer(offer){
    if(!pc)return;
    const coll=makingOffer||pc.signalingState!=='stable'; ignoreOffer=!polite&&coll; if(ignoreOffer)return;
    try{if(coll){await Promise.all([pc.setLocalDescription({type:'rollback'}),pc.setRemoteDescription(offer)]);}else{await pc.setRemoteDescription(offer);}
    const ans=await pc.createAnswer(); await pc.setLocalDescription(ans); SocketClient.send('webrtc_answer',{call_id:callId,answer:pc.localDescription});}
    catch(e){console.error(e);}
  }
  async function handleAnswer(answer){if(!pc||pc.signalingState!=='have-local-offer')return;try{await pc.setRemoteDescription(answer);}catch(e){console.error(e);}}
  async function handleICE(candidate){if(!pc)return;try{await pc.addIceCandidate(candidate);}catch(e){if(!ignoreOffer)console.error(e);}}

  function scheduleRestart(){clearTimer();iceRestartTimer=setTimeout(async()=>{if(reconnectAttempts>=MAX_RECONNECT){endCall('failed');return;}reconnectAttempts++;try{if(role==='caller'&&pc){const o=await pc.createOffer({iceRestart:true});await pc.setLocalDescription(o);SocketClient.send('webrtc_offer',{call_id:callId,offer:pc.localDescription});}else{SocketClient.send('webrtc_ice_restart',{call_id:callId});}}catch(e){}},3000);}
  function clearTimer(){if(iceRestartTimer){clearTimeout(iceRestartTimer);iceRestartTimer=null;}}
  function handleFailed(){if(reconnectAttempts>=MAX_RECONNECT)endCall('failed');else{updateState('reconnecting');scheduleRestart();}}

  function toggleMute(){if(!localStream)return false;const t=localStream.getAudioTracks()[0];if(t){t.enabled=!t.enabled;return!t.enabled;}return false;}
  function toggleCamera(){if(!localStream)return false;const t=localStream.getVideoTracks()[0];if(t){t.enabled=!t.enabled;return!t.enabled;}return false;}

  function endCall(reason='ended'){clearTimer();SocketClient.send('call_end',{call_id:callId});cleanup();Sounds.ended();showEnded(reason);}
  function cleanup(){if(localStream){localStream.getTracks().forEach(t=>t.stop());localStream=null;}if(pc){pc.close();pc=null;}remoteStream=null;}

  function updateState(state){
    const overlay=document.getElementById('call-status-overlay');
    const recon=document.getElementById('reconnecting-overlay');
    if(state==='connected'){overlay.style.opacity='0';setTimeout(()=>overlay.classList.add('hidden'),400);recon.classList.add('hidden');startTimer();}
    else if(state==='reconnecting'){recon.classList.remove('hidden');}
    else if(state==='acquiring'){document.getElementById('call-status-text').textContent='Acquiring media…';}
  }
  let timerStart=null,timerInterval=null;
  function startTimer(){if(timerStart)return;timerStart=Date.now();timerInterval=setInterval(()=>{const s=Math.floor((Date.now()-timerStart)/1000);const m=Math.floor(s/60).toString().padStart(2,'0');const sec=(s%60).toString().padStart(2,'0');document.getElementById('call-timer').textContent=m+':'+sec;},1000);}

  function setLocalStream(stream){const lv=document.getElementById('local-video');lv.srcObject=stream;const hasCam=stream.getVideoTracks().length>0&&stream.getVideoTracks()[0].enabled;if(!hasCam||callType==='voice'){document.getElementById('local-cam-off').classList.remove('hidden');lv.classList.add('hidden');}else{document.getElementById('local-cam-off').classList.add('hidden');lv.classList.remove('hidden');}}
  function setRemoteStream(stream){if(callType==='video'){const rv=document.getElementById('remote-video');rv.srcObject=stream;rv.style.display='block';document.getElementById('remote-audio-only').classList.add('hidden');}else{const a=document.createElement('audio');a.srcObject=stream;a.autoplay=true;a.style.display='none';document.body.appendChild(a);}}
  function showEnded(reason){document.getElementById('call-status-overlay').classList.remove('hidden');document.getElementById('call-status-overlay').style.opacity='1';document.getElementById('call-status-text').textContent='Call ended'+(reason&&reason!=='ended'?' ('+reason+')':'');setTimeout(()=>{window.location.href='/dashboard';},2500);}

  SocketClient.on('webrtc_offer',async d=>{if(d.call_id!==callId)return;await handleOffer(d.offer);});
  SocketClient.on('webrtc_answer',async d=>{if(d.call_id!==callId)return;await handleAnswer(d.answer);});
  SocketClient.on('webrtc_ice',async d=>{if(d.call_id!==callId||!d.candidate)return;await handleICE(d.candidate);});
  SocketClient.on('webrtc_ice_restart',async d=>{if(d.call_id!==callId||role!=='caller')return;try{const o=await pc.createOffer({iceRestart:true});await pc.setLocalDescription(o);SocketClient.send('webrtc_offer',{call_id:callId,offer:pc.localDescription});}catch(e){}});
  SocketClient.on('call_ended',d=>{if(d.call_id!==callId)return;cleanup();Sounds.ended();showEnded(d.reason||'ended');});
  window.addEventListener('beforeunload',()=>{if(callId&&pc)SocketClient.send('call_end',{call_id:callId});});

  return {initAsCaller,initAsCallee,toggleMute,toggleCamera,endCall};
})();

// Call UI init
(async () => {
  const params=new URLSearchParams(window.location.search);
  const role=params.get('role')||'caller';
  const peer=params.get('peer')||'';
  const callType=params.get('type')||'video';
  const callId=window.CALL_ID;

  if(callType==='voice'){
    document.getElementById('ctrl-camera').style.display='none';
    document.getElementById('remote-video').style.display='none';
    document.getElementById('remote-audio-only').classList.remove('hidden');
    document.getElementById('local-pip').style.display='none';
  }
  try{const r=await fetch('/api/users/'+peer,{credentials:'include'});const u=await r.json();document.getElementById('call-peer-name').textContent=u.display_name;document.getElementById('call-peer-avatar').src=u.avatar_url;document.getElementById('remote-avatar-img').src=u.avatar_url;document.getElementById('remote-user-name').textContent=u.display_name;}catch{}

  SocketClient.connect();
  await new Promise(r=>SocketClient.on('connected',r));
  if(role==='caller') await WebRTCEngine.initAsCaller(callId,callType);
  else await WebRTCEngine.initAsCallee(callId,callType);

  // Controls
  let isMuted=false,isCamOff=false;
  document.getElementById('ctrl-mute').addEventListener('click',()=>{
    isMuted=WebRTCEngine.toggleMute();
    document.getElementById('ctrl-mute').classList.toggle('muted',isMuted);
    document.getElementById('icon-mic-on').classList.toggle('hidden',isMuted);
    document.getElementById('icon-mic-off').classList.toggle('hidden',!isMuted);
    document.getElementById('mute-label').textContent=isMuted?'Unmute':'Mute';
  });
  document.getElementById('ctrl-camera').addEventListener('click',()=>{
    isCamOff=WebRTCEngine.toggleCamera();
    document.getElementById('icon-cam-on').classList.toggle('hidden',isCamOff);
    document.getElementById('icon-cam-off').classList.toggle('hidden',!isCamOff);
    document.getElementById('cam-label').textContent=isCamOff?'Camera Off':'Camera';
    document.getElementById('local-cam-off').classList.toggle('hidden',!isCamOff);
    document.getElementById('local-video').classList.toggle('hidden',isCamOff);
  });
  document.getElementById('ctrl-end').addEventListener('click',()=>WebRTCEngine.endCall('ended'));
})();
</script>
</body></html>"""

# ── DB Init + Seed ─────────────────────────────────────────────────────────────
with app.app_context():
    db.create_all()
    if User.query.count() == 0:
        for username, display_name, email, password in [
            ('alice', 'Alice Johnson', 'alice@demo.com', 'demo1234'),
            ('bob', 'Bob Smith', 'bob@demo.com', 'demo1234'),
        ]:
            pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
            db.session.add(User(username=username, display_name=display_name, email=email, password_hash=pw_hash))
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    debug = not _is_prod  # debug=True locally, False on Render
    print(f"[VoiceVid] async_mode={_ASYNC_MODE}  debug={debug}  port={port}")
    print(f"[VoiceVid] Starting on http://0.0.0.0:{port}")
    socketio.run(
        app,
        host='0.0.0.0',
        port=port,
        debug=debug,
        # allow_unsafe_werkzeug only needed when debug=True with threading
        allow_unsafe_werkzeug=(_ASYNC_MODE == 'threading'),
    )

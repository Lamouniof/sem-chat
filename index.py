import json
import os
import socket
import time
from flask import Flask, render_template, request
from flask_socketio import SocketIO, emit, join_room
from zeroconf import ServiceInfo, Zeroconf

import firebase_admin
from firebase_admin import credentials, auth as fb_auth

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'chat_ultra_pro_2026')
socketio = SocketIO(app, cors_allowed_origins="*", allow_unsafe_werkzeug=True, max_http_buffer_size=50 * 1024 * 1024)

DATA_FILE = 'chat_data.json'
ADMIN_EMAIL = os.environ.get('ADMIN_EMAIL', 'TON_EMAIL_ADMIN@exemple.com')

data_storage = {
    "users": {},           # uid -> pseudo
    "general_history": [],
    "private_history": {},
    "leaderboard": []
}

# Initialisation Firebase
firebase_creds_json = os.environ.get('FIREBASE_SERVICE_ACCOUNT')
if firebase_creds_json:
    cred = credentials.Certificate(json.loads(firebase_creds_json))
else:
    cred_path = os.environ.get('FIREBASE_CREDENTIALS_PATH', 'firebase-service-account.json')
    cred = credentials.Certificate(cred_path)

firebase_admin.initialize_app(cred)

# Session active : sid -> {"uid": str, "pseudo": str, "email": str, "is_admin": bool, "last_seen": float}
active_sessions = {}


def load_data():
    global data_storage
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, 'r', encoding='utf-8') as f:
                loaded = json.load(f)
                for key in data_storage:
                    if key in loaded:
                        data_storage[key] = loaded[key]
        except Exception:
            pass


def save_data():
    with open(DATA_FILE, 'w', encoding='utf-8') as f:
        json.dump(data_storage, f, indent=4, ensure_ascii=False)


load_data()


@app.route('/')
def index():
    return render_template('index.html')


@socketio.on('login_register')
def handle_auth(data):
    token = data.get('token', '')
    requested_pseudo = (data.get('pseudo') or '').strip()

    try:
        decoded = fb_auth.verify_id_token(token)
    except Exception:
        emit('auth_response', {'success': False, 'message': 'Session invalide, reconnecte-toi.'})
        return

    uid = decoded['uid']
    email = decoded.get('email', '')
    pseudo = requested_pseudo or email or uid
    is_admin = (email == ADMIN_EMAIL)

    # Empêcher la double connexion
    if any(sess['uid'] == uid for sess in active_sessions.values()):
        emit('auth_response', {'success': False, 'message': 'Ce compte est déjà connecté sur un autre appareil.'})
        return

    data_storage["users"][uid] = pseudo
    save_data()

    # Enregistrement de la session liée au sid unique de la connexion WebSocket
    active_sessions[request.sid] = {
        "uid": uid,
        "pseudo": pseudo,
        "email": email,
        "is_admin": is_admin,
        "last_seen": time.time()
    }

    join_room(pseudo)

    emit('auth_response', {'success': True, 'pseudo': pseudo, 'is_admin': is_admin})
    emit('load_history', data_storage["general_history"])
    emit('update_users', [s["pseudo"] for s in active_sessions.values()], broadcast=True)


@socketio.on('message')
def handle_message(data):
    session = active_sessions.get(request.sid)
    if not session:
        return

    # Utilisation du pseudo authentifié pour éviter l'usurpation
    sender_pseudo = session["pseudo"]
    target = data.get('target', 'Général')

    msg_payload = {
        'id': int(time.time() * 1000),
        'user': sender_pseudo,
        'text': data.get('text', ''),
        'target': target
    }

    if target == 'Général':
        data_storage["general_history"].append(msg_payload)
        if len(data_storage["general_history"]) > 100:
            data_storage["general_history"].pop(0)
        emit('message', msg_payload, broadcast=True)
    else:
        room_key = "-".join(sorted([sender_pseudo, target]))
        if room_key not in data_storage["private_history"]:
            data_storage["private_history"][room_key] = []
        data_storage["private_history"][room_key].append(msg_payload)
        emit('private_message', msg_payload, room=target)
        emit('private_message', msg_payload, room=sender_pseudo)

    save_data()


@socketio.on('delete_message')
def delete_message(msg_id, *args):
    session = active_sessions.get(request.sid)
    # Vérification stricte du rôle admin serveur
    if not session or not session.get('is_admin'):
        return

    data_storage["general_history"] = [m for m in data_storage["general_history"] if m.get('id') != msg_id]
    for key in data_storage["private_history"]:
        data_storage["private_history"][key] = [m for m in data_storage["private_history"][key] if m.get('id') != msg_id]

    save_data()
    emit('message_deleted', msg_id, broadcast=True)


@socketio.on('ban_user')
def ban_user(data):
    session = active_sessions.get(request.sid)
    # Vérification stricte du rôle admin serveur
    if not session or not session.get('is_admin'):
        return

    target_pseudo = data.get('target')
    target_uid = next((u for u, p in data_storage["users"].items() if p == target_pseudo), None)

    if target_uid:
        del data_storage["users"][target_uid]

        # Déconnexion forcée des sessions actives de la cible
        sids_to_remove = [sid for sid, s in active_sessions.items() if s["uid"] == target_uid]
        for sid in sids_to_remove:
            del active_sessions[sid]

        save_data()

        try:
            fb_auth.revoke_refresh_tokens(target_uid)
        except Exception:
            pass

        emit('user_banned_notice', target_pseudo, broadcast=True)
        emit('update_users', [s["pseudo"] for s in active_sessions.values()], broadcast=True)


@socketio.on('disconnect')
def handle_disconnect():
    if request.sid in active_sessions:
        del active_sessions[request.sid]
        emit('update_users', [s["pseudo"] for s in active_sessions.values()], broadcast=True)
@socketio.on('get_private_history')
def send_private_history(data):
    session = active_sessions.get(request.sid)
    if not session:
        return

    user_pseudo = session["pseudo"]  # Récupéré depuis la session serveur sécurisée
    target = data.get('target')

    if not target:
        return

    # Clé de salon unique alphabétique (ex: "Alice-Bob")
    room_key = "-".join(sorted([user_pseudo, target]))
    history = data_storage["private_history"].get(room_key, [])
    
    emit('load_private_history', {'target': target, 'history': history})

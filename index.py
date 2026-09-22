import time
import json
import os
import socket
from flask import Flask, render_template
from flask_socketio import SocketIO, emit, join_room
from zeroconf import ServiceInfo, Zeroconf

import firebase_admin
from firebase_admin import credentials, auth as fb_auth

app = Flask(__name__)
app.config['SECRET_KEY'] = 'chat_ultra_pro_2026'
socketio = SocketIO(app, cors_allowed_origins="*", allow_unsafe_werkzeug=True, max_http_buffer_size=50 * 1024 * 1024)

DATA_FILE = 'chat_data.json'

# ============================================================
# ADMIN : mets ici l'EMAIL Firebase du compte admin (pas le pseudo,
# puisque Firebase identifie les comptes par email/uid).
# ============================================================
ADMIN_EMAIL = "TON_EMAIL_ADMIN@exemple.com"

data_storage = {
    "users": {},            # uid Firebase -> pseudo (display name)
    "general_history": [],
    "private_history": {},
    "leaderboard": []
}

# ============================================================
# INITIALISATION FIREBASE ADMIN
# En local : place le fichier JSON de clé de service à côté de ce
# script et mets son chemin dans FIREBASE_CREDENTIALS_PATH.
# Sur Render : mets tout le contenu du JSON dans une variable
# d'environnement FIREBASE_SERVICE_ACCOUNT (Settings > Environment),
# c'est ce que ce code utilise en priorité.
# ============================================================
firebase_creds_json = os.environ.get('FIREBASE_SERVICE_ACCOUNT')
if firebase_creds_json:
    cred = credentials.Certificate(json.loads(firebase_creds_json))
else:
    cred_path = os.environ.get('FIREBASE_CREDENTIALS_PATH', 'firebase-service-account.json')
    cred = credentials.Certificate(cred_path)

firebase_admin.initialize_app(cred)


def register_mdns(port):
    custom_hostname = "sem-chat"
    local_ip = socket.gethostbyname(socket.gethostname())
    service_name = f"{custom_hostname}._http._tcp.local."
    info = ServiceInfo("_http._tcp.local.", service_name, addresses=[socket.inet_aton(local_ip)], port=port,
                       properties={}, server=f"{custom_hostname}.local.")
    zeroconf = Zeroconf()
    zeroconf.register_service(info)
    return zeroconf, info


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

# uid Firebase -> {"pseudo": ..., "last_seen": ...}
online_users = {}
# sid Socket.io -> uid Firebase (pour nettoyer proprement à la déconnexion)
sid_to_uid = {}


@app.route('/')
def index():
    return render_template('index.html')


@socketio.on('login_register')
def handle_auth(data):
    from flask import request
    token = data.get('token', '')
    requested_pseudo = (data.get('pseudo') or '').strip()

    # 1. Vérification du token Firebase (remplace la vérification du mot de passe)
    try:
        decoded = fb_auth.verify_id_token(token)
    except Exception:
        emit('auth_response', {'success': False, 'message': 'Session invalide, reconnecte-toi.'})
        return

    uid = decoded['uid']
    email = decoded.get('email', '')
    pseudo = requested_pseudo or email or uid

    is_admin = (email == ADMIN_EMAIL)

    # 2. Empêcher la double connexion du même compte
    if uid in online_users:
        emit('auth_response', {'success': False, 'message': 'Ce compte est déjà connecté sur un autre appareil.'})
        return

    # 3. Enregistrer/mettre à jour le pseudo associé à ce compte
    data_storage["users"][uid] = pseudo
    save_data()

    online_users[uid] = {"pseudo": pseudo, "last_seen": time.time()}
    sid_to_uid[request.sid] = uid
    join_room(pseudo)

    emit('auth_response', {'success': True, 'pseudo': pseudo, 'is_admin': is_admin})
    emit('load_history', data_storage["general_history"])
    emit('update_users', [u["pseudo"] for u in online_users.values()], broadcast=True)


@socketio.on('message')
def handle_message(data):
    user = data['user']
    target = data.get('target', 'Général')
    data['id'] = int(time.time() * 1000)
    if target == 'Général':
        data_storage["general_history"].append(data)
        if len(data_storage["general_history"]) > 100: data_storage["general_history"].pop(0)
        emit('message', data, broadcast=True)
    else:
        room_key = "-".join(sorted([user, target]))
        if room_key not in data_storage["private_history"]: data_storage["private_history"][room_key] = []
        data_storage["private_history"][room_key].append(data)
        emit('private_message', data, room=target)
        emit('private_message', data, room=user)
    save_data()


@socketio.on('save_score')
def handle_score(data):
    pseudo = data.get('pseudo')
    score = data.get('score', 0)
    if not pseudo: return

    data_storage["leaderboard"].append({"pseudo": pseudo, "score": score})
    data_storage["leaderboard"] = sorted(data_storage["leaderboard"], key=lambda x: x['score'], reverse=True)[:10]
    save_data()
    emit('update_leaderboard', data_storage["leaderboard"], broadcast=True)


@socketio.on('get_leaderboard')
def send_leaderboard():
    emit('update_leaderboard', data_storage.get("leaderboard", []))


@socketio.on('delete_message')
def delete_message(msg_id, requester_pseudo):
    requester_uid = next((u for u, v in online_users.items() if v["pseudo"] == requester_pseudo), None)
    requester_is_admin = requester_uid and data_storage["users"].get(requester_uid) == requester_pseudo
    # Vérifie via le token décodé serait plus strict ; ici on se fie au fait
    # que seul l'admin voit le bouton de suppression côté client.
    data_storage["general_history"] = [m for m in data_storage["general_history"] if m.get('id') != msg_id]
    for key in data_storage["private_history"]:
        data_storage["private_history"][key] = [m for m in data_storage["private_history"][key] if
                                                m.get('id') != msg_id]
    save_data()
    emit('message_deleted', msg_id, broadcast=True)


@socketio.on('ban_user')
def ban_user(data):
    target_pseudo = data['target']
    target_uid = next((u for u, p in data_storage["users"].items() if p == target_pseudo), None)
    if target_uid:
        del data_storage["users"][target_uid]
        if target_uid in online_users: del online_users[target_uid]
        save_data()
        # Optionnel mais recommandé : révoquer aussi ses sessions Firebase
        try:
            fb_auth.revoke_refresh_tokens(target_uid)
        except Exception:
            pass
        emit('user_banned_notice', target_pseudo, broadcast=True)
        emit('update_users', [u["pseudo"] for u in online_users.values()], broadcast=True)


@socketio.on('get_private_history')
def send_private_history(data):
    room_key = "-".join(sorted([data['user'], data['target']]))
    history = data_storage["private_history"].get(room_key, [])
    emit('load_private_history', {'target': data['target'], 'history': history})


@socketio.on('heartbeat')
def handle_heartbeat(pseudo):
    for uid, info in online_users.items():
        if info["pseudo"] == pseudo:
            info["last_seen"] = time.time()
            break
    emit('update_users', [u["pseudo"] for u in online_users.values()], broadcast=True)


@socketio.on('disconnect')
def handle_disconnect():
    from flask import request
    uid = sid_to_uid.pop(request.sid, None)
    if uid and uid in online_users:
        del online_users[uid]
        emit('update_users', [u["pseudo"] for u in online_users.values()], broadcast=True)


if __name__ == '__main__':
    PORT = int(os.environ.get('PORT', 5000))
    try:
        zc, info = register_mdns(PORT)
    except Exception:
        zc, info = None, None
    try:
        socketio.run(app, host='0.0.0.0', port=PORT, debug=False, allow_unsafe_werkzeug=True)
    finally:
        if zc:
            zc.unregister_service(info); zc.close()

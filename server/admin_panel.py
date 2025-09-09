# admin_panel.py (renamed or new file)
from flask import Flask, render_template, request, jsonify
import json
import os
import struct
import inspect
import threading
import time
from BitBuffer import BitBuffer
from entity import Send_Entity_Data

app = Flask(__name__)

DATA_FOLDER = "data"
PACKETS_FILE = os.path.join(DATA_FOLDER, "packet_types.json")
ENT_FILE = os.path.join(DATA_FOLDER, "EntTypes.json")

packet_loop_event = threading.Event()  # For packet loop stop signal
npc_loop_event = threading.Event()     # For NPC loop stop signal

if not os.path.exists(DATA_FOLDER):
    os.makedirs(DATA_FOLDER)

# Initialize packets JSON file if missing
if not os.path.exists(PACKETS_FILE):
    with open(PACKETS_FILE, "w") as f:
        json.dump({}, f, indent=4)

# Load NPC list for cycling
npc_list = []
if os.path.exists(ENT_FILE):
    try:
        with open(ENT_FILE, "r") as f:
            raw = json.load(f)
            if isinstance(raw, list):
                npc_list = [ent.get("EntName", "") for ent in raw if "EntName" in ent]
            elif isinstance(raw, dict):
                npc_list = [raw.get("EntName", "")]
    except Exception as e:
        print(f"[Admin] Failed to load EntTypes.json: {e}")

packets_data = {}
with open(PACKETS_FILE, "r") as f:
    packets_data = json.load(f)

method_suggestions = sorted(
    name for name, fn in inspect.getmembers(BitBuffer, predicate=inspect.isfunction) if name.startswith("write_")
)

sessions_getter = None  # Will be set externally


def build_custom_packet(method_calls, pkt_type):
    bb = BitBuffer(debug=True)
    for method_name, args in method_calls:
        method = getattr(bb, method_name, None)
        if not method:
            raise ValueError(f"Unknown BitBuffer method: {method_name}")
        if isinstance(args, (tuple, list)):
            method(*args)
        else:
            method(args)
    payload = bb.to_bytes()
    header = struct.pack(">HH", pkt_type, len(payload))
    return header + payload


def get_free_entity_id():
    used_ids = set()
    for session in list(sessions_getter()):
        used_ids.update(session.entities.keys())
    candidate = 20000
    while candidate in used_ids:
        candidate += 1
    return candidate

@app.route('/active_players', methods=['GET'])
def active_players():
    players = []
    for session in list(sessions_getter()):
        if hasattr(session, 'current_character') and session.current_character:
            players.append(session.current_character)
    return jsonify(players)


@app.route('/')
def index():
    return render_template('admin_panel.html',
                           saved_packets=list(packets_data.keys()),
                           method_suggestions=method_suggestions,
                           npc_list=npc_list)


@app.route('/load_packet', methods=['POST'])
def load_packet():
    name = request.json.get('name')
    if name in packets_data:
        return jsonify(packets_data[name])
    return jsonify({'error': 'Packet not found'}), 404


@app.route('/save_packet', methods=['POST'])
def save_packet():
    data = request.json
    name = data.get('name')
    if not name:
        return jsonify({'error': 'Name required'}), 400
    packets_data[name] = {
        "packet_type": data['packet_type'],
        "description": data['description'],
        "buffers": data['buffers']
    }
    with open(PACKETS_FILE, "w") as f:
        json.dump(packets_data, f, indent=4)
    return jsonify({'success': True, 'saved_packets': list(packets_data.keys())})


@app.route('/send_packet', methods=['POST'])
def send_packet():
    data = request.json
    pkt_type = int(data['packet_type'], 16)
    method_calls = []
    for buf in data['buffers']:
        method = buf['method'].strip()
        val_str = buf['value'].strip()
        if not method or not val_str:
            continue
        parts = [p.strip() for p in val_str.split(",")]
        args = []
        for p in parts:
            if p == "":
                continue
            try:
                if "." in p:
                    args.append(float(p))
                else:
                    args.append(int(p))
            except ValueError:
                args.append(p)
        if len(args) == 1:
            args = args[0]
        method_calls.append((method, args))
    if not method_calls:
        return jsonify({'error': 'No buffers to send'})
    packet = build_custom_packet(method_calls, pkt_type)

    def send_once():
        sent_count = 0
        target_name = data.get('target_player', '').strip().lower()
        for session in list(sessions_getter()):
            if target_name and session.current_character.lower() != target_name:
                continue
            try:
                session.conn.sendall(packet)
                sent_count += 1
            except:
                pass
        return sent_count, target_name

    if not data.get('loop', False):
        sent_count, target_name = send_once()
        target_msg = f" to player '{target_name}'" if target_name else " to all clients"
        return jsonify({'success': True, 'message': f'Packet 0x{pkt_type:X} sent to {sent_count} clients{target_msg}.'})
    else:
        def loop_send():
            delay = float(data.get('delay', 1.0))
            packet_loop_event.clear()  # Reset event to not-stopped
            while not packet_loop_event.is_set():
                send_once()
                time.sleep(delay)

        threading.Thread(target=loop_send, daemon=True).start()
        target_msg = f" for player '{data.get('target_player', '')}'" if data.get('target_player',
                                                                                  '') else " for all clients"
        return jsonify({'success': True, 'message': f'Looping packet started{target_msg}. Use Stop button to halt.'})

@app.route('/stop_packet_loop', methods=['POST'])
def stop_packet_loop():
    packet_loop_event.set()  # Signal stop
    return jsonify({'success': True, 'message': 'Packet loop stopped.'})


@app.route('/spawn_npc', methods=['POST'])
def spawn_npc():
    data = request.json
    target_name = data.get('target_player', '').strip().lower()

    def do_spawn_once(current_name=None):
        x_val = int(data.get('x', 0))
        y_val = int(data.get('y', 0))

        # Only replace coordinates that are 0
        for session in list(sessions_getter()):
            if target_name and session.current_character.lower() != target_name:
                continue
            if session.clientEntID and session.clientEntID in session.entities:
                player_entity = session.entities[session.clientEntID]
                if x_val == 0:
                    px = player_entity.get("pos_x")
                    if px is not None:
                        x_val = int(px)
                if y_val == 0:
                    py = player_entity.get("pos_y")
                    if py is not None:
                        y_val = int(py)
                break  # Stop after finding the first matching player

        # Determine NPC ID
        try:
            requested_id = int(data.get('id', '0'))
        except ValueError:
            requested_id = 0

        npc_id = get_free_entity_id() if requested_id == 0 else requested_id

        # Build the NPC
        npc = {
            "id": npc_id,
            "name": current_name or data.get('name', 'FirePriestBossHard'),
            "x": x_val,
            "y": y_val,
            "v": int(data.get('v', 0)),
            "team": int(data.get('team', 2)),
            "untargetable": data.get('untargetable', False),
            "render_depth_offset": int(data.get('render_depth_offset', 0)),
            "behavior_speed": float(data.get('behavior_speed', 0)),
            "Linked_Mission": data.get('Linked_Mission', ''),
            "DramaAnim": data.get('DramaAnim', ''),
            "SleepAnim": data.get('SleepAnim', ''),
            "summonerId": int(data.get('summonerId', 0)),
            "power_id": int(data.get('power_id', 0)),
            "entState": int(data.get('entState', 0)),
            "facing_left": data.get('facing_left', False),
            "health_delta": int(data.get('health_delta', 0)),
            "buffs": [],
            "max_hp": 100,
            "mount_id": 1,
            "buff_icon": 1,
            "is_player": False
        }

        payload = Send_Entity_Data(npc)
        packet = struct.pack(">HH", 0x0F, len(payload)) + payload
        sent_count = 0

        for session in list(sessions_getter()):
            try:
                session.conn.sendall(packet)
                # Track entity in session
                session.entities[npc_id] = {"pos_x": x_val, "pos_y": y_val}
                sent_count += 1
            except:
                pass

        return sent_count, npc['name']

    if not data.get('loop', False):
        sent_count, npc_name = do_spawn_once()
        return jsonify({'success': True, 'message': f'Spawned NPC {npc_name} to {sent_count} clients.'})
    else:
        def loop_spawn():
            start_index = data.get('start_index', '')
            idx = int(start_index) if start_index and start_index.strip() else 0
            delay = float(data.get('delay', 2))
            npc_loop_event.clear()  # Reset event to not-stopped
            while not npc_loop_event.is_set():
                current_name = None
                if data.get('cycle', False) and npc_list:
                    current_name = npc_list[idx % len(npc_list)]
                    idx += 1
                do_spawn_once(current_name)
                time.sleep(delay)

        threading.Thread(target=loop_spawn, daemon=True).start()
        return jsonify({'success': True, 'message': 'Looping NPC spawn started. Use Stop button to halt.'})

@app.route('/stop_npc_loop', methods=['POST'])
def stop_npc_loop():
    npc_loop_event.set()  # Signal stop
    return jsonify({'success': True, 'message': 'NPC loop stopped.'})


@app.route('/save_npc_to_json', methods=['POST'])
def save_npc_to_json():
    data = request.json
    json_path = data.get('json_path')
    if not json_path:
        return jsonify({'error': 'JSON path required'}), 400
    target_name = data.get('target_player', '').strip().lower()
    x_val = int(data.get('x', 0))
    y_val = int(data.get('y', 0))
    if x_val == 0 and y_val == 0:
        for session in list(sessions_getter()):
            if target_name and session.current_character.lower() != target_name:
                continue
            if session.clientEntID and session.clientEntID in session.entities:
                player_entity = session.entities[session.clientEntID]
                px = player_entity.get("pos_x")
                py = player_entity.get("pos_y")
                if px is not None and py is not None:
                    x_val = int(px)
                    y_val = int(py)
                    break
    try:
        requested_id = int(data.get('id', '0'))
    except ValueError:
        requested_id = 0
    npc_id = get_free_entity_id() if requested_id == 0 else (
        requested_id if not any(requested_id in s.entities for s in list(sessions_getter())) else get_free_entity_id()
    )
    npc = {
        "id": npc_id,
        "name": data.get('name', 'FirePriestBossHard'),
        "x": x_val,
        "y": y_val,
        "v": int(data.get('v', 0)),
        "team": int(data.get('team', 2)),
        "entState": int(data.get('entState', 0)),
        "untargetable": data.get('untargetable', False),
        "render_depth_offset": int(data.get('render_depth_offset', 0)),
        "behavior_speed": float(data.get('behavior_speed', 0)),
        "Linked_Mission": data.get('Linked_Mission', ''),
        "DramaAnim": data.get('DramaAnim', ''),
        "SleepAnim": data.get('SleepAnim', ''),
        "summonerId": int(data.get('summonerId', 0)),
        "power_id": int(data.get('power_id', 0)),
        "facing_left": data.get('facing_left', False),
        "health_delta": int(data.get('health_delta', 0)),
        "buffs": [],
    }
    if os.path.exists(json_path):
        with open(json_path, "r") as f:
            file_data = json.load(f)
            if not isinstance(file_data, list):
                file_data = []
    else:
        file_data = []
    file_data.append(npc)
    with open(json_path, "w") as f:
        json.dump(file_data, f, indent=2)
    return jsonify({'success': True, 'message': f'NPC saved to {json_path}'})

@app.route('/delete_packet', methods=['POST'])
def delete_packet():
    name = request.json.get('name')
    if not name:
        return jsonify({'error': 'Name required'}), 400
    if name not in packets_data:
        return jsonify({'error': 'Packet not found'}), 404
    del packets_data[name]
    with open(PACKETS_FILE, "w") as f:
        json.dump(packets_data, f, indent=4)
    return jsonify({'success': True, 'saved_packets': list(packets_data.keys())})

def run_admin_panel(getter, port=5000):
    global sessions_getter
    sessions_getter = getter
    app.run(host='127.0.0.1', port=port, debug=True, use_reloader=False)
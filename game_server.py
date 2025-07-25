from flask import Flask,  request, send_from_directory
from flask_socketio import SocketIO, join_room, leave_room, emit
import time
from threading import Lock
import random

game_server = Flask(__name__)
game_server.config['SECRET_KEY'] = 'your-secret-key-here'
socketio = SocketIO(game_server, async_mode='eventlet')

# Game data (in-memory for simplicity)
rooms = {}  # {room_code: [sids]}
room_states = {}  # {room_code: "waiting" | "playing" | "post_game" | "ended"}
player_scores = {}  # {username: score}
room_locks = {}  # {room_code: Lock}
room_settings = {}  # {room_code: settings}
players_in_game = {}  # {room_code: {sid: {name: username}}}

# Default settings
ROUND_TIME = 45  # Seconds per round
WORD_LIST = ["apple", "house", "car", "tree", "cat", "dog", "sun", "moon", "bird", "fish"]

def safe_room_operation(room_code, operation):
    """Ensure thread-safe room operations."""
    if room_code not in room_locks:
        room_locks[room_code] = Lock()
    with room_locks[room_code]:
        return operation()

@game_server.route('/')
def index():
    return "Skribble Multiplayer Game Server"

@game_server.route('/client_front.html')
def serve_index():
    return send_from_directory('.', 'client_front.html')

@socketio.on('connect')
def handle_connect():
    print(f"New connection: {request.sid}")
    emit('connection_success', {'sid': request.sid})

@socketio.on('create_room')
def handle_create_room(data):
    room_code = data.get('room_code')
    username = data.get('username', f"Player_{request.sid[:4]}")
    if not room_code or room_code in rooms:
        emit('error', {'message': "Room code invalid or taken!"})
        return
    safe_room_operation(room_code, lambda: rooms.__setitem__(room_code, []))
    room_states[room_code] = "waiting"
    players_in_game[room_code] = {}
    room_settings[room_code] = {
        'round_time': ROUND_TIME,
        'word_list': WORD_LIST.copy(),
        'used_words': [],
        'current_word': None,
        'drawer': None,
        'current_round': 0,
        'drawn_players': [],
        'guesses': {},  # {sid: {'count': int, 'correct': bool, 'time': float}}
        'round_start_time': None,
        'continue_choices': {}  # {sid: bool} to track continue/leave decisions
    }
    join_room(room_code)
    safe_room_operation(room_code, lambda: rooms[room_code].append(request.sid))
    players_in_game[room_code][request.sid] = {'name': username}
    emit('room_created', {'room_code': room_code}, room=room_code)
    emit('player_list', {'players': players_in_game[room_code]}, room=room_code)
    print(f"Room {room_code} created by {username}")

@socketio.on('join_room')
def handle_join_room(data):
    room_code = data.get('room_code')
    username = data.get('username', f"Player_{request.sid[:4]}")
    if not room_code or room_code not in rooms:
        emit('error', {'message': "Room doesn’t exist!"})
        return
    if request.sid in rooms[room_code]:
        emit('error', {'message': "Already in this room!"})
        return
    safe_room_operation(room_code, lambda: rooms[room_code].append(request.sid))
    join_room(room_code)
    players_in_game[room_code][request.sid] = {'name': username}
    emit('player_joined', {'sid': request.sid, 'room_code': room_code, 'name': username}, room=room_code)
    emit('player_list', {'players': players_in_game[room_code]}, room=room_code)
    print(f"Player {username} joined {room_code}")

@socketio.on('leave_room')
def handle_leave_room(data):
    room_code = data.get('room_code')
    if not room_code or room_code not in rooms or request.sid not in rooms[room_code]:
        return
    safe_room_operation(room_code, lambda: rooms[room_code].remove(request.sid))
    leave_room(room_code)
    player_name = players_in_game[room_code].pop(request.sid, {}).get('name', 'Unknown')
    emit('player_left', {'sid': request.sid, 'name': player_name}, room=room_code)
    emit('player_list', {'players': players_in_game[room_code]}, room=room_code)
    if not rooms[room_code]:
        del rooms[room_code]
        del room_states[room_code]
        del room_locks[room_code]
        del room_settings[room_code]
        del players_in_game[room_code]
        print(f"Room {room_code} deleted (empty)")
    elif room_states.get(room_code) == "playing" and len(rooms[room_code]) < 2:
        emit('end_game', {'reason': 'Not enough players!'}, room=room_code)
        room_states[room_code] = "ended"

@socketio.on('draw')
def handle_draw(data):
    room_code = data.get('room_code')
    x, y, color = data.get('x'), data.get('y'), data.get('color')
    brush_size = data.get('brushSize', 5)
    if not all([room_code, x is not None, y is not None, color]) or room_code not in rooms:
        return
    if room_states.get(room_code) != "playing" or request.sid != room_settings[room_code]['drawer']:
        return
    emit('draw_update', {'x': x, 'y': y, 'color': color, 'brushSize': brush_size if color != 'clear' else None}, room=room_code)

@socketio.on('chat')
def handle_chat(data):
    room_code = data.get('room_code')
    message = data.get('message')
    if not room_code or not message or room_code not in rooms:
        return
    player_sid = request.sid
    player_name = players_in_game[room_code][player_sid]['name']
    emit('chat_message', {'message': message, 'sid': player_sid, 'name': player_name}, room=room_code)
    
    if room_states[room_code] == "playing" and player_sid != room_settings[room_code]['drawer']:
        guesses = room_settings[room_code]['guesses'].get(player_sid, {'count': 0, 'correct': False, 'time': None})
        if guesses['correct']:
            return
        if guesses['count'] >= 3:
            emit('chat_message', {'message': f"{player_name}, no more guesses!", 'sid': 'System'}, to=player_sid)
            return
        guesses['count'] += 1
        guesses['time'] = time.time() - room_settings[room_code]['round_start_time']
        room_settings[room_code]['guesses'][player_sid] = guesses
        if message.lower() == room_settings[room_code]['current_word'].lower():
            guesses['correct'] = True
            handle_correct_guess(room_code, player_sid, player_name, guesses['time'])
            check_all_guessed(room_code)

@socketio.on('select_word')
def handle_select_word(data):
    room_code = data.get('room_code')
    word = data.get('word')
    if room_code not in rooms or request.sid != room_settings[room_code]['drawer']:
        return
    room_settings[room_code]['current_word'] = word
    drawer_name = players_in_game[room_code][request.sid]['name']
    emit('game_started', {'round_time': ROUND_TIME, 'drawer': request.sid, 'drawer_name': drawer_name, 'word': word, 'round': room_settings[room_code]['current_round']}, to=request.sid)
    emit('game_started', {'round_time': ROUND_TIME, 'drawer': request.sid, 'drawer_name': drawer_name, 'word': '-' * len(word), 'round': room_settings[room_code]['current_round']}, room=room_code, include_self=False)
    socketio.start_background_task(game_timer, room_code)

def handle_correct_guess(room_code, player_sid, player_name, guess_time):
    score = max(0, 50 - int(guess_time))  # Faster guesses = more points
    player_scores[player_name] = player_scores.get(player_name, 0) + score
    emit('update_scores', {'scores': player_scores}, room=room_code)
    emit('chat_message', {'message': f"{player_name} guessed it in {int(guess_time)}s! (+{score})", 'sid': 'System'}, room=room_code)

def check_all_guessed(room_code):
    all_guessed = all(room_settings[room_code]['guesses'].get(sid, {}).get('correct', False) 
                      for sid in rooms[room_code] if sid != room_settings[room_code]['drawer'])
    if all_guessed:
        emit('chat_message', {'message': "Everyone guessed it! Next round...", 'sid': 'System'}, room=room_code)
        start_next_round(room_code)

def game_timer(room_code):
    room_settings[room_code]['round_start_time'] = time.time()
    total_seconds = ROUND_TIME
    while time.time() - room_settings[room_code]['round_start_time'] < total_seconds and room_states.get(room_code) == "playing":
        time_left = total_seconds - (time.time() - room_settings[room_code]['round_start_time'])
        emit('time_left', {'time_left': int(time_left)}, room=room_code)
        time.sleep(1)
    if room_states.get(room_code) == "playing":
        emit('chat_message', {'message': "Time’s up! Next round...", 'sid': 'System'}, room=room_code)
        start_next_round(room_code)

@socketio.on('start_game')
def start_game(data):
    room_code = data.get('room_code')
    if not room_code or room_code not in rooms or room_states.get(room_code) == "playing" or len(rooms[room_code]) < 2:
        emit('error', {'message': "Can’t start game! Check room or players."})
        return
    room_states[room_code] = "playing"
    room_settings[room_code]['current_round'] = 1
    start_round(room_code)

@socketio.on('continue_choice')
def handle_continue_choice(data):
    room_code = data.get('room_code')
    continue_game = data.get('continue', False)
    if not room_code or room_code not in rooms or room_states[room_code] != "post_game":
        return
    safe_room_operation(room_code, lambda: room_settings[room_code]['continue_choices'].__setitem__(request.sid, continue_game))
    player_name = players_in_game[room_code][request.sid]['name']
    emit('chat_message', {'message': f"{player_name} chose to {'continue' if continue_game else 'leave'}!", 'sid': 'System'}, room=room_code)
    
    if not continue_game:
        # Remove player immediately
        safe_room_operation(room_code, lambda: rooms[room_code].remove(request.sid))
        leave_room(room_code)
        player_name = players_in_game[room_code].pop(request.sid, {}).get('name', 'Unknown')
        emit('player_left', {'sid': request.sid, 'name': player_name}, room=room_code)
        emit('player_list', {'players': players_in_game[room_code]}, room=room_code)
        if not rooms[room_code]:
            del rooms[room_code]
            del room_states[room_code]
            del room_locks[room_code]
            del room_settings[room_code]
            del players_in_game[room_code]
            print(f"Room {room_code} deleted (empty)")
    
    # Check if all players have made a choice
    if len(room_settings[room_code]['continue_choices']) == len(rooms[room_code]):
        process_game_continuation(room_code)

def process_game_continuation(room_code):
    if room_code not in rooms or room_states[room_code] != "post_game":
        return
    continue_players = [sid for sid, choice in room_settings[room_code]['continue_choices'].items() if choice]
    if len(continue_players) >= 2:
        # Reset game state for new game
        room_states[room_code] = "waiting"
        room_settings[room_code] = {
            'round_time': ROUND_TIME,
            'word_list': WORD_LIST.copy(),
            'used_words': [],
            'current_word': None,
            'drawer': None,
            'current_round': 0,
            'drawn_players': [],
            'guesses': {},
            'round_start_time': None,
            'continue_choices': {}
        }
        emit('game_reset', {'message': "Starting a new game!"}, room=room_code)
        emit('player_list', {'players': players_in_game[room_code]}, room=room_code)
        print(f"Room {room_code} starting new game with {len(continue_players)} players")
    else:
        room_states[room_code] = "ended"
        emit('end_game', {'reason': "Not enough players to continue!"}, room=room_code)
        print(f"Room {room_code} ended due to insufficient players")

def start_round(room_code):
    settings = room_settings[room_code]
    available_words = [w for w in settings['word_list'] if w not in settings['used_words']]
    if not available_words:
        settings['used_words'] = []
        available_words = settings['word_list']
    word_options = random.sample(available_words, min(3, len(available_words)))  # 3 word choices
    drawer = settings['drawer']
    if drawer and drawer not in settings['drawn_players']:
        settings['drawn_players'].append(drawer)
    next_drawer_idx = (rooms[room_code].index(drawer) + 1 if drawer else 0) % len(rooms[room_code])
    settings['drawer'] = rooms[room_code][next_drawer_idx]
    settings['guesses'] = {sid: {'count': 0, 'correct': False, 'time': None} for sid in rooms[room_code]}
    emit('draw_update', {'x': -1, 'y': -1, 'color': 'clear'}, room=room_code)
    emit('word_choice', {'words': word_options}, to=settings['drawer'])
    if len(settings['drawn_players']) >= len(rooms[room_code]):
        room_states[room_code] = "post_game"
        emit('post_game_choice', {'message': "Game over! Choose to continue or leave."}, room=room_code)

def start_next_round(room_code):
    if room_code not in rooms or room_states[room_code] != "playing":
        return
    room_settings[room_code]['current_round'] += 1
    start_round(room_code)

@socketio.on('disconnect')
def handle_disconnect():
    for room_code in list(rooms.keys()):
        if request.sid in rooms[room_code]:
            safe_room_operation(room_code, lambda: rooms[room_code].remove(request.sid))
            player_name = players_in_game[room_code].pop(request.sid, {}).get('name', 'Unknown')
            emit('player_left', {'sid': request.sid, 'name': player_name}, room=room_code)
            emit('player_list', {'players': players_in_game[room_code]}, room=room_code)
            if len(rooms[room_code]) < 2 and room_states.get(room_code) == "playing":
                emit('end_game', {'reason': 'Not enough players!'}, room=room_code)
                room_states[room_code] = "ended"
            elif room_states.get(room_code) == "post_game" and len(room_settings[room_code]['continue_choices']) == len(rooms[room_code]):
                process_game_continuation(room_code)
    print(f"Player {request.sid} disconnected")

if __name__ == "__main__":
    socketio.run(game_server, host='0.0.0.0', port=8765, debug=True, use_reloader=False)
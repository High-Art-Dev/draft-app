import json
import os
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import asyncio
from typing import List, Dict


# ---------------- CONFIG ----------------

PICK_TIME = 60
ROUNDS = 7
MAX_TEAMS = 32


# ---------------- APP LIFESPAN ----------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    asyncio.create_task(timer_loop())
    yield

app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory="static"), name="static")


# ---------------- ROOM CLASS ----------------

class DraftRoom:
    def __init__(self):
        self.reset()

    def reset(self):
        self.teams: List[str] = []
        self.available_characters: List[str] = []
        self.picks: Dict[str, List[str]] = {}
        self.team_owners: Dict[str, WebSocket] = {}
        self.clients: List[WebSocket] = []

        self.current_pick_index = 0
        self.timer = PICK_TIME
        self.chat: List[str] = []

        self.commissioner: WebSocket | None = None
        self.draft_paused = False
        self.draft_finished = False
        self.draft_started = False

    def total_picks(self):
        return len(self.teams) * ROUNDS if self.teams else 0

    def current_round(self):
        if not self.teams:
            return 0
        return (self.current_pick_index // len(self.teams)) + 1

    def get_current_team(self):
        if not self.teams:
            return None

        round_number = self.current_pick_index // len(self.teams)
        index_in_round = self.current_pick_index % len(self.teams)

        if round_number % 2 == 0:
            return self.teams[index_in_round]
        else:
            return self.teams[-(index_in_round + 1)]


rooms: Dict[str, DraftRoom] = {}

def save_room_state(room_code: str):
    room = rooms[room_code]

    data = {
        "teams": room.teams,
        "available_characters": room.available_characters,
        "picks": room.picks,
        "current_pick_index": room.current_pick_index,
        "timer": room.timer,
        "draft_paused": room.draft_paused,
        "draft_finished": room.draft_finished,
    }

    os.makedirs("saves", exist_ok=True)

    with open(f"saves/{room_code}.json", "w") as f:
        json.dump(data, f)

def load_room_state(room_code: str):
    path = f"saves/{room_code}.json"

    if not os.path.exists(path):
        return

    with open(path, "r") as f:
        data = json.load(f)

    room = get_room(room_code)

    room.teams = data["teams"]
    room.available_characters = data["available_characters"]
    room.picks = data["picks"]
    room.current_pick_index = data["current_pick_index"]
    room.timer = data["timer"]
    room.draft_paused = data["draft_paused"]
    room.draft_finished = data["draft_finished"]
    room.draft_started = True

def get_room(code: str):
    if code not in rooms:
        rooms[code] = DraftRoom()
        load_room_state(code)
    return rooms[code]


# ---------------- ROUTES ----------------

@app.get("/")
async def home():
    return FileResponse("static/index.html")


# ---------------- WEBSOCKET ----------------

@app.websocket("/ws/{room_code}")
async def websocket_endpoint(websocket: WebSocket, room_code: str):
    await websocket.accept()
    room = get_room(room_code)

    room.clients.append(websocket)

    if room.commissioner is None:
        room.commissioner = websocket

    await send_state(room)

    try:
        while True:
            data = await websocket.receive_json()
            print("MESSAGE RECEIVED:", data)
            await handle_message(room, websocket, data)

    except WebSocketDisconnect:
        print("Client disconnected")
        if websocket in room.clients:
            room.clients.remove(websocket)

    except Exception as e:
        print("WebSocket unexpected error:", e)
        if websocket in room.clients:
            room.clients.remove(websocket)


# ---------------- MESSAGE HANDLER ----------------


async def handle_message(room: DraftRoom, ws: WebSocket, data: dict):
    action = data.get("action")



    # -------- SETUP --------

    if action == "setup":

        # Preserve connected clients & commissioner
        existing_clients = room.clients
        existing_commissioner = room.commissioner

        room.reset()

        room.clients = existing_clients
        room.commissioner = existing_commissioner

        teams = data["teams"][:MAX_TEAMS]
        characters = data["characters"]

        if not teams:
            return  # 🔒 prevent empty draft

        room.teams = teams
        room.available_characters = characters
        room.picks = {team: [] for team in teams}

        room.current_pick_index = 0
        room.timer = PICK_TIME
        room.draft_finished = False
        room.draft_paused = False
        room.draft_started = True
        await send_state(room)



    # -------- CLAIM TEAM --------
    elif action == "claim_team":
        team = data["team"]

        # Remove any previous team owned by this websocket
        for t, owner in list(room.team_owners.items()):
            if owner == ws:
                del room.team_owners[t]

        if team in room.teams and team not in room.team_owners:
            room.team_owners[team] = ws

        await send_state(room)

    # -------- PICK --------
    elif action == "pick":
        if room.draft_finished:
            return

        current_team = room.get_current_team()

        if room.team_owners.get(current_team) != ws:
            return

        character = data["character"]

        if character in room.available_characters:
            room.picks[current_team].append(character)
            room.available_characters.remove(character)

            room.current_pick_index += 1
            room.timer = PICK_TIME

            if room.total_picks() > 0 and room.current_pick_index >= room.total_picks():
                room.draft_finished = True

            await send_state(room)

    # -------- CHAT --------
    elif action == "chat":
        room.chat.append(data["message"])
        room.chat = room.chat[-50:]
        await send_chat(room)

    # -------- COMMISSIONER --------
    elif ws == room.commissioner:

        if action == "pause_draft":
            room.draft_paused = True

        elif action == "resume_draft":
            room.draft_paused = False

        elif action == "reset_draft":
            room.reset()

        elif action == "force_pick":
            character = data["character"]
            current_team = room.get_current_team()

            if character in room.available_characters:
                room.picks[current_team].append(character)
                room.available_characters.remove(character)
                room.current_pick_index += 1
                room.timer = PICK_TIME

                if room.total_picks() > 0 and room.current_pick_index >= room.total_picks():
                    room.draft_finished = True

                await send_state(room)


# ---------------- TIMER LOOP ----------------

async def timer_loop():
    while True:
        await asyncio.sleep(1)

        for room in rooms.values():

            if (
                    not room.teams
                    or room.draft_finished
                    or room.draft_paused
            ):
                continue

            if room.draft_paused or room.draft_finished:
                continue

            room.timer -= 1

            if room.timer <= 0:
                room.current_pick_index += 1
                room.timer = PICK_TIME

                if room.total_picks() > 0 and room.current_pick_index >= room.total_picks():
                    room.draft_finished = True

            await send_state(room)


# ---------------- BROADCAST HELPERS ----------------

async def send_state(room: DraftRoom):
    dead_clients = []

    for client in room.clients:
        try:
            await client.send_json({
                "type": "state",
                "data": get_state(room, client)
            })
        except Exception as e:
            print("Send failed:", e)
            dead_clients.append(client)

    # Remove disconnected clients
    for dc in dead_clients:
        if dc in room.clients:
            room.clients.remove(dc)

    # 🔥 ADD THIS RIGHT HERE
    for code, r in rooms.items():
            if r == room:
                save_room_state(code)
                break


async def send_chat(room: DraftRoom):
    print("SENDING STATE")
    dead_clients = []

    for client in room.clients:
        try:
            await client.send_json({
                "type": "chat",
                "data": room.chat
            })
        except Exception as e:
            print("Chat send failed:", e)
            dead_clients.append(client)

    for dc in dead_clients:
        if dc in room.clients:
            room.clients.remove(dc)


# ---------------- STATE BUILDER ----------------

def get_state(room: DraftRoom, ws: WebSocket):
    return {
        "teams": list(room.teams),
        "available_characters": list(room.available_characters),
        "picks": {k: list(v) for k, v in room.picks.items()},
        "current_team": room.get_current_team(),
        "round": room.current_round(),
        "timer": room.timer,
        "chat": list(room.chat),
        "team_owners": list(room.team_owners.keys()),
        "commissioner": ws is room.commissioner,
        "draft_paused": room.draft_paused,
        "draft_finished": room.draft_finished,
    }
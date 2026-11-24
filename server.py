import asyncio
import atexit
import json
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import httpx
import psutil
import qrcode
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


PORT = find_free_port()
SESSION_TOKEN = secrets.token_urlsafe(12)

champions_list: list[dict] = []
champions_by_id: dict[int, dict] = {}
lcu_creds: tuple[Optional[str], Optional[str]] = (None, None)

VALID_POSITIONS = {"TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY", "FILL", "UNSELECTED"}
auto_accept_enabled: bool = False
player_info_cache: dict[str, dict] = {}
VALID_CUSTOM_TEAMS = {"TEAM1", "TEAM2", "SPECTATOR"}


async def auto_accept_loop():
    while True:
        await asyncio.sleep(1.2)
        if not auto_accept_enabled:
            continue
        phase = await lcu_request("GET", "/lol-gameflow/v1/gameflow-phase")
        if phase == "ReadyCheck":
            ready = await lcu_request("GET", "/lol-matchmaking/v1/ready-check")
            if isinstance(ready, dict) and ready.get("playerResponse") == "None":
                await lcu_request("POST", "/lol-matchmaking/v1/ready-check/accept")


async def fetch_player_summary(puuid: str) -> dict:
    if not puuid:
        return {"riotId": "", "tier": "", "division": "", "wins": 0, "losses": 0}
    if puuid in player_info_cache:
        return player_info_cache[puuid]

    riot_id = ""
    summ = await lcu_request("GET", f"/lol-summoner/v2/summoners/puuid/{puuid}")
    if isinstance(summ, dict) and "error" not in summ:
        gn = summ.get("gameName") or summ.get("displayName") or ""
        tl = summ.get("tagLine") or ""
        riot_id = f"{gn}#{tl}" if gn and tl else gn

    tier, division, wins, losses = "", "", 0, 0
    ranked = await lcu_request("GET", f"/lol-ranked/v1/ranked-stats/{puuid}")
    if isinstance(ranked, dict) and "error" not in ranked:
        qmap = ranked.get("queueMap") or {}
        solo = qmap.get("RANKED_SOLO_5x5") or qmap.get("RANKED_FLEX_SR") or {}
        tier = solo.get("tier") or ""
        division = solo.get("division") or ""
        wins = int(solo.get("wins") or 0)
        losses = int(solo.get("losses") or 0)

    info = {
        "riotId": riot_id,
        "tier": tier,
        "division": "" if tier in ("", "NONE", "MASTER", "GRANDMASTER", "CHALLENGER") else division,
        "wins": wins,
        "losses": losses,
    }
    if riot_id or tier:
        player_info_cache[puuid] = info
    return info


def resource_path(filename: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
    return base / filename


def read_lockfile(path: Path) -> tuple[Optional[str], Optional[str]]:
    if path.exists():
        try:
            parts = path.read_text(encoding="utf-8").strip().split(":")
            if len(parts) >= 4:
                return parts[2], parts[3]
        except OSError:
            pass
    return None, None


def find_lcu_credentials() -> tuple[Optional[str], Optional[str]]:
    client_running = False
    exe_dir: Optional[Path] = None

    for proc in psutil.process_iter(["name", "cmdline", "exe"]):
        try:
            if proc.info["name"] == "LeagueClientUx.exe":
                client_running = True
                cmdline = " ".join(proc.info["cmdline"] or [])
                port_match = re.search(r"--app-port=(\d+)", cmdline)
                token_match = re.search(r"--remoting-auth-token=([\w-]+)", cmdline)
                if port_match and token_match:
                    return port_match.group(1), token_match.group(1)
                if proc.info.get("exe"):
                    exe_dir = Path(proc.info["exe"]).parent
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue

    if not client_running:
        return None, None

    if exe_dir:
        port, token = read_lockfile(exe_dir / "lockfile")
        if port and token:
            return port, token

    rel_candidates = [
        "Riot Games/League of Legends/lockfile",
        "Games/Riot Games/League of Legends/lockfile",
        "Online/Riot Games/League of Legends/lockfile",
        "League of Legends/lockfile",
    ]
    for part in psutil.disk_partitions(all=False):
        root = Path(part.mountpoint)
        for rel in rel_candidates:
            port, token = read_lockfile(root / rel)
            if port and token:
                return port, token

    return None, None


async def lcu_request(method: str, endpoint: str, json_data=None):
    global lcu_creds
    port, token = lcu_creds

    if not port or not token:
        port, token = find_lcu_credentials()
        lcu_creds = (port, token)
        if not port or not token:
            return None

    url = f"https://127.0.0.1:{port}{endpoint}"
    try:
        async with httpx.AsyncClient(auth=("riot", token), verify=False, timeout=2.5) as client:
            resp = await client.request(method, url, json=json_data)
            if resp.status_code == 204 or not resp.content:
                return {"ok": True}
            if resp.status_code >= 400:
                return {"error": resp.status_code, "detail": resp.text}
            return resp.json()
    except (httpx.ConnectError, httpx.ReadTimeout):
        lcu_creds = (None, None)
        return None


async def get_lcu_queues() -> list[dict]:
    res = await lcu_request("GET", "/lol-game-queues/v1/queues")
    return res if isinstance(res, list) else []


async def resolve_queue_id(requested_id: int) -> int:
    if requested_id not in (1700, 2400):
        return requested_id
    queues = await get_lcu_queues()
    if not queues:
        return requested_id

    for q in queues:
        if q.get("id") == requested_id and q.get("queueAvailability") == "Available":
            return requested_id

    for q in queues:
        if q.get("queueAvailability") != "Available":
            continue
        if q.get("category") == "Custom" or q.get("isCustom"):
            continue
        gm = str(q.get("gameMode", "")).upper()
        qtype = str(q.get("type", "")).upper()
        qname = str(q.get("name", ""))
        if requested_id == 1700 and gm == "CHERRY":
            return int(q["id"])
        if requested_id == 2400 and (
            gm == "KIWI" or "MAYHEM" in qtype or "MAYHEM" in qname.upper() or "아수라장" in qname
        ):
            return int(q["id"])
    return requested_id


async def load_champions():
    global champions_list, champions_by_id
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            ver_resp = await client.get("https://ddragon.leagueoflegends.com/api/versions.json")
            latest_ver = ver_resp.json()[0]
            champ_resp = await client.get(
                f"https://ddragon.leagueoflegends.com/cdn/{latest_ver}/data/ko_KR/champion.json"
            )
            raw_data = champ_resp.json()["data"]

            temp_list = []
            for eng_id, info in raw_data.items():
                cid = int(info["key"])
                item = {
                    "id": cid,
                    "eng": eng_id,
                    "name": info["name"],
                    "img": f"https://ddragon.leagueoflegends.com/cdn/{latest_ver}/img/champion/{eng_id}.png",
                }
                temp_list.append(item)
                champions_by_id[cid] = item

            champions_list = sorted(temp_list, key=lambda x: x["name"])
    except Exception as e:
        print(f"[WARN] 챔피언 데이터 로드 실패: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await load_champions()
    task = asyncio.create_task(auto_accept_loop())
    try:
        yield
    finally:
        task.cancel()


app = FastAPI(lifespan=lifespan)


@app.middleware("http")
async def verify_token(request: Request, call_next):
    if request.url.path.startswith("/api/"):
        if request.cookies.get("lol_token") != SESSION_TOKEN:
            return JSONResponse({"detail": "Unauthorized"}, status_code=403)
    return await call_next(request)


class ActionPayload(BaseModel):
    action_id: int
    champion_id: int
    completed: bool = False


class SpellPayload(BaseModel):
    spell1Id: int
    spell2Id: int


class RunePayload(BaseModel):
    page_id: int


class PositionPayload(BaseModel):
    first: str = "UNSELECTED"
    second: str = "UNSELECTED"


class CustomLobbyPayload(BaseModel):
    name: str = "Custom Room"
    map_id: int = 11
    game_mode: str = "CLASSIC"
    team_size: int = 5
    picking_policy: int = 6
    spectator_policy: str = "AllAllowed"


class AutoAcceptPayload(BaseModel):
    enabled: bool


class SkinPayload(BaseModel):
    skin_id: int


class ChatPayload(BaseModel):
    body: str


@app.post("/api/settings/auto-accept")
async def set_auto_accept(payload: AutoAcceptPayload):
    global auto_accept_enabled
    auto_accept_enabled = bool(payload.enabled)
    return {"ok": True, "autoAccept": auto_accept_enabled}


@app.post("/api/invitations/{inv_id}/accept")
async def accept_invitation(inv_id: str):
    res = await lcu_request("POST", f"/lol-lobby/v2/received-invitations/{inv_id}/accept")
    if res is None:
        raise HTTPException(503, "롤 클라이언트 미연결")
    return res


@app.post("/api/invitations/{inv_id}/decline")
async def decline_invitation(inv_id: str):
    res = await lcu_request("POST", f"/lol-lobby/v2/received-invitations/{inv_id}/decline")
    if res is None:
        raise HTTPException(503, "롤 클라이언트 미연결")
    return res


@app.post("/api/swap/{kind}/{swap_id}/{decision}")
async def handle_swap(kind: str, swap_id: str, decision: str):
    if decision not in ("accept", "decline"):
        raise HTTPException(400, "잘못된 요청입니다.")
    path_map = {
        "pick-order": f"/lol-champ-select/v1/session/pick-order-swaps/{swap_id}/{decision}",
        "position": f"/lol-champ-select/v1/session/position-swaps/{swap_id}/{decision}",
        "champion": f"/lol-champ-select/v1/session/champion-swaps/{swap_id}/{decision}",
    }
    ep = path_map.get(kind)
    if not ep:
        raise HTTPException(400, "지원하지 않는 교환 종류입니다.")
    res = await lcu_request("POST", ep)
    if res is None:
        raise HTTPException(503, "롤 클라이언트 미연결")
    return res


@app.get("/api/skins")
async def get_skins():
    skins = await lcu_request("GET", "/lol-champ-select/v1/skin-carousel-skins")
    if not isinstance(skins, list):
        return []
    result = []
    for s in skins:
        owned = bool(s.get("unlocked")) or bool((s.get("ownership") or {}).get("owned"))
        if owned:
            result.append({
                "id": s.get("id"),
                "name": s.get("name") or "",
                "selected": bool(s.get("selected")),
            })
    return result


@app.post("/api/skins")
async def set_skin(payload: SkinPayload):
    res = await lcu_request(
        "PATCH",
        "/lol-champ-select/v1/session/my-selection",
        {"selectedSkinId": payload.skin_id},
    )
    if res is None:
        raise HTTPException(503, "롤 클라이언트 미연결")
    return res


async def _find_champ_select_chat_id() -> Optional[str]:
    convs = await lcu_request("GET", "/lol-chat/v1/conversations")
    if not isinstance(convs, list):
        return None
    for c in convs:
        if c.get("type") == "championSelect" and c.get("id"):
            return str(c["id"])
    return None


@app.get("/api/chat")
async def get_chat():
    cid = await _find_champ_select_chat_id()
    if not cid:
        return []
    msgs = await lcu_request("GET", f"/lol-chat/v1/conversations/{cid}/messages")
    if not isinstance(msgs, list):
        return []
    return [
        {
            "fromSummonerId": m.get("fromSummonerId"),
            "body": m.get("body", ""),
            "timestamp": m.get("timestamp", ""),
        }
        for m in msgs[-20:]
        if m.get("body")
    ]


@app.post("/api/chat")
async def send_chat(payload: ChatPayload):
    text = (payload.body or "").strip()
    if not text:
        raise HTTPException(400, "메시지가 비어 있습니다.")
    cid = await _find_champ_select_chat_id()
    if not cid:
        raise HTTPException(404, "픽창 채팅방을 찾을 수 없습니다.")
    res = await lcu_request(
        "POST",
        f"/lol-chat/v1/conversations/{cid}/messages",
        {"body": text, "type": "chat"},
    )
    if res is None:
        raise HTTPException(503, "롤 클라이언트 미연결")
    return res


@app.get("/api/ingame")
async def get_ingame():
    sess = await lcu_request("GET", "/lol-gameflow/v1/session")
    if not isinstance(sess, dict) or "error" in sess:
        return {"team1": [], "team2": []}
    gdata = sess.get("gameData") or {}

    async def build_team(raw_list):
        items = raw_list or []
        summaries = await asyncio.gather(*(fetch_player_summary(p.get("puuid", "")) for p in items))
        out = []
        for p, sm in zip(items, summaries):
            out.append({
                "championId": int(p.get("championId") or 0),
                "riotId": sm.get("riotId") or p.get("summonerName") or "",
                "tier": sm.get("tier") or "",
                "division": sm.get("division") or "",
                "wins": sm.get("wins") or 0,
                "losses": sm.get("losses") or 0,
            })
        return out

    t1, t2 = await asyncio.gather(build_team(gdata.get("teamOne")), build_team(gdata.get("teamTwo")))
    return {"team1": t1, "team2": t2}


@app.get("/")
async def serve_ui(request: Request, t: Optional[str] = None):
    if t == SESSION_TOKEN:
        resp = RedirectResponse(url="/", status_code=302)
        resp.set_cookie("lol_token", SESSION_TOKEN, httponly=True, samesite="lax")
        return resp
    if request.cookies.get("lol_token") != SESSION_TOKEN:
        raise HTTPException(403, "권한 없음: PC 화면의 QR코드로 접속하세요.")
    return FileResponse(resource_path("index.html"))


@app.get("/api/champions")
async def get_champions():
    return champions_list


@app.get("/api/runes")
async def get_runes():
    pages = await lcu_request("GET", "/lol-perks/v1/pages")
    if not isinstance(pages, list):
        return []
    return [
        {"id": p["id"], "name": p["name"], "current": p.get("current", False)}
        for p in pages
        if p.get("isValid", True)
    ]


@app.post("/api/runes")
async def set_rune_page(payload: RunePayload):
    res = await lcu_request("PUT", "/lol-perks/v1/currentpage", payload.page_id)
    if res is None:
        raise HTTPException(503, "롤 클라이언트 미연결")
    return res


@app.post("/api/spells")
async def set_spells(payload: SpellPayload):
    res = await lcu_request(
        "PATCH",
        "/lol-champ-select/v1/session/my-selection",
        {"spell1Id": payload.spell1Id, "spell2Id": payload.spell2Id},
    )
    if res is None:
        raise HTTPException(503, "롤 클라이언트 미연결")
    return res


@app.post("/api/lobby/positions")
async def set_position_preferences(payload: PositionPayload):
    first = payload.first.upper() if payload.first else "UNSELECTED"
    second = payload.second.upper() if payload.second else "UNSELECTED"
    if first not in VALID_POSITIONS:
        first = "UNSELECTED"
    if second not in VALID_POSITIONS:
        second = "UNSELECTED"
    if first == "FILL":
        second = "UNSELECTED"
    elif first != "UNSELECTED" and first == second:
        second = "UNSELECTED"

    body = {"firstPreference": first, "secondPreference": second}
    res = await lcu_request("PUT", "/lol-lobby/v2/lobby/members/localMember/position-preferences", body)
    if res is None:
        raise HTTPException(503, "롤 클라이언트 미연결")
    if isinstance(res, dict) and "error" in res:
        print(f"[DEBUG] set_position_preferences failed: {body} -> {res}")
    return res


@app.post("/api/aram/reroll")
async def aram_reroll():
    res = await lcu_request("POST", "/lol-champ-select/v1/session/my-selection/reroll")
    if res is None:
        raise HTTPException(503, "롤 클라이언트 미연결")
    return res


@app.post("/api/aram/swap/{champion_id}")
async def aram_bench_swap(champion_id: int):
    res = await lcu_request("POST", f"/lol-champ-select/v1/session/bench/swap/{champion_id}")
    if res is None:
        raise HTTPException(503, "롤 클라이언트 미연결")
    return res


@app.post("/api/aram/pick/{champion_id}")
async def aram_pick_or_swap(champion_id: int):
    session = await lcu_request("GET", "/lol-champ-select/v1/session")
    if session is None:
        raise HTTPException(503, "롤 클라이언트 미연결")

    if isinstance(session, dict) and "error" not in session:
        my_cell = session.get("localPlayerCellId")
        target_act = None
        for group in session.get("actions", []):
            for act in group:
                if act.get("actorCellId") == my_cell and not act.get("completed") and act.get("type") == "pick":
                    if act.get("isInProgress"):
                        target_act = act
                        break
                    if target_act is None:
                        target_act = act
            if target_act and target_act.get("isInProgress"):
                break
        if target_act:
            act_res = await lcu_request(
                "PATCH",
                f"/lol-champ-select/v1/session/actions/{target_act['id']}",
                {"championId": champion_id, "completed": True},
            )
            if isinstance(act_res, dict) and "error" not in act_res:
                return act_res

    res = await lcu_request("POST", f"/lol-champ-select/v1/session/bench/swap/{champion_id}")
    if res is None:
        raise HTTPException(503, "롤 클라이언트 미연결")
    return res


@app.get("/api/debug/lobby")
async def debug_lobby():
    return await lcu_request("GET", "/lol-lobby/v2/lobby")


@app.get("/api/debug/queues")
async def debug_queues():
    queues = await get_lcu_queues()
    return [
        {
            "id": q.get("id"),
            "name": q.get("name"),
            "type": q.get("type"),
            "category": q.get("category"),
            "gameMode": q.get("gameMode"),
            "mapId": q.get("mapId"),
            "isCustom": q.get("isCustom"),
            "queueAvailability": q.get("queueAvailability"),
            "gameTypeConfig": q.get("gameTypeConfig"),
        }
        for q in queues
        if q.get("queueAvailability") == "Available"
        or q.get("category") == "Custom"
        or q.get("id") in (400, 420, 440, 450, 1700, 2400, 3270)
    ]


@app.get("/api/debug/champ-select")
async def debug_champ_select():
    return await lcu_request("GET", "/lol-champ-select/v1/session")


@app.get("/api/status")
async def get_status():
    phase = await lcu_request("GET", "/lol-gameflow/v1/gameflow-phase")
    if phase is None or isinstance(phase, dict):
        return {"connected": False, "phase": "Disconnected", "autoAccept": auto_accept_enabled}

    invitations = []
    if phase in ("None", "Lobby"):
        raw_inv = await lcu_request("GET", "/lol-lobby/v2/received-invitations")
        if isinstance(raw_inv, list):
            invitations = [
                {
                    "invitationId": inv.get("invitationId"),
                    "fromSummonerName": inv.get("fromSummonerName") or str(inv.get("fromSummonerId") or ""),
                    "queueId": (inv.get("gameConfig") or {}).get("queueId"),
                }
                for inv in raw_inv
                if inv.get("state") == "Pending" and inv.get("invitationId")
            ]

    result = {
        "connected": True,
        "phase": phase,
        "autoAccept": auto_accept_enabled,
        "invitations": invitations,
    }

    if phase == "Lobby":
        lobby = await lcu_request("GET", "/lol-lobby/v2/lobby")
        if isinstance(lobby, dict) and "error" not in lobby:
            cfg = lobby.get("gameConfig", {}) or {}
            local_member = lobby.get("localMember", {}) or {}
            is_custom = bool(cfg.get("isCustom", False))
            queue_id = int(cfg.get("queueId") or 0)
            my_team = ""
            team100 = cfg.get("customTeam100", []) or []
            team200 = cfg.get("customTeam200", []) or []
            if is_custom:
                my_puuid = local_member.get("puuid")
                my_sid = local_member.get("summonerId")
                if any(
                    (my_puuid and m.get("puuid") == my_puuid)
                    or (my_sid and m.get("summonerId") == my_sid)
                    for m in team100
                ):
                    my_team = "BLUE"
                elif any(
                    (my_puuid and m.get("puuid") == my_puuid)
                    or (my_sid and m.get("summonerId") == my_sid)
                    for m in team200
                ):
                    my_team = "RED"
                elif local_member.get("teamId") == 100:
                    my_team = "BLUE"
                elif local_member.get("teamId") == 200:
                    my_team = "RED"

            show_pos = bool(cfg.get("showPositionSelector", False)) or (
                not is_custom and queue_id in (400, 420, 440)
            )
            result["lobby"] = {
                "isCustom": is_custom,
                "queueId": queue_id,
                "gameMode": cfg.get("gameMode", ""),
                "showPositionSelector": show_pos,
                "firstPosition": local_member.get("firstPositionPreference") or "UNSELECTED",
                "secondPosition": local_member.get("secondPositionPreference") or "UNSELECTED",
                "myTeam": my_team,
                "blueCount": len(team100),
                "redCount": len(team200),
            }

    elif phase == "ReadyCheck":
        ready = await lcu_request("GET", "/lol-matchmaking/v1/ready-check")
        if isinstance(ready, dict) and "error" not in ready:
            result["ready_check"] = {
                "timer": round(ready.get("timer", 0), 1),
                "response": ready.get("playerResponse", "None"),
            }

    elif phase == "ChampSelect":
        session = await lcu_request("GET", "/lol-champ-select/v1/session")
        if isinstance(session, dict) and "error" not in session:
            my_cell = session.get("localPlayerCellId")
            active_action = None
            intent_action = None
            banned_ids = set(session.get("bans", {}).get("myTeamBans", [])) | set(
                session.get("bans", {}).get("theirTeamBans", [])
            )
            picked_ids = set()

            for group in session.get("actions", []):
                for act in group:
                    cid = act.get("championId", 0)
                    if act.get("completed") and cid > 0:
                        if act.get("type") == "ban":
                            banned_ids.add(cid)
                        elif act.get("type") == "pick":
                            picked_ids.add(cid)

                    if act.get("actorCellId") == my_cell and not act.get("completed"):
                        if act.get("isInProgress"):
                            active_action = {
                                "id": act["id"],
                                "type": act["type"],
                                "championId": cid,
                                "canLock": True,
                            }
                        elif act.get("type") == "pick" and intent_action is None:
                            intent_action = {
                                "id": act["id"],
                                "type": "intent",
                                "championId": cid,
                                "canLock": False,
                            }

            timer_info = session.get("timer", {})
            my_spells = {"spell1Id": 4, "spell2Id": 14}
            my_champ_id = 0
            my_team = []
            for m in session.get("myTeam", []):
                is_me = m["cellId"] == my_cell
                cid = m.get("championId") or m.get("championPickIntent", 0)
                if is_me:
                    my_champ_id = cid
                    my_spells = {
                        "spell1Id": m.get("spell1Id", 4),
                        "spell2Id": m.get("spell2Id", 14),
                    }
                my_team.append(
                    {
                        "cellId": m["cellId"],
                        "isMe": is_me,
                        "championId": cid,
                        "position": m.get("assignedPosition", ""),
                    }
                )

            their_team = [
                {"championId": e.get("championId", 0)}
                for e in session.get("theirTeam", [])
            ]

            raw_bench = session.get("benchChampions", []) or []
            bench_ids: list[int] = []
            card_ids: list[int] = []
            for b in raw_bench:
                if isinstance(b, dict):
                    cid = int(b.get("championId") or 0)
                    if cid <= 0:
                        continue
                    if b.get("isPriority"):
                        card_ids.append(cid)
                    else:
                        bench_ids.append(cid)
                elif b:
                    bench_ids.append(int(b))

            bench_enabled = bool(session.get("benchEnabled", False))
            allow_subset = bool(session.get("allowSubsetChampionPicks", False))
            allow_reroll = bool(session.get("allowRerolling", False) or session.get("allowReroll", False))
            rerolls_rem = int(session.get("rerollsRemaining", 0) or 0)

            if allow_subset or (bench_enabled and not card_ids):
                subset = await lcu_request("GET", "/lol-lobby-team-builder/champ-select/v1/subset-champion-list")
                if isinstance(subset, list) and subset:
                    card_ids = [
                        int(cid)
                        for cid in subset
                        if int(cid) > 0 and int(cid) != my_champ_id
                    ]

            is_aram_like = bench_enabled or allow_subset or allow_reroll or rerolls_rem > 0 or bool(card_ids)

            swaps = []
            for s in session.get("pickOrderSwaps", []) or []:
                if isinstance(s, dict) and s.get("state") == "RECEIVED":
                    swaps.append({"kind": "pick-order", "id": s.get("id"), "cellId": s.get("cellId")})

            for s in session.get("positionSwaps", []) or []:
                if isinstance(s, dict) and s.get("state") == "RECEIVED":
                    swaps.append({"kind": "position", "id": s.get("id"), "cellId": s.get("cellId")})

            champ_swaps = await lcu_request("GET", "/lol-champ-select/v1/session/champion-swaps")
            if isinstance(champ_swaps, list):
                for s in champ_swaps:
                    if isinstance(s, dict) and s.get("state") == "RECEIVED":
                        swaps.append({"kind": "champion", "id": s.get("id"), "cellId": s.get("cellId")})

            result["champ_select"] = {
                "timerPhase": timer_info.get("phase", ""),
                "timeLeft": max(0, int(timer_info.get("adjustedTimeLeftInPhase", 0) / 1000)),
                "myAction": active_action or intent_action,
                "myChampionId": my_champ_id,
                "myTeam": my_team,
                "theirTeam": their_team,
                "bannedIds": [x for x in banned_ids if x > 0],
                "pickedIds": [x for x in picked_ids if x > 0],
                "mySpells": my_spells,
                "benchEnabled": bench_enabled,
                "allowSubset": allow_subset,
                "allowReroll": allow_reroll,
                "rerollsRemaining": rerolls_rem,
                "benchIds": bench_ids,
                "cardIds": card_ids,
                "isAramLike": is_aram_like,
                "swaps": swaps,
            }

    return result


@app.post("/api/lobby/custom")
async def create_custom_lobby(payload: CustomLobbyPayload):
    spec_map = {
        "All": "AllAllowed",
        "LobbyOnly": "LobbyAllowed",
        "FriendsOnly": "FriendsAllowed",
        "None": "NotAllowed",
        "AllAllowed": "AllAllowed",
        "LobbyAllowed": "LobbyAllowed",
        "FriendsAllowed": "FriendsAllowed",
        "NotAllowed": "NotAllowed",
    }
    spec_val = spec_map.get(payload.spectator_policy, "AllAllowed")
    mutator_id = payload.picking_policy if payload.picking_policy in (1, 2, 4, 6) else 6

    req_mode = (payload.game_mode or "CLASSIC").upper()
    if req_mode == "KIWI":
        map_id = 12
        game_mode = "KIWI"
    elif payload.map_id == 12 or req_mode == "ARAM":
        map_id = 12
        game_mode = "ARAM"
    else:
        map_id = 11
        game_mode = "CLASSIC"

    if map_id == 12 and mutator_id == 2:
        mutator_id = 6

    lobby_name = (payload.name or "").strip() or "Custom Room"
    if lobby_name == "Custom Room":
        lobby_name = f"Custom_{secrets.token_hex(3)}"

    queues = await get_lcu_queues()
    custom_queues = [
        q
        for q in queues
        if (q.get("category") == "Custom" or q.get("isCustom"))
        and str(q.get("gameMode", "")).upper() not in ("PRACTICETOOL", "TUTORIAL", "TFT")
        and "TUTORIAL" not in str(q.get("type", "")).upper()
    ]

    matched_queue = None
    for q in custom_queues:
        q_gm = str(q.get("gameMode", "")).upper()
        q_map = int(q.get("mapId") or 0)
        q_avail = q.get("queueAvailability") == "Available"
        q_cfg_id = (q.get("gameTypeConfig") or {}).get("id")
        if game_mode == "KIWI":
            if q.get("id") == 3270 or q_gm == "KIWI" or "MAYHEM" in str(q.get("type", "")).upper():
                matched_queue = q
                break
        elif q_map == map_id and q_gm == game_mode and q_avail:
            if q_cfg_id == mutator_id:
                matched_queue = q
                break
            if matched_queue is None:
                matched_queue = q

    if matched_queue is None and custom_queues:
        for q in custom_queues:
            if int(q.get("mapId") or 0) == map_id and q.get("queueAvailability") == "Available":
                matched_queue = q
                break

    base_cfg = {
        "gameMode": matched_queue.get("gameMode", game_mode) if matched_queue else game_mode,
        "gameMutator": "",
        "gameServerRegion": "",
        "mapId": map_id,
        "spectatorPolicy": spec_val,
        "teamSize": payload.team_size,
    }

    candidates = []
    if matched_queue and matched_queue.get("id"):
        q_id = int(matched_queue["id"])
        cfg1 = dict(base_cfg, gameTypeConfig={"id": mutator_id})
        candidates.append(
            {
                "queueId": q_id,
                "isCustom": True,
                "customGameLobby": {
                    "lobbyName": lobby_name,
                    "lobbyPassword": "",
                    "configuration": cfg1,
                },
            }
        )
        cfg2 = dict(base_cfg, gameTypeConfig={"id": mutator_id}, mutators={"id": mutator_id})
        candidates.append(
            {
                "queueId": q_id,
                "isCustom": True,
                "customGameLobby": {
                    "lobbyName": lobby_name,
                    "lobbyPassword": None,
                    "configuration": cfg2,
                },
            }
        )

    candidates.append(
        {
            "isCustom": True,
            "customGameLobby": {
                "lobbyName": lobby_name,
                "lobbyPassword": None,
                "configuration": dict(base_cfg, gameTypeConfig={"id": mutator_id}, mutators={"id": mutator_id}),
            },
        }
    )

    last_res = None
    for body in candidates:
        res = await lcu_request("POST", "/lol-lobby/v2/lobby", body)
        if res is None:
            raise HTTPException(503, "롤 클라이언트 미연결")
        last_res = res
        if isinstance(res, dict) and "error" not in res:
            return res
        print(f"[DEBUG] create_custom_lobby try failed: {body} -> {res}")

    brief_customs = [
        {
            "id": q.get("id"),
            "type": q.get("type"),
            "gameMode": q.get("gameMode"),
            "mapId": q.get("mapId"),
            "avail": q.get("queueAvailability"),
            "cfg": (q.get("gameTypeConfig") or {}).get("id"),
        }
        for q in custom_queues[:15]
    ]
    print(f"[DEBUG] available custom queues: {json.dumps(brief_customs, ensure_ascii=False)}")
    return last_res


@app.post("/api/lobby/custom/start")
async def start_custom_lobby():
    res = await lcu_request("POST", "/lol-lobby/v1/lobby/custom/start-champ-select")
    if res is None:
        raise HTTPException(503, "롤 클라이언트 미연결")
    return res


@app.post("/api/lobby/custom/switch-teams")
async def switch_custom_team():
    lobby = await lcu_request("GET", "/lol-lobby/v2/lobby")
    if lobby is None:
        raise HTTPException(503, "롤 클라이언트 미연결")

    my_team = ""
    if isinstance(lobby, dict) and "error" not in lobby:
        cfg = lobby.get("gameConfig", {}) or {}
        local_member = lobby.get("localMember", {}) or {}
        my_puuid = local_member.get("puuid")
        my_sid = local_member.get("summonerId")
        team100 = cfg.get("customTeam100", []) or []
        team200 = cfg.get("customTeam200", []) or []
        if any(
            (my_puuid and m.get("puuid") == my_puuid)
            or (my_sid and m.get("summonerId") == my_sid)
            for m in team100
        ) or local_member.get("teamId") == 100:
            my_team = "BLUE"
        elif any(
            (my_puuid and m.get("puuid") == my_puuid)
            or (my_sid and m.get("summonerId") == my_sid)
            for m in team200
        ) or local_member.get("teamId") == 200:
            my_team = "RED"

    target = "TEAM2" if my_team == "BLUE" else "TEAM1"
    res = await lcu_request("POST", f"/lol-lobby/v2/lobby/team/{target}")
    if res is None:
        raise HTTPException(503, "롤 클라이언트 미연결")
    if isinstance(res, dict) and "error" in res:
        print(f"[DEBUG] switch_custom_team failed (target={target}): {res}")
    return res


@app.post("/api/lobby/custom/team/{team}")
async def move_custom_team(team: str):
    target = team.upper()
    if target not in ("TEAM1", "TEAM2", "SPECTATOR"):
        raise HTTPException(400, "지원하지 않는 팀 값입니다.")
    res = await lcu_request("POST", f"/lol-lobby/v2/lobby/team/{target}")
    if res is None:
        raise HTTPException(503, "롤 클라이언트 미연결")
    if isinstance(res, dict) and "error" in res:
        print(f"[DEBUG] move_custom_team failed (target={target}): {res}")
    return res


@app.post("/api/lobby/leave")
async def leave_lobby():
    endpoints = [
        ("DELETE", "/lol-lobby/v2/lobby"),
        ("DELETE", "/lol-lobby/v2/lobby/members/localMember"),
        ("DELETE", "/lol-lobby/v1/lobby"),
        ("POST", "/lol-lobby/v2/lobby/custom/leave"),
    ]
    last_res = None
    for method, ep in endpoints:
        res = await lcu_request(method, ep)
        if res is None:
            raise HTTPException(503, "롤 클라이언트 미연결")
        last_res = res
        if isinstance(res, dict) and "error" not in res:
            return res
    print(f"[DEBUG] leave_lobby failed: {last_res}")
    return last_res


@app.post("/api/lobby/{queue_id:int}")
async def create_lobby(queue_id: int):
    resolved_id = await resolve_queue_id(queue_id)
    res = await lcu_request("POST", "/lol-lobby/v2/lobby", {"queueId": resolved_id})
    if res is None:
        raise HTTPException(503, "롤 클라이언트 미연결")
    if isinstance(res, dict) and "error" in res:
        print(f"[DEBUG] create_lobby({queue_id} -> {resolved_id}) failed: {res}")
    return res


@app.post("/api/queue/start")
async def start_queue():
    return await lcu_request("POST", "/lol-lobby/v2/lobby/matchmaking/search")


@app.post("/api/queue/cancel")
async def cancel_queue():
    return await lcu_request("DELETE", "/lol-lobby/v2/lobby/matchmaking/search")


@app.post("/api/ready/accept")
async def accept_ready():
    return await lcu_request("POST", "/lol-matchmaking/v1/ready-check/accept")


@app.post("/api/ready/decline")
async def decline_ready():
    return await lcu_request("POST", "/lol-matchmaking/v1/ready-check/decline")


@app.post("/api/action")
async def do_champ_action(payload: ActionPayload):
    res = await lcu_request(
        "PATCH",
        f"/lol-champ-select/v1/session/actions/{payload.action_id}",
        {"championId": payload.champion_id, "completed": payload.completed},
    )
    if res is None:
        raise HTTPException(503, "롤 클라이언트 미연결")
    return res


class BackgroundServer(uvicorn.Server):
    def install_signal_handlers(self):
        pass


def get_cloudflared_bin() -> str:
    bundled = resource_path("cloudflared.exe")
    if bundled.exists():
        return str(bundled)
    local = Path.cwd() / "cloudflared.exe"
    if local.exists():
        return str(local)
    found = shutil.which("cloudflared")
    if found:
        return found
    raise FileNotFoundError("cloudflared.exe를 찾을 수 없습니다.")


def print_qr(url: str):
    qr = qrcode.QRCode(border=2)
    qr.add_data(url)
    qr.make(fit=True)
    print("\n" + "=" * 54)
    print(" 스마트폰 카메라로 아래 QR코드를 스캔하세요 (5G/LTE 가능)")
    print("=" * 54)
    qr.print_ascii(invert=True)
    print(f"접속 주소: {url}")
    print("=" * 54)
    print("종료하려면 이 창을 닫거나 Ctrl+C를 누르세요.\n")


def main():
    print(f"[1/2] 로컬 중계 서버 시작 중... (포트: {PORT})")
    config = uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="error")
    server = BackgroundServer(config)
    threading.Thread(target=server.run, daemon=True).start()
    time.sleep(1.2)

    print("[2/2] Cloudflare 임시 터널 생성 중...")
    cf_bin = get_cloudflared_bin()
    proc = subprocess.Popen(
        [
            cf_bin,
            "tunnel",
            "--url",
            f"http://127.0.0.1:{PORT}",
            "--protocol",
            "http2",
            "--no-autoupdate",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="ignore",
    )

    def cleanup():
        if proc.poll() is None:
            proc.terminate()

    atexit.register(cleanup)

    url_pattern = re.compile(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com")
    qr_printed = False

    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            if not qr_printed:
                match = url_pattern.search(line)
                if match:
                    raw_url = match.group(0)
                    host = match.group(0).replace("https://", "")
                    deadline = time.monotonic() + 15.0
                    while time.monotonic() < deadline:
                        try:
                            socket.gethostbyname(host)
                            break
                        except socket.gaierror:
                            time.sleep(0.5)
                    full_url = f"{raw_url}/?t={SESSION_TOKEN}"
                    print_qr(full_url)
                    qr_printed = True
    except KeyboardInterrupt:
        print("\n종료합니다...")
    finally:
        cleanup()


if __name__ == "__main__":
    main()
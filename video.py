"""
Domyos Woodrower — Vidéo Jellyfin incrustée dans le dashboard pendant la séance

Le serveur (server.py) tient l'état de lecture ; le dashboard (index.html) lit le flux
HLS avec hls.js et renvoie sa position ; la page de contrôle (control.html) sert de
télécommande. Jellyfin transcode tout en H.264/AAC HLS (avi, mkv, HEVC…) : Brave n'a
pas à savoir lire les formats d'origine.

    cfg : JELLYFIN_URL, JELLYFIN_API_KEY, JELLYFIN_USER (nom, vide = premier utilisateur)

Messages WS serveur → clients :
    {"type":"video", active, item:{…}, src, play_session_id, position_s, duration_s,
                     playing, volume, subtitle_index, subtitles:[…]}
    {"type":"video", active:false}
    {"type":"video_cmd", action:"seek"|"volume", position_s | volume}   (pour le dashboard)
Commandes clients → serveur :
    {"type":"video_play", item_id, position_s?, subtitle_index?, audio_index?}
    {"type":"video_pause"} | {"type":"video_resume"} | {"type":"video_stop"}
    {"type":"video_seek", delta_s | position_s} | {"type":"video_volume", volume}
    {"type":"video_progress", position_s, paused, duration_s}   (dashboard, toutes les 3 s)
    {"type":"video_ended"}                                       (dashboard → épisode suivant)
REST : GET /api/video/home | /api/video/search?q= | /api/video/episodes/{series_id}
Proxy : GET /jf/{chemin} → Jellyfin avec l'en-tête d'authentification (la clé ne sort
        jamais vers le navigateur ; seuls /videos/… et les images /Items/…/Images/… passent)
"""

import asyncio
import time
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import aiohttp
from aiohttp import web

TICKS = 10_000_000                     # Jellyfin : 1 s = 10 000 000 ticks
DEVICE_ID = "domyos-dashboard"
REPORT_EVERY_S = 10                    # rapport de progression à Jellyfin (reprise / vu)

# Profil minimal : tout en HLS H.264 + AAC stéréo, sous-titres incrustés par le transcodeur
PROFILE = {
    "Name": "domyos-dashboard",
    "MaxStreamingBitrate": 20_000_000,
    "DirectPlayProfiles": [],
    "TranscodingProfiles": [{
        "Container": "ts", "Type": "Video", "VideoCodec": "h264", "AudioCodec": "aac",
        "Protocol": "hls", "Context": "Streaming", "MaxAudioChannels": "2",
        "MinSegments": 1, "BreakOnNonKeyFrames": True,
    }],
    "CodecProfiles": [],
    "SubtitleProfiles": [{"Format": f, "Method": "Encode"}
                         for f in ("subrip", "srt", "ass", "ssa", "pgssub", "dvdsub", "vtt", "sub")],
}

ITEM_FIELDS = "MediaStreams,Overview,ParentId,SeriesId,UserData,RunTimeTicks,ImageTags,SeriesPrimaryImageTag"


def _log(tag, msg):
    from server import log
    log(tag, msg)


class Video:
    def __init__(self, cfg, server):
        self.url = cfg.JELLYFIN_URL.rstrip("/")
        self.key = cfg.JELLYFIN_API_KEY
        self.user_name = getattr(cfg, "JELLYFIN_USER", "") or ""
        self.server = server
        self.session: aiohttp.ClientSession | None = None
        self.user_id: str | None = None
        self.enabled = False
        self.volume = 100
        self.autonext = True
        self.state: dict = {"type": "video", "active": False}
        self._last_report = 0.0
        self._lock = asyncio.Lock()

    # ── HTTP Jellyfin ─────────────────────────────────────────────────────────
    @property
    def _headers(self) -> dict:
        return {"Authorization": f'MediaBrowser Token="{self.key}", Client="domyos-coach", '
                                 f'Device="dashboard", DeviceId="{DEVICE_ID}", Version="1.0"',
                "Accept-Encoding": "gzip, deflate"}     # pas de brotli : aiohttp ne le décode pas sans dépendance

    async def start(self) -> None:
        self.session = aiohttp.ClientSession(headers=self._headers,
                                             timeout=aiohttp.ClientTimeout(total=30))
        try:
            await self._ensure_user()
            _log("VIDEO", f"Jellyfin {self.url} — utilisateur {self.user_name} ({self.user_id})")
        except Exception as e:                      # serveur éteint, clé invalide… : on réessaie à l'usage
            _log("VIDEO", f"Jellyfin injoignable ({e}) — réessai à la première demande")

    async def stop(self) -> None:
        if self.state.get("active"):
            await self.stop_playback()
        if self.session:
            await self.session.close()

    async def _ensure_user(self) -> str:
        if self.user_id:
            return self.user_id
        users = await self._get("/Users")
        if not users:
            raise RuntimeError("aucun utilisateur Jellyfin")
        u = next((x for x in users if x["Name"].lower() == self.user_name.lower()), None) if self.user_name else None
        u = u or next((x for x in users if x.get("Policy", {}).get("IsAdministrator")), users[0])
        self.user_id, self.user_name, self.enabled = u["Id"], u["Name"], True
        return self.user_id

    async def _get(self, path: str, params: dict | None = None):
        async with self.session.get(self.url + path, params=params) as r:
            r.raise_for_status()
            return await r.json() if r.content_type == "application/json" else await r.read()

    async def _post(self, path: str, body: dict | None = None, params: dict | None = None):
        async with self.session.post(self.url + path, json=body, params=params) as r:
            r.raise_for_status()
            return await r.json() if r.content_type == "application/json" else None

    async def _delete(self, path: str, params: dict | None = None) -> None:
        async with self.session.delete(self.url + path, params=params) as r:
            r.raise_for_status()

    # ── catalogue ─────────────────────────────────────────────────────────────
    def item_info(self, i: dict) -> dict:
        ud = i.get("UserData") or {}
        image = None
        if (i.get("ImageTags") or {}).get("Primary"):
            image = f"/jf/Items/{i['Id']}/Images/Primary?maxWidth=320&tag={i['ImageTags']['Primary']}"
        elif i.get("SeriesPrimaryImageTag") and i.get("SeriesId"):
            image = f"/jf/Items/{i['SeriesId']}/Images/Primary?maxWidth=320&tag={i['SeriesPrimaryImageTag']}"
        subs = [{"index": s["Index"], "label": s.get("DisplayTitle") or s.get("Language") or f"piste {s['Index']}"}
                for s in i.get("MediaStreams") or [] if s.get("Type") == "Subtitle"]
        audio = [{"index": s["Index"], "label": s.get("DisplayTitle") or s.get("Language") or f"piste {s['Index']}"}
                 for s in i.get("MediaStreams") or [] if s.get("Type") == "Audio"]
        return {
            "id": i["Id"], "type": i.get("Type"), "name": i.get("Name"),
            "series": i.get("SeriesName"), "series_id": i.get("SeriesId"),
            "season": i.get("ParentIndexNumber"), "episode": i.get("IndexNumber"),
            "year": i.get("ProductionYear"),
            "runtime_s": int((i.get("RunTimeTicks") or 0) / TICKS),
            "position_s": int((ud.get("PlaybackPositionTicks") or 0) / TICKS),
            "played": bool(ud.get("Played")),
            "unplayed_count": ud.get("UnplayedItemCount"),
            "image": image, "subtitles": subs, "audio": audio,
        }

    async def home(self) -> dict:
        uid = await self._ensure_user()
        resume, nextup = await asyncio.gather(
            self._get("/UserItems/Resume", {"userId": uid, "Limit": 8, "MediaTypes": "Video", "Fields": ITEM_FIELDS}),
            self._get("/Shows/NextUp", {"userId": uid, "Limit": 8, "Fields": ITEM_FIELDS}))
        return {"resume": [self.item_info(i) for i in resume.get("Items", [])],
                "nextup": [self.item_info(i) for i in nextup.get("Items", [])]}

    async def search(self, q: str) -> list[dict]:
        uid = await self._ensure_user()
        d = await self._get("/Items", {"userId": uid, "searchTerm": q, "Recursive": "true",
                                       "IncludeItemTypes": "Series,Movie,Episode", "Limit": 24,
                                       "Fields": ITEM_FIELDS})
        return [self.item_info(i) for i in d.get("Items", [])]

    async def episodes(self, series_id: str) -> list[dict]:
        uid = await self._ensure_user()
        d = await self._get(f"/Shows/{series_id}/Episodes", {"userId": uid, "Fields": ITEM_FIELDS})
        return [self.item_info(i) for i in d.get("Items", [])]

    # ── lecture ───────────────────────────────────────────────────────────────
    async def play(self, item_id: str, position_s: float | None = None,
                   subtitle_index: int | None = None, audio_index: int | None = None) -> dict:
        async with self._lock:
            uid = await self._ensure_user()
            if self.state.get("active"):
                await self._end_current(report=True)
            raw = await self._get(f"/Items/{item_id}", {"userId": uid, "Fields": ITEM_FIELDS})
            item = self.item_info(raw)
            if position_s is None:                                   # reprise là où Jellyfin en était
                position_s = item["position_s"] if item["position_s"] < item["runtime_s"] - 30 else 0
            body = {"DeviceProfile": PROFILE, "EnableDirectPlay": False, "EnableDirectStream": False,
                    "EnableTranscoding": True, "AutoOpenLiveStream": True}
            if subtitle_index is not None:
                body["SubtitleStreamIndex"] = int(subtitle_index)
            if audio_index is not None:
                body["AudioStreamIndex"] = int(audio_index)
            pi = await self._post(f"/Items/{item_id}/PlaybackInfo", body, {"userId": uid})
            ms = next((m for m in pi.get("MediaSources", []) if m.get("TranscodingUrl")), None)
            if not ms:
                raise RuntimeError(pi.get("ErrorCode") or "Jellyfin ne propose pas de flux HLS pour cet élément")
            self.state = {
                "type": "video", "active": True, "item": item,
                "src": "/jf" + self._strip_key(ms["TranscodingUrl"]),
                "play_session_id": pi["PlaySessionId"], "media_source_id": ms["Id"],
                "position_s": float(position_s), "duration_s": item["runtime_s"] or None,
                "playing": True, "volume": self.volume,
                "subtitle_index": ms.get("DefaultSubtitleStreamIndex", -1 if subtitle_index is None else subtitle_index),
                "audio_index": ms.get("DefaultAudioStreamIndex"),
                "subtitles": item["subtitles"], "audio": item["audio"],
            }
            _log("VIDEO", f"lecture « {self.title()} » à {int(position_s)} s")
            await self._report("/Sessions/Playing", playing_started=True)
            await self.server.broadcast(self.state)
            return self.state

    @staticmethod
    def _strip_key(url: str) -> str:
        """Retire ApiKey de l'URL de transcodage (le proxy ajoute l'en-tête lui-même)."""
        parts = urlsplit(url)
        q = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k.lower() != "apikey"]
        return urlunsplit(("", "", parts.path, urlencode(q), ""))

    def title(self) -> str:
        it = self.state.get("item") or {}
        if it.get("series"):
            return f"{it['series']} S{it.get('season') or 0:02d}E{it.get('episode') or 0:02d} · {it['name']}"
        return it.get("name") or "?"

    async def set_playing(self, playing: bool) -> None:
        if not self.state.get("active"):
            return
        self.state["playing"] = playing
        await self._report("/Sessions/Playing/Progress", event="unpause" if playing else "pause", force=True)
        await self.server.broadcast(self.state)

    async def seek(self, position_s: float | None = None, delta_s: float | None = None) -> None:
        if not self.state.get("active"):
            return
        pos = self.state["position_s"] + (delta_s or 0) if position_s is None else position_s
        pos = max(0.0, min(pos, (self.state.get("duration_s") or pos) ))
        self.state["position_s"] = pos
        await self.server.broadcast({"type": "video_cmd", "action": "seek", "position_s": pos})
        await self.server.broadcast(self.state)

    async def set_volume(self, volume: int) -> None:
        self.volume = max(0, min(100, int(volume)))
        self.state["volume"] = self.volume
        await self.server.broadcast({"type": "video_cmd", "action": "volume", "volume": self.volume})
        if self.state.get("active"):
            await self.server.broadcast(self.state)

    async def progress(self, position_s: float, paused: bool, duration_s: float | None) -> None:
        """Position renvoyée par le dashboard (toutes les 3 s) → état + rapport Jellyfin."""
        if not self.state.get("active"):
            return
        self.state["position_s"] = float(position_s)
        if duration_s:
            self.state["duration_s"] = float(duration_s)
        self.state["playing"] = not paused
        await self._report("/Sessions/Playing/Progress", event="timeupdate")
        await self.server.broadcast(self.state)

    async def ended(self) -> None:
        """Fin de l'épisode : rapport « vu », puis épisode suivant de la série s'il existe."""
        if not self.state.get("active"):
            return
        st = self.state
        item = st["item"]
        st["position_s"] = st.get("duration_s") or st["position_s"]
        self.state = {"type": "video", "active": False}          # avant tout await : ignore les progress tardifs
        await self._end_current(report=True, st=st)
        nxt = None
        if self.autonext and item.get("type") == "Episode" and item.get("series_id"):
            try:
                uid = await self._ensure_user()
                d = await self._get("/Shows/NextUp", {"userId": uid, "SeriesId": item["series_id"], "Limit": 1, "Fields": ITEM_FIELDS})
                nxt = next((i for i in d.get("Items", []) if i["Id"] != item["id"]), None)
            except Exception as e:
                _log("VIDEO", f"épisode suivant introuvable : {e}")
        if nxt:
            await self.play(nxt["Id"], 0)
        else:
            self.state = {"type": "video", "active": False, "finished": True, "name": self.title_of(item)}
            await self.server.broadcast(self.state)

    @staticmethod
    def title_of(it: dict) -> str:
        return f"{it['series']} S{it.get('season') or 0:02d}E{it.get('episode') or 0:02d} · {it['name']}" if it.get("series") else it.get("name", "?")

    async def stop_playback(self) -> None:
        if not self.state.get("active"):
            return
        async with self._lock:
            st = self.state
            self.state = {"type": "video", "active": False}
            await self.server.broadcast(self.state)          # le dashboard coupe tout de suite…
            await self._end_current(report=True, st=st)      # …puis on prévient Jellyfin

    async def _end_current(self, report: bool, st: dict | None = None) -> None:
        """Rapport d'arrêt + arrêt du transcodage côté Jellyfin (sans toucher à self.state)."""
        st = st or self.state
        _log("VIDEO", f"arrêt « {self.title_of(st['item'])} » à {int(st.get('position_s', 0))} s")
        try:
            if report:
                await self._report("/Sessions/Playing/Stopped", force=True, st=st)
            await self._delete("/Videos/ActiveEncodings", {"deviceId": DEVICE_ID, "playSessionId": st["play_session_id"]})
        except Exception as e:
            _log("VIDEO", f"arrêt côté Jellyfin : {e}")

    async def _save_userdata(self, st: dict) -> None:
        """Position / « vu » dans le profil Jellyfin de l'utilisateur.

        Une clé d'API n'est liée à aucun utilisateur : les rapports /Sessions/Playing
        alimentent « lecture en cours » mais pas la reprise ni l'état « vu ». On écrit
        donc les données utilisateur nous-mêmes (reprise, à suivre, épisode suivant)."""
        uid = await self._ensure_user()
        item, pos, runtime = st["item"], st.get("position_s") or 0, st.get("duration_s") or st["item"].get("runtime_s") or 0
        if runtime and pos >= 0.92 * runtime:
            await self._post(f"/UserPlayedItems/{item['id']}", {}, {"userId": uid})
            _log("VIDEO", f"« {self.title_of(item)} » marqué vu")
        else:
            now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            await self._post(f"/UserItems/{item['id']}/UserData",
                             {"PlaybackPositionTicks": int(pos * TICKS), "LastPlayedDate": now}, {"userId": uid})

    async def _report(self, path: str, event: str | None = None, force: bool = False,
                      playing_started: bool = False, st: dict | None = None) -> None:
        now = time.monotonic()
        if not force and not playing_started and now - self._last_report < REPORT_EVERY_S:
            return
        self._last_report = now
        st = st or self.state
        body = {"ItemId": st["item"]["id"], "MediaSourceId": st["media_source_id"],
                "PlaySessionId": st["play_session_id"], "PositionTicks": int(st["position_s"] * TICKS),
                "IsPaused": not st.get("playing", True), "PlayMethod": "Transcode", "CanSeek": True}
        if event:
            body["EventName"] = event
        try:
            await self._post(path, body)
            if not playing_started:
                await self._save_userdata(st)
        except Exception as e:
            _log("VIDEO", f"rapport {path} : {e}")

    # ── proxy vers Jellyfin (flux HLS + images) ───────────────────────────────
    async def proxy(self, request: web.Request) -> web.StreamResponse:
        path = request.match_info["path"]
        low = path.lower()
        if not (low.startswith("videos/") or (low.startswith("items/") and "/images/" in low)):
            raise web.HTTPForbidden(text="proxy Jellyfin limité aux flux vidéo et aux images")
        if not self.session:
            raise web.HTTPServiceUnavailable(text="Jellyfin désactivé")
        url = f"{self.url}/{path}"
        if request.query_string:
            url += "?" + request.query_string
        try:
            async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=120)) as r:
                resp = web.StreamResponse(status=r.status)
                for h in ("Content-Type", "Content-Length", "Cache-Control", "ETag", "Last-Modified"):
                    if h in r.headers:
                        resp.headers[h] = r.headers[h]
                await resp.prepare(request)
                async for chunk in r.content.iter_chunked(64 * 1024):
                    await resp.write(chunk)
                await resp.write_eof()
                return resp
        except (ConnectionResetError, asyncio.CancelledError):
            raise
        except aiohttp.ClientError as e:
            raise web.HTTPBadGateway(text=f"Jellyfin : {e}")

    # ── API REST ──────────────────────────────────────────────────────────────
    async def api_home(self, _request) -> web.Response:
        try:
            return web.json_response(await self.home())
        except Exception as e:
            return web.json_response({"error": str(e)}, status=502)

    async def api_search(self, request) -> web.Response:
        q = (request.query.get("q") or "").strip()
        if not q:
            return web.json_response([])
        try:
            return web.json_response(await self.search(q))
        except Exception as e:
            return web.json_response({"error": str(e)}, status=502)

    async def api_episodes(self, request) -> web.Response:
        try:
            return web.json_response(await self.episodes(request.match_info["id"]))
        except Exception as e:
            return web.json_response({"error": str(e)}, status=502)

    def add_routes(self, app: web.Application) -> None:
        app.router.add_get("/api/video/home", self.api_home)
        app.router.add_get("/api/video/search", self.api_search)
        app.router.add_get("/api/video/episodes/{id}", self.api_episodes)
        app.router.add_get("/jf/{path:.*}", self.proxy)

    # ── commandes WebSocket ───────────────────────────────────────────────────
    async def handle_command(self, msg: dict) -> dict:
        cmd = msg["type"]
        try:
            if cmd == "video_play":
                st = await self.play(msg["item_id"], msg.get("position_s"), msg.get("subtitle_index"), msg.get("audio_index"))
                return {"type": "ack", "command": cmd, "title": self.title(), "src": st["src"]}
            if cmd == "video_pause":
                await self.set_playing(False)
            elif cmd == "video_resume":
                await self.set_playing(True)
            elif cmd == "video_stop":
                await self.stop_playback()
            elif cmd == "video_seek":
                await self.seek(msg.get("position_s"), msg.get("delta_s"))
            elif cmd == "video_volume":
                await self.set_volume(msg["volume"])
            elif cmd == "video_progress":
                await self.progress(msg.get("position_s") or 0, bool(msg.get("paused")), msg.get("duration_s"))
            elif cmd == "video_ended":
                await self.ended()
            else:
                return {"type": "error", "error": "unknown_command", "command": cmd}
            return {"type": "ack", "command": cmd}
        except Exception as e:
            _log("VIDEO", f"{cmd} : {e}")
            return {"type": "error", "error": str(e), "command": cmd}

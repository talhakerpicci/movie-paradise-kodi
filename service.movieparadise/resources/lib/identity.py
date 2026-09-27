"""Works out what is playing, as ids Movie Paradise understands.

Uses JSON-RPC, which returns unique ids on every supported Kodi version.
We share:
- movies with a TMDB or IMDb id
- episodes as the show's TMDB or IMDb id plus season and episode number
  (for library episodes the ids come from the parent show)
- plugin episodes only if they carry their own IMDb episode id

Skipped: live TV, stacked files, music videos, TVDB-only items, trailers and
extras, and by default anything played through the Plex, Jellyfin or Emby
add-ons (those servers report the plays themselves).

Trailers often carry the movie's ids, so they are caught by source (trailer
add-ons, the library movie's trailer, extras folders and file names) and by
length: a file under half the title's known runtime isn't the title.
"""

import re

SERVER_ADDON_PREFIXES = (
    "plugin://plugin.video.plexmod",
    "plugin://plugin.video.composite_for_plex",
    "plugin://plugin.video.plex",
    "plugin://plugin.video.jellyfin",
    "plugin://plugin.video.jellycon",
    "plugin://plugin.video.emby",
    "plugin://plugin.video.embycon",
)

TRAILER_PLUGIN_PREFIXES = (
    "plugin://plugin.video.youtube",
    "plugin://plugin.video.tubed",
    "plugin://plugin.video.invidious",
)

# Extras naming used by Kodi, Plex, Jellyfin and Emby. Only the unambiguous
# forms: words like "short" or "interview" are also film titles.
_EXTRA_DIRS = ("trailers", "extras", "featurettes", "behind the scenes", "deleted scenes", "interviews", "sample", "samples")
_EXTRA_SUFFIX = re.compile(r"-(trailer|teaser|sample|behindthescenes|deleted|featurette)s?$")
_EXTRA_NAME = re.compile(r"^(trailer|teaser|sample)s?\d*$")

_IMDB = re.compile(r"^tt\d{5,}$")

# A file shorter than this fraction of the title's runtime isn't the title.
RUNTIME_RATIO = 0.5

SKIP_NOT_PLAYING = "not a video"
SKIP_LIVE_TV = "live TV"
SKIP_STACK = "a stacked file"
SKIP_SERVER_ADDON = "played through a media server add-on"
SKIP_TYPE = "not a movie or an episode"
SKIP_NO_IDS = "no supported media id"
SKIP_EXTRA = "a trailer or an extra"


class Identity(object):
    """A title in the API's format. `key` identifies the title, `path` is the
    file that was playing and `runtime_ms` the runtime Kodi knows (or None)."""

    def __init__(self, media, key, label, path="", runtime_ms=None):
        self.media = media
        self.key = key
        self.label = label
        self.path = path
        self.runtime_ms = runtime_ms


def plausible_length(found, duration_ms):
    """False if the file is much shorter than the title's runtime."""
    if found is None or not found.runtime_ms or not duration_ms or duration_ms <= 0:
        return True
    return duration_ms >= RUNTIME_RATIO * found.runtime_ms


def _runtime_ms(item):
    runtime = item.get("runtime")
    return runtime * 1000 if isinstance(runtime, int) and not isinstance(runtime, bool) and runtime > 0 else None


def is_extra(path):
    """Trailer or extra, judging by the path."""
    lowered = (path or "").lower().replace("\\", "/")
    if any(lowered.startswith(p) for p in TRAILER_PLUGIN_PREFIXES):
        return True
    if "://" in lowered and not lowered.startswith(("smb://", "nfs://", "ftp://", "sftp://", "dav://", "davs://", "http://", "https://", "upnp://")):
        return False  # other plugin and virtual paths carry no file-name convention
    parts = [p for p in lowered.split("?")[0].split("/") if p]
    if not parts:
        return False
    for folder in parts[:-1]:
        if folder in _EXTRA_DIRS:
            return True
    stem = parts[-1].rsplit(".", 1)[0]
    return bool(_EXTRA_SUFFIX.search(stem) or _EXTRA_NAME.match(stem))


def _ids(raw):
    """The ids we can use from a JSON-RPC uniqueid map."""
    out = {}
    if not isinstance(raw, dict):
        return out
    tmdb = raw.get("tmdb")
    try:
        tmdb_value = int(str(tmdb).strip()) if tmdb not in (None, "") else 0
    except (TypeError, ValueError):
        tmdb_value = 0
    if tmdb_value > 0:
        out["tmdb"] = tmdb_value
    imdb = str(raw.get("imdb") or "").strip()
    if _IMDB.match(imdb):
        out["imdb"] = imdb
    return out


def _with_imdbnumber(ids, imdbnumber):
    imdbnumber = str(imdbnumber or "").strip()
    if "imdb" not in ids and _IMDB.match(imdbnumber):
        ids = dict(ids)
        ids["imdb"] = imdbnumber
    return ids


def _active_video_player(rpc):
    players = rpc("Player.GetActivePlayers", {}) or []
    for player in players:
        if isinstance(player, dict) and player.get("type") == "video":
            return player.get("playerid")
    return None


def current(rpc, exclude_server_addons=True):
    """(Identity, "") for the playing video, or (None, skip reason)."""
    player_id = _active_video_player(rpc)
    if player_id is None:
        return None, SKIP_NOT_PLAYING
    result = rpc("Player.GetItem", {
        "playerid": player_id,
        "properties": ["uniqueid", "tvshowid", "season", "episode", "showtitle", "title", "file", "imdbnumber", "runtime"],
    }) or {}
    item = result.get("item") if isinstance(result, dict) else None
    if not isinstance(item, dict):
        return None, SKIP_NOT_PLAYING
    return from_item(rpc, item, exclude_server_addons)


def from_item(rpc, item, exclude_server_addons=True):
    path = str(item.get("file") or "")
    lowered = path.lower()
    if lowered.startswith("pvr://") or item.get("type") in ("channel",):
        return None, SKIP_LIVE_TV
    if lowered.startswith("stack://"):
        return None, SKIP_STACK
    if exclude_server_addons and any(lowered.startswith(p) for p in SERVER_ADDON_PREFIXES):
        return None, SKIP_SERVER_ADDON
    if is_extra(path):
        return None, SKIP_EXTRA
    kind = item.get("type")
    library_id = item.get("id") if isinstance(item.get("id"), int) else -1
    if kind == "movie":
        ids = _with_imdbnumber(_ids(item.get("uniqueid")), item.get("imdbnumber"))
        if library_id > 0:
            details = (rpc("VideoLibrary.GetMovieDetails", {"movieid": library_id, "properties": ["uniqueid", "imdbnumber", "trailer"]}) or {}).get("moviedetails") or {}
            trailer = str(details.get("trailer") or "")
            if trailer and _same_path(trailer, path):
                return None, SKIP_EXTRA  # the library movie's own trailer
            if not ids:
                ids = _with_imdbnumber(_ids(details.get("uniqueid")), details.get("imdbnumber"))
        if not ids:
            return None, SKIP_NO_IDS
        key = "movie:" + (("tmdb:%d" % ids["tmdb"]) if "tmdb" in ids else ("imdb:" + ids["imdb"]))
        return Identity({"movie": {"ids": ids}}, key, item.get("title") or "", path, _runtime_ms(item)), ""
    if kind == "episode":
        season, number = item.get("season"), item.get("episode")
        show_id = item.get("tvshowid") if isinstance(item.get("tvshowid"), int) else -1
        if library_id > 0 and show_id > 0 and isinstance(season, int) and isinstance(number, int) and season >= 0 and number >= 1:
            show = (rpc("VideoLibrary.GetTVShowDetails", {"tvshowid": show_id, "properties": ["uniqueid", "imdbnumber"]}) or {}).get("tvshowdetails") or {}
            ids = _with_imdbnumber(_ids(show.get("uniqueid")), show.get("imdbnumber"))
            if ids:
                show_key = ("tmdb:%d" % ids["tmdb"]) if "tmdb" in ids else ("imdb:" + ids["imdb"])
                media = {"show": {"ids": ids}, "episode": {"season": season, "number": number}}
                label = "%s S%02dE%02d" % (item.get("showtitle") or "", season, number)
                return Identity(media, "episode:%s:%d:%d" % (show_key, season, number), label.strip(), path, _runtime_ms(item)), ""
            return None, SKIP_NO_IDS
        # Plugin episodes carry episode ids, not show ids. Only an IMDb episode
        # id identifies one; a TMDB episode id can't be used as the show's.
        episode_ids = _ids(item.get("uniqueid"))
        if "imdb" in episode_ids:
            imdb = episode_ids["imdb"]
            return Identity({"episode": {"ids": {"imdb": imdb}}}, "episode:imdb:" + imdb, item.get("title") or "", path, _runtime_ms(item)), ""
        return None, SKIP_NO_IDS
    return None, SKIP_TYPE


def _same_path(a, b):
    return a.strip().lower().replace("\\", "/") == b.strip().lower().replace("\\", "/")

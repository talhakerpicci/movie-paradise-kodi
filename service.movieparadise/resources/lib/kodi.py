"""Wrappers around Kodi's modules, so the rest can be tested without Kodi."""

import json
import uuid

import xbmc
import xbmcaddon
import xbmcgui
import xbmcvfs

from . import log

ADDON_ID = "service.movieparadise"
API_BASE = "https://api.movieparadise.app"
# Public client id; the server only uses it to label the device as Kodi.
CLIENT_ID = "movieparadise-kodi"
BOOT_PROPERTY = "movieparadise.boot"

_LEVELS = {"DEBUG": xbmc.LOGDEBUG, "INFO": xbmc.LOGINFO, "WARNING": xbmc.LOGWARNING, "ERROR": xbmc.LOGERROR}


def addon():
    return xbmcaddon.Addon(ADDON_ID)


def install_log_sink():
    log.set_sink(lambda message, level: xbmc.log(message, _LEVELS.get(level, xbmc.LOGDEBUG)))


def profile_dir():
    path = xbmcvfs.translatePath(addon().getAddonInfo("profile"))
    if not xbmcvfs.exists(path):
        xbmcvfs.mkdirs(path)
    return path


def text(string_id, **values):
    message = addon().getLocalizedString(string_id)
    for key, value in values.items():
        message = message.replace("{" + key + "}", str(value))
    return message


def setting_bool(setting_id, default=True):
    try:
        return addon().getSettingBool(setting_id)
    except (RuntimeError, TypeError):
        return default


def user_agent():
    version = addon().getAddonInfo("version")
    build = xbmc.getInfoLabel("System.BuildVersion") or "unknown"
    return "MovieParadise-Kodi/%s (Kodi %s)" % (version, build.split(" ")[0])


def device_label():
    return (xbmc.getInfoLabel("System.FriendlyName") or "Kodi")[:80]


def boot_id():
    """An id for this Kodi session, kept in a Home window property. It survives
    a service restart but not a Kodi restart."""
    window = xbmcgui.Window(10000)
    value = window.getProperty(BOOT_PROPERTY)
    if not value:
        value = uuid.uuid4().hex
        window.setProperty(BOOT_PROPERTY, value)
    return value


def rpc(method, params):
    request = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
    try:
        response = json.loads(xbmc.executeJSONRPC(json.dumps(request)))
    except (ValueError, TypeError):
        return None
    if not isinstance(response, dict) or "error" in response:
        return None
    return response.get("result")


def notify(message, error=False):
    if not setting_bool("notifications", True) and not error:
        return
    icon = xbmcgui.NOTIFICATION_ERROR if error else xbmcgui.NOTIFICATION_INFO
    xbmcgui.Dialog().notification(text(32000), message, icon, 6000)


def player_timing(player):
    """(position ms, duration ms) of the playing video, or (None, None)."""
    try:
        if not player.isPlayingVideo():
            return None, None
        return int(player.getTime() * 1000), int(player.getTotalTime() * 1000)
    except RuntimeError:
        return None, None


def playing_file(player):
    """The file Kodi is playing (the resolved stream for a plugin item), or ""."""
    try:
        return player.getPlayingFile() if player.isPlayingVideo() else ""
    except RuntimeError:
        return ""


def is_paused():
    return xbmc.getCondVisibility("Player.Paused")

"""Puts the add-on on sys.path and stubs out Kodi's modules."""

import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ADDON = os.path.join(ROOT, "service.movieparadise")
if ADDON not in sys.path:
    sys.path.insert(0, ADDON)


def _stub(name, **attrs):
    module = types.ModuleType(name)
    module.__dict__.update(attrs)
    sys.modules.setdefault(name, module)
    return module


class _Player(object):
    """Stand-in for xbmc.Player. Tests set `playing` to None or a dict with
    file, position_ms and duration_ms."""

    playing = None

    def __init__(self, *args, **kwargs):
        pass

    def isPlayingVideo(self):
        return self.playing is not None

    def getPlayingFile(self):
        if self.playing is None:
            raise RuntimeError("Kodi is not playing any media file")
        return self.playing["file"]

    def getTime(self):
        if self.playing is None:
            raise RuntimeError("Kodi is not playing any media file")
        return self.playing["position_ms"] / 1000.0

    def getTotalTime(self):
        if self.playing is None:
            raise RuntimeError("Kodi is not playing any media file")
        return self.playing["duration_ms"] / 1000.0


class _Monitor(object):
    def abortRequested(self):
        return True

    def waitForAbort(self, seconds=0):
        return True


_stub("xbmc", Player=_Player, Monitor=_Monitor, LOGDEBUG=0, LOGINFO=1, LOGWARNING=2, LOGERROR=3,
      log=lambda *a, **k: None, getInfoLabel=lambda label: "", getCondVisibility=lambda cond: False,
      executeJSONRPC=lambda request: "{}")
_stub("xbmcaddon", Addon=lambda *a, **k: None)
_stub("xbmcgui", Dialog=object, DialogProgress=object, Window=object,
      NOTIFICATION_INFO="info", NOTIFICATION_ERROR="error")
_stub("xbmcvfs", translatePath=lambda p: p, exists=lambda p: True, mkdirs=lambda p: True)

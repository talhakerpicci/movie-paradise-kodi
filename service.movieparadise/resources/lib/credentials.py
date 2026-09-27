"""The account link, stored in a private file in the add-on's profile.

The token is never written to settings, the queue or the log. Each link gets
a new pairing_id, and queued events carry it, so events recorded under one
account are never sent with another account's token.
"""

import json
import os
import uuid

from . import log

FILE_NAME = "link.json"


class Credentials(object):
    def __init__(self, token, pairing_id, device_id=None, capabilities=None, api_base=None):
        self.token = token
        self.pairing_id = pairing_id
        self.device_id = device_id
        self.capabilities = list(capabilities or [])
        self.api_base = api_base

    def to_dict(self):
        return {
            "token": self.token,
            "pairing_id": self.pairing_id,
            "device_id": self.device_id,
            "capabilities": self.capabilities,
            "api_base": self.api_base,
        }

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, dict) or not value.get("token") or not value.get("pairing_id"):
            return None
        return cls(value["token"], value["pairing_id"], value.get("device_id"), value.get("capabilities"), value.get("api_base"))


def new_pairing_id():
    return uuid.uuid4().hex


class CredentialStore(object):
    def __init__(self, directory):
        self.directory = directory
        self.path = os.path.join(directory, FILE_NAME)

    def load(self):
        try:
            with open(self.path, "r") as handle:
                creds = Credentials.from_dict(json.load(handle))
        except (OSError, IOError, ValueError):
            return None
        if creds is not None:
            log.remember_secret(creds.token)
        return creds

    def save(self, creds):
        """Write to a temp file and rename it over the old one."""
        if not os.path.isdir(self.directory):
            os.makedirs(self.directory)
        tmp = self.path + ".tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        fd = os.open(tmp, flags, 0o600)
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump(creds.to_dict(), handle)
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise
        os.replace(tmp, self.path)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        log.remember_secret(creds.token)

    def clear(self):
        current = self.load()
        try:
            os.remove(self.path)
        except OSError:
            pass
        if current is not None:
            log.forget_secret(current.token)

    def clear_if(self, pairing_id):
        """Remove the link only if it is still `pairing_id`, so a late 401 for
        an old link can't delete a new one. Returns True if it removed it."""
        current = self.load()
        if current is None or current.pairing_id != pairing_id:
            return False
        try:
            os.remove(self.path)
        except OSError:
            return False
        log.forget_secret(current.token)
        return True

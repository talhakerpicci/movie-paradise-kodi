#!/usr/bin/env python3
"""Build the release: versioned add-on zips and the repository index.

    python3 tools/build_release.py [--out dist]

Output (the layout Kodi 19+ repositories read, the <dir> form):

    dist/
      addons.xml              every add-on's <addon> manifest
      addons.xml.md5          its checksum (Kodi's change check)
      service.movieparadise/service.movieparadise-<version>.zip
      repository.movieparadise/repository.movieparadise-<version>.zip

Zips only contain the add-on files (no caches or dotfiles) and use fixed
timestamps, so building the same sources twice gives identical zips.
"""

import argparse
import hashlib
import os
import sys
import xml.etree.ElementTree as ET
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ADDONS = ("service.movieparadise", "repository.movieparadise")
SKIP_DIRS = {"__pycache__", ".pytest_cache", ".git"}
SKIP_SUFFIXES = (".pyc", ".pyo", ".orig", ".rej", ".swp")
FIXED_TIME = (2026, 1, 1, 0, 0, 0)


def release_files(addon_dir):
    for base, dirs, files in os.walk(addon_dir):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith("."))
        for name in sorted(files):
            if name.startswith(".") or name.endswith(SKIP_SUFFIXES):
                continue
            yield os.path.join(base, name)


def manifest(addon_dir):
    return ET.parse(os.path.join(addon_dir, "addon.xml")).getroot()


def build_zip(addon_id, out_dir):
    addon_dir = os.path.join(ROOT, addon_id)
    root = manifest(addon_dir)
    version = root.get("version")
    target_dir = os.path.join(out_dir, addon_id)
    os.makedirs(target_dir, exist_ok=True)
    target = os.path.join(target_dir, "%s-%s.zip" % (addon_id, version))
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in release_files(addon_dir):
            arcname = os.path.join(addon_id, os.path.relpath(path, addon_dir)).replace(os.sep, "/")
            info = zipfile.ZipInfo(arcname, FIXED_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            with open(path, "rb") as handle:
                archive.writestr(info, handle.read())
    return target, root


def check_hosting(allow_placeholder):
    """Refuse to build while the repository URL is still a placeholder."""
    with open(os.path.join(ROOT, "repository.movieparadise", "addon.xml"), encoding="utf-8") as handle:
        placeholder = "HOSTING_URL" in handle.read()
    if placeholder and not allow_placeholder:
        raise SystemExit("repository.movieparadise/addon.xml still says HOSTING_URL: set the hosting URL first "
                         "(or pass --allow-placeholder for a local test build)")


def build(out_dir):
    os.makedirs(out_dir, exist_ok=True)
    manifests = []
    for addon_id in ADDONS:
        target, root = build_zip(addon_id, out_dir)
        manifests.append(ET.tostring(root, encoding="unicode").strip())
        print("built", os.path.relpath(target, ROOT))
    index = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<addons>\n%s\n</addons>\n' % "\n".join(manifests)
    with open(os.path.join(out_dir, "addons.xml"), "w", encoding="utf-8") as handle:
        handle.write(index)
    with open(os.path.join(out_dir, "addons.xml.md5"), "w") as handle:
        handle.write(hashlib.md5(index.encode("utf-8")).hexdigest())
    print("built addons.xml + addons.xml.md5")


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", default=os.path.join(ROOT, "dist"))
    parser.add_argument("--allow-placeholder", action="store_true", help="build even though the hosting URL is unset (local tests only)")
    args = parser.parse_args(argv)
    check_hosting(args.allow_placeholder)
    build(args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

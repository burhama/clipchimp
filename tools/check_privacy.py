"""Privacy check for this repository (standard library only).

    python tools/check_privacy.py                 check the working tree against tools/manifest.json
    python tools/check_privacy.py --commits A..B  also check every commit in A..B (identity, time zone, message)
    python tools/check_privacy.py --binary DIR    check built files (strings) before they are published

It prints rule names and file names only, and exits 1 on any problem. It keeps the repository to an exact list
of files: every image is pinned by its SHA-256, so a changed or new picture fails until the list is updated on
purpose.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import struct
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "tools" / "manifest.json"
BINARY_EXT = {".png", ".gif", ".ico"}
PNG_CHUNKS = {b"IHDR", b"PLTE", b"tRNS", b"sRGB", b"gAMA", b"pHYs", b"IDAT", b"IEND"}
NOREPLY = re.compile(r"^[0-9]+\+[A-Za-z0-9-]+@users\.noreply\.github\.com$")
GITHUB_WEB = re.compile(r"^noreply@github\.com$")   # the committer of merges and edits made on github.com

TEXT_RULES = {
    "user-folder-path": re.compile(r"(?i)\b[a-z]:[\\/]+(users|documents and settings)[\\/]+[^\\/\s\"']+"),
    "home-path": re.compile(r"(?<![\w.])/(home|Users)/[A-Za-z0-9._-]+"),
    "email": re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    "ipv4": re.compile(r"\b(?!127\.0\.0\.1\b)(?!0\.0\.0\.0\b)(?:\d{1,3}\.){3}\d{1,3}\b"),
    "github-token": re.compile(r"\b(ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"),
    "api-key": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}|\bAKIA[0-9A-Z]{16}\b|\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    "private-key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "jwt": re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
}
EMAIL_OK = re.compile(r"@users\.noreply\.github\.com$|@example\.(com|org)$")

problems: list[tuple[str, str]] = []


def problem(rule: str, where: str) -> None:
    problems.append((rule, where))


def tracked_files() -> list[str]:
    out = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z"], check=True, capture_output=True).stdout
    return sorted(p for p in out.decode("utf-8").split("\0") if p)


def check_text(name: str, text: str) -> None:
    for rule, rx in TEXT_RULES.items():
        for m in rx.finditer(text):
            if rule == "email" and EMAIL_OK.search(m.group(0)):
                continue
            problem(rule, name)
            break


def png_chunks(data: bytes, name: str) -> None:
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        problem("not-a-png", name); return
    i = 8
    while i + 8 <= len(data):
        n = struct.unpack(">I", data[i:i + 4])[0]
        kind = data[i + 4:i + 8]
        if kind not in PNG_CHUNKS:
            problem("png-metadata-" + kind.decode("latin1", "replace"), name)
        i += 12 + n
        if kind == b"IEND":
            break
    if i != len(data):
        problem("png-trailing-data", name)


def gif_blocks(data: bytes, name: str) -> None:
    if data[:6] not in (b"GIF87a", b"GIF89a"):
        problem("not-a-gif", name); return
    flags = data[10]
    i = 13 + (3 * 2 ** ((flags & 7) + 1) if flags & 0x80 else 0)
    while i < len(data):
        t = data[i]
        if t == 0x3B:
            if i + 1 != len(data):
                problem("gif-trailing-data", name)
            return
        if t == 0x21:
            label, j = data[i + 1], i + 2
            first = data[j + 1:j + 1 + data[j]]
            if not (label == 0xF9 or (label == 0xFF and first.startswith(b"NETSCAPE2.0"))):
                problem("gif-metadata-block", name)
            while data[j]:
                j += data[j] + 1
            i = j + 1
        elif t == 0x2C:
            f = data[i + 9]
            j = i + 10 + (3 * 2 ** ((f & 7) + 1) if f & 0x80 else 0) + 1
            while data[j]:
                j += data[j] + 1
            i = j + 1
        else:
            problem("gif-malformed", name); return
    problem("gif-no-trailer", name)


def ico_images(data: bytes, name: str) -> None:
    reserved, kind, count = struct.unpack("<HHH", data[:6])
    if reserved != 0 or kind != 1 or not count:
        problem("not-an-ico", name); return
    end = 6 + 16 * count
    for k in range(count):
        size, offset = struct.unpack("<II", data[6 + 16 * k + 8:6 + 16 * k + 16])
        img = data[offset:offset + size]
        end = max(end, offset + size)
        if img.startswith(b"\x89PNG"):
            png_chunks(img, f"{name}#{k}")
        elif struct.unpack("<I", img[:4])[0] != 40:
            problem("ico-unknown-image", f"{name}#{k}")
    if end != len(data):
        problem("ico-trailing-data", name)


def check_tree() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    files = tracked_files()
    for f in files:
        if f not in manifest:
            problem("not-in-manifest", f)
    for f in manifest:
        if f not in files:
            problem("in-manifest-but-missing", f)
    for f in files:
        data = (ROOT / f).read_bytes()
        ext = Path(f).suffix.lower()
        if ext in BINARY_EXT:
            if manifest.get(f) != hashlib.sha256(data).hexdigest():
                problem("image-not-pinned", f)
            {".png": png_chunks, ".gif": gif_blocks, ".ico": ico_images}[ext](data, f)
        elif f in manifest and manifest[f] is not None:
            problem("text-file-pinned", f)
        else:
            try:
                check_text(f, data.decode("utf-8"))
            except UnicodeDecodeError:
                problem("unexpected-binary", f)


def owner_login() -> str:
    login = os.environ.get("GITHUB_REPOSITORY_OWNER", "")
    if not login:
        url = subprocess.run(["git", "-C", str(ROOT), "remote", "get-url", "origin"],
                             capture_output=True, text=True).stdout.strip()
        m = re.search(r"github\.com[/:]([^/]+)/", url)
        login = m.group(1) if m else ""
    return login.lower()


def check_commits(span: str) -> None:
    """Every commit: GitHub noreply addresses only (GitHub's own web merges allowed), nothing private in the
    message, and the repository owner's commits carry the UTC offset (+0000) so they reveal no time zone."""
    owner = owner_login()
    fmt = "%H%x1f%ae%x1f%ce%x1f%ad%x1f%cd%x1f%B%x1e"
    out = subprocess.run(["git", "-C", str(ROOT), "log", "--date=raw", f"--format={fmt}", span],
                         check=True, capture_output=True, text=True, encoding="utf-8").stdout
    for rec in filter(None, (r.strip() for r in out.split("\x1e"))):
        sha, ae, ce, ad, cd, body = rec.split("\x1f")
        if not NOREPLY.match(ae) or not (NOREPLY.match(ce) or GITHUB_WEB.match(ce)):
            problem("commit-email-not-noreply", sha[:8])
        for email, date in ((ae, ad), (ce, cd)):
            if owner and email.lower().endswith(f"+{owner}@users.noreply.github.com") and not date.endswith("+0000"):
                problem("owner-commit-not-utc", sha[:8])
                break
        check_text("commit " + sha[:8], body)


def check_binary(folder: Path) -> None:
    """Built files: no path into anyone's user folder (GitHub's build machine excepted) and no secrets. Email
    addresses are not checked here: the bundled third-party licences carry their authors' addresses."""
    printable = re.compile(rb"[\x20-\x7e]{6,}")
    for p in sorted(folder.rglob("*")):
        if p.is_file():
            text = "\n".join(m.group(0).decode("ascii") for m in printable.finditer(p.read_bytes()))
            text = re.sub(r"(?i)[a-z]:[\\/]+users[\\/]+runneradmin\b", "", text)   # the build machine is GitHub's
            for rule in ("user-folder-path", "github-token", "api-key", "private-key"):
                if TEXT_RULES[rule].search(text):
                    problem("binary-" + rule, str(p.relative_to(folder)))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--commits")
    ap.add_argument("--binary")
    a = ap.parse_args()
    if a.binary:
        check_binary(Path(a.binary))
    else:
        check_tree()
        if a.commits:
            check_commits(a.commits)
    if not problems:
        print("privacy check: clean")
        return 0
    for rule, where in problems:
        print(f"privacy check: {rule}: {where}")
    return 1


if __name__ == "__main__":
    sys.exit(main())

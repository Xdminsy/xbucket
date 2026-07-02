#!/usr/bin/env python3
import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path


USER_AGENT = "xbucket-new-manifest"


def github_api(url):
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": USER_AGENT,
        },
    )
    with urllib.request.urlopen(req) as resp:
        return json.load(resp)


def parse_github_url(raw_url):
    parsed = urllib.parse.urlparse(raw_url)
    if parsed.netloc not in ("github.com", "www.github.com"):
        raise SystemExit("Only GitHub URLs are supported.")

    parts = [urllib.parse.unquote(p) for p in parsed.path.strip("/").split("/") if p]
    if len(parts) < 2:
        raise SystemExit("Expected a GitHub repository URL, release URL, or release asset URL.")

    owner = parts[0]
    repo = re.sub(r"\.git$", "", parts[1])
    tag = None
    asset_name = None

    if len(parts) >= 5 and parts[2:4] == ["releases", "tag"]:
        tag = parts[4]

    if len(parts) >= 6 and parts[2:4] == ["releases", "download"]:
        tag = parts[4]
        asset_name = parts[5]

    return {
        "owner": owner,
        "repo": repo,
        "tag": tag,
        "asset_name": asset_name,
        "api_url": f"https://api.github.com/repos/{owner}/{repo}",
    }


def manifest_name(value):
    value = re.sub(r"\.desktop$", "", value)
    value = re.sub(r"[^A-Za-z0-9._+-]+", "-", value)
    return value.strip("-").lower()


def version_from_tag(tag):
    if not tag:
        raise SystemExit("Release tag is empty.")
    return re.sub(r"^[vV]", "", tag)


def asset_score(name, architecture):
    lower = name.lower()
    score = 0

    if re.search(r"win|windows", lower):
        score += 50
    if architecture == "64bit" and re.search(r"x64|x86_64|amd64|win64|64", lower):
        score += 35
    if architecture == "arm64" and re.search(r"arm64|aarch64", lower):
        score += 35
    if re.search(r"portable|green", lower):
        score += 30
    if re.search(r"\.(zip|7z)$", lower):
        score += 25
    if lower.endswith(".exe"):
        score += 10
    if re.search(r"setup|installer|install", lower):
        score -= 8
    if re.search(r"symbols|debug|source|src|sha256|checksums?", lower):
        score -= 100
    if re.search(r"\.(zip|7z|exe)$", lower):
        score += 10

    return score


def select_asset(assets, architecture, preferred_name=None):
    if preferred_name:
        for asset in assets:
            if asset.get("name") == preferred_name:
                return asset
        raise SystemExit(f"The requested asset was not found in this release: {preferred_name}")

    candidates = []
    for asset in assets:
        name = asset.get("name", "")
        if not asset.get("browser_download_url"):
            continue
        if not re.search(r"\.(zip|7z|exe)$", name, re.IGNORECASE):
            continue
        score = asset_score(name, architecture)
        if score > 0:
            candidates.append((score, name, asset))

    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    if not candidates:
        raise SystemExit("No suitable Windows .zip, .7z, or .exe release asset was found.")

    top_score = candidates[0][0]
    top = [item for item in candidates if item[0] == top_score]
    if len(top) == 1:
        return top[0][2]

    print("Multiple release assets look suitable:")
    for i, (_, name, _) in enumerate(candidates[:10], start=1):
        print(f"[{i}] {name}")

    while True:
        choice = input("Select asset number: ").strip()
        if choice.isdigit() and 1 <= int(choice) <= min(len(candidates), 10):
            return candidates[int(choice) - 1][2]


def autoupdate_url(download_url, tag, version):
    result = download_url
    if tag:
        replacement = "v$version" if re.match(r"^[vV]", tag) else "$version"
        result = result.replace(tag, replacement, 1)
    if version:
        result = result.replace(version, "$version", 1)
    return result


def inspect_zip(path):
    try:
        with zipfile.ZipFile(path) as archive:
            names = [name for name in archive.namelist() if name and not name.endswith("/")]
    except zipfile.BadZipFile:
        return None, None

    if not names:
        return None, None

    top_dirs = {
        name.split("/", 1)[0]
        for name in names
        if "/" in name and name.split("/", 1)[0]
    }
    extract_dir = top_dirs.pop() if len(top_dirs) == 1 else None

    exe_names = [name for name in names if name.lower().endswith(".exe")]
    top_level_exes = [
        name
        for name in exe_names
        if "/" not in name or (extract_dir and name.count("/") == 1 and name.startswith(f"{extract_dir}/"))
    ]
    preferred_exes = [
        name
        for name in top_level_exes or exe_names
        if not re.search(r"ffmpeg|pythonw?|runtime|helper|unins", name, re.IGNORECASE)
    ]

    exe_name = None
    if preferred_exes:
        exe_name = preferred_exes[0]
        if extract_dir and exe_name.startswith(f"{extract_dir}/"):
            exe_name = exe_name[len(extract_dir) + 1 :]

    return extract_dir, exe_name


def download_and_hash(url, asset_name):
    fd, temp_path = tempfile.mkstemp(prefix="xbucket-", suffix=".download")
    os.close(fd)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        digest = hashlib.sha256()
        saw_inno = False

        with urllib.request.urlopen(req) as resp, open(temp_path, "wb") as out:
            while True:
                chunk = resp.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                if b"Inno Setup" in chunk:
                    saw_inno = True
                out.write(chunk)

        extract_dir = None
        exe_name = None
        if asset_name.lower().endswith(".zip"):
            extract_dir, exe_name = inspect_zip(temp_path)

        return digest.hexdigest(), saw_inno, extract_dir, exe_name
    finally:
        try:
            os.remove(temp_path)
        except FileNotFoundError:
            pass


def guessed_exe_name(asset_name, fallback):
    base = Path(asset_name).stem
    base = re.sub(r"[-_]?win(dows)?[-_]?.*$", "", base, flags=re.IGNORECASE)
    base = re.sub(r"[-_]?x(86_)?64.*$", "", base, flags=re.IGNORECASE)
    base = re.sub(r"[-_]?amd64.*$", "", base, flags=re.IGNORECASE)
    base = re.sub(r"[-_]?arm64.*$", "", base, flags=re.IGNORECASE)
    base = re.sub(r"[-_]?portable.*$", "", base, flags=re.IGNORECASE)
    base = re.sub(r"[-_]?green.*$", "", base, flags=re.IGNORECASE)
    base = re.sub(r"[-_]?v?\d+(\.\d+.*)?$", "", base, flags=re.IGNORECASE)
    return f"{base}.exe" if base else f"{fallback}.exe"


def choose_entrypoint(args, default_exe, default_shortcut, auto_shortcut=False):
    if args.bin or args.exe or args.shortcut:
        return args.bin, args.exe, args.shortcut

    if auto_shortcut:
        return None, default_exe, default_shortcut

    print("Entrypoint was not specified.")
    print("[1] GUI shortcut")
    print("[2] Command-line bin")
    print("[3] Both")
    print("[4] None")

    while True:
        choice = input("Select entrypoint type: ").strip()
        if choice in {"1", "2", "3", "4"}:
            break

    bin_name = None
    exe_name = None
    shortcut_name = None

    if choice in {"1", "3"}:
        exe_name = input(f"Executable path for shortcut [{default_exe}]: ").strip() or default_exe
        shortcut_name = input(f"Shortcut name [{default_shortcut}]: ").strip() or default_shortcut

    if choice in {"2", "3"}:
        bin_name = input(f"Executable path for bin [{default_exe}]: ").strip() or default_exe

    return bin_name, exe_name, shortcut_name


def write_manifest(path, manifest, force):
    if path.exists() and not force:
        raise SystemExit(f"Manifest already exists: {path}. Use --force to overwrite it.")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=4) + "\n", encoding="utf-8")


def build_manifest(args):
    repo_info = parse_github_url(args.url)
    name = manifest_name(args.name or repo_info["repo"])
    root = Path(__file__).resolve().parents[1]
    manifest_path = root / "bucket" / f"{name}.json"

    if manifest_path.exists() and not args.force:
        raise SystemExit(f"Manifest already exists: {manifest_path}. Use --force to overwrite it.")

    repo = github_api(repo_info["api_url"])
    release_api = (
        f'{repo_info["api_url"]}/releases/tags/{repo_info["tag"]}'
        if repo_info["tag"]
        else f'{repo_info["api_url"]}/releases/latest'
    )
    release = github_api(release_api)
    asset = select_asset(release.get("assets", []), args.architecture, repo_info["asset_name"])

    version = version_from_tag(release.get("tag_name"))
    download_url = asset["browser_download_url"]
    hash_value, saw_inno, extract_dir, inspected_exe = download_and_hash(download_url, asset["name"])
    default_exe = inspected_exe or guessed_exe_name(asset["name"], name)
    default_shortcut = repo.get("name") or name
    bin_name, exe_name, shortcut_name = choose_entrypoint(
        args,
        default_exe,
        default_shortcut,
        auto_shortcut=bool(inspected_exe),
    )

    license_info = repo.get("license") or {}
    license_id = license_info.get("spdx_id")
    if not license_id or license_id == "NOASSERTION":
        license_id = "Unknown"

    description = args.description or repo.get("description") or input("Description: ").strip()

    manifest = {
        "version": version,
        "description": description,
        "homepage": repo.get("html_url"),
        "license": license_id,
    }

    is_exe = asset["name"].lower().endswith(".exe")
    if is_exe:
        manifest["url"] = download_url
        manifest["hash"] = hash_value
        if saw_inno:
            manifest["depends"] = "innounp"
            manifest["installer"] = {
                "script": 'Expand-InnoArchive -Path "$dir\\$fname" -Removal'
            }
    else:
        manifest["architecture"] = {
            args.architecture: {
                "url": download_url,
                "hash": hash_value,
            }
        }
        if extract_dir:
            manifest["architecture"][args.architecture]["extract_dir"] = extract_dir

    if bin_name:
        manifest["bin"] = bin_name

    if exe_name or shortcut_name:
        manifest["shortcuts"] = [[exe_name or default_exe, shortcut_name or default_shortcut]]

    update_url = autoupdate_url(download_url, release.get("tag_name"), version)
    manifest["checkver"] = "github"
    if is_exe:
        manifest["autoupdate"] = {"url": update_url}
    else:
        manifest["autoupdate"] = {
            "architecture": {
                args.architecture: {
                    "url": update_url,
                }
            }
        }

    return manifest_path, manifest


def main():
    parser = argparse.ArgumentParser(
        description="Create a Scoop manifest from a GitHub repository, release, or release asset URL."
    )
    parser.add_argument("url", help="GitHub repository, release, or release asset URL")
    parser.add_argument("-n", "--name", help="Manifest name. Defaults to the repository name.")
    parser.add_argument("-d", "--description", help="Manifest description. Defaults to the GitHub repo description.")
    parser.add_argument("--bin", help="Command-line executable path to expose through Scoop.")
    parser.add_argument("--exe", help="GUI executable path for a Start Menu shortcut.")
    parser.add_argument("--shortcut", help="Start Menu shortcut display name.")
    parser.add_argument("--architecture", choices=("64bit", "arm64"), default="64bit")
    parser.add_argument("-f", "--force", action="store_true", help="Overwrite an existing manifest.")
    args = parser.parse_args()

    try:
        manifest_path, manifest = build_manifest(args)
        write_manifest(manifest_path, manifest, args.force)
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"GitHub or download request failed: HTTP {exc.code} {exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f"Network request failed: {exc.reason}") from exc

    print(f"Created {manifest_path}")


if __name__ == "__main__":
    main()

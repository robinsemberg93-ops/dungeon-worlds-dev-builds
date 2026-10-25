"""GitHub-hosted copy only: no signing key, no game sources, no publication/deletion."""
import gzip
import hashlib
import json
import os
import re
import time
import urllib.request
import urllib.parse

REPOSITORY = "robinsemberg93-ops/dungeon-worlds-dev-builds"
API = "https://api.github.com/repos/" + REPOSITORY


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        destination = urllib.parse.urlsplit(newurl)
        if (destination.scheme != "https" or destination.username or destination.password
                or destination.port not in (None, 443)
                or destination.hostname not in ("release-assets.githubusercontent.com", "objects.githubusercontent.com")):
            raise ValueError("Unexpected asset redirect destination")
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None:
            redirected.remove_header("Authorization")
        return redirected


def request(url, token, data=None, binary=False):
    headers = {"Authorization": "Bearer " + token, "User-Agent": "DungeonWorlds-ChunkReuse",
               "Accept": "application/octet-stream" if binary else "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28"}
    if data is not None:
        headers["Content-Type"] = "application/octet-stream"
    with urllib.request.build_opener(SafeRedirect()).open(urllib.request.Request(url, data=data, headers=headers), timeout=120) as response:
        payload = response.read(10 * 1024 * 1024 + 1)
    if len(payload) > 10 * 1024 * 1024:
        raise ValueError("Response exceeds chunk/metadata limit")
    return payload if binary else json.loads(payload)


def assets(release_id, token):
    result = {}
    for page in range(1, 12):
        batch = request(f"{API}/releases/{release_id}/assets?per_page=100&page={page}", token)
        for item in batch:
            if item["name"] in result:
                raise ValueError("Duplicate asset name")
            result[item["name"]] = item
        if len(batch) < 100:
            return result
    raise ValueError("Too many assets")


def validate_chunk(data, asset, chunk):
    if len(data) != chunk["packedSize"] or len(data) != asset["size"]:
        raise ValueError("Compressed size mismatch")
    if "sha256:" + hashlib.sha256(data).hexdigest() != asset.get("digest"):
        raise ValueError("Compressed digest mismatch")
    # Bound decompression even if a privileged maintainer supplies malformed metadata.
    import io
    with gzip.GzipFile(fileobj=io.BytesIO(data)) as packed:
        raw = packed.read(8 * 1024 * 1024 + 1)
    if len(raw) != chunk["size"] or hashlib.sha256(raw).hexdigest() != chunk["hash"]:
        raise ValueError("Raw chunk hash/size mismatch")


def main():
    if os.environ.get("GITHUB_REPOSITORY") != REPOSITORY:
        raise ValueError("Unexpected repository")
    token = os.environ["GH_TOKEN"]
    source_id, target_id = os.environ["SOURCE_ID"], os.environ["TARGET_ID"]
    if not source_id.isdigit() or not target_id.isdigit() or source_id == target_id:
        raise ValueError("Invalid release IDs")
    source = request(f"{API}/releases/{source_id}", token)
    target = request(f"{API}/releases/{target_id}", token)
    if source["draft"] or not target["draft"]:
        raise ValueError("Source must be published and target must be a draft")
    for release in (source, target):
        if not re.fullmatch(r"dev-\d{1,3}\.\d{2}\.\d{2}", release["tag_name"]):
            raise ValueError("Not a Dev release")
    source_assets, target_assets = assets(source_id, token), assets(target_id, token)
    manifest_asset = target_assets["manifest.json"]
    manifest_data = request(f"{API}/releases/assets/{manifest_asset['id']}", token, binary=True)
    if "sha256:" + hashlib.sha256(manifest_data).hexdigest() != manifest_asset.get("digest"):
        raise ValueError("Manifest download changed")
    manifest = json.loads(manifest_data)
    if manifest["channel"] != "dev" or manifest["tag"] != target["tag_name"]:
        raise ValueError("Manifest release mismatch")
    chunks = {chunk["hash"]: chunk for file in manifest["files"] for chunk in file["chunks"]}
    if len(chunks) > 997:
        raise ValueError("Release asset limit")
    allowed_text = os.environ.get("REUSE_HASHES", "")
    allowed = allowed_text.split(",")
    if (not allowed_text or len(allowed) > 997 or len(set(allowed)) != len(allowed)
            or any(not re.fullmatch("[0-9a-f]{64}", value) or value not in chunks for value in allowed)):
        raise ValueError("Invalid explicitly authorized chunk hashes")
    allowed = set(allowed)
    copied = total = 0
    for raw_hash, chunk in chunks.items():
        if not re.fullmatch("[0-9a-f]{64}", raw_hash) or not 0 < chunk["size"] <= 8 * 1024 * 1024:
            raise ValueError("Invalid chunk")
        name = raw_hash + ".gz"
        if raw_hash not in allowed:
            continue
        if name in target_assets or name not in source_assets:
            continue
        asset = source_assets[name]
        if asset["size"] != chunk["packedSize"]:
            continue  # Different compression representation: upload local candidate instead.
        data = request(f"{API}/releases/assets/{asset['id']}", token, binary=True)
        validate_chunk(data, asset, chunk)
        if not request(f"{API}/releases/{target_id}", token)["draft"]:
            raise ValueError("Target was published concurrently")
        uploaded = request(f"https://uploads.github.com/repos/{REPOSITORY}/releases/{target_id}/assets?name={name}", token, data=data)
        if uploaded.get("digest") != asset["digest"] or uploaded["size"] != len(data):
            raise ValueError("Uploaded chunk mismatch")
        copied += 1
        total += len(data)
        time.sleep(1)
    current_assets = assets(target_id, token)
    if current_assets["manifest.json"]["digest"] != manifest_asset["digest"]:
        raise ValueError("Draft manifest changed during copy; rerun publisher")
    print(f"Copied {copied} chunks, {total} bytes within GitHub. Draft remains unpublished.")


if __name__ == "__main__":
    main()

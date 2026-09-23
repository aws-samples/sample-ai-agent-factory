"""Resolve a public ECR image tag to its exact digest without a container engine.

Used to pin fixture and base images by content address (the Dockerfile contract
is digest-pinned). Anonymous read against the public ECR registry v2 API:

  python resolve_public_image_digest.py docker/library/python 3.11.0-slim-bullseye

Prints the index digest, whether a linux/arm64 manifest exists, and that
manifest's digest. No AWS credentials, no writes.
"""

from __future__ import annotations

import json
import sys

import httpx

REGISTRY = "https://public.ecr.aws"
ACCEPT = ", ".join(
    [
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    ]
)


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    repository, tag = sys.argv[1], sys.argv[2]
    with httpx.Client(timeout=30, follow_redirects=True) as client:
        token = client.get(
            f"{REGISTRY}/token/", params={"scope": f"repository:{repository}:pull"}
        )
        token.raise_for_status()
        headers = {
            "Authorization": f"Bearer {token.json()['token']}",
            "Accept": ACCEPT,
        }
        resp = client.get(f"{REGISTRY}/v2/{repository}/manifests/{tag}", headers=headers)
        resp.raise_for_status()
        body = resp.json()
        result = {
            "repository": repository,
            "tag": tag,
            "indexDigest": resp.headers.get("docker-content-digest"),
            "mediaType": body.get("mediaType") or resp.headers.get("content-type"),
            "arm64ManifestDigest": None,
            "platforms": [],
        }
        for manifest in body.get("manifests", []):
            platform = manifest.get("platform") or {}
            key = f"{platform.get('os')}/{platform.get('architecture')}"
            result["platforms"].append(key)
            if key == "linux/arm64":
                result["arm64ManifestDigest"] = manifest.get("digest")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

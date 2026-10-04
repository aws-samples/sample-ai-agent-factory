"""An in-memory S3 client that models object versions the way S3 does (F-60).

A ``MagicMock`` cannot tell a hard delete from a delete marker, because both are just a
successful ``delete_object`` call. So a suite built on one reports a teardown as
complete while every byte is still readable by VersionId. This fake implements the
real semantics:

* ``delete_object`` without a VersionId on an Enabled or Suspended bucket writes a
  delete marker and removes nothing. On a never-versioned bucket it removes the object.
* A never-versioned bucket's one version id is the literal ``"null"``.
* ``get_object_tagging`` reads the current version, or the one named. Reading a marker
  fails, as S3 does (NoSuchKey for the current version, MethodNotAllowed by id).
* ``list_object_versions`` filters by prefix, not by exact key.
"""

from __future__ import annotations

import itertools

from botocore.exceptions import ClientError


def client_error(code: str, op: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, op)


class FakeVersionedS3:
    def __init__(self, versioning: str = "Enabled"):
        assert versioning in ("Enabled", "Suspended", "Never")
        self.versioning = versioning
        # (bucket, key) -> [{"VersionId", "marker", "tags"}], oldest first
        self.objects: dict[tuple[str, str], list[dict]] = {}
        self._ids = itertools.count(1)
        self.calls: list[tuple[str, dict]] = []
        self.deny: set[str] = set()

    # --- test setup -------------------------------------------------------------------
    def put(self, bucket: str, key: str, tags: dict[str, str]) -> str:
        version_id = "null" if self.versioning == "Never" else f"v{next(self._ids)}"
        entries = self.objects.setdefault((bucket, key), [])
        if version_id == "null":
            entries[:] = [e for e in entries if e["VersionId"] != "null"]
        entries.append({"VersionId": version_id, "marker": False, "tags": dict(tags)})
        return version_id

    def data_versions(self, bucket: str, key: str) -> list[str]:
        return [e["VersionId"] for e in self.objects.get((bucket, key), []) if not e["marker"]]

    def markers(self, bucket: str, key: str) -> list[str]:
        return [e["VersionId"] for e in self.objects.get((bucket, key), []) if e["marker"]]

    # --- the S3 API the product calls -------------------------------------------------
    def _check(self, op: str, kwargs: dict) -> None:
        self.calls.append((op, dict(kwargs)))
        if op in self.deny:
            raise client_error("AccessDenied", op)

    def list_object_versions(self, *, Bucket, Prefix="", **kwargs):
        self._check("ListObjectVersions", {"Bucket": Bucket, "Prefix": Prefix, **kwargs})
        versions, markers = [], []
        for (bucket, key), entries in sorted(self.objects.items()):
            if bucket != Bucket or not key.startswith(Prefix):
                continue
            for i, e in enumerate(entries):
                row = {"Key": key, "VersionId": e["VersionId"], "IsLatest": i == len(entries) - 1}
                (markers if e["marker"] else versions).append(row)
        return {"Versions": versions, "DeleteMarkers": markers, "IsTruncated": False}

    def get_object_tagging(self, *, Bucket, Key, VersionId=None, **kwargs):
        self._check("GetObjectTagging", {"Bucket": Bucket, "Key": Key, "VersionId": VersionId, **kwargs})
        entries = self.objects.get((Bucket, Key), [])
        if VersionId is None:
            if not entries or entries[-1]["marker"]:
                raise client_error("NoSuchKey", "GetObjectTagging")
            entry = entries[-1]
        else:
            match = [e for e in entries if e["VersionId"] == VersionId]
            if not match:
                raise client_error("NoSuchVersion", "GetObjectTagging")
            entry = match[0]
            if entry["marker"]:
                raise client_error("MethodNotAllowed", "GetObjectTagging")
        return {"TagSet": [{"Key": k, "Value": v} for k, v in entry["tags"].items()]}

    def delete_object(self, *, Bucket, Key, VersionId=None, **kwargs):
        self._check("DeleteObject", {"Bucket": Bucket, "Key": Key, "VersionId": VersionId, **kwargs})
        entries = self.objects.setdefault((Bucket, Key), [])
        if VersionId is not None:
            entries[:] = [e for e in entries if e["VersionId"] != VersionId]
        elif self.versioning == "Never":
            entries.clear()
        else:
            marker_id = "null" if self.versioning == "Suspended" else f"m{next(self._ids)}"
            entries.append({"VersionId": marker_id, "marker": True, "tags": {}})
        return {}

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class FakeBlob:
    data: bytes
    metadata: dict[str, str]
    content_md5: bytes | None = None


class FakeBlobStore:
    def __init__(
        self,
        bucket: str = "lab5-gemini-dev1-credit-docs",
        container: str = "credit-policies",
    ) -> None:
        self.bucket = bucket
        self.container = container
        self.blobs: dict[str, FakeBlob] = {}
        self.container_created = False
        self.public_access: str | None = "unset"
        self.uploads: list[str] = []
        self.deletes: list[str] = []
        self.sha_lookups: list[str] = []
        self.fail_on_upload = False

    def ensure_container(self) -> None:
        self.container_created = True
        self.public_access = None

    def existing_sha256(self, blob_name: str) -> str | None:
        self.sha_lookups.append(blob_name)
        blob = self.blobs.get(blob_name)
        if blob is None:
            return None
        digest = blob.metadata.get("content_sha256")
        return digest.lower() if digest else None

    def blob_url(self, blob_name: str) -> str:
        return f"gs://{self.bucket}/{self.container}/{blob_name}"

    def upload_markdown(
        self, blob_name: str, data: bytes, metadata: dict[str, str]
    ) -> str:
        if self.fail_on_upload:
            raise RuntimeError("simulated upload failure")
        self.blobs[blob_name] = FakeBlob(data=data, metadata=dict(metadata))
        self.uploads.append(blob_name)
        return self.blob_url(blob_name)

    def list_markdown_names(self) -> list[str]:
        return [name for name in self.blobs if name.endswith(".md")]

    def delete_blob(self, blob_name: str) -> None:
        self.deletes.append(blob_name)
        self.blobs.pop(blob_name, None)

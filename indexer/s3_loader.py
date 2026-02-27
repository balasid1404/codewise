"""Load codebase from S3 for indexing."""

import os
import tempfile
import boto3
from pathlib import Path


class S3CodebaseLoader:
    """Download codebase from S3 for indexing."""

    def __init__(self, region: str = "us-east-1"):
        self.s3 = boto3.client("s3", region_name=region)

    def download(self, s3_uri: str) -> Path:
        """
        Download codebase from S3 to temp directory.
        
        Args:
            s3_uri: s3://bucket/prefix/
            
        Returns:
            Path to local directory with downloaded files
        """
        bucket, prefix = self._parse_s3_uri(s3_uri)
        local_dir = Path(tempfile.mkdtemp(prefix="codebase_"))

        paginator = self.s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                
                # Skip directories and non-code files
                if key.endswith("/"):
                    continue
                if not (key.endswith(".py") or key.endswith(".java")):
                    continue

                # Download file
                relative_path = key[len(prefix):].lstrip("/")
                local_path = local_dir / relative_path
                local_path.parent.mkdir(parents=True, exist_ok=True)

                self.s3.download_file(bucket, key, str(local_path))

        return local_dir

    def _parse_s3_uri(self, uri: str) -> tuple[str, str]:
        """Parse s3://bucket/prefix into (bucket, prefix)."""
        uri = uri.replace("s3://", "")
        parts = uri.split("/", 1)
        bucket = parts[0]
        prefix = parts[1] if len(parts) > 1 else ""
        return bucket, prefix

    def cleanup(self, local_dir: Path) -> None:
        """Remove downloaded files."""
        import shutil
        shutil.rmtree(local_dir, ignore_errors=True)

"""Load codebase from S3 for indexing — parallel downloads."""

import os
import tempfile
import boto3
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

# File extensions we index
CODE_EXTENSIONS = {".py", ".java", ".js", ".ts", ".html"}


class S3CodebaseLoader:
    """Download codebase from S3 for indexing with parallel transfers."""

    def __init__(self, region: str = "us-east-1"):
        self.s3 = boto3.client("s3", region_name=region)

    def download(self, s3_uri: str, max_workers: int = 16) -> Path:
        """
        Download code files from S3 in parallel.

        Args:
            s3_uri: s3://bucket/prefix/
            max_workers: parallel download threads

        Returns:
            Path to local directory with downloaded files
        """
        bucket, prefix = self._parse_s3_uri(s3_uri)
        local_dir = Path(tempfile.mkdtemp(prefix="codebase_"))

        # List all code files first
        keys = []
        paginator = self.s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if key.endswith("/"):
                    continue
                ext = os.path.splitext(key)[1]
                if ext in CODE_EXTENSIONS:
                    keys.append(key)

        if not keys:
            return local_dir

        # Prepare local paths
        downloads = []
        for key in keys:
            relative_path = key[len(prefix):].lstrip("/")
            local_path = local_dir / relative_path
            local_path.parent.mkdir(parents=True, exist_ok=True)
            downloads.append((key, str(local_path)))

        # Parallel download — each thread gets its own S3 client for thread safety
        def download_file(args):
            key, local_path = args
            client = boto3.client("s3", region_name=self.s3.meta.region_name)
            client.download_file(bucket, key, local_path)

        with ThreadPoolExecutor(max_workers=min(max_workers, len(downloads))) as pool:
            list(pool.map(download_file, downloads))

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

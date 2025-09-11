import asyncio
from pathlib import Path
from typing import Any, Optional, Tuple

import aioboto3
import aiohttp
import boto3
from aiobotocore.config import AioConfig
from botocore.exceptions import ClientError, NoCredentialsError
from pydantic import BaseModel

from config.logging_config import get_logger
from models.job import TrackType

logger = get_logger(__name__)


class R2Storage(BaseModel):
    s3_url: str
    access_key_id: str
    secret_access_key: str
    bucket_name: str
    public_domain: str
    max_pool_connections: int = 10
    max_connections_per_host: int = 5


class CloudflareR2:

    def __init__(
        self,
        config: R2Storage,
    ):
        self.s3_url = config.s3_url
        self.access_key_id = config.access_key_id
        self.secret_access_key = config.secret_access_key
        self.bucket_name = config.bucket_name
        self.public_domain = config.public_domain
        self.client: Optional[Any] = None

        # Connection pooling configuration
        self._connector: Optional[aiohttp.TCPConnector] = None
        self._session: Optional[aioboto3.Session] = None
        self._config: Optional[AioConfig] = None
        self._pool_config = config

        self.client = boto3.client(
            "s3",
            endpoint_url=self.s3_url,
            aws_access_key_id=self.access_key_id,
            aws_secret_access_key=self.secret_access_key,
            region_name="auto",
        )

        self._init_sync_config(config)

    def _init_sync_config(self, config: R2Storage) -> None:
        try:
            self._config = AioConfig(
                max_pool_connections=config.max_pool_connections,
                retries={"max_attempts": 3, "mode": "adaptive"},
            )
            self._session = aioboto3.Session()
        except (ValueError, OSError, RuntimeError) as e:
            logger.error("Failed to initialize R2 connection pool config: %s", str(e))
            self._config = None
            self._session = aioboto3.Session()

    async def _ensure_async_connector(self) -> None:
        if self._connector is None:
            try:
                self._connector = aiohttp.TCPConnector(
                    limit=self._pool_config.max_pool_connections,
                    limit_per_host=self._pool_config.max_connections_per_host,
                    ttl_dns_cache=300,
                    use_dns_cache=True,
                    keepalive_timeout=30,
                    enable_cleanup_closed=True,
                )
                logger.info("Created R2 async connector")
            except (ValueError, OSError, RuntimeError) as e:
                logger.error("Failed to create R2 async connector: %s", str(e))
                self._connector = None

    async def close(self) -> None:
        if self._connector:
            try:
                await self._connector.close()
            except (OSError, RuntimeError) as e:
                logger.error("Error closing R2 connection pool: %s", str(e))
            finally:
                self._connector = None

    def __del__(self) -> None:
        if self._connector and not self._connector.closed:
            logger.warning(
                "R2Storage object destroyed with open connections. Consider calling close() explicitly."
            )

    def upload_file(self, file_path: Path, key: str) -> bool:
        if not self.client:
            return False
        try:
            self.client.upload_file(
                str(file_path), self.bucket_name, key, ExtraArgs={"ContentType": "audio/mpeg"}
            )
            logger.info("Uploaded %s to R2 as %s", file_path, key)
            return True
        except (NoCredentialsError, ClientError, OSError, FileNotFoundError) as e:
            logger.error("R2 upload error: %s", str(e))
            return False

    def file_exists(self, key: str) -> bool:
        if not self.client:
            return False
        try:
            self.client.head_object(Bucket=self.bucket_name, Key=key)
            return True
        except ClientError:
            return False

    def delete_file(self, key: str) -> bool:
        if not self.client:
            return False
        try:
            self.client.delete_object(Bucket=self.bucket_name, Key=key)
            return True
        except (NoCredentialsError, ClientError) as e:
            logger.error("R2 delete error: %s", str(e))
            return False

    def get_download_url(self, key: str, public_domain: str = "") -> Optional[str]:
        if not public_domain:
            return None
        url = f"https://{public_domain}/{key}"
        return url

    async def upload_file_async(self, file_path: Path, key: str) -> bool:
        if not self._session:
            logger.error("Async session not initialized")
            return False

        await self._ensure_async_connector()

        try:
            async with self._session.client(  # type: ignore[attr-defined]
                "s3",
                endpoint_url=self.s3_url,
                aws_access_key_id=self.access_key_id,
                aws_secret_access_key=self.secret_access_key,
                region_name="auto",
                config=self._config,
            ) as client:
                await client.upload_file(
                    str(file_path), self.bucket_name, key, ExtraArgs={"ContentType": "audio/mpeg"}
                )
                logger.info("Uploaded %s to R2 as %s (async)", file_path, key)
                return True
        except (NoCredentialsError, ClientError, OSError, FileNotFoundError) as e:
            logger.error("R2 async upload error: %s", str(e))
            return False

    async def upload_files_parallel(self, file_uploads: list[tuple[Path, str]]) -> list[bool]:
        upload_tasks = [self.upload_file_async(file_path, key) for file_path, key in file_uploads]
        results = await asyncio.gather(*upload_tasks, return_exceptions=True)

        final_results = []
        for result in results:
            if isinstance(result, Exception):
                logger.error("Parallel upload exception: %s", str(result))
                final_results.append(False)
            else:
                final_results.append(result)

        return final_results

    async def file_exists_async(self, key: str) -> bool:
        if not self._session:
            logger.error("Async session not initialized")
            return False

        await self._ensure_async_connector()

        try:
            async with self._session.client(  # type: ignore[attr-defined]
                "s3",
                endpoint_url=self.s3_url,
                aws_access_key_id=self.access_key_id,
                aws_secret_access_key=self.secret_access_key,
                region_name="auto",
                config=self._config,
            ) as client:
                await client.head_object(Bucket=self.bucket_name, Key=key)
                return True
        except ClientError:
            return False

    TRACK_PATTERNS = {
        TrackType.VOCALS: ["vocals", "(Vocals)"],
        TrackType.INSTRUMENTAL: ["instrumental", "(Instrumental)", "no_vocals"],
    }

    def cleanup_track_files(self, track_id: str):
        """Clean up track files from R2 storage."""
        try:
            for track_type in TrackType:
                r2_key = f"{track_id}/{track_type.value}.mp3"
                try:
                    if self.delete_file(r2_key):
                        logger.debug("Cleaned up R2 file: %s", r2_key)
                except (ClientError, NoCredentialsError, OSError) as e:
                    logger.warning("Failed to clean up R2 file %s: %s", r2_key, e)
        except (TypeError, ValueError, AttributeError) as e:
            logger.error("Unexpected error during cleanup for %s: %s", track_id, e)

    @staticmethod
    def detect_separated_files(output_dir: Path) -> Tuple[Optional[Path], Optional[Path]]:
        """Detect vocals and instrumental files in the output directory."""
        vocals_file = None
        instrumental_file = None

        all_files = list(output_dir.iterdir())

        for file_path in all_files:
            if not file_path.is_file():
                continue

            filename = file_path.name.lower()

            for track_type, patterns in CloudflareR2.TRACK_PATTERNS.items():
                matching_patterns = [pattern for pattern in patterns if pattern.lower() in filename]
                if not matching_patterns:
                    continue

                if track_type == TrackType.VOCALS:
                    vocals_file = file_path
                else:
                    instrumental_file = file_path
                break

        return vocals_file, instrumental_file

    def upload_track_files(self, track_id: str, vocals_file: Path, instrumental_file: Path) -> bool:
        """Upload vocals and instrumental files to R2."""
        try:
            vocals_key = f"{track_id}/vocals.mp3"
            instrumental_key = f"{track_id}/instrumental.mp3"

            vocals_uploaded = self.upload_file(vocals_file, vocals_key)
            instrumental_uploaded = self.upload_file(instrumental_file, instrumental_key)

            return vocals_uploaded and instrumental_uploaded
        except (OSError, ClientError, NoCredentialsError) as e:
            logger.error("Failed to upload track files to R2: %s", e)
            return False

    async def upload_track_files_async(
        self, track_id: str, vocals_file: Path, instrumental_file: Path
    ) -> bool:
        """Upload vocals and instrumental files to R2 asynchronously."""
        try:
            vocals_key = f"{track_id}/vocals.mp3"
            instrumental_key = f"{track_id}/instrumental.mp3"

            file_uploads = [(vocals_file, vocals_key), (instrumental_file, instrumental_key)]

            upload_results = await self.upload_files_parallel(file_uploads)
            return all(upload_results)

        except (OSError, ClientError, NoCredentialsError) as e:
            logger.error("Failed to upload track files to R2 (async): %s", e)
            return False

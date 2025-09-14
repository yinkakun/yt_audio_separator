import asyncio
from pathlib import Path
from typing import Optional, Tuple

import aioboto3
import boto3
from aiobotocore.config import AioConfig
from botocore.exceptions import ClientError, NoCredentialsError
from pydantic import BaseModel

from config.logger import get_logger
from services.audio_classifier import AudioClassifier, AudioType

logger = get_logger(__name__)


def get_storage_client(
    account_id: str,
    access_key_id: str,
    secret_access_key: str,
    bucket_name: str,
    public_domain: str,
) -> "CloudflareR2":
    """create storage client"""
    config = R2Storage(
        account_id=account_id,
        access_key_id=access_key_id,
        secret_access_key=secret_access_key,
        bucket_name=bucket_name,
        public_domain=public_domain,
    )
    return CloudflareR2(config)


class R2Storage(BaseModel):
    account_id: str
    access_key_id: str
    secret_access_key: str
    bucket_name: str
    public_domain: str
    region: str = "auto"
    max_pool_connections: int = 10


class CloudflareR2:

    def __init__(
        self,
        config: R2Storage,
    ):
        self.endpoint_url = f"https://{config.account_id}.r2.cloudflarestorage.com"
        self.access_key_id = config.access_key_id
        self.secret_access_key = config.secret_access_key
        self.bucket_name = config.bucket_name
        self.public_domain = config.public_domain
        self.region = config.region

        self.client = boto3.client(
            "s3",
            endpoint_url=self.endpoint_url,
            aws_access_key_id=self.access_key_id,
            aws_secret_access_key=self.secret_access_key,
            region_name=self.region,
        )

        self._session: Optional[aioboto3.Session] = None
        self._config: Optional[AioConfig] = None
        self._init_async_config(config)

    def _init_async_config(self, config: R2Storage) -> None:
        try:
            self._config = AioConfig(
                max_pool_connections=config.max_pool_connections,
                retries={"max_attempts": 3, "mode": "adaptive"},
            )
            self._session = aioboto3.Session()
            logger.info("Initialized R2")
        except (ValueError, OSError, RuntimeError) as e:
            logger.error("Failed to initialize R2: %s", str(e))
            self._config = AioConfig(retries={"max_attempts": 3, "mode": "adaptive"})
            self._session = aioboto3.Session()

    async def close(self) -> None:
        logger.debug("R2Storage closed")

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

        try:
            async with self._session.client(  # type: ignore[attr-defined]
                "s3",
                endpoint_url=self.endpoint_url,
                aws_access_key_id=self.access_key_id,
                aws_secret_access_key=self.secret_access_key,
                region_name=self.region,
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

        try:
            async with self._session.client(  # type: ignore[attr-defined]
                "s3",
                endpoint_url=self.endpoint_url,
                aws_access_key_id=self.access_key_id,
                aws_secret_access_key=self.secret_access_key,
                region_name=self.region,
                config=self._config,
            ) as client:
                await client.head_object(Bucket=self.bucket_name, Key=key)
                return True
        except ClientError:
            return False

    def cleanup_track_files(self, track_id: str):
        try:
            for track_type in AudioType:
                r2_key = AudioClassifier.get_track_filename(track_id, track_type)
                try:
                    if self.delete_file(r2_key):
                        logger.debug("Cleaned up R2 file: %s", r2_key)
                except (ClientError, NoCredentialsError, OSError) as e:
                    logger.warning("Failed to clean up R2 file %s: %s", r2_key, e)
        except (TypeError, ValueError, AttributeError) as e:
            logger.error("Unexpected error during cleanup for %s: %s", track_id, e)

    @staticmethod
    def detect_separated_files(output_dir: Path) -> Tuple[Optional[Path], Optional[Path]]:
        return AudioClassifier.detect_separated_files(output_dir)

    def upload_track_files(self, track_id: str, vocals_file: Path, instrumental_file: Path) -> bool:
        try:
            vocals_key = f"{track_id}/vocals.mp3"
            instrumental_key = f"{track_id}/instrumental.mp3"

            vocals_uploaded = self.upload_file(vocals_file, vocals_key)
            instrumental_uploaded = self.upload_file(instrumental_file, instrumental_key)

            return vocals_uploaded and instrumental_uploaded
        except (ClientError, NoCredentialsError) as e:
            logger.error("Failed to upload track files to R2: %s", e)
            return False

    async def upload_track_files_async(
        self, track_id: str, vocals_file: Path, instrumental_file: Path
    ) -> bool:
        try:
            vocals_key = f"{track_id}/vocals.mp3"
            instrumental_key = f"{track_id}/instrumental.mp3"

            file_uploads = [(vocals_file, vocals_key), (instrumental_file, instrumental_key)]

            upload_results = await self.upload_files_parallel(file_uploads)
            return all(upload_results)

        except (ClientError, NoCredentialsError) as e:
            logger.error("Failed to upload track files to R2 (async): %s", e)
            return False

import asyncio
from pathlib import Path
from typing import Any, Optional

import aiohttp
import aioboto3
import boto3
from aiobotocore.config import AioConfig
from botocore.exceptions import ClientError, NoCredentialsError
from pydantic import BaseModel

from config.logging_config import get_logger

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
        self.access_key = config.access_key_id
        self.secret_key = config.secret_access_key
        self.bucket_name = config.bucket_name
        self.public_domain = config.public_domain
        self.client: Optional[Any] = None

        # Connection pooling configuration
        self._connector: Optional[aiohttp.TCPConnector] = None
        self._session: Optional[aioboto3.Session] = None
        self._config: Optional[AioConfig] = None

        # Initialize sync client
        self.client = boto3.client(
            "s3",
            endpoint_url=self.s3_url,
            aws_access_key_id=self.access_key,
            aws_secret_access_key=self.secret_key,
            region_name="auto",
        )

        self._init_async_pool(config)

    def _init_async_pool(self, config: R2Storage) -> None:
        try:
            self._connector = aiohttp.TCPConnector(
                limit=config.max_pool_connections,
                limit_per_host=config.max_connections_per_host,
                ttl_dns_cache=300,
                use_dns_cache=True,
                keepalive_timeout=30,
                enable_cleanup_closed=True,
            )

            self._config = AioConfig(
                connector=self._connector,
                max_pool_connections=config.max_pool_connections,
                retries={"max_attempts": 3, "mode": "adaptive"},
            )

            self._session = aioboto3.Session()
            logger.info(
                "Initialized R2 connection pool",
                max_pool_connections=config.max_pool_connections,
                max_connections_per_host=config.max_connections_per_host,
            )
        except Exception as e:
            logger.error("Failed to initialize R2 connection pool: %s", str(e))
            self._connector = None
            self._config = None
            self._session = aioboto3.Session()

    async def close(self) -> None:
        if self._connector:
            try:
                await self._connector.close()
                logger.info("Closed R2 connection pool")
            except Exception as e:
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

        try:
            async with self._session.client(  # type: ignore[attr-defined]
                "s3",
                endpoint_url=self.s3_url,
                aws_access_key_id=self.access_key,
                aws_secret_access_key=self.secret_key,
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

        try:
            async with self._session.client(  # type: ignore[attr-defined]
                "s3",
                endpoint_url=self.s3_url,
                aws_access_key_id=self.access_key,
                aws_secret_access_key=self.secret_key,
                region_name="auto",
                config=self._config,
            ) as client:
                await client.head_object(Bucket=self.bucket_name, Key=key)
                return True
        except ClientError:
            return False

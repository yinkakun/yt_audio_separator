import logging
from pathlib import Path
from typing import Optional

import boto3
from botocore.exceptions import ClientError, NoCredentialsError

logger = logging.getLogger(__name__)


class CloudflareR2:
    def __init__(
        self,
        account_id: str,
        access_key: str,
        secret_key: str,
        bucket_name: str,
        public_domain: str = "",
    ):
        self.bucket_name = bucket_name
        self.public_domain = public_domain

        self.client = boto3.client(
            "s3",
            endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name="auto",
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

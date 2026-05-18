# app/utils/bos_helper.py

import os
from pathlib import Path
from loguru import logger
from typing import Optional, Dict, Any, List
import asyncio
import uuid

# 导入BOS SDK
try:
    from baidubce.auth.bce_credentials import BceCredentials
    from baidubce.bce_client_configuration import BceClientConfiguration
    from baidubce.services.bos.bos_client import BosClient
    HAS_BCE_SDK = True
except ImportError:
    HAS_BCE_SDK = False
    # 如果SDK未安装，创建一个假的 BosClient 以避免导入时崩溃
    class BosClient:
        def __init__(self, *args, **kwargs):
            pass

class BosHandler:
    """
    一个无状态的、专用于从BOS智能查找和下载文件的工具类。
    【已扩展】增加了查找特定页面图片并生成预签名URL的功能。
    """
    def __init__(self, bos_config: Dict[str, Any]):
        """
        初始化BOS客户端。
        Args:
            bos_config (Dict): 包含 'access_key', 'secret_key', 'endpoint', 'bucket_name' 的字典。
        """
        if not HAS_BCE_SDK:
            raise ImportError("BOS功能需要 `bce-python-sdk`。请运行: pip install bce-python-sdk")
        
        if not all(k in bos_config for k in ["access_key", "secret_key", "endpoint", "bucket_name"]):
            raise ValueError("BOS配置不完整，需要 access_key, secret_key, endpoint, bucket_name。")

        credentials = BceCredentials(bos_config["access_key"], bos_config["secret_key"])
        self.client_config = BceClientConfiguration(credentials=credentials, endpoint=bos_config["endpoint"])
        self.client = BosClient(self.client_config)
        self.bucket_name = bos_config["bucket_name"]
        logger.info(f"BosHandler 初始化成功，目标存储桶: '{self.bucket_name}'")

    def _find_latest_file_recursively(self, root_prefix: str, bucket_name: str, target_extensions: Optional[List[str]] = None, excluded_paths_keywords: Optional[List[str]] = None) -> Optional[Dict]:
        """
        在给定的根前缀下递归查找最新的、符合条件的文件。
        这是查找逻辑的核心主力。
        """
        candidate_files_info = []
        marker = None
        target_extensions_lower = [ext.lower() for ext in target_extensions] if target_extensions else []
        
        logger.info(f"在根路径 '{root_prefix}' 下递归搜索 *.{','.join(target_extensions_lower)} 文件...")
        
        while True:
            try:
                response = self.client.list_objects(bucket_name, prefix=root_prefix, marker=marker)
            except Exception as e:
                logger.error(f"列举 BOS 对象时出错: {e}")
                return None

            if hasattr(response, 'contents') and response.contents:
                for obj in response.contents:
                    obj_key_lower = obj.key.lower()
                    if obj_key_lower.endswith('/'): 
                        continue
                    if excluded_paths_keywords and any(f"/{keyword.lower().strip('/')}/" in f"/{obj_key_lower}/" for keyword in excluded_paths_keywords):
                        continue
                    if not target_extensions_lower or any(obj_key_lower.endswith(ext) for ext in target_extensions_lower):
                        candidate_files_info.append({'key': obj.key, 'last_modified': obj.last_modified, 'size': obj.size})
            
            if response.is_truncated:
                marker = response.next_marker
            else:
                break
        
        if not candidate_files_info:
            return None
        
        logger.info(f"在路径 '{root_prefix}' 下找到 {len(candidate_files_info)} 个候选文件，将选择最新的一个。")
        return max(candidate_files_info, key=lambda x: x['last_modified'])

    def _download_bos_obj(self, bos_obj_info: Dict, bucket_name: str, output_dir: Path, local_filename_prefix: str = "") -> Optional[Path]:
        """通用下载逻辑。"""
        original_filename = Path(bos_obj_info['key']).name
        local_filename = f"{local_filename_prefix}{original_filename}"
        local_path = output_dir / local_filename
        output_dir.mkdir(parents=True, exist_ok=True) 
        try:
            logger.info(f"开始下载文件 '{bos_obj_info['key']}' 到 '{local_path}'")
            self.client.get_object_to_file(bucket_name, bos_obj_info['key'], str(local_path))
            logger.success(f"文件下载成功: '{local_path}'")
            return local_path
        except Exception as e:
            logger.error(f"下载文件 '{bos_obj_info['key']}' 失败: {e}", exc_info=True)
            if local_path.exists(): 
                try:
                    local_path.unlink(missing_ok=True)
                except OSError: pass
            return None

    # --- 【新增】一个更通用的按扩展名查找的函数 ---
    def find_and_download_file_by_ext(
        self, 
        file_id: str, 
        output_dir: Path, 
        path_template: str, 
        bucket_name: str, 
        target_extensions: List[str]
    ) -> Optional[Path]:
        """
        根据给定的扩展名列表，查找并下载最新的文件。
        Args:
            file_id (str): 要替换模板中 {file_id} 的ID。
            output_dir (Path): 下载文件的本地目录。
            path_template (str): 包含 {file_id} 占位符的基础搜索路径模板。
            bucket_name (str): 存储桶名称。
            target_extensions (List[str]): 目标文件的扩展名列表 (例如 ['.md', '.txt'])。
        """
        base_prefix = path_template.format(file_id=file_id)
        
        # 下载源文件时，应排除图片目录以避免混淆
        latest_file_info = self._find_latest_file_recursively(
            root_prefix=base_prefix,
            bucket_name=bucket_name,
            target_extensions=target_extensions,
            excluded_paths_keywords=['images_dpi'] 
        )
        
        if latest_file_info:
            ext_str = "_".join(e.replace('.', '') for e in target_extensions)
            return self._download_bos_obj(latest_file_info, bucket_name, output_dir, f"{file_id}_{ext_str}_")
        
        logger.warning(f"在路径 '{base_prefix}' 下未能找到任何 {','.join(target_extensions)} 文件。")
        return None

    # --- 生成预签名URL的方法 ---
    def generate_presigned_url(
        self,
        object_key: str,
        bucket_name: Optional[str] = None,
        expiration_in_seconds: Optional[int] = None,
    ) -> Optional[str]:
        """
        为BOS上的对象生成一个有时效的公共访问URL。
        Args:
            object_key (str): 对象在BOS上的完整路径 (Key)。
            bucket_name (Optional[str]): 存储桶名称，如果为None则使用handler的默认值。
            expiration_in_seconds (int): URL的有效时间（秒）。
        Returns:
            一个可公开访问的URL字符串，或者在失败时返回None。
        """
        target_bucket = bucket_name or self.bucket_name
        logger.debug(f"准备为 Bucket='{target_bucket}', Key='{object_key}' 生成URL...")

        # I5: 默认 24h，clamp 到 [60, 7*86400]
        if expiration_in_seconds is None:
            try:
                expiration_in_seconds = int(os.getenv("BOS_PRESIGN_TTL_SEC", "86400"))
            except (TypeError, ValueError):
                expiration_in_seconds = 86400
        clamped = max(60, min(int(expiration_in_seconds), 7 * 86400))
        if clamped != expiration_in_seconds:
            logger.warning(f"预签名 URL 有效期 {expiration_in_seconds}s 超出范围，已 clamp 到 {clamped}s")
            expiration_in_seconds = clamped

        try:
            # 直接将原始的、未编码的 object_key 传递给SDK
            url_bytes = self.client.generate_pre_signed_url(
                target_bucket,
                object_key,
                expiration_in_seconds=expiration_in_seconds
            )
            
            if isinstance(url_bytes, bytes):
                url_str = url_bytes.decode('utf-8')
                logger.success(f"为对象 '{object_key}' 成功生成并解码URL。")
                return url_str
            
            # 如果SDK返回的已经是字符串（不太可能，但作为保险）
            if isinstance(url_bytes, str):
                logger.success(f"为对象 '{object_key}' 成功生成URL (已是str类型)。")
                return url_bytes

            logger.warning(f"为对象 '{object_key}' 生成的URL类型未知: {type(url_bytes)}。")
            return str(url_bytes)

        except Exception as e:
            logger.error(f"为对象 '{object_key}' 生成预签名URL时发生严重错误: {e}", exc_info=True)
            return None

    # --- 查找特定页面图片并返回URL的核心函数 ---
    async def find_page_image_url(
        self, 
        file_id: str, 
        page_num: int, 
        path_template: str,
        bucket_name: str,
        expiration_in_seconds: int = 3600
    ) -> Optional[str]:
        """
        在BOS上查找指定ID和页码的图片，并返回其预签名URL。
        此函数假定图片存储在类似 '.../{file_id}/images_dpi_200/page_1_... .png' 的路径下。
        """
        # 1. 构建图片目录的搜索前缀
        base_path = path_template.format(file_id=file_id)
        # 我们假设图片目录固定为 images_dpi_200
        search_prefix = f"{base_path}/images_dpi_200/"

        # 2. 查找该目录下所有文件（通常不多，不需要递归）
        logger.debug(f"正在BOS路径 '{search_prefix}' 下查找第 {page_num} 页的图片...")
        try:
            # 使用同步的 list_objects 并在异步函数中 await to_thread
            response = await asyncio.to_thread(
                self.client.list_objects, bucket_name, prefix=search_prefix
            )
        except Exception as e:
            logger.error(f"列举BOS对象时出错: {e}")
            return None

        # 3. 在结果中匹配正确的页面文件
        target_file_key = None
        if hasattr(response, 'contents') and response.contents:
            # 构造要查找的文件名片段，例如 "page_0_"
            target_filename_part = f"page_{page_num}_"
            for obj in response.contents:
                if target_filename_part in Path(obj.key).name:
                    target_file_key = obj.key
                    logger.success(f"找到匹配的图片文件: {target_file_key}")
                    break # 找到第一个就停止
        
        if not target_file_key:
            logger.warning(f"在路径 '{search_prefix}' 下未找到第 {page_num} 页的图片。")
            return None
            
        # 4. 生成并返回URL
        return await asyncio.to_thread(
            self.generate_presigned_url,
            target_file_key,
            bucket_name,
            expiration_in_seconds
        )

    def download_directory(self, remote_dir: str, local_dir: Path, bucket_name: Optional[str] = None) -> bool:
        """
        从BOS下载整个目录（文件夹）。
        
        Args:
            remote_dir (str): 要下载的BOS上的远程目录路径 (Key前缀)。
            local_dir (Path): 要保存到的本地目录。
            bucket_name (Optional[str]): 存储桶名称，如果为None则使用handler的默认值。

        Returns:
            bool: 是否所有文件都下载成功。
        """
        target_bucket = bucket_name or self.bucket_name
        logger.info(f"准备从 Bucket '{target_bucket}' 下载目录 '{remote_dir}' 到 '{local_dir}'...")

        try:
            # 确保本地目录存在
            local_dir.mkdir(parents=True, exist_ok=True)
            
            # 确保远程目录路径以斜杠结尾，以便正确匹配前缀
            if not remote_dir.endswith('/'):
                remote_dir += '/'
            
            # 列出目录下的所有对象
            marker = None
            all_successful = True
            
            while True:
                response = self.client.list_objects(target_bucket, prefix=remote_dir, marker=marker)
                
                if hasattr(response, 'contents') and response.contents:
                    for obj in response.contents:
                        # 获取相对路径
                        relative_path = obj.key[len(remote_dir):]
                        
                        # 如果是空字符串，说明是目录本身，跳过
                        if not relative_path:
                            continue
                            
                        local_file_path = local_dir / relative_path
                        
                        # 如果是子目录，则创建它
                        if obj.key.endswith('/'):
                            local_file_path.mkdir(parents=True, exist_ok=True)
                            continue
                        
                        # 如果是文件，下载它
                        # 确保文件的父目录存在
                        local_file_path.parent.mkdir(parents=True, exist_ok=True)
                        logger.debug(f"正在下载文件: {obj.key} -> {local_file_path}")
                        try:
                            self.client.get_object_to_file(target_bucket, obj.key, str(local_file_path))
                        except Exception as e_file:
                            logger.error(f"下载单个文件 {obj.key} 失败: {e_file}")
                            all_successful = False # 标记下载失败
                
                if response.is_truncated:
                    marker = response.next_marker
                else:
                    break
            
            if all_successful:
                logger.success(f"目录 '{remote_dir}' 已成功下载到 '{local_dir}'")
            else:
                logger.warning(f"目录 '{remote_dir}' 下载完成，但部分文件失败。")

            return all_successful

        except Exception as e:
            logger.error(f"下载目录 '{remote_dir}' 时发生严重错误: {e}", exc_info=True)
            return False

    def find_and_download_md_file(self, file_id: str, output_dir: Path, path_template: str, bucket_name: str) -> Optional[Path]:
        """
        【强化版】查找并下载 .md 文件。
        主要依赖强大的递归搜索来应对复杂的BOS路径结构。
        
        Args:
            file_id (str): 要替换模板中 {file_id} 的ID。
            output_dir (Path): 下载文件的本地目录。
            path_template (str): 包含 {file_id} 占位符的基础搜索路径模板。
        """
        # 1. 使用模板来构建递归搜索的根路径
        base_prefix = path_template.format(file_id=file_id)
        
        # 2. 直接调用递归搜索
        latest_md_info = self._find_latest_file_recursively(
            root_prefix=base_prefix,
            bucket_name=bucket_name,
            target_extensions=['.md'],
            excluded_paths_keywords=['images_dpi_200']
        )
        
        # 3. 处理结果
        if latest_md_info:
            return self._download_bos_obj(latest_md_info, bucket_name, output_dir, f"{file_id}_rec_")
        
        logger.error(f"在模板路径 '{path_template}' (ID: {file_id}) 下，递归搜索也未能找到任何 .md 文件。")
        return None

    def download_file(
        self,
        remote_file: str,
        local_file: Path,
        bucket_name: Optional[str] = None,
        quiet_on_missing: bool = False,
    ) -> bool:
        """从BOS下载单个文件。

        Args:
            quiet_on_missing: 当对象不存在 (NoSuchKey) 时仅 debug 日志，避免噪音。
                其它异常仍按 ERROR 记录。
        """
        target_bucket = bucket_name or self.bucket_name
        try:
            local_file.parent.mkdir(parents=True, exist_ok=True)
            self.client.get_object_to_file(target_bucket, remote_file, str(local_file))
            return True
        except Exception as e:
            msg = str(e)
            if quiet_on_missing and ("NoSuchKey" in msg or "specified key does not exist" in msg):
                logger.debug(f"BOS 对象不存在（quiet）：bos://{target_bucket}/{remote_file}")
            else:
                logger.error(f"从BOS下载文件时发生严重错误: {e}", exc_info=True)
            return False
        
    async def download_file_to_temp(self, bos_path: str, temp_dir: Path) -> Optional[Path]:
        """专门用于下载文件到临时目录的异步包装器。"""
        if not bos_path.startswith("bos://"):
            logger.error(f"无效的BOS路径格式: {bos_path}")
            return None
        
        parts = bos_path.replace("bos://", "").split("/", 1)
        if len(parts) != 2: 
            logger.error(f"无效的BOS路径格式，无法解析 bucket 和 key: {bos_path}")
            return None
            
        bucket_name, remote_key = parts
        
        local_path = temp_dir / f"{uuid.uuid4().hex}_{Path(remote_key).name}"
        
        success = await asyncio.to_thread(
            self.download_file,
            remote_file=remote_key,
            local_file=local_path,
            bucket_name=bucket_name
        )
        return local_path if success else None
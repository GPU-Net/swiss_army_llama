from embeddings_data_models import Base, TextEmbedding, DocumentEmbedding, Document, TokenLevelEmbedding, TokenLevelEmbeddingBundle, TokenLevelEmbeddingBundleCombinedFeatureVector, AudioTranscript
from embeddings_data_models import EmbeddingRequest, SemanticSearchRequest, AdvancedSemanticSearchRequest, SimilarityRequest, TextCompletionRequest
from embeddings_data_models import EmbeddingResponse, SemanticSearchResponse, AdvancedSemanticSearchResponse, SimilarityResponse, AllStringsResponse, AllDocumentsResponse, TextCompletionResponse,  AudioTranscriptResponse
from embeddings_data_models import ShowLogsIncrementalModel
from log_viewer_functions import show_logs_incremental_func, show_logs_func
import asyncio
import io
import glob
import json
import logging
import os 
import random
import re
import shutil
import subprocess
import tempfile
import time
import traceback
import urllib.request
import zipfile
from collections import defaultdict
from datetime import datetime
from hashlib import sha3_256
from logging.handlers import RotatingFileHandler
from typing import List, Optional, Tuple, Dict, Any
from urllib.parse import quote, unquote
import numpy as np
from decouple import config
import uvicorn
import psutil
import fastapi
import textract
from fastapi import FastAPI, HTTPException, Request, UploadFile, File, Depends
from fastapi.responses import JSONResponse, FileResponse, HTMLResponse, Response
from fastapi.concurrency import run_in_threadpool
from langchain.embeddings import LlamaCppEmbeddings
from sqlalchemy import select
from sqlalchemy import text as sql_text
from sqlalchemy.exc import SQLAlchemyError, OperationalError, IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker, joinedload
import faiss
import pandas as pd
from magic import Magic
from llama_cpp import Llama, LlamaGrammar
import fast_vector_similarity as fvs
from faster_whisper import WhisperModel

# Note: the Ramdisk setup and teardown requires sudo; to enable password-less sudo, edit your sudoers file with `sudo visudo`.
# Add the following lines, replacing username with your actual username
# username ALL=(ALL) NOPASSWD: /bin/mount -t tmpfs -o size=*G tmpfs /mnt/ramdisk
# username ALL=(ALL) NOPASSWD: /bin/umount /mnt/ramdisk

# Setup logging
old_logs_dir = 'old_logs' # Ensure the old_logs directory exists
if not os.path.exists(old_logs_dir):
    os.makedirs(old_logs_dir)
logger = logging.getLogger()
logger.setLevel(logging.INFO)
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
log_file_path = 'llama2_embeddings_fastapi_service.log'
fh = RotatingFileHandler(log_file_path, maxBytes=10*1024*1024, backupCount=5)
fh.setFormatter(formatter)
logger.addHandler(fh)
def namer(default_log_name): # Move rotated logs to the old_logs directory
    return os.path.join(old_logs_dir, os.path.basename(default_log_name))
def rotator(source, dest):
    shutil.move(source, dest)
fh.namer = namer
fh.rotator = rotator
sh = logging.StreamHandler()
sh.setFormatter(formatter)
logger.addHandler(sh)
logger = logging.getLogger(__name__)
logging.getLogger('sqlalchemy.engine').setLevel(logging.WARNING)
configured_logger = logger

# Global variables
use_hardcoded_security_token = 0
if use_hardcoded_security_token:
    SECURITY_TOKEN = "Test123$"
    USE_SECURITY_TOKEN = config("USE_SECURITY_TOKEN", default=False, cast=bool)
else:
    USE_SECURITY_TOKEN = False
DATABASE_URL = "sqlite+aiosqlite:///embeddings.sqlite"
LLAMA_EMBEDDING_SERVER_LISTEN_PORT = config("LLAMA_EMBEDDING_SERVER_LISTEN_PORT", default=8089, cast=int)
DEFAULT_MODEL_NAME = config("DEFAULT_MODEL_NAME", default="openchat_v3.2_super", cast=str) 
LLM_CONTEXT_SIZE_IN_TOKENS = config("LLM_CONTEXT_SIZE_IN_TOKENS", default=512, cast=int)
TEXT_COMPLETION_CONTEXT_SIZE_IN_TOKENS = config("TEXT_COMPLETION_CONTEXT_SIZE_IN_TOKENS", default=4000, cast=int)
DEFAULT_MAX_COMPLETION_TOKENS = config("DEFAULT_MAX_COMPLETION_TOKENS", default=100, cast=int)
DEFAULT_NUMBER_OF_COMPLETIONS_TO_GENERATE = config("DEFAULT_NUMBER_OF_COMPLETIONS_TO_GENERATE", default=4, cast=int)
DEFAULT_COMPLETION_TEMPERATURE = config("DEFAULT_COMPLETION_TEMPERATURE", default=0.7, cast=float)
MINIMUM_STRING_LENGTH_FOR_DOCUMENT_EMBEDDING = config("MINIMUM_STRING_LENGTH_FOR_DOCUMENT_EMBEDDING", default=15, cast=int)
USE_PARALLEL_INFERENCE_QUEUE = config("USE_PARALLEL_INFERENCE_QUEUE", default=False, cast=bool)
MAX_CONCURRENT_PARALLEL_INFERENCE_TASKS = config("MAX_CONCURRENT_PARALLEL_INFERENCE_TASKS", default=10, cast=int)
USE_RAMDISK = config("USE_RAMDISK", default=False, cast=bool)
RAMDISK_PATH = config("RAMDISK_PATH", default="/mnt/ramdisk", cast=str)
RAMDISK_SIZE_IN_GB = config("RAMDISK_SIZE_IN_GB", default=1, cast=int)
MAX_RETRIES = config("MAX_RETRIES", default=3, cast=int)
DB_WRITE_BATCH_SIZE = config("DB_WRITE_BATCH_SIZE", default=25, cast=int) 
RETRY_DELAY_BASE_SECONDS = config("RETRY_DELAY_BASE_SECONDS", default=1, cast=int)
JITTER_FACTOR = config("JITTER_FACTOR", default=0.1, cast=float)
BASE_DIRECTORY = os.path.dirname(os.path.abspath(__file__))
embedding_model_cache = {} # Model cache to store loaded models
token_level_embedding_model_cache = {} # Model cache to store loaded token-level embedding models
text_completion_model_cache = {} # Model cache to store loaded text completion models
logger.info(f"USE_RAMDISK is set to: {USE_RAMDISK}")
db_writer = None

app = FastAPI(docs_url="/")  # Set the Swagger UI to root
engine = create_async_engine(DATABASE_URL, echo=False, connect_args={"check_same_thread": False})
AsyncSessionLocal = sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False
)

# Misc. utility functions and db writer class:
def clean_filename_for_url_func(dirty_filename: str) -> str:
    clean_filename = re.sub(r'[^\w\s]', '', dirty_filename) # Remove special characters and replace spaces with underscores
    clean_filename = clean_filename.replace(' ', '_')
    return clean_filename

class DatabaseWriter:
    def __init__(self, queue):
        self.queue = queue
        self.processing_hashes = set() # Set to store the hashes if everything that is currently being processed in the queue (to avoid duplicates of the same task being added to the queue)

    def _get_hash_from_operation(self, operation):
        attr_name = {
            TextEmbedding: 'text_hash',
            DocumentEmbedding: 'file_hash',
            Document: 'document_hash',
            TokenLevelEmbedding: 'token_hash',
            TokenLevelEmbeddingBundle: 'input_text_hash',
            TokenLevelEmbeddingBundleCombinedFeatureVector: 'combined_feature_vector_hash',
            AudioTranscript: 'audio_file_hash'
        }.get(type(operation))
        hash_value = getattr(operation, attr_name, None)
        llm_model_name = getattr(operation, 'llm_model_name', None)
        return f"{hash_value}_{llm_model_name}" if hash_value and llm_model_name else None

    async def initialize_processing_hashes(self, chunk_size=1000):
        start_time = datetime.utcnow()
        async with AsyncSessionLocal() as session:
            queries = [
                (select(TextEmbedding.text_hash, TextEmbedding.llm_model_name), True),
                (select(DocumentEmbedding.file_hash, DocumentEmbedding.llm_model_name), True),
                (select(Document.document_hash, Document.llm_model_name), True),
                (select(TokenLevelEmbedding.token_hash, TokenLevelEmbedding.llm_model_name), True),
                (select(TokenLevelEmbeddingBundle.input_text_hash, TokenLevelEmbeddingBundle.llm_model_name), True),
                (select(TokenLevelEmbeddingBundleCombinedFeatureVector.combined_feature_vector_hash, TokenLevelEmbeddingBundleCombinedFeatureVector.llm_model_name), True),
                (select(AudioTranscript.audio_file_hash), False)
            ]
            for query, has_llm in queries:
                offset = 0
                while True:
                    result = await session.execute(query.limit(chunk_size).offset(offset))
                    rows = result.fetchall()
                    if not rows:
                        break
                    for row in rows:
                        if has_llm:
                            hash_with_model = f"{row[0]}_{row[1]}"
                        else:
                            hash_with_model = row[0]
                        self.processing_hashes.add(hash_with_model)
                    offset += chunk_size
        end_time = datetime.utcnow()
        total_time = (end_time - start_time).total_seconds()
        if len(self.processing_hashes) > 0:
            logger.info(f"Finished initializing set of input hash/llm_model_name combinations that are either currently being processed or have already been processed. Set size: {len(self.processing_hashes)}; Took {total_time} seconds, for an average of {total_time / len(self.processing_hashes)} seconds per hash.")

    async def _handle_integrity_error(self, e, write_operation, session):
        unique_constraint_msg = {
            TextEmbedding: "token_embeddings.token_hash, token_embeddings.llm_model_name",
            DocumentEmbedding: "document_embeddings.file_hash, document_embeddings.llm_model_name",
            Document: "documents.document_hash, documents.llm_model_name",
            TokenLevelEmbedding: "token_level_embeddings.token_hash, token_level_embeddings.llm_model_name",
            TokenLevelEmbeddingBundle: "token_level_embedding_bundles.input_text_hash, token_level_embedding_bundles.llm_model_name",
            AudioTranscript: "audio_transcripts.audio_file_hash"
        }.get(type(write_operation))
        if unique_constraint_msg and unique_constraint_msg in str(e):
            logger.warning(f"Embedding already exists in the database for given input and llm_model_name: {e}")
            await session.rollback()
        else:
            raise
        
    async def dedicated_db_writer(self):
        while True:
            write_operations_batch = await self.queue.get()
            async with AsyncSessionLocal() as session:
                try:
                    for write_operation in write_operations_batch:
                        session.add(write_operation)
                    await session.flush()  # Flush to get the IDs
                    await session.commit()
                    for write_operation in write_operations_batch:
                        hash_to_remove = self._get_hash_from_operation(write_operation)
                        if hash_to_remove is not None and hash_to_remove in self.processing_hashes:
                            self.processing_hashes.remove(hash_to_remove)
                except IntegrityError as e:
                    await self._handle_integrity_error(e, write_operation, session)
                except SQLAlchemyError as e:
                    logger.error(f"Database error: {e}")
                    await session.rollback()
                except Exception as e:
                    tb = traceback.format_exc()
                    logger.error(f"Unexpected error: {e}\n{tb}")
                    await session.rollback()
                self.queue.task_done()
                    
    async def enqueue_write(self, write_operations):
        write_operations = [op for op in write_operations if self._get_hash_from_operation(op) not in self.processing_hashes]  # Filter out write operations for hashes that are already being processed
        if not write_operations:  # If there are no write operations left after filtering, return early
            return
        for op in write_operations:  # Add the hashes of the write operations to the set
            hash_value = self._get_hash_from_operation(op)
            if hash_value:
                self.processing_hashes.add(hash_value)
        await self.queue.put(write_operations)


async def execute_with_retry(func, *args, **kwargs):
    retries = 0
    while retries < MAX_RETRIES:
        try:
            return await func(*args, **kwargs)
        except OperationalError as e:
            if 'database is locked' in str(e):
                retries += 1
                sleep_time = RETRY_DELAY_BASE_SECONDS * (2 ** retries) + (random.random() * JITTER_FACTOR) # Implementing exponential backoff with jitter
                logger.warning(f"Database is locked. Retrying ({retries}/{MAX_RETRIES})... Waiting for {sleep_time} seconds")
                await asyncio.sleep(sleep_time)
            else:
                raise
    raise OperationalError("Database is locked after multiple retries")

async def initialize_db():
    logger.info("Initializing database, creating tables, and setting SQLite PRAGMAs...")
    list_of_sqlite_pragma_strings = ["PRAGMA journal_mode=WAL;", "PRAGMA synchronous = NORMAL;", "PRAGMA cache_size = -1048576;", "PRAGMA busy_timeout = 2000;", "PRAGMA wal_autocheckpoint = 100;"]
    list_of_sqlite_pragma_justification_strings = ["Set SQLite to use Write-Ahead Logging (WAL) mode (from default DELETE mode) so that reads and writes can occur simultaneously",
                                                "Set synchronous mode to NORMAL (from FULL) so that writes are not blocked by reads",
                                                "Set cache size to 1GB (from default 2MB) so that more data can be cached in memory and not read from disk; to make this 256MB, set it to -262144 instead",
                                                "Increase the busy timeout to 2 seconds so that the database waits",
                                                "Set the WAL autocheckpoint to 100 (from default 1000) so that the WAL file is checkpointed more frequently"]
    assert(len(list_of_sqlite_pragma_strings) == len(list_of_sqlite_pragma_justification_strings))
    async with engine.begin() as conn:
        for pragma_string in list_of_sqlite_pragma_strings:
            await conn.execute(sql_text(pragma_string))
            logger.info(f"Executed SQLite PRAGMA: {pragma_string}")
            logger.info(f"Justification: {list_of_sqlite_pragma_justification_strings[list_of_sqlite_pragma_strings.index(pragma_string)]}")
        await conn.run_sync(Base.metadata.create_all) # Create tables if they don't exist
    logger.info("Database initialization completed.")

def get_db_writer() -> DatabaseWriter:
    return db_writer  # Return the existing DatabaseWriter instance

def check_that_user_has_required_permissions_to_manage_ramdisks():
    try: # Try to run a harmless command with sudo to test if the user has password-less sudo permissions
        result = subprocess.run(["sudo", "ls"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if "password" in result.stderr.lower():
            raise PermissionError("Password required for sudo")
        logger.info("User has sufficient permissions to manage RAM Disks.")
        return True
    except (PermissionError, subprocess.CalledProcessError) as e:
        logger.info("Sorry, current user does not have sufficient permissions to manage RAM Disks! Disabling RAM Disks for now...")
        logger.debug(f"Permission check error detail: {e}")
        return False
    
def setup_ramdisk():
    cmd_check = f"sudo mount | grep {RAMDISK_PATH}" # Check if RAM disk already exists at the path
    result = subprocess.run(cmd_check, shell=True, stdout=subprocess.PIPE).stdout.decode('utf-8')
    if RAMDISK_PATH in result:
        logger.info(f"RAM Disk already set up at {RAMDISK_PATH}. Skipping setup.")
        return
    total_ram_gb = psutil.virtual_memory().total / (1024 ** 3)
    free_ram_gb = psutil.virtual_memory().free / (1024 ** 3)
    buffer_gb = 2  # buffer to ensure we don't use all the free RAM
    ramdisk_size_gb = max(min(RAMDISK_SIZE_IN_GB, free_ram_gb - buffer_gb), 0.1)
    ramdisk_size_mb = int(ramdisk_size_gb * 1024)
    ramdisk_size_str = f"{ramdisk_size_mb}M"
    logger.info(f"Total RAM: {total_ram_gb}G")
    logger.info(f"Free RAM: {free_ram_gb}G")
    logger.info(f"Calculated RAM Disk Size: {ramdisk_size_gb}G")
    if RAMDISK_SIZE_IN_GB > total_ram_gb:
        raise ValueError(f"Cannot allocate {RAMDISK_SIZE_IN_GB}G for RAM Disk. Total system RAM is {total_ram_gb:.2f}G.")
    logger.info("Setting up RAM Disk...")
    os.makedirs(RAMDISK_PATH, exist_ok=True)
    mount_command = ["sudo", "mount", "-t", "tmpfs", "-o", f"size={ramdisk_size_str}", "tmpfs", RAMDISK_PATH]
    subprocess.run(mount_command, check=True)
    logger.info(f"RAM Disk set up at {RAMDISK_PATH} with size {ramdisk_size_gb}G")

def copy_models_to_ramdisk(models_directory, ramdisk_directory):
    total_size = sum(os.path.getsize(os.path.join(models_directory, model)) for model in os.listdir(models_directory))
    free_ram = psutil.virtual_memory().free
    if total_size > free_ram:
        logger.warning(f"Not enough space on RAM Disk. Required: {total_size}, Available: {free_ram}. Rebuilding RAM Disk.")
        clear_ramdisk()
        free_ram = psutil.virtual_memory().free  # Recompute the available RAM after clearing the RAM disk
        if total_size > free_ram:
            logger.error(f"Still not enough space on RAM Disk even after clearing. Required: {total_size}, Available: {free_ram}.")
            raise ValueError("Not enough RAM space to copy models.")
        setup_ramdisk()
    os.makedirs(ramdisk_directory, exist_ok=True)
    for model in os.listdir(models_directory):
        shutil.copyfile(os.path.join(models_directory, model), os.path.join(ramdisk_directory, model))
        logger.info(f"Copied model {model} to RAM Disk at {os.path.join(ramdisk_directory, model)}")

def clear_ramdisk():
    while True:
        cmd_check = f"sudo mount | grep {RAMDISK_PATH}"
        result = subprocess.run(cmd_check, shell=True, stdout=subprocess.PIPE).stdout.decode('utf-8')
        if RAMDISK_PATH not in result:
            break  # Exit the loop if the RAMDISK_PATH is not in the mount list
        cmd_umount = f"sudo umount -l {RAMDISK_PATH}"
        subprocess.run(cmd_umount, shell=True, check=True)
    logger.info(f"Cleared RAM Disk at {RAMDISK_PATH}")

async def build_faiss_indexes():
    global faiss_indexes, token_faiss_indexes, associated_texts_by_model
    faiss_indexes = {}
    token_faiss_indexes = {} # Separate FAISS indexes for token-level embeddings
    associated_texts_by_model = defaultdict(list)  # Create a dictionary to store associated texts by model name
    async with AsyncSessionLocal() as session:
        result = await session.execute(sql_text("SELECT llm_model_name, text, embedding_json FROM embeddings")) # Query regular embeddings
        token_result = await session.execute(sql_text("SELECT llm_model_name, token, token_level_embedding_json FROM token_level_embeddings")) # Query token-level embeddings
        embeddings_by_model = defaultdict(list)
        token_embeddings_by_model = defaultdict(list)
        for row in result.fetchall(): # Process regular embeddings
            llm_model_name = row[0]
            associated_texts_by_model[llm_model_name].append(row[1])  # Store the associated text by model name
            embeddings_by_model[llm_model_name].append((row[1], json.loads(row[2])))
        for row in token_result.fetchall(): # Process token-level embeddings
            llm_model_name = row[0]
            token_embeddings_by_model[llm_model_name].append(json.loads(row[2]))
        for llm_model_name, embeddings in embeddings_by_model.items():
            logger.info(f"Building Faiss index over embeddings for model {llm_model_name}...")
            embeddings_array = np.array([e[1] for e in embeddings]).astype('float32')
            if embeddings_array.size == 0:
                logger.error(f"No embeddings were loaded from the database for model {llm_model_name}, so nothing to build the Faiss index with!")
                continue
            logger.info(f"Loaded {len(embeddings_array)} embeddings for model {llm_model_name}.")
            logger.info(f"Embedding dimension for model {llm_model_name}: {embeddings_array.shape[1]}")
            logger.info(f"Normalizing {len(embeddings_array)} embeddings for model {llm_model_name}...")
            faiss.normalize_L2(embeddings_array)  # Normalize the vectors for cosine similarity
            faiss_index = faiss.IndexFlatIP(embeddings_array.shape[1])  # Use IndexFlatIP for cosine similarity
            faiss_index.add(embeddings_array)
            logger.info(f"Faiss index built for model {llm_model_name}.")
            faiss_indexes[llm_model_name] = faiss_index  # Store the index by model name
        for llm_model_name, token_embeddings in token_embeddings_by_model.items():
            token_embeddings_array = np.array(token_embeddings).astype('float32')
            if token_embeddings_array.size == 0:
                logger.error(f"No token-level embeddings were loaded from the database for model {llm_model_name}, so nothing to build the Faiss index with!")
                continue
            logger.info(f"Normalizing {len(token_embeddings_array)} token-level embeddings for model {llm_model_name}...")
            faiss.normalize_L2(token_embeddings_array)  # Normalize the vectors for cosine similarity
            token_faiss_index = faiss.IndexFlatIP(token_embeddings_array.shape[1])  # Use IndexFlatIP for cosine similarity
            token_faiss_index.add(token_embeddings_array)
            logger.info(f"Token-level Faiss index built for model {llm_model_name}.")
            token_faiss_indexes[llm_model_name] = token_faiss_index  # Store the token-level index by model name
    return faiss_indexes, token_faiss_indexes, associated_texts_by_model

class JSONAggregator:
    def __init__(self):
        self.completions = []
        self.aggregate_result = None

    @staticmethod
    def weighted_vote(values, weights):
        tally = defaultdict(float)
        for v, w in zip(values, weights):
            tally[v] += w
        return max(tally, key=tally.get)

    @staticmethod
    def flatten_json(json_obj, parent_key='', sep='->'):
        items = {}
        for k, v in json_obj.items():
            new_key = f"{parent_key}{sep}{k}" if parent_key else k
            if isinstance(v, dict):
                items.update(JSONAggregator.flatten_json(v, new_key, sep=sep))
            else:
                items[new_key] = v
        return items

    @staticmethod
    def get_value_by_path(json_obj, path, sep='->'):
        keys = path.split(sep)
        item = json_obj
        for k in keys:
            item = item[k]
        return item

    @staticmethod
    def set_value_by_path(json_obj, path, value, sep='->'):
        keys = path.split(sep)
        item = json_obj
        for k in keys[:-1]:
            item = item.setdefault(k, {})
        item[keys[-1]] = value

    def calculate_path_weights(self):
        all_paths = []
        for j in self.completions:
            all_paths += list(self.flatten_json(j).keys())
        path_weights = defaultdict(float)
        for path in all_paths:
            path_weights[path] += 1.0
        return path_weights

    def aggregate(self):
        path_weights = self.calculate_path_weights()
        aggregate = {}
        for path, weight in path_weights.items():
            values = [self.get_value_by_path(j, path) for j in self.completions if path in self.flatten_json(j)]
            weights = [weight] * len(values)
            aggregate_value = self.weighted_vote(values, weights)
            self.set_value_by_path(aggregate, path, aggregate_value)
        self.aggregate_result = aggregate

class FakeUploadFile:
    def __init__(self, filename: str, content: Any, content_type: str = 'text/plain'):
        self.filename = filename
        self.content_type = content_type
        self.file = io.BytesIO(content)
    def read(self, size: int = -1) -> bytes:
        return self.file.read(size)
    def seek(self, offset: int, whence: int = 0) -> int:
        return self.file.seek(offset, whence)
    def tell(self) -> int:
        return self.file.tell()

async def get_transcript_from_db(audio_file_hash: str):
    return await execute_with_retry(_get_transcript_from_db, audio_file_hash)

async def _get_transcript_from_db(audio_file_hash: str) -> Optional[dict]:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            sql_text("SELECT * FROM audio_transcripts WHERE audio_file_hash=:audio_file_hash"),
            {"audio_file_hash": audio_file_hash},
        )
        row = result.fetchone()
        if row:
            try:
                segments_json = json.loads(row.segments_json)
                combined_transcript_text_list_of_metadata_dicts = json.loads(row.combined_transcript_text_list_of_metadata_dicts)
                info_json = json.loads(row.info_json)
                if hasattr(info_json, '__dict__'):
                    info_json = vars(info_json)
            except json.JSONDecodeError as e:
                raise ValueError(f"JSON Decode Error: {e}")
            if not isinstance(segments_json, list) or not isinstance(combined_transcript_text_list_of_metadata_dicts, list) or not isinstance(info_json, dict):
                logger.error(f"Type of segments_json: {type(segments_json)}, Value: {segments_json}")
                logger.error(f"Type of combined_transcript_text_list_of_metadata_dicts: {type(combined_transcript_text_list_of_metadata_dicts)}, Value: {combined_transcript_text_list_of_metadata_dicts}")
                logger.error(f"Type of info_json: {type(info_json)}, Value: {info_json}")
                raise ValueError("Deserialized JSON does not match the expected format.")
            audio_transcript_response = {
                "id": row.id,
                "audio_file_name": row.audio_file_name,
                "audio_file_size_mb": row.audio_file_size_mb,
                "segments_json": segments_json,
                "combined_transcript_text": row.combined_transcript_text,
                "combined_transcript_text_list_of_metadata_dicts": combined_transcript_text_list_of_metadata_dicts,
                "info_json": info_json,
                "ip_address": row.ip_address,
                "request_time": row.request_time,
                "response_time": row.response_time,
                "total_time": row.total_time,
                "url_to_download_zip_file_of_embeddings": ""
            }
            return AudioTranscriptResponse(**audio_transcript_response)
        return None

async def save_transcript_to_db(audio_file_hash, audio_file_name, audio_file_size_mb, transcript_segments, info, ip_address, request_time, response_time, total_time, combined_transcript_text, combined_transcript_text_list_of_metadata_dicts):
    existing_transcript = await get_transcript_from_db(audio_file_hash)
    if existing_transcript:
        return existing_transcript
    audio_transcript = AudioTranscript(
        audio_file_hash=audio_file_hash,
        audio_file_name=audio_file_name,
        audio_file_size_mb=audio_file_size_mb,
        segments_json=json.dumps(transcript_segments),
        combined_transcript_text=combined_transcript_text,
        combined_transcript_text_list_of_metadata_dicts=json.dumps(combined_transcript_text_list_of_metadata_dicts),
        info_json=json.dumps(info),
        ip_address=ip_address,
        request_time=request_time,
        response_time=response_time,
        total_time=total_time
    )
    await db_writer.enqueue_write([audio_transcript])

def normalize_logprobs(avg_logprob, min_logprob, max_logprob):
    range_logprob = max_logprob - min_logprob
    return (avg_logprob - min_logprob) / range_logprob if range_logprob != 0 else 0.5

def remove_pagination_breaks(text: str) -> str:
    text = re.sub(r'-(\n)(?=[a-z])', '', text) # Remove hyphens at the end of lines when the word continues on the next line
    text = re.sub(r'(?<=\w)(?<![.?!-]|\d)\n(?![\nA-Z])', ' ', text) # Replace line breaks that are not preceded by punctuation or list markers and not followed by an uppercase letter or another line break   
    return text

def sophisticated_sentence_splitter(text):
    text = remove_pagination_breaks(text)
    pattern = r'\.(?!\s*(com|net|org|io)\s)(?![0-9])'  # Split on periods that are not followed by a space and a top-level domain or a number
    pattern += r'|[.!?]\s+'  # Split on whitespace that follows a period, question mark, or exclamation point
    pattern += r'|\.\.\.(?=\s)'  # Split on ellipses that are followed by a space
    sentences = re.split(pattern, text)
    refined_sentences = []
    temp_sentence = ""
    for sentence in sentences:
        if sentence is not None:
            temp_sentence += sentence
            if temp_sentence.count('"') % 2 == 0:  # If the number of quotes is even, then we have a complete sentence
                refined_sentences.append(temp_sentence.strip())
                temp_sentence = ""
    if temp_sentence:
        refined_sentences[-1] += temp_sentence
    return [s.strip() for s in refined_sentences if s.strip()]

def merge_transcript_segments_into_combined_text(segments):
    if not segments:
        return "", [], []
    min_logprob = min(segment['avg_logprob'] for segment in segments)
    max_logprob = max(segment['avg_logprob'] for segment in segments)
    combined_text = ""
    sentence_buffer = ""
    list_of_metadata_dicts = []
    list_of_sentences = []
    char_count = 0
    time_start = None
    time_end = None
    total_logprob = 0.0
    segment_count = 0
    for segment in segments:
        if time_start is None:
            time_start = segment['start']
        time_end = segment['end']
        total_logprob += segment['avg_logprob']
        segment_count += 1
        sentence_buffer += segment['text'] + " "
        sentences = sophisticated_sentence_splitter(sentence_buffer)
        for sentence in sentences:
            combined_text += sentence.strip() + " "
            list_of_sentences.append(sentence.strip())
            char_count += len(sentence.strip()) + 1  # +1 for the space
            avg_logprob = total_logprob / segment_count
            model_confidence_score = normalize_logprobs(avg_logprob, min_logprob, max_logprob)
            metadata = {
                'start_char_count': char_count - len(sentence.strip()) - 1,
                'end_char_count': char_count - 2,
                'time_start': time_start,
                'time_end': time_end,
                'model_confidence_score': model_confidence_score
            }
            list_of_metadata_dicts.append(metadata)
        sentence_buffer = sentences[-1] if len(sentences) % 2 != 0 else ""
    return combined_text, list_of_metadata_dicts, list_of_sentences

async def compute_and_store_transcript_embeddings(audio_file_name, list_of_transcript_sentences, llm_model_name, ip_address, combined_transcript_text, req: Request):
    logger.info(f"Now computing embeddings for entire transcript of {audio_file_name}...")
    zip_dir = 'generated_transcript_embeddings_zip_files'
    if not os.path.exists(zip_dir):
        os.makedirs(zip_dir)
    sanitized_file_name = clean_filename_for_url_func(audio_file_name)
    document_name = f"automatic_whisper_transcript_of__{sanitized_file_name}"
    file_hash = sha3_256(combined_transcript_text.encode('utf-8')).hexdigest()
    computed_embeddings = await compute_embeddings_for_document(list_of_transcript_sentences, llm_model_name, ip_address, file_hash)
    zip_file_path = f"{zip_dir}/{quote(document_name)}.zip"
    with zipfile.ZipFile(zip_file_path, 'w') as zipf:
        zipf.writestr("embeddings.txt", json.dumps(computed_embeddings))
    download_url = f"download/{quote(document_name)}.zip"
    full_download_url = f"{req.base_url}{download_url}"
    logger.info(f"Generated download URL for transcript embeddings: {full_download_url}")
    fake_upload_file = FakeUploadFile(filename=document_name, content=combined_transcript_text.encode(), content_type='text/plain')
    logger.info(f"Storing transcript embeddings for {audio_file_name} in the database...")
    await store_document_embeddings_in_db(fake_upload_file, file_hash, combined_transcript_text.encode(), json.dumps(computed_embeddings).encode(), computed_embeddings, llm_model_name, ip_address, datetime.utcnow())
    return full_download_url

async def compute_transcript_with_whisper_from_audio_func(audio_file_hash, audio_file_path, audio_file_name, audio_file_size_mb, ip_address,  req: Request, compute_embeddings_for_resulting_transcript_document=True, llm_model_name=DEFAULT_MODEL_NAME):
    model_size = "large-v2"
    logger.info(f"Loading Whisper model {model_size}...")
    num_workers = 1 if psutil.virtual_memory().total < 32 * (1024 ** 3) else min(4, max(1, int((psutil.virtual_memory().total - 32 * (1024 ** 3)) / (4 * (1024 ** 3))))) # Only use more than 1 worker if there is at least 32GB of RAM; then use 1 worker per additional 4GB of RAM up to 4 workers max
    model = await run_in_threadpool(WhisperModel, model_size, device="cpu", compute_type="auto", cpu_threads=os.cpu_count(), num_workers=num_workers)
    request_time = datetime.utcnow()
    logger.info(f"Computing transcript for {audio_file_name} which has a {audio_file_size_mb :.2f}MB file size...")
    segments, info = await run_in_threadpool(model.transcribe, audio_file_path, beam_size=20)
    if not segments:
        logger.warning(f"No segments were returned for file {audio_file_name}.")
        return [], {}, "", [], request_time, datetime.utcnow(), 0, ""    
    segment_details = []
    for idx, segment in enumerate(segments):
        details = {
            "start": round(segment.start, 2),
            "end": round(segment.end, 2),
            "text": segment.text,
            "avg_logprob": round(segment.avg_logprob, 2)
        }
        logger.info(f"Details of transcript segment {idx} from file {audio_file_name}: {details}")
        segment_details.append(details)
    combined_transcript_text, combined_transcript_text_list_of_metadata_dicts, list_of_transcript_sentences = merge_transcript_segments_into_combined_text(segment_details)    
    if compute_embeddings_for_resulting_transcript_document:
        download_url = await compute_and_store_transcript_embeddings(audio_file_name, list_of_transcript_sentences, llm_model_name, ip_address, combined_transcript_text, req)
    else:
        download_url = ''
    response_time = datetime.utcnow()
    total_time = (response_time - request_time).total_seconds()
    logger.info(f"Transcript computed in {total_time} seconds.")
    await save_transcript_to_db(audio_file_hash, audio_file_name, audio_file_size_mb, segment_details, info, ip_address, request_time, response_time, total_time, combined_transcript_text, combined_transcript_text_list_of_metadata_dicts)
    info_dict = info._asdict()
    return segment_details, info_dict, combined_transcript_text, combined_transcript_text_list_of_metadata_dicts, request_time, response_time, total_time, download_url
    
async def get_or_compute_transcript(file: UploadFile, compute_embeddings_for_resulting_transcript_document: bool, llm_model_name: str, req: Request = None) -> dict:
    request_time = datetime.utcnow()
    ip_address = req.client.host if req else "127.0.0.1"
    file_contents = await file.read()
    audio_file_hash = sha3_256(file_contents).hexdigest()
    file.file.seek(0)  # Reset file pointer after read
    existing_audio_transcript = await get_transcript_from_db(audio_file_hash)
    if existing_audio_transcript:
        return existing_audio_transcript
    current_position = file.file.tell()
    file.file.seek(0, os.SEEK_END)
    audio_file_size_mb = file.file.tell() / (1024 * 1024)
    file.file.seek(current_position)
    with tempfile.NamedTemporaryFile(delete=False) as tmp_file:
        shutil.copyfileobj(file.file, tmp_file)
        audio_file_name = tmp_file.name
    segment_details, info, combined_transcript_text, combined_transcript_text_list_of_metadata_dicts, request_time, response_time, total_time, download_url = await compute_transcript_with_whisper_from_audio_func(audio_file_hash, audio_file_name, file.filename, audio_file_size_mb, ip_address, req, compute_embeddings_for_resulting_transcript_document, llm_model_name)
    audio_transcript_response = {
        "audio_file_hash": audio_file_hash,
        "audio_file_name": file.filename,
        "audio_file_size_mb": audio_file_size_mb,
        "segments_json": segment_details,
        "combined_transcript_text": combined_transcript_text,
        "combined_transcript_text_list_of_metadata_dicts": combined_transcript_text_list_of_metadata_dicts,
        "info_json": info,
        "ip_address": ip_address,
        "request_time": request_time,
        "response_time": response_time,
        "total_time": total_time,
        "url_to_download_zip_file_of_embeddings": download_url if compute_embeddings_for_resulting_transcript_document else ""
    }
    os.remove(audio_file_name)
    return AudioTranscriptResponse(**audio_transcript_response)
    
    
# Core embedding functions start here:    

def download_models() -> List[str]:
    list_of_model_download_urls = [
        'https://huggingface.co/TheBloke/Yarn-Llama-2-13B-128K-GGUF/resolve/main/yarn-llama-2-13b-128k.Q4_K_M.gguf',
        'https://huggingface.co/TheBloke/Yarn-Llama-2-7B-128K-GGUF/resolve/main/yarn-llama-2-7b-128k.Q4_K_M.gguf',
        'https://huggingface.co/TheBloke/openchat_v3.2_super-GGUF/resolve/main/openchat_v3.2_super.Q4_K_M.gguf',
        'https://huggingface.co/TheBloke/Phind-CodeLlama-34B-Python-v1-GGUF/resolve/main/phind-codellama-34b-python-v1.Q4_K_M.gguf',
    ]
    model_names = [os.path.basename(url) for url in list_of_model_download_urls]
    current_file_path = os.path.abspath(__file__)
    base_dir = os.path.dirname(current_file_path)
    models_dir = os.path.join(base_dir, 'models')
    logger.info("Checking models directory...")
    if USE_RAMDISK:
        ramdisk_models_dir = os.path.join(RAMDISK_PATH, 'models')
        if not os.path.exists(RAMDISK_PATH): # Check if RAM disk exists, and set it up if not
            setup_ramdisk()
        if all(os.path.exists(os.path.join(ramdisk_models_dir, llm_model_name)) for llm_model_name in model_names): # Check if models already exist in RAM disk
            logger.info("Models found in RAM Disk.")
            return model_names
    if not os.path.exists(models_dir): # Check if models directory exists, and create it if not
        os.makedirs(models_dir)
        logger.info(f"Created models directory: {models_dir}")
    else:
        logger.info(f"Models directory exists: {models_dir}")
    for url, model_name_with_extension in zip(list_of_model_download_urls, model_names): # Check if models are in regular disk, download if not
        filename = os.path.join(models_dir, model_name_with_extension)
        if not os.path.exists(filename):
            logger.info(f"Downloading model {model_name_with_extension} from {url}...")
            urllib.request.urlretrieve(url, filename)
            logger.info(f"Downloaded: {filename}")
        else:
            logger.info(f"File already exists: {filename}")
    if USE_RAMDISK: # If RAM disk is enabled, copy models from regular disk to RAM disk
        copy_models_to_ramdisk(models_dir, ramdisk_models_dir)
    logger.info("Model downloads completed.")
    return model_names

async def get_embedding_from_db(text: str, llm_model_name: str):
    text_hash = sha3_256(text.encode('utf-8')).hexdigest() # Compute the hash
    return await execute_with_retry(_get_embedding_from_db, text_hash, llm_model_name)

async def _get_embedding_from_db(text_hash: str, llm_model_name: str) -> Optional[dict]:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            sql_text("SELECT embedding_json FROM embeddings WHERE text_hash=:text_hash AND llm_model_name=:llm_model_name"),
            {"text_hash": text_hash, "llm_model_name": llm_model_name},
        )
        row = result.fetchone()
        if row:
            embedding_json = row[0]
            logger.info(f"Embedding found in database for text hash '{text_hash}' using model '{llm_model_name}'")
            return json.loads(embedding_json)
        return None
    
async def get_or_compute_embedding(request: EmbeddingRequest, req: Request = None, client_ip: str = None, document_file_hash: str = None) -> dict:
    request_time = datetime.utcnow()  # Capture request time as datetime object
    ip_address = client_ip or (req.client.host if req else "localhost") # If client_ip is provided, use it; otherwise, try to get from req; if not available, default to "localhost"
    logger.info(f"Received request for embedding for '{request.text}' using model '{request.llm_model_name}' from IP address '{ip_address}'")
    embedding_list = await get_embedding_from_db(request.text, request.llm_model_name) # Check if embedding exists in the database
    if embedding_list is not None:
        response_time = datetime.utcnow()  # Capture response time as datetime object
        total_time = (response_time - request_time).total_seconds()  # Calculate time taken in seconds
        logger.info(f"Embedding found in database for '{request.text}' using model '{request.llm_model_name}'; returning in {total_time:.4f} seconds")
        return {"embedding": embedding_list}
    model = load_model(request.llm_model_name)
    embedding_list = calculate_sentence_embedding(model, request.text) # Compute the embedding if not in the database
    if embedding_list is None:
        logger.error(f"Could not calculate the embedding for the given text: '{request.text}' using model '{request.llm_model_name}!'")
        raise HTTPException(status_code=400, detail="Could not calculate the embedding for the given text")
    embedding_json = json.dumps(embedding_list) # Serialize the numpy array to JSON and save to the database
    response_time = datetime.utcnow()  # Capture response time as datetime object
    total_time = (response_time - request_time).total_seconds() # Calculate total time using datetime objects
    word_length_of_input_text = len(request.text.split())
    if word_length_of_input_text > 0:
        logger.info(f"Embedding calculated for '{request.text}' using model '{request.llm_model_name}' in {total_time} seconds, or an average of {total_time/word_length_of_input_text :.2f} seconds per word. Now saving to database...")
    await save_embedding_to_db(request.text, request.llm_model_name, embedding_json, ip_address, request_time, response_time, total_time, document_file_hash)
    return {"embedding": embedding_list}

async def save_embedding_to_db(text: str, llm_model_name: str, embedding_json: str, ip_address: str, request_time: datetime, response_time: datetime, total_time: float, document_file_hash: str = None):
    existing_embedding = await get_embedding_from_db(text, llm_model_name) # Check if the embedding already exists
    if existing_embedding is not None:
        return existing_embedding
    return await execute_with_retry(_save_embedding_to_db, text, llm_model_name, embedding_json, ip_address, request_time, response_time, total_time, document_file_hash)

async def _save_embedding_to_db(text: str, llm_model_name: str, embedding_json: str, ip_address: str, request_time: datetime, response_time: datetime, total_time: float, document_file_hash: str = None):
    existing_embedding = await get_embedding_from_db(text, llm_model_name)
    if existing_embedding:
        return existing_embedding
    embedding = TextEmbedding(
        text=text,
        llm_model_name=llm_model_name,
        embedding_json=embedding_json,
        ip_address=ip_address,
        request_time=request_time,
        response_time=response_time,
        total_time=total_time,
        document_file_hash=document_file_hash
    )
    await db_writer.enqueue_write([embedding])  # Enqueue the write operation using the db_writer instance

def load_model(llm_model_name: str, raise_http_exception: bool = True):
    try:
        models_dir = os.path.join(RAMDISK_PATH, 'models') if USE_RAMDISK else os.path.join(BASE_DIRECTORY, 'models')
        if llm_model_name in embedding_model_cache:
            return embedding_model_cache[llm_model_name]
        matching_files = glob.glob(os.path.join(models_dir, f"{llm_model_name}*"))
        if not matching_files:
            logger.error(f"No model file found matching: {llm_model_name}")
            raise FileNotFoundError
        matching_files.sort(key=os.path.getmtime, reverse=True)
        model_file_path = matching_files[0]
        model_instance = LlamaCppEmbeddings(model_path=model_file_path, use_mlock=True, n_ctx=LLM_CONTEXT_SIZE_IN_TOKENS)
        model_instance.client.verbose = False
        embedding_model_cache[llm_model_name] = model_instance
        return model_instance
    except TypeError as e:
        logger.error(f"TypeError occurred while loading the model: {e}")
        raise
    except Exception as e:
        logger.error(f"Exception occurred while loading the model: {e}")
        if raise_http_exception:
            raise HTTPException(status_code=404, detail="Model file not found")
        else:
            raise FileNotFoundError(f"No model file found matching: {llm_model_name}")

def load_token_level_embedding_model(llm_model_name: str, raise_http_exception: bool = True):
    try:
        if llm_model_name in token_level_embedding_model_cache: # Check if the model is already loaded in the cache
            return token_level_embedding_model_cache[llm_model_name]
        models_dir = os.path.join(RAMDISK_PATH, 'models') if USE_RAMDISK else os.path.join(BASE_DIRECTORY, 'models') # Determine the model directory path
        matching_files = glob.glob(os.path.join(models_dir, f"{llm_model_name}*")) # Search for matching model files
        if not matching_files:
            logger.error(f"No model file found matching: {llm_model_name}")
            raise FileNotFoundError
        matching_files.sort(key=os.path.getmtime, reverse=True) # Sort the files based on modification time (recently modified files first)
        model_file_path = matching_files[0]
        model_instance = Llama(model_path=model_file_path, embedding=True, n_ctx=LLM_CONTEXT_SIZE_IN_TOKENS, verbose=False) # Load the model
        token_level_embedding_model_cache[llm_model_name] = model_instance # Cache the loaded model
        return model_instance
    except TypeError as e:
        logger.error(f"TypeError occurred while loading the model: {e}")
        raise
    except Exception as e:
        logger.error(f"Exception occurred while loading the model: {e}")
        if raise_http_exception:
            raise HTTPException(status_code=404, detail="Model file not found")
        else:
            raise FileNotFoundError(f"No model file found matching: {llm_model_name}")

async def compute_token_level_embedding_bundle_combined_feature_vector(token_level_embeddings) -> List[float]:
    start_time = datetime.utcnow()
    logger.info("Extracting token-level embeddings from the bundle")
    parsed_df = pd.read_json(token_level_embeddings) # Parse the json_content back to a DataFrame
    token_level_embeddings = list(parsed_df['embedding'])
    embeddings = np.array(token_level_embeddings) # Convert the list of embeddings to a NumPy array
    logger.info(f"Computing column-wise means/mins/maxes/std_devs of the embeddings... (shape: {embeddings.shape})")
    assert(len(embeddings) > 0)
    means = np.mean(embeddings, axis=0)
    mins = np.min(embeddings, axis=0)
    maxes = np.max(embeddings, axis=0)
    stds = np.std(embeddings, axis=0)
    logger.info("Concatenating the computed statistics to form the combined feature vector")
    combined_feature_vector = np.concatenate([means, mins, maxes, stds])
    end_time = datetime.utcnow()
    total_time = (end_time - start_time).total_seconds()
    logger.info(f"Computed the token-level embedding bundle's combined feature vector computed in {total_time: .2f} seconds.")
    return combined_feature_vector.tolist()

async def get_or_compute_token_level_embedding_bundle_combined_feature_vector(token_level_embedding_bundle_id, token_level_embeddings, db_writer: DatabaseWriter) -> List[float]:
    request_time = datetime.utcnow()
    logger.info(f"Checking for existing combined feature vector for token-level embedding bundle ID: {token_level_embedding_bundle_id}")
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(TokenLevelEmbeddingBundleCombinedFeatureVector)
            .filter(TokenLevelEmbeddingBundleCombinedFeatureVector.token_level_embedding_bundle_id == token_level_embedding_bundle_id)
        )
        existing_combined_feature_vector = result.scalar_one_or_none()
        if existing_combined_feature_vector:
            response_time = datetime.utcnow()
            total_time = (response_time - request_time).total_seconds()
            logger.info(f"Found existing combined feature vector for token-level embedding bundle ID: {token_level_embedding_bundle_id}. Returning cached result in {total_time:.2f} seconds.")
            return json.loads(existing_combined_feature_vector.combined_feature_vector_json)  # Parse the JSON string into a list
    logger.info(f"No cached combined feature_vector found for token-level embedding bundle ID: {token_level_embedding_bundle_id}. Computing now...")
    combined_feature_vector = await compute_token_level_embedding_bundle_combined_feature_vector(token_level_embeddings)
    combined_feature_vector_db_object = TokenLevelEmbeddingBundleCombinedFeatureVector(
        token_level_embedding_bundle_id=token_level_embedding_bundle_id,
        combined_feature_vector_json=json.dumps(combined_feature_vector)  # Convert the list to a JSON string
    )
    logger.info(f"Writing combined feature vector for database write for token-level embedding bundle ID: {token_level_embedding_bundle_id} to the database...")
    await db_writer.enqueue_write([combined_feature_vector_db_object])
    return combined_feature_vector

async def calculate_token_level_embeddings(text: str, llm_model_name: str, client_ip: str, token_level_embedding_bundle_id: int) -> List[np.array]:
    request_time = datetime.utcnow()
    logger.info(f"Starting token-level embedding calculation for text: '{text}' using model: '{llm_model_name}'")
    logger.info(f"Loading model: '{llm_model_name}'")
    llm = load_token_level_embedding_model(llm_model_name)  # Assuming this method returns an instance of the Llama class
    token_embeddings = []
    tokens = text.split()  # Simple whitespace tokenizer; can be replaced with a more advanced one if needed
    logger.info(f"Tokenized text into {len(tokens)} tokens")
    for idx, token in enumerate(tokens, start=1):
        try:  # Check if the embedding is already available in the database
            existing_embedding = await get_token_level_embedding_from_db(token, llm_model_name)
            if existing_embedding is not None:
                token_embeddings.append(np.array(existing_embedding))
                logger.info(f"Embedding retrieved from database for token '{token}'")
                continue
            logger.info(f"Processing token {idx} of {len(tokens)}: '{token}'")
            token_embedding = llm.embed(token)
            token_embedding_array = np.array(token_embedding)
            token_embeddings.append(token_embedding_array)
            response_time = datetime.utcnow()
            token_level_embedding_json = json.dumps(token_embedding_array.tolist())
            await store_token_level_embeddings_in_db(token, llm_model_name, token_level_embedding_json, client_ip, request_time, response_time, token_level_embedding_bundle_id)
        except RuntimeError as e:
            logger.error(f"Failed to calculate embedding for token '{token}': {e}")
    logger.info(f"Completed token embedding calculation for all tokens in text: '{text}'")
    return token_embeddings

async def get_token_level_embedding_from_db(token: str, llm_model_name: str) -> Optional[List[float]]:
    token_hash = sha3_256(token.encode('utf-8')).hexdigest() # Compute the hash
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            sql_text("SELECT token_level_embedding_json FROM token_level_embeddings WHERE token_hash=:token_hash AND llm_model_name=:llm_model_name"),
            {"token_hash": token_hash, "llm_model_name": llm_model_name},
        )
        row = result.fetchone()
        if row:
            embedding_json = row[0]
            logger.info(f"Embedding found in database for token hash '{token_hash}' using model '{llm_model_name}'")
            return json.loads(embedding_json)
        return None

async def store_token_level_embeddings_in_db(token: str, llm_model_name: str, token_level_embedding_json: str, ip_address: str, request_time: datetime, response_time: datetime, token_level_embedding_bundle_id: int):
    total_time = (response_time - request_time).total_seconds()
    embedding = TokenLevelEmbedding(
        token=token,
        llm_model_name=llm_model_name,
        token_level_embedding_json=token_level_embedding_json,
        ip_address=ip_address,
        request_time=request_time,
        response_time=response_time,
        total_time=total_time,
        token_level_embedding_bundle_id=token_level_embedding_bundle_id
    )
    await db_writer.enqueue_write([embedding]) # Enqueue the write operation for the token-level embedding
        
def calculate_sentence_embedding(llama: Llama, text: str) -> np.array:
    sentence_embedding = None
    retry_count = 0
    while sentence_embedding is None and retry_count < 3:
        try:
            if retry_count > 0:
                logger.info(f"Attempting again calculate sentence embedding. Attempt number {retry_count + 1}")
            sentence_embedding = llama.embed_query(text)
        except TypeError as e:
            logger.error(f"TypeError in calculate_sentence_embedding: {e}")
            raise
        except Exception as e:
            logger.error(f"Exception in calculate_sentence_embedding: {e}")
            text = text[:-int(len(text) * 0.1)]
            retry_count += 1
            logger.info(f"Trimming sentence due to too many tokens. New length: {len(text)}")
    if sentence_embedding is None:
        logger.error("Failed to calculate sentence embedding after multiple attempts")
    return sentence_embedding

async def compute_embeddings_for_document(strings: list, llm_model_name: str, client_ip: str, document_file_hash: str) -> List[Tuple[str, np.array]]:
    results = []
    if USE_PARALLEL_INFERENCE_QUEUE:
        logger.info(f"Using parallel inference queue to compute embeddings for {len(strings)} strings")
        start_time = time.perf_counter()  # Record the start time
        semaphore = asyncio.Semaphore(MAX_CONCURRENT_PARALLEL_INFERENCE_TASKS)
        async def compute_embedding(text):  # Define a function to compute the embedding for a given text
            try:
                async with semaphore:  # Acquire a semaphore slot
                    request = EmbeddingRequest(text=text, llm_model_name=llm_model_name)
                    embedding = await get_embedding_vector_for_string(request, client_ip=client_ip, document_file_hash=document_file_hash)
                    return text, embedding["embedding"]
            except Exception as e:
                logger.error(f"Error computing embedding for text '{text}': {e}")
                return text, None
        results = await asyncio.gather(*[compute_embedding(s) for s in strings])  # Use asyncio.gather to run the tasks concurrently
        end_time = time.perf_counter()  # Record the end time
        duration = end_time - start_time
        if len(strings) > 0:
            logger.info(f"Parallel inference task for {len(strings)} strings completed in {duration:.2f} seconds; {duration / len(strings):.2f} seconds per string")
    else:  # Compute embeddings sequentially
        logger.info(f"Using sequential inference to compute embeddings for {len(strings)} strings")
        start_time = time.perf_counter()  # Record the start time
        for s in strings:
            embedding_request = EmbeddingRequest(text=s, llm_model_name=llm_model_name)
            embedding = await get_embedding_vector_for_string(embedding_request, client_ip=client_ip, document_file_hash=document_file_hash)
            results.append((s, embedding["embedding"]))
        end_time = time.perf_counter()  # Record the end time
        duration = end_time - start_time
        if len(strings) > 0:
            logger.info(f"Sequential inference task for {len(strings)} strings completed in {duration:.2f} seconds; {duration / len(strings):.2f} seconds per string")
    filtered_results = [(text, embedding) for text, embedding in results if embedding is not None] # Filter out results with None embeddings (applicable to parallel processing) and return
    return filtered_results

async def parse_submitted_document_file_into_sentence_strings_func(temp_file_path: str, mime_type: str):
    strings = []
    if mime_type.startswith('text/'):
        with open(temp_file_path, 'r') as buffer:
            content = buffer.read()
    else:
        try:
            content = textract.process(temp_file_path).decode('utf-8')
        except UnicodeDecodeError:
            try:
                content = textract.process(temp_file_path).decode('unicode_escape')
            except Exception as e:
                logger.error(f"Error while processing file: {e}, mime_type: {mime_type}")
                raise HTTPException(status_code=400, detail=f"Unsupported file type or error: {e}")
        except Exception as e:
            logger.error(f"Error while processing file: {e}, mime_type: {mime_type}")
            raise HTTPException(status_code=400, detail=f"Unsupported file type or error: {e}")
    sentences = sophisticated_sentence_splitter(content)
    if len(sentences) == 0 and temp_file_path.lower().endswith('.pdf'):
        logger.info("No sentences found, attempting OCR using Tesseract.")
        try:
            content = textract.process(temp_file_path, method='tesseract').decode('utf-8')
            sentences = sophisticated_sentence_splitter(content)
        except Exception as e:
            logger.error(f"Error while processing file with OCR: {e}")
            raise HTTPException(status_code=400, detail=f"OCR failed: {e}")
    if len(sentences) == 0:
        logger.info("No sentences found in the document")
        raise HTTPException(status_code=400, detail="No sentences found in the document")
    logger.info(f"Extracted {len(sentences)} sentences from the document")
    strings = [s.strip() for s in sentences if len(s.strip()) > MINIMUM_STRING_LENGTH_FOR_DOCUMENT_EMBEDDING]
    return strings

async def _get_document_from_db(file_hash: str):
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Document).filter(Document.document_hash == file_hash))
        return result.scalar_one_or_none()

async def store_document_embeddings_in_db(file: File, file_hash: str, original_file_content: bytes, json_content: bytes, results: List[Tuple[str, np.array]], llm_model_name: str, client_ip: str, request_time: datetime):
    document = await _get_document_from_db(file_hash) # First, check if a Document with the same hash already exists
    if not document: # If not, create a new Document object
        document = Document(document_hash=file_hash, llm_model_name=llm_model_name)
        await db_writer.enqueue_write([document])    
    document_embedding = DocumentEmbedding(
        filename=file.filename,
        mimetype=file.content_type,
        file_hash=file_hash,
        llm_model_name=llm_model_name,
        file_data=original_file_content,
        document_embedding_results_json=json.loads(json_content.decode()),
        ip_address=client_ip,
        request_time=request_time,
        response_time=datetime.utcnow(),
        total_time=(datetime.utcnow() - request_time).total_seconds()
    )
    document.document_embeddings.append(document_embedding)  # Associate it with the Document
    document.update_hash() # This will trigger the SQLAlchemy event to update the document_hash
    await db_writer.enqueue_write([document, document_embedding])  # Enqueue the write operation for the document embedding
    write_operations = []  # Collect text embeddings to write
    logger.info(f"Storing {len(results)} text embeddings in database")
    for text, embedding in results:
        embedding_entry = await _get_embedding_from_db(text, llm_model_name)
        if not embedding_entry:
            embedding_entry = TextEmbedding(
                text=text,
                llm_model_name=llm_model_name,
                embedding_json=json.dumps(embedding),
                ip_address=client_ip,
                request_time=request_time,
                response_time=datetime.utcnow(),
                total_time=(datetime.utcnow() - request_time).total_seconds(),
                document_file_hash=file_hash  # Link it to the DocumentEmbedding via file_hash
            )
        else:
            write_operations.append(embedding_entry)
    await db_writer.enqueue_write(write_operations)  # Enqueue the write operation for text embeddings

def load_text_completion_model(llm_model_name: str, raise_http_exception: bool = True):
    try:
        if llm_model_name in text_completion_model_cache: # Check if the model is already loaded in the cache
            return text_completion_model_cache[llm_model_name]
        models_dir = os.path.join(RAMDISK_PATH, 'models') if USE_RAMDISK else os.path.join(BASE_DIRECTORY, 'models') # Determine the model directory path
        matching_files = glob.glob(os.path.join(models_dir, f"{llm_model_name}*")) # Search for matching model files
        if not matching_files:
            logger.error(f"No model file found matching: {llm_model_name}")
            raise FileNotFoundError
        matching_files.sort(key=os.path.getmtime, reverse=True) # Sort the files based on modification time (recently modified files first)
        model_file_path = matching_files[0]
        model_instance = Llama(model_path=model_file_path, embedding=True, n_ctx=TEXT_COMPLETION_CONTEXT_SIZE_IN_TOKENS, verbose=False) # Load the model
        text_completion_model_cache[llm_model_name] = model_instance # Cache the loaded model
        return model_instance
    except TypeError as e:
        logger.error(f"TypeError occurred while loading the model: {e}")
        raise
    except Exception as e:
        logger.error(f"Exception occurred while loading the model: {e}")
        if raise_http_exception:
            raise HTTPException(status_code=404, detail="Model file not found")
        else:
            raise FileNotFoundError(f"No model file found matching: {llm_model_name}")
        
async def generate_completion_from_llm(request: TextCompletionRequest, req: Request = None, client_ip: str = None) -> List[TextCompletionResponse]:
    request_time = datetime.utcnow()
    logger.info(f"Starting text completion calculation using model: '{request.llm_model_name}'for input prompt: '{request.input_prompt}'")
    logger.info(f"Loading model: '{request.llm_model_name}'")
    llm = load_text_completion_model(request.llm_model_name)
    list_of_llm_outputs = []
    if request.grammar_file_string != "":
        list_of_grammar_files = glob.glob("./grammar_files/*.gbnf")
        matching_grammar_files = [x for x in list_of_grammar_files if request.grammar_file_string in x]
        if len(matching_grammar_files) == 0:
            logger.error(f"No grammar file found matching: {request.grammar_file_string}")
            raise FileNotFoundError
        matching_grammar_files.sort(key=os.path.getmtime, reverse=True) # Sort the files based on modification time (recently modified files first)
        grammar_file_path = matching_grammar_files[0]
        logger.info(f"Loading selected grammar file: '{grammar_file_path}'")
        llama_grammar = LlamaGrammar.from_file(grammar_file_path)
        for ii in range(request.number_of_completions_to_generate):
            logger.info(f"Generating completion {ii+1} of {request.number_of_completions_to_generate} with model {request.llm_model_name} for input prompt: '{request.input_prompt}'")
            output = llm(prompt=request.input_prompt, grammar=llama_grammar, max_tokens=request.number_of_tokens_to_generate, temperature=request.temperature)
            list_of_llm_outputs.append(output)
    else:
        for ii in range(request.number_of_completions_to_generate):
            output = llm(prompt=request.input_prompt, max_tokens=request.number_of_tokens_to_generate, temperature=request.temperature)
            list_of_llm_outputs.append(output)
    response_time = datetime.utcnow()
    total_time_per_completion = ((response_time - request_time).total_seconds()) / request.number_of_completions_to_generate
    list_of_responses = []
    for idx, current_completion_output in enumerate(list_of_llm_outputs):
        generated_text = current_completion_output['choices'][0]['text']
        if request.grammar_file_string == 'json':
            generated_text = generated_text.encode('unicode_escape').decode()
        llm_model_usage_json = json.dumps(current_completion_output['usage'])
        logger.info(f"Completed text completion {idx} in an average of {total_time_per_completion:.2f} seconds for input prompt: '{request.input_prompt}'; Beginning of generated text: \n'{generated_text[:100]}'")
        response = TextCompletionResponse(input_prompt = request.input_prompt,
                                            llm_model_name = request.llm_model_name,
                                            grammar_file_string = request.grammar_file_string,
                                            number_of_tokens_to_generate = request.number_of_tokens_to_generate,
                                            number_of_completions_to_generate = request.number_of_completions_to_generate,
                                            time_taken_in_seconds = float(total_time_per_completion),
                                            generated_text = generated_text,
                                            llm_model_usage_json = llm_model_usage_json)
        list_of_responses.append(response)
    return list_of_responses


@app.exception_handler(SQLAlchemyError) 
async def sqlalchemy_exception_handler(request: Request, exc: SQLAlchemyError) -> JSONResponse:
    logger.exception(exc)
    return JSONResponse(status_code=500, content={"message": "Database error occurred"})

@app.exception_handler(Exception)
async def general_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception(exc)
    return JSONResponse(status_code=500, content={"message": "An unexpected error occurred"})

#FastAPI Endpoints start here:

@app.get("/", include_in_schema=False)
async def custom_swagger_ui_html():
    return fastapi.templating.get_swagger_ui_html(openapi_url="/openapi.json", title=app.title, swagger_favicon_url=app.swagger_ui_favicon_url)


@app.get("/get_list_of_available_model_names/",
        summary="Retrieve Available Model Names",
        description="""Retrieve the list of available model names for generating embeddings.

### Parameters:
- `token`: Security token (optional).

### Response:
The response will include a JSON object containing the list of available model names. Note that these are all GGML format models designed to work with llama_cpp.

### Example Response:
```json
{
  "model_names": ["yarn-llama-2-7b-128k", "yarn-llama-2-13b-128k", "openchat_v3.2_super", "phind-codellama-34b-python-v1", "my_super_custom_model"]
}
```""",
        response_description="A JSON object containing the list of available model names.")
async def get_list_of_available_model_names(token: str = None) -> Dict[str, List[str]]:
    if USE_SECURITY_TOKEN and (token is None or token != SECURITY_TOKEN):
        raise HTTPException(status_code=403, detail="Unauthorized")
    models_dir = os.path.join(RAMDISK_PATH, 'models') if USE_RAMDISK else os.path.join(BASE_DIRECTORY, 'models')
    logger.info(f"Looking for models in: {models_dir}") # Add this line for debugging
    logger.info(f"Directory content: {os.listdir(models_dir)}") # Add this line for debugging
    model_files = glob.glob(os.path.join(models_dir, "*.bin")) +  glob.glob(os.path.join(models_dir, "*.gguf"))# Find all files with .bin or .gguf extension
    model_names = [os.path.splitext(os.path.splitext(os.path.basename(model_file))[0])[0] for model_file in model_files] # Remove both extensions
    return {"model_names": model_names}



@app.get("/get_all_stored_strings/",
        summary="Retrieve All Strings",
        description="""Retrieve a list of all stored strings from the database for which embeddings have been computed.

### Parameters:
- `token`: Security token (optional).

### Response:
The response will include a JSON object containing the list of all stored strings with computed embeddings.

### Example Response:
```json
{
  "strings": ["The quick brown fox jumps over the lazy dog", "To be or not to be", "Hello, World!"]
}
```""",
        response_description="A JSON object containing the list of all strings with computed embeddings.")
async def get_all_stored_strings(req: Request, token: str = None) -> AllStringsResponse:
    logger.info("Received request to retrieve all stored strings for which embeddings have been computed")
    if USE_SECURITY_TOKEN and use_hardcoded_security_token and (token is None or token != SECURITY_TOKEN):
        logger.warning(f"Unauthorized request to retrieve all stored strings for which embeddings have been computed from {req.client.host}")
        raise HTTPException(status_code=403, detail="Unauthorized")
    try:
        logger.info("Retrieving all stored strings with computed embeddings from the database")
        async with AsyncSessionLocal() as session:
            result = await session.execute(sql_text("SELECT DISTINCT text FROM embeddings"))
            all_strings = [row[0] for row in result.fetchall()]
        logger.info(f"Retrieved {len(all_strings)} stored strings with computed embeddings from the database; Last 10 embedded strings: {all_strings[-10:]}")
        return {"strings": all_strings}
    except Exception as e:
        logger.error(f"An error occurred while processing the request: {e}")
        logger.error(traceback.format_exc())  # Print the traceback
        raise HTTPException(status_code=500, detail="Internal Server Error")



@app.get("/get_all_stored_documents/",
        summary="Retrieve All Stored Documents",
        description="""Retrieve a list of all stored documents from the database for which embeddings have been computed.

### Parameters:
- `token`: Security token (optional).

### Response:
The response will include a JSON object containing the list of all stored documents with computed embeddings.

### Example Response:
```json
{
  "documents": ["document1.pdf", "document2.txt", "document3.md", "document4.json"]
}
```""",
        response_description="A JSON object containing the list of all documents with computed embeddings.")
async def get_all_stored_documents(req: Request, token: str = None) -> AllDocumentsResponse:
    logger.info("Received request to retrieve all stored documents with computed embeddings")
    if USE_SECURITY_TOKEN and use_hardcoded_security_token and (token is None or token != SECURITY_TOKEN):
        logger.warning(f"Unauthorized request to retrieve all stored documents for which all sentence embeddings have been computed from {req.client.host}")
        raise HTTPException(status_code=403, detail="Unauthorized")
    try:
        logger.info("Retrieving all stored documents with computed embeddings from the database")
        async with AsyncSessionLocal() as session:
            result = await session.execute(sql_text("SELECT DISTINCT filename FROM document_embeddings"))
            all_documents = [row[0] for row in result.fetchall()]
        logger.info(f"Retrieved {len(all_documents)} stored documents with computed embeddings from the database; Last 10 processed document filenames: {all_documents[-10:]}")
        return {"documents": all_documents}
    except Exception as e:
        logger.error(f"An error occurred while processing the request: {e}")
        logger.error(traceback.format_exc())  # Print the traceback
        raise HTTPException(status_code=500, detail="Internal Server Error")



@app.post("/get_embedding_vector_for_string/",
        response_model=EmbeddingResponse,
        summary="Retrieve Embedding Vector for a Given Text String",
        description="""Retrieve the embedding vector for a given input text string using the specified model.

### Parameters:
- `request`: A JSON object containing the input text string (`text`) and the model name.
- `token`: Security token (optional).
- `document_file_hash`: The SHA3-256 hash of the document file, if applicable (optional).

### Request JSON Format:
The request must contain the following attributes:
- `text`: The input text for which the embedding vector is to be retrieved.
- `llm_model_name`: The model used to calculate the embedding (optional, will use the default model if not provided).

### Example (note that `llm_model_name` is optional):
```json
{
  "text": "This is a sample text.",
  "llm_model_name": "openchat_v3.2_super"
}
```

### Response:
The response will include the embedding vector for the input text string.

### Example Response:
```json
{
  "embedding": [0.1234, 0.5678, ...]
}
```""", response_description="A JSON object containing the embedding vector for the input text.")
async def get_embedding_vector_for_string(request: EmbeddingRequest, req: Request = None, token: str = None, client_ip: str = None, document_file_hash: str = None) -> EmbeddingResponse:
    if USE_SECURITY_TOKEN and use_hardcoded_security_token and (token is None or token != SECURITY_TOKEN):
        logger.warning(f"Unauthorized request from client IP {client_ip}")
        raise HTTPException(status_code=403, detail="Unauthorized")
    try:
        return await get_or_compute_embedding(request, req, client_ip, document_file_hash)
    except Exception as e:
        logger.error(f"An error occurred while processing the request: {e}")
        logger.error(traceback.format_exc()) # Print the traceback
        raise HTTPException(status_code=500, detail="Internal Server Error")


@app.post("/get_token_level_embeddings_matrix_and_combined_feature_vector_for_string/",
        summary="Retrieve Token-Level Embeddings and Combined Feature Vector for a Given Input String",
        description="""Retrieve the token-level embeddings and combined feature vector for a given input text using the specified model.

### Parameters:
- `request`: A JSON object containing the text and the model name.
- `db_writer`: Database writer instance for managing write operations.
- `req`: HTTP request object (optional).
- `token`: Security token (optional).
- `client_ip`: Client IP address (optional).
- `json_format`: Format for JSON response of token-level embeddings (optional).
- `send_back_json_or_zip_file`: Whether to return a JSON response or a ZIP file containing the JSON file (optional, defaults to `zip`).

### Request JSON Format:
The request must contain the following attributes:
- `text`: The input text for which the embeddings are to be retrieved.
- `llm_model_name`: The model used to calculate the embeddings (optional).

### Example Request:
```json
{
  "text": "This is a sample text.",
  "llm_model_name": "openchat_v3.2_super"
}
```

### Response:

The response will include the input text for reference, and token-level embeddings matrix for the input text. The response is organized as a JSON array of objects, each containing a token and its corresponding embedding vector. 
Token level embeddings represent a text by breaking it down into individual tokens (words) and associating an embedding vector with each token. These embeddings capture the semantic and
syntactic meaning of each token within the context of the text. Token level embeddings result in a matrix (number of tokens by embedding size), whereas a single embedding vector results 
in a one-dimensional vector of fixed size.

The response will also include a combined feature vector derived from the the token-level embeddings matrix; this combined feature vector has the great benefit that it is always the same length
for all input texts, regardless of length (whereas the token-level embeddings matrix will have a different number of rows for each input text, depending on the number of tokens in the text).
The combined feature vector is obtained by calculating the column-wise means, mins, maxes, and standard deviations of the token-level embeddings matrix; thus if the token-level embedding vectors
are of length `n`, the combined feature vector will be of length `4n`.
 
- `input_text`: The original input text.
- `token_level_embedding_bundle`: Either a ZIP file containing the JSON file, or a direct JSON array containing the token-level embeddings and combined feature vector for the input text, depending on the value of `send_back_json_or_zip_file`.
- `combined_feature_vector`: A list containing the combined feature vector, obtained by calculating the column-wise means, mins, maxes, and standard deviations of the token-level embeddings. This vector is always of length `4n`, where `n` is the length of the token-level embedding vectors.

### Example Response:
```json
{
  "input_text": "This is a sample text.",
  "token_level_embedding_bundle": [
    {"token": "This", "embedding": [0.1234, 0.5678, ...]},
    {"token": "is", "embedding": [...]},
    ...
  ],
  "combined_feature_vector": [0.5678, 0.1234, ...]
}
```
""",
        response_description="A JSON object containing the input text, token embeddings, and combined feature vector for the input text.")
async def get_token_level_embeddings_matrix_and_combined_feature_vector_for_string(
    request: EmbeddingRequest, 
    db_writer: DatabaseWriter = Depends(get_db_writer),
    req: Request = None, 
    token: str = None, 
    client_ip: str = None, 
    json_format: str = 'records',
    send_back_json_or_zip_file: str = 'zip'
) -> Response:
    logger.info(f"Received request for token embeddings with text length {len(request.text)} and model: '{request.llm_model_name}' from client IP: {client_ip}; input text: {request.text}")
    request_time = datetime.utcnow()
    if USE_SECURITY_TOKEN and use_hardcoded_security_token and (token is None or token != SECURITY_TOKEN):
        logger.warning(f"Unauthorized request from client IP {client_ip}")
        raise HTTPException(status_code=403, detail="Unauthorized")
    input_text_hash = sha3_256(request.text.encode('utf-8')).hexdigest()
    logger.info(f"Computed input text hash: {input_text_hash}")
    async with AsyncSessionLocal() as session:
        logger.info(f"Querying database for existing token-level embedding bundle for input text string {request.text} and model {request.llm_model_name}")
        result = await session.execute(
                select(TokenLevelEmbeddingBundle)
                .options(joinedload(TokenLevelEmbeddingBundle.token_level_embeddings)) # Eagerly load the relationship
                .filter(TokenLevelEmbeddingBundle.input_text_hash == input_text_hash, TokenLevelEmbeddingBundle.llm_model_name == request.llm_model_name)
            )
        existing_embedding_bundle = result.unique().scalar()
        if existing_embedding_bundle:
            logger.info("Found existing token-level embedding bundle in the database.")
            combined_feature_vector = await get_or_compute_token_level_embedding_bundle_combined_feature_vector(existing_embedding_bundle.id, existing_embedding_bundle.token_level_embeddings, db_writer)
            response_content = {
                'input_text': request.text,
                'token_level_embedding_bundle': json.loads(existing_embedding_bundle.token_level_embeddings_bundle_json),
                'combined_feature_vector': combined_feature_vector
            }
            return JSONResponse(content=response_content)
    logger.info("No cached result found. Calculating token-level embeddings now...")
    try:
        embedding_bundle = TokenLevelEmbeddingBundle(
            input_text=request.text,
            llm_model_name=request.llm_model_name,
            ip_address=client_ip,
            request_time=request_time
        )
        token_embeddings = await calculate_token_level_embeddings(request.text, request.llm_model_name, client_ip, embedding_bundle.id)
        tokens = re.findall(r'\b\w+\b', request.text)
        logger.info(f"Tokenized text into {len(tokens)} tokens. Organizing results.")
        df = pd.DataFrame({
            'token': tokens,
            'embedding': [embedding.tolist() for embedding in token_embeddings]
        })
        json_content = df.to_json(orient=json_format or 'records')
        response_time=datetime.utcnow()
        total_time = (response_time - request_time).total_seconds()
        embedding_bundle.token_level_embeddings_bundle_json = json_content
        embedding_bundle.response_time = response_time
        embedding_bundle.total_time = total_time
        combined_feature_vector = await get_or_compute_token_level_embedding_bundle_combined_feature_vector(embedding_bundle.id, json_content, db_writer)        
        response_content = {
            'input_text': request.text,
            'token_level_embedding_bundle': json.loads(embedding_bundle.token_level_embeddings_bundle_json),
            'combined_feature_vector': combined_feature_vector
        }
        logger.info(f"Done getting token-level embedding matrix and combined feature vector for input text string {request.text} and model {request.llm_model_name}")
        json_content = embedding_bundle.token_level_embeddings_bundle_json
        json_content_length = len(json.dumps(response_content))
        overall_total_time = (datetime.utcnow() - request_time).total_seconds()
        if len(embedding_bundle.token_level_embeddings_bundle_json) > 0:
            tokens = re.findall(r'\b\w+\b', request.text)
            logger.info(f"The response took {overall_total_time} seconds to generate, or {overall_total_time / (float(len(tokens))/1000.0)} seconds per thousand input tokens and {overall_total_time / (float(json_content_length)/1000000.0)} seconds per million output characters.")
        if send_back_json_or_zip_file == 'json': # Assume 'json' response should be sent back
            logger.info(f"Now sending back JSON response for input text string {request.text} and model {request.llm_model_name}; First 100 characters of JSON response out of {len(json_content)} total characters: {json_content[:100]}")
            return JSONResponse(content=response_content)
        else: # Assume 'zip' file should be sent back
            output_file_name_without_extension = f"token_level_embeddings_and_combined_feature_vector_for_input_hash_{input_text_hash}_and_model_name__{request.llm_model_name}"
            json_file_path = f"/tmp/{output_file_name_without_extension}.json"
            with open(json_file_path, 'w') as json_file:
                json.dump(response_content, json_file)
            zip_file_path = f"/tmp/{output_file_name_without_extension}.zip"
            with zipfile.ZipFile(zip_file_path, 'w') as zipf:
                zipf.write(json_file_path, os.path.basename(json_file_path))
            logger.info(f"Now sending back ZIP file response for input text string {request.text} and model {request.llm_model_name}; First 100 characters of zipped JSON file out of {len(json_content)} total characters: {json_content[:100]}")                            
            return FileResponse(zip_file_path, headers={"Content-Disposition": f"attachment; filename={output_file_name_without_extension}.zip"})
    except Exception as e:
        logger.error(f"An error occurred while processing the request: {e}")
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail="Internal Server Error")


@app.post("/compute_similarity_between_strings/",
        response_model=SimilarityResponse,
        summary="Compute Similarity Between Two Strings",
        description="""Compute the similarity between two given input strings using specified model embeddings and a selected similarity measure.

### Parameters:
- `request`: A JSON object containing the two strings, the model name, and the similarity measure.
- `token`: Security token (optional).

### Request JSON Format:
The request must contain the following attributes:
- `text1`: The first input text.
- `text2`: The second input text.
- `llm_model_name`: The model used to calculate embeddings (optional).
- `similarity_measure`: The similarity measure to be used. Supported measures include `all`, `spearman_rho`, `kendall_tau`, `approximate_distance_correlation`, `jensen_shannon_similarity`, and `hoeffding_d` (optional, default is `all`).

### Example Request (note that `llm_model_name` and `similarity_measure` are optional):
```json
{
  "text1": "This is a sample text.",
  "text2": "This is another sample text.",
  "llm_model_name": "openchat_v3.2_super",
  "similarity_measure": "all"
}
```""")
async def compute_similarity_between_strings(request: SimilarityRequest, req: Request, token: str = None) -> SimilarityResponse:
    logger.info(f"Received request: {request}")
    request_time = datetime.utcnow()
    similarity_measure = request.similarity_measure.lower()
    if USE_SECURITY_TOKEN and use_hardcoded_security_token and (token is None or token != SECURITY_TOKEN):
        raise HTTPException(status_code=403, detail="Unauthorized")
    try:
        client_ip = req.client.host if req else "localhost"
        embedding_request1 = EmbeddingRequest(text=request.text1, llm_model_name=request.llm_model_name)
        embedding_request2 = EmbeddingRequest(text=request.text2, llm_model_name=request.llm_model_name)
        embedding1_response = await get_or_compute_embedding(embedding_request1, client_ip=client_ip)
        embedding2_response = await get_or_compute_embedding(embedding_request2, client_ip=client_ip)
        embedding1 = np.array(embedding1_response["embedding"])
        embedding2 = np.array(embedding2_response["embedding"])
        if embedding1.size == 0 or embedding2.size == 0:
            raise HTTPException(status_code=400, detail="Could not calculate embeddings for the given texts")
        params = {
            "vector_1": embedding1.tolist(),
            "vector_2": embedding2.tolist(),
            "similarity_measure": similarity_measure
        }
        similarity_stats_str = fvs.py_compute_vector_similarity_stats(json.dumps(params))
        similarity_stats_json = json.loads(similarity_stats_str)
        if similarity_measure == 'all':
            similarity_score = similarity_stats_json
        else:
            similarity_score = similarity_stats_json.get(similarity_measure, None)
            if similarity_score is None:
                raise HTTPException(status_code=400, detail="Invalid similarity measure specified")
        response_time = datetime.utcnow()
        total_time = (response_time - request_time).total_seconds()
        logger.info(f"Computed similarity using {similarity_measure} in {total_time} seconds; similarity score: {similarity_score}")
        return {
            "text1": request.text1,
            "text2": request.text2,
            "similarity_measure": similarity_measure,
            "similarity_score": similarity_score,
            "embedding1": embedding1.tolist(),
            "embedding2": embedding2.tolist()
        }
    except Exception as e:
        logger.error(f"An error occurred while processing the request: {e}")
        raise HTTPException(status_code=500, detail="Internal Server Error")



@app.post("/search_stored_embeddings_with_query_string_for_semantic_similarity/",
        response_model=SemanticSearchResponse,
        summary="Get Most Similar Strings from Stored Embedddings in Database",
        description="""Find the most similar strings in the database to the given input "query" text. This endpoint uses a pre-computed FAISS index to quickly search for the closest matching strings.

### Parameters:
- `request`: A JSON object containing the query text, model name, and an optional number of most semantically similar strings to return.
- `req`: HTTP request object (internal use).
- `token`: Security token (optional).

### Request JSON Format:
The request must contain the following attributes:
- `query_text`: The input text for which to find the most similar string.
- `llm_model_name`: The model used to calculate embeddings.
- `number_of_most_similar_strings_to_return`: (Optional) The number of most similar strings to return, defaults to 10.

### Example:
```json
{
  "query_text": "Find me the most similar string!",
  "llm_model_name": "openchat_v3.2_super",
  "number_of_most_similar_strings_to_return": 5
}
```

### Response:
The response will include the most similar strings found in the database, along with the similarity scores.

### Example Response:
```json
{
  "query_text": "Find me the most similar string!",  
  "results": [
    {"search_result_text": "This is the most similar string!", "similarity_to_query_text": 0.9823},
    {"search_result_text": "Another similar string.", "similarity_to_query_text": 0.9721},
    ...
  ]
}
```""",
        response_description="A JSON object containing the query text along with the most similar strings and similarity scores.")
async def search_stored_embeddings_with_query_string_for_semantic_similarity(request: SemanticSearchRequest, req: Request, token: str = None) -> SemanticSearchResponse:
    global faiss_indexes, token_faiss_indexes, associated_texts_by_model
    faiss_indexes, token_faiss_indexes, associated_texts_by_model = await build_faiss_indexes()
    request_time = datetime.utcnow()
    llm_model_name = request.llm_model_name
    num_results = request.number_of_most_similar_strings_to_return
    total_entries = len(associated_texts_by_model[llm_model_name])  # Get the total number of entries for the model
    num_results = min(num_results, total_entries)  # Ensure num_results doesn't exceed the total number of entries
    logger.info(f"Received request to find {num_results} most similar strings for query text: `{request.query_text}` using model: {llm_model_name}")
    if USE_SECURITY_TOKEN and use_hardcoded_security_token and (token is None or token != SECURITY_TOKEN):
        raise HTTPException(status_code=403, detail="Unauthorized")
    try:
        logger.info(f"Computing embedding for input text: {request.query_text}")
        embedding_request = EmbeddingRequest(text=request.query_text, llm_model_name=request.llm_model_name)
        embedding_response = await get_embedding_vector_for_string(embedding_request, req)
        input_embedding = np.array(embedding_response["embedding"]).astype('float32').reshape(1, -1)
        faiss.normalize_L2(input_embedding)  # Normalize the input vector for cosine similarity
        logger.info(f"Computed embedding for input text: {request.query_text}")
        faiss_index = faiss_indexes.get(llm_model_name)  # Retrieve the correct FAISS index for the llm_model_name
        if faiss_index is None:
            raise HTTPException(status_code=400, detail=f"No FAISS index found for model: {llm_model_name}")
        logger.info("Searching for the most similar string in the FAISS index")
        similarities, indices = faiss_index.search(input_embedding.reshape(1, -1), num_results)  # Search for num_results similar strings
        results = []  # Create an empty list to store the results
        for ii in range(num_results):
            similarity = float(similarities[0][ii])  # Convert numpy.float32 to native float
            most_similar_text = associated_texts_by_model[llm_model_name][indices[0][ii]]
            if most_similar_text != request.query_text:  # Don't return the query text as a result
                results.append({"search_result_text": most_similar_text, "similarity_to_query_text": similarity})
        response_time = datetime.utcnow()
        total_time = (response_time - request_time).total_seconds()
        logger.info(f"Finished searching for the most similar string in the FAISS index in {total_time} seconds. Found {len(results)} results, returning the top {num_results}.")
        logger.info(f"Found most similar strings for query string {request.query_text}: {results}")
        return {"query_text": request.query_text, "results": results} # Return the response matching the SemanticSearchResponse model
    except Exception as e:
        logger.error(f"An error occurred while processing the request: {e}")
        logger.error(traceback.format_exc())  # Print the traceback
        raise HTTPException(status_code=500, detail="Internal Server Error")


@app.post("/advanced_search_stored_embeddings_with_query_string_for_semantic_similarity/",
        response_model=AdvancedSemanticSearchResponse,
        summary="Advanced Semantic Search with Two-Step Similarity Measures",
        description="""Perform an advanced semantic search by first using FAISS and cosine similarity to narrow down the most similar strings in the database, and then applying additional similarity measures for finer comparison.

### Parameters:
- `request`: A JSON object containing the query text, model name, an optional similarity filter percentage, and an optional number of most similar strings to return.
- `req`: HTTP request object (internal use).
- `token`: Security token (optional).

### Request JSON Format:
The request must contain the following attributes:
- `query_text`: The input text for which to find the most similar string.
- `llm_model_name`: The model used to calculate embeddings.
- `similarity_filter_percentage`: (Optional) The percentage of embeddings to filter based on cosine similarity, defaults to 0.02 (i.e., top 2%).
- `number_of_most_similar_strings_to_return`: (Optional) The number of most similar strings to return after applying the second similarity measure, defaults to 10.

### Example:
```json
{
  "query_text": "Find me the most similar string!",
  "llm_model_name": "openchat_v3.2_super",
  "similarity_filter_percentage": 0.02,
  "number_of_most_similar_strings_to_return": 5
}
```

### Response:
The response will include the most similar strings found in the database, along with their similarity scores for multiple measures.

### Example Response:
```json
{
  "query_text": "Find me the most similar string!",
  "results": [
    {"search_result_text": "This is the most similar string!", "similarity_to_query_text": {"cosine_similarity": 0.9823, "spearman_rho": 0.8, ... }},
    {"search_result_text": "Another similar string.", "similarity_to_query_text": {"cosine_similarity": 0.9721, "spearman_rho": 0.75, ... }},
    ...
  ]
}
```""",
        response_description="A JSON object containing the query text and the most similar strings, along with their similarity scores for multiple measures.")
async def advanced_search_stored_embeddings_with_query_string_for_semantic_similarity(request: AdvancedSemanticSearchRequest, req: Request, token: str = None) -> AdvancedSemanticSearchResponse:
    global faiss_indexes, token_faiss_indexes, associated_texts_by_model
    faiss_indexes, token_faiss_indexes, associated_texts_by_model = await build_faiss_indexes()
    request_time = datetime.utcnow()
    llm_model_name = request.llm_model_name
    total_entries = len(associated_texts_by_model[llm_model_name])
    num_results = max([1, int((1 - request.similarity_filter_percentage) * total_entries)])
    logger.info(f"Received request to find {num_results} most similar strings for query text: `{request.query_text}` using model: {llm_model_name}")
    if USE_SECURITY_TOKEN and use_hardcoded_security_token and (token is None or token != SECURITY_TOKEN):
        raise HTTPException(status_code=403, detail="Unauthorized")
    try:
        logger.info(f"Computing embedding for input text: {request.query_text}")
        embedding_request = EmbeddingRequest(text=request.query_text, llm_model_name=llm_model_name)
        embedding_response = await get_embedding_vector_for_string(embedding_request, req)
        input_embedding = np.array(embedding_response["embedding"]).astype('float32').reshape(1, -1)
        faiss.normalize_L2(input_embedding)
        logger.info(f"Computed embedding for input text: {request.query_text}")
        faiss_index = faiss_indexes.get(llm_model_name)
        if faiss_index is None:
            raise HTTPException(status_code=400, detail=f"No FAISS index found for model: {llm_model_name}")
        _, indices = faiss_index.search(input_embedding, num_results)
        filtered_indices = indices[0]
        similarity_results = []
        for idx in filtered_indices:
            associated_text = associated_texts_by_model[llm_model_name][idx]
            embedding_request = EmbeddingRequest(text=associated_text, llm_model_name=llm_model_name)
            embedding_response = await get_embedding_vector_for_string(embedding_request, req)
            filtered_embedding = np.array(embedding_response["embedding"])
            params = {
                "vector_1": input_embedding.tolist()[0],
                "vector_2": filtered_embedding.tolist(),
                "similarity_measure": "all"
            }
            similarity_stats_str = fvs.py_compute_vector_similarity_stats(json.dumps(params))
            similarity_stats_json = json.loads(similarity_stats_str)
            similarity_results.append({
                "search_result_text": associated_text,
                "similarity_to_query_text": similarity_stats_json
            })
        num_to_return = request.number_of_most_similar_strings_to_return if request.number_of_most_similar_strings_to_return is not None else len(similarity_results)
        results = sorted(similarity_results, key=lambda x: x["similarity_to_query_text"]["hoeffding_d"], reverse=True)[:num_to_return]
        response_time = datetime.utcnow()
        total_time = (response_time - request_time).total_seconds()
        logger.info(f"Finished advanced search in {total_time} seconds. Found {len(results)} results.")
        return {"query_text": request.query_text, "results": results}
    except Exception as e:
        logger.error(f"An error occurred while processing the request: {e}")
        raise HTTPException(status_code=500, detail="Internal Server Error")

    

@app.post("/get_all_embedding_vectors_for_document/",
        summary="Get Embeddings for a Document",
        description="""Extract text embeddings for a document. This endpoint supports plain text, .doc/.docx (MS Word), PDF files, images (using Tesseract OCR), and many other file types supported by the textract library.

### Parameters:
- `file`: The uploaded document file (either plain text, .doc/.docx, PDF, etc.).
- `llm_model_name`: The model used to calculate embeddings (optional).
- `json_format`: The format of the JSON response (optional, see details below).
- `send_back_json_or_zip_file`: Whether to return a JSON file or a ZIP file containing the embeddings file (optional, defaults to `zip`).
- `token`: Security token (optional).

### JSON Format Options:
The format of the JSON string returned by the endpoint (default is `records`; these are the options supported by the Pandas `to_json()` function):

- `split` : dict like {`index` -> [index], `columns` -> [columns], `data` -> [values]}
- `records` : list like [{column -> value}, … , {column -> value}]
- `index` : dict like {index -> {column -> value}}
- `columns` : dict like {column -> {index -> value}}
- `values` : just the values array
- `table` : dict like {`schema`: {schema}, `data`: {data}}

### Examples:
- Plain Text: Submit a file containing plain text.
- MS Word: Submit a `.doc` or `.docx` file.
- PDF: Submit a `.pdf` file.""",
        response_description="Either a ZIP file containing the embeddings JSON file or a direct JSON response, depending on the value of `send_back_json_or_zip_file`.")
async def get_all_embedding_vectors_for_document(file: UploadFile = File(...),
                                                llm_model_name: str = DEFAULT_MODEL_NAME,
                                                json_format: str = 'records',
                                                token: str = None,
                                                send_back_json_or_zip_file: str = 'zip',
                                                req: Request = None) -> Response:
    client_ip = req.client.host if req else "localhost"
    request_time = datetime.utcnow() 
    if USE_SECURITY_TOKEN and use_hardcoded_security_token and (token is None or token != SECURITY_TOKEN): raise HTTPException(status_code=403, detail="Unauthorized")  # noqa: E701
    _, extension = os.path.splitext(file.filename)
    temp_file = tempfile.NamedTemporaryFile(suffix=extension, delete=False)
    temp_file_path = temp_file.name
    with open(temp_file_path, 'wb') as buffer:
        chunk_size = 1024
        chunk = await file.read(chunk_size)
        while chunk:
            buffer.write(chunk)
            chunk = await file.read(chunk_size)
    hash_obj = sha3_256()
    with open(temp_file_path, 'rb') as buffer:
        for chunk in iter(lambda: buffer.read(chunk_size), b''):
            hash_obj.update(chunk)
    file_hash = hash_obj.hexdigest()
    logger.info(f"SHA3-256 hash of submitted file: {file_hash}")
    async with AsyncSessionLocal() as session: # Check if the document has been processed before
        result = await session.execute(select(DocumentEmbedding).filter(DocumentEmbedding.file_hash == file_hash, DocumentEmbedding.llm_model_name == llm_model_name))
        existing_document_embedding = result.scalar_one_or_none()
        if existing_document_embedding: # If the document has been processed before, return the existing result
            logger.info(f"Document {file.filename} has been processed before, returning existing result")
            json_content = json.dumps(existing_document_embedding.document_embedding_results_json).encode()
        else: # If the document has not been processed, continue processing
            mime = Magic(mime=True)
            mime_type = mime.from_file(temp_file_path)            
            logger.info(f"Received request to extract embeddings for document {file.filename} with MIME type: {mime_type} and size: {os.path.getsize(temp_file_path)} bytes from IP address: {client_ip}")
            strings = await parse_submitted_document_file_into_sentence_strings_func(temp_file_path, mime_type)
            results = await compute_embeddings_for_document(strings, llm_model_name, client_ip, file_hash) # Compute the embeddings and json_content for new documents
            df = pd.DataFrame(results, columns=['text', 'embedding'])
            json_content = df.to_json(orient=json_format or 'records').encode()
            with open(temp_file_path, 'rb') as file_buffer: # Store the results in the database
                original_file_content = file_buffer.read()
            await store_document_embeddings_in_db(file, file_hash, original_file_content, json_content, results, llm_model_name, client_ip, request_time)
    overall_total_time = (datetime.utcnow() - request_time).total_seconds()
    logger.info(f"Done getting all embeddings for document {file.filename} containing {len(strings)} with model {llm_model_name}")
    json_content_length = len(json_content)
    if len(json_content) > 0:
        logger.info(f"The response took {overall_total_time} seconds to generate, or {overall_total_time / (len(strings)/1000.0)} seconds per thousand input tokens and {overall_total_time / (float(json_content_length)/1000000.0)} seconds per million output characters.")
    if send_back_json_or_zip_file == 'json': # Assume 'json' response should be sent back
        logger.info(f"Returning JSON response for document {file.filename} containing {len(strings)} with model {llm_model_name}; first 100 characters out of {json_content_length} total of JSON response: {json_content[:100]}")
        return JSONResponse(content=json.loads(json_content.decode())) # Decode the content and parse it as JSON
    else: # Assume 'zip' file should be sent back
        original_filename_without_extension, _ = os.path.splitext(file.filename)
        json_file_path = f"/tmp/{original_filename_without_extension}.json"
        with open(json_file_path, 'wb') as json_file: # Write the JSON content as bytes
            json_file.write(json_content)
        zip_file_path = f"/tmp/{original_filename_without_extension}.zip"
        with zipfile.ZipFile(zip_file_path, 'w') as zipf:
            zipf.write(json_file_path, os.path.basename(json_file_path))
        logger.info(f"Returning ZIP response for document {file.filename} containing {len(strings)} with model {llm_model_name}; first 100 characters out of {json_content_length} total of JSON response: {json_content[:100]}")
        return FileResponse(zip_file_path, headers={"Content-Disposition": f"attachment; filename={original_filename_without_extension}.zip"})


@app.post("/get_text_completions_from_input_prompt/",
        response_model=List[TextCompletionResponse],
        summary="Generate Text Completions for a Given Input Prompt",
        description="""Generate text completions for a given input prompt string using the specified model.
### Parameters:
- `request`: A JSON object containing the input prompt string (`input_prompt`), the model name, an optional grammar file, an optional number of tokens to generate, and an optional number of completions to generate.
- `token`: Security token (optional).

### Request JSON Format:
The request must contain the following attributes:
- `input_prompt`: The input prompt from which to generate a completion with the LLM model.
- `llm_model_name`: The model used to calculate the embedding (optional, will use the default model if not provided).
- `temperature`: The temperature to use for text generation (optional, defaults to 0.7).
- `grammar_file_string`: The grammar file used to restrict text generation (optional; default is to not use any grammar file). Examples: `json`, `list`)
- `number_of_completions_to_generate`: The number of completions to generate (optional, defaults to 1).
- `number_of_tokens_to_generate`: The number of tokens to generate (optional, defaults to 1000).

### Example (note that `llm_model_name` is optional):
```json
{
  "input_prompt": "The Kings of France in the 17th Century:",
  "llm_model_name": "phind-codellama-34b-python-v1",
  "temperature": 0.95,
  "grammar_file_string": "json",
  "number_of_tokens_to_generate": 500,
  "number_of_completions_to_generate": 3
}
```

### Response:
The response will include the generated text completion, the time taken to compute the generation in seconds, and the request details (input prompt, model name, grammar file, and number of tokens to generate).

### Example Response:
```json
[
  {
    "input_prompt": "The Kings of France in the 17th Century:",
    "llm_model_name": "phind-codellama-34b-python-v1",
    "grammar_file_string": "json",
    "number_of_tokens_to_generate": 500,
    "number_of_completions_to_generate": 3,
    "time_taken_in_seconds": 67.17598033333333,
    "generated_text": "{\"kings\":[\\n    {\\n        \"name\": \"Henry IV\",\\n        \"reign_start\": 1589,\\n        \"reign_end\": 1610\\n    },\\n    {\\n        \"name\": \"Louis XIII\",\\n        \"reign_start\": 1610,\\n        \"reign_end\": 1643\\n    },\\n    {\\n        \"name\": \"Louis XIV\",\\n        \"reign_start\": 1643,\\n        \"reign_end\": 1715\\n    },\\n    {\\n        \"name\": \"Louis XV\",\\n        \"reign_start\": 1715,\\n        \"reign_end\": 1774\\n    },\\n    {\\n        \"name\": \"Louis XVI\",\\n        \"reign_start\": 1774,\\n        \"reign_end\": 1792\\n    }\\n]}",
    "llm_model_usage_json": "{\"prompt_tokens\": 13, \"completion_tokens\": 218, \"total_tokens\": 231}"
  },
  {
    "input_prompt": "The Kings of France in the 17th Century:",
    "llm_model_name": "phind-codellama-34b-python-v1",
    "grammar_file_string": "json",
    "number_of_tokens_to_generate": 500,
    "number_of_completions_to_generate": 3,
    "time_taken_in_seconds": 67.17598033333333,
    "generated_text": "{\"kings\":\\n   [ {\"name\": \"Henry IV\",\\n      \"reignStart\": \"1589\",\\n      \"reignEnd\": \"1610\"},\\n     {\"name\": \"Louis XIII\",\\n      \"reignStart\": \"1610\",\\n      \"reignEnd\": \"1643\"},\\n     {\"name\": \"Louis XIV\",\\n      \"reignStart\": \"1643\",\\n      \"reignEnd\": \"1715\"}\\n   ]}",
    "llm_model_usage_json": "{\"prompt_tokens\": 13, \"completion_tokens\": 115, \"total_tokens\": 128}"
  },
  {
    "input_prompt": "The Kings of France in the 17th Century:",
    "llm_model_name": "phind-codellama-34b-python-v1",
    "grammar_file_string": "json",
    "number_of_tokens_to_generate": 500,
    "number_of_completions_to_generate": 3,
    "time_taken_in_seconds": 67.17598033333333,
    "generated_text": "{\\n\"Henri IV\": \"1589-1610\",\\n\"Louis XIII\": \"1610-1643\",\\n\"Louis XIV\": \"1643-1715\",\\n\"Louis XV\": \"1715-1774\",\\n\"Louis XVI\": \"1774-1792\",\\n\"Louis XVIII\": \"1814-1824\",\\n\"Charles X\": \"1824-1830\",\\n\"Louis XIX (previously known as Charles X): \" \\n    : \"1824-1830\",\\n\"Charles X (previously known as Louis XIX)\": \"1824-1830\"}",
    "llm_model_usage_json": "{\"prompt_tokens\": 13, \"completion_tokens\": 168, \"total_tokens\": 181}"
  }
]
```""", response_description="A JSON object containing the the generated text completion of the input prompt and the request details.")
async def get_text_completions_from_input_prompt(request: TextCompletionRequest, req: Request = None, token: str = None, client_ip: str = None) -> List[TextCompletionResponse]:
    if USE_SECURITY_TOKEN and use_hardcoded_security_token and (token is None or token != SECURITY_TOKEN):
        logger.warning(f"Unauthorized request from client IP {client_ip}")
        raise HTTPException(status_code=403, detail="Unauthorized")
    try:
        return await generate_completion_from_llm(request, req, client_ip)
    except Exception as e:
        logger.error(f"An error occurred while processing the request: {e}")
        logger.error(traceback.format_exc()) # Print the traceback
        raise HTTPException(status_code=500, detail="Internal Server Error")


@app.post("/compute_transcript_with_whisper_from_audio/",
        summary="Transcribe and Embed Audio using Whisper and LLM",
        description="""Transcribe an audio file and optionally compute document embeddings. This endpoint uses the Whisper model for transcription and a specified or default language model for embeddings. The transcription and embeddings are then stored, and a ZIP file containing the embeddings can be downloaded.

### Parameters:
- `file`: The uploaded audio file.
- `compute_embeddings_for_resulting_transcript_document`: Boolean to indicate if document embeddings should be computed (optional, defaults to False).
- `llm_model_name`: The language model used for computing embeddings (optional, defaults to the default model name).
- `req`: HTTP Request object for additional request metadata (optional).

### Examples:
- Audio File: Submit an audio file for transcription.
- Audio File with Embeddings: Submit an audio file and set `compute_embeddings_for_resulting_transcript_document` to True to also get embeddings.""",
        response_description="A JSON object containing the complete transcription details, computational times, and an optional URL for downloading a ZIP file of the document embeddings.")
async def compute_transcript_with_whisper_from_audio(
        file: UploadFile, 
        compute_embeddings_for_resulting_transcript_document: Optional[bool] = True, 
        llm_model_name: Optional[str] = DEFAULT_MODEL_NAME, 
        req: Request = None, 
        token: str = None, 
        client_ip: str = None):
    if USE_SECURITY_TOKEN and use_hardcoded_security_token and (token is None or token != SECURITY_TOKEN):
        logger.warning(f"Unauthorized request from client IP {client_ip}")
        raise HTTPException(status_code=403, detail="Unauthorized")
    try:
        audio_transcript = await get_or_compute_transcript(file, compute_embeddings_for_resulting_transcript_document, llm_model_name, req)
        return audio_transcript
    except Exception as e:
        logger.error(f"An error occurred while processing the request: {e}")
        logger.error(traceback.format_exc())  # Print the traceback
        raise HTTPException(status_code=500, detail="Internal Server Error")


@app.post("/clear_ramdisk/")
async def clear_ramdisk_endpoint(token: str = None):
    if USE_SECURITY_TOKEN and use_hardcoded_security_token and (token is None or token != SECURITY_TOKEN):
        raise HTTPException(status_code=403, detail="Unauthorized")
    if USE_RAMDISK:
        clear_ramdisk()
        return {"message": "RAM Disk cleared successfully."}
    return {"message": "RAM Disk usage is disabled."}


@app.on_event("startup")
async def startup_event():
    global db_writer, faiss_indexes, token_faiss_indexes, associated_texts_by_model
    await initialize_db()
    queue = asyncio.Queue()
    db_writer = DatabaseWriter(queue)
    await db_writer.initialize_processing_hashes()
    asyncio.create_task(db_writer.dedicated_db_writer())    
    global USE_RAMDISK
    if USE_RAMDISK and not check_that_user_has_required_permissions_to_manage_ramdisks():
        USE_RAMDISK = False
    elif USE_RAMDISK:
        setup_ramdisk()    
    list_of_downloaded_model_names = download_models()
    for llm_model_name in list_of_downloaded_model_names:
        try:
            load_model(llm_model_name, raise_http_exception=False)
        except FileNotFoundError as e:
            logger.error(e)
    faiss_indexes, token_faiss_indexes, associated_texts_by_model = await build_faiss_indexes()


@app.get("/download/{file_name}")
async def download_file(file_name: str):
    decoded_file_name = unquote(file_name)
    file_path = os.path.join("generated_transcript_embeddings_zip_files", decoded_file_name)
    absolute_file_path = os.path.abspath(file_path)
    logger.info(f"Trying to fetch file from: {absolute_file_path}")
    if os.path.exists(absolute_file_path):
        with open(absolute_file_path, 'rb') as f:
            logger.info(f"File first 10 bytes: {f.read(10)}")
        return FileResponse(absolute_file_path, media_type="application/zip", filename=decoded_file_name)
    else:
        logger.error(f"File not found at: {absolute_file_path}")
        raise HTTPException(status_code=404, detail="File not found")
    

@app.get("/show_logs_incremental/{minutes}/{last_position}", response_model=ShowLogsIncrementalModel)
def show_logs_incremental(minutes: int, last_position: int):
    return show_logs_incremental_func(minutes, last_position)

@app.get("/show_logs/{minutes}", response_class=HTMLResponse)
def show_logs(minutes: int = 5):
    return show_logs_func(minutes)
        
@app.get("/show_logs", response_class=HTMLResponse)
def show_logs_default():
    return show_logs_func(5)
    
    
if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=LLAMA_EMBEDDING_SERVER_LISTEN_PORT)

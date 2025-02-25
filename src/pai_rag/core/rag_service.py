import asyncio
import os
import traceback
from asgi_correlation_id import correlation_id
from pai_rag.core.models.errors import ServiceError, UserInputError
from pai_rag.core.rag_application import RagApplication
from pai_rag.core.rag_configuration import RagConfiguration
from pai_rag.app.api.models import (
    RagQuery,
    LlmQuery,
    RetrievalQuery,
    LlmResponse,
)
from openinference.instrumentation import using_attributes
from typing import Any, List
import logging

TASK_STATUS_FILE = "__upload_task_status.tmp"
logger = logging.getLogger(__name__)


def trace_correlation_id(function):
    def _trace_correlation_id(*args, **kwargs):
        session_id = correlation_id.get()
        with using_attributes(
            session_id=session_id,
        ):
            return function(*args, **kwargs)

    async def _a_trace_correlation_id(*args, **kwargs):
        session_id = correlation_id.get()
        with using_attributes(
            session_id=session_id,
        ):
            return await function(*args, **kwargs)

    if asyncio.iscoroutinefunction(function):
        return _a_trace_correlation_id
    else:
        return _trace_correlation_id


class RagService:
    def initialize(self, config_file: str):
        self.config_file = config_file
        self.rag_configuration = RagConfiguration.from_file(config_file)
        self.config_dict_value = self.rag_configuration.get_value().to_dict()
        self.config_modified_time = self.rag_configuration.get_config_mtime()

        self.rag_configuration.persist()

        self.rag = RagApplication()
        self.rag.initialize(self.rag_configuration.get_value())

        if os.path.exists(TASK_STATUS_FILE):
            open(TASK_STATUS_FILE, "w").close()

    def get_config(self):
        try:
            self.check_updates()
        except Exception as ex:
            logger.error(traceback.format_exc())
            raise ServiceError(f"Get RAG configuration failed: {ex}")
        return self.config_dict_value

    def reload(self, new_config: Any = None):
        try:
            rag_snapshot = RagConfiguration.from_snapshot()
            if new_config:
                # 多worker模式，读取最新的setting
                rag_snapshot.update(new_config)
            config_snapshot = rag_snapshot.get_value()

            new_dict_value = config_snapshot.to_dict()
            if self.config_dict_value != new_dict_value:
                logger.debug("Config changed, reload")
                self.rag.reload(config_snapshot)
                self.config_dict_value = new_dict_value
                self.rag_configuration = rag_snapshot
                self.rag_configuration.persist()
            else:
                logger.debug("Config not changed, not reload")
        except Exception as ex:
            logger.error(traceback.format_exc())
            raise UserInputError(f"Update RAG configuration failed: {ex}")

    def check_updates(self):
        # Check config changes for multiple worker mode.
        logger.debug("Checking configuration updates")
        new_modified_time = self.rag_configuration.get_config_mtime()
        if self.config_modified_time != new_modified_time:
            self.reload()
            self.config_modified_time = new_modified_time
        else:
            logger.debug("No configuration updates")

    async def add_knowledge_async(
        self,
        task_id: str,
        input_files: List[str],
        filter_pattern: str = None,
        faiss_path: str = None,
        enable_qa_extraction: bool = False,
    ):
        self.check_updates()
        with open(TASK_STATUS_FILE, "a") as f:
            f.write(f"{task_id}\tprocessing\n")
        try:
            await self.rag.aload_knowledge(
                input_files, filter_pattern, faiss_path, enable_qa_extraction
            )
            with open(TASK_STATUS_FILE, "a") as f:
                f.write(f"{task_id}\tcompleted\n")
        except Exception as ex:
            logger.error(
                f"Upload failed: {ex} {str(ex.__cause__)} {traceback.format_exc()}"
            )
            with open(TASK_STATUS_FILE, "a") as f:
                detail = f"{ex}: {str(ex.__cause__)}".replace("\t", " ").replace(
                    "\n", " "
                )
                f.write(f"{task_id}\tfailed\t{detail}\n")
            raise UserInputError(f"Upload knowledge failed: {ex}")

    def get_task_status(self, task_id: str) -> str:
        self.check_updates()
        status = "unknown"
        detail = None
        if not os.path.exists(TASK_STATUS_FILE):
            return status

        lines = open(TASK_STATUS_FILE).readlines()
        for line in lines[::-1]:
            if line.startswith(task_id):
                parts = line.strip().split("\t")
                status = parts[1]
                if len(parts) == 3:
                    detail = parts[2]
                break

        return status, detail

    async def aquery(self, query: RagQuery):
        try:
            self.check_updates()
            return await self.rag.aquery(query)
        except Exception as ex:
            logger.error(traceback.format_exc())
            raise UserInputError(f"Query RAG failed: {ex}")

    async def aquery_llm(self, query: LlmQuery):
        try:
            self.check_updates()
            return await self.rag.aquery_llm(query)
        except Exception as ex:
            logger.error(traceback.format_exc())
            raise UserInputError(f"Query RAG failed: {ex}")

    async def aquery_retrieval(self, query: RetrievalQuery):
        try:
            self.check_updates()
            return await self.rag.aquery_retrieval(query)
        except Exception as ex:
            logger.error(traceback.format_exc())
            raise UserInputError(f"Query RAG failed: {ex}")

    async def aquery_agent(self, query: LlmQuery) -> LlmResponse:
        try:
            self.check_updates()
            return await self.rag.aquery_agent(query)
        except Exception as ex:
            logger.error(traceback.format_exc())
            raise UserInputError(f"Query RAG failed: {ex}")

    async def aload_evaluation_qa_dataset(self, overwrite: bool = False):
        try:
            return await self.rag.aload_evaluation_qa_dataset(overwrite)
        except Exception as ex:
            logger.error(traceback.format_exc())
            raise UserInputError(f"Query RAG failed: {ex}")

    async def aevaluate_retrieval_and_response(
        self, type: str = "all", overwrite: bool = False
    ):
        try:
            return await self.rag.aevaluate_retrieval_and_response(type, overwrite)
        except Exception as ex:
            logger.error(traceback.format_exc())
            raise UserInputError(f"Query RAG failed: {ex}")


rag_service = RagService()

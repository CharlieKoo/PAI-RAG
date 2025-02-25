import datetime
import json
import os
from typing import Any, Dict, List
from fastapi.concurrency import run_in_threadpool
from llama_index.core import Settings
from llama_index.core.schema import TextNode
from llama_index.llms.huggingface import HuggingFaceLLM

from pai_rag.integrations.nodeparsers.base import MarkdownNodeParser
from pai_rag.integrations.extractors.html_qa_extractor import HtmlQAExtractor
from pai_rag.integrations.extractors.text_qa_extractor import TextQAExtractor
from pai_rag.modules.nodeparser.node_parser import node_id_hash
from pai_rag.data.open_dataset import MiraclOpenDataSet


import logging

logger = logging.getLogger(__name__)

DEFAULT_LOCAL_QA_MODEL_PATH = "./model_repository/qwen_1.8b"

DOC_TYPES_DO_NOT_NEED_CHUNKING = set([".csv", ".xlsx", ".xls", ".htm", ".html"])


class RagDataLoader:
    """
    RagDataLoader:
    Load data with corresponding data readers according to config.
    """

    def __init__(
        self,
        datareader_factory,
        node_parser,
        index,
        bm25_index,
        oss_cache,
        use_local_qa_model=False,
    ):
        self.datareader_factory = datareader_factory
        self.node_parser = node_parser
        self.oss_cache = oss_cache
        self.index = index
        self.bm25_index = bm25_index

        if use_local_qa_model:
            # API暂不支持此选项
            self.qa_llm = HuggingFaceLLM(
                model_name=DEFAULT_LOCAL_QA_MODEL_PATH,
                tokenizer_name=DEFAULT_LOCAL_QA_MODEL_PATH,
            )
        else:
            self.qa_llm = Settings.llm

        html_extractor = HtmlQAExtractor(llm=self.qa_llm)
        txt_extractor = TextQAExtractor(llm=self.qa_llm)

        self.extractors = [html_extractor, txt_extractor]

        logger.info("RagDataLoader initialized.")

    def _extract_file_type(self, metadata: Dict[str, Any]):
        file_name = metadata.get("file_name", "dummy.txt")
        return os.path.splitext(file_name)[1]

    def _get_nodes(
        self,
        file_path: str | List[str],
        filter_pattern: str,
        enable_qa_extraction: bool,
    ):
        filter_pattern = filter_pattern or "*"
        if isinstance(file_path, list):
            input_files = [f for f in file_path if os.path.isfile(f)]
        elif isinstance(file_path, str) and os.path.isdir(file_path):
            import pathlib

            directory = pathlib.Path(file_path)
            input_files = [
                f for f in directory.rglob(filter_pattern) if os.path.isfile(f)
            ]
        else:
            input_files = [file_path]

        if len(input_files) == 0:
            return

        data_reader = self.datareader_factory.get_reader(input_files)
        docs = data_reader.load_data()
        logger.info(f"[DataReader] Loaded {len(docs)} docs.")

        nodes = []

        doc_cnt_map = {}
        for doc in docs:
            doc_type = self._extract_file_type(doc.metadata)

            if doc_type in DOC_TYPES_DO_NOT_NEED_CHUNKING:
                doc_key = f"""{doc.metadata.get("file_path", "dummy")}"""
                if doc_key not in doc_cnt_map:
                    doc_cnt_map[doc_key] = 0
                doc_cnt_map[doc_key] += 1
                node_id = node_id_hash(doc_cnt_map[doc_key], doc)
                nodes.append(
                    TextNode(id_=node_id, text=doc.text, metadata=doc.metadata)
                )
            elif doc_type == ".md":
                md_node_parser = MarkdownNodeParser(id_func=node_id_hash)
                nodes.extend(md_node_parser.get_nodes_from_documents([doc]))
            else:
                nodes.extend(self.node_parser.get_nodes_from_documents([doc]))

        logger.info(f"[DataReader] Split into {len(nodes)} nodes.")

        # QA metadata extraction
        if enable_qa_extraction:
            qa_nodes = []

            for extractor in self.extractors:
                metadata_list = extractor.extract(nodes)
                for i, node in enumerate(nodes):
                    qa_extraction_result = metadata_list[i].get(
                        "qa_extraction_result", {}
                    )
                    q_cnt = 0
                    metadata = node.metadata
                    for q, a in qa_extraction_result.items():
                        metadata["answer"] = a
                        qa_nodes.append(
                            TextNode(
                                id_=f"{node.id_}_{q_cnt}", text=q, metadata=metadata
                            )
                        )
                        q_cnt += 1
            for node in qa_nodes:
                node.excluded_embed_metadata_keys.append("answer")
                node.excluded_llm_metadata_keys.append("question")
            nodes.extend(qa_nodes)

        return nodes

    def load(
        self,
        file_path: str | List[str],
        filter_pattern: str,
        enable_qa_extraction: bool,
    ):
        nodes = self._get_nodes(file_path, filter_pattern, enable_qa_extraction)

        if not nodes:
            logger.info("[DataReader] could not find files")
            return

        logger.info("[DataReader] Start inserting to index.")
        self.index.vector_index.insert_nodes(nodes)
        self.index.vector_index.storage_context.persist(
            persist_dir=self.index.persist_path
        )

        index_metadata_file = os.path.join(self.index.persist_path, "index.metadata")
        if self.bm25_index:
            self.bm25_index.add_docs(nodes)
            metadata_str = json.dumps({"lastUpdated": f"{datetime.datetime.now()}"})
            with open(index_metadata_file, "w") as wf:
                wf.write(metadata_str)

        logger.info(f"Inserted {len(nodes)} nodes successfully.")
        return

    async def aload(
        self,
        file_path: str | List[str],
        filter_pattern: str,
        enable_qa_extraction: bool,
    ):
        nodes = await run_in_threadpool(
            lambda: self._get_nodes(file_path, filter_pattern, enable_qa_extraction)
        )
        if not nodes:
            logger.info("[DataReader] could not find files")
            return

        logger.info("[DataReader] Start inserting to index.")

        await self.index.vector_index.insert_nodes_async(nodes)
        self.index.vector_index.storage_context.persist(
            persist_dir=self.index.persist_path
        )

        index_metadata_file = os.path.join(self.index.persist_path, "index.metadata")
        if self.bm25_index:
            await run_in_threadpool(lambda: self.bm25_index.add_docs(nodes))
            metadata_str = json.dumps({"lastUpdated": f"{datetime.datetime.now()}"})
            with open(index_metadata_file, "w") as wf:
                wf.write(metadata_str)

        logger.info(f"Inserted {len(nodes)} nodes successfully.")
        return

    async def aload_eval_data(self, name: str):
        logger.info("[DataReader-Evaluation Dataset]")
        if name == "miracl":
            miracl_dataset = MiraclOpenDataSet()
            miracl_nodes, _ = miracl_dataset.load_related_corpus()
            nodes = []
            for node in miracl_nodes:
                node_metadata = {
                    "title": node[2],
                    "file_path": node[3],
                    "file_name": node[3],
                }
                nodes.append(
                    TextNode(id_=node[0], text=node[1], metadata=node_metadata)
                )

            print(f"[DataReader-Evaluation Dataset] Split into {len(nodes)} nodes.")

            print("[DataReader-Evaluation Dataset] Start inserting to index.")

            await self.index.vector_index.insert_nodes_async(nodes)
            self.index.vector_index.storage_context.persist(
                persist_dir=self.index.persist_path
            )

            index_metadata_file = os.path.join(
                self.index.persist_path, "index.metadata"
            )
            if self.bm25_index:
                await run_in_threadpool(lambda: self.bm25_index.add_docs(nodes))
                metadata_str = json.dumps({"lastUpdated": f"{datetime.datetime.now()}"})
                with open(index_metadata_file, "w") as wf:
                    wf.write(metadata_str)

            print(
                f"[DataReader-Evaluation Dataset] Inserted {len(nodes)} nodes successfully."
            )
            return
        else:
            raise ValueError(f"Not supported eval dataset name with {name}")

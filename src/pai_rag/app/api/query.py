from typing import Any, List
from fastapi import APIRouter, Body, BackgroundTasks, UploadFile, Form
import uuid
import hashlib
import os
import tempfile
from pai_rag.core.rag_service import rag_service
from pai_rag.app.api.models import (
    RagQuery,
    LlmQuery,
    RetrievalQuery,
    LlmResponse,
)
from fastapi.responses import StreamingResponse

router = APIRouter()


async def event_generator_async(response):
    delimiter = "\0"
    async for token in response.async_response_gen():
        yield token + delimiter


@router.post("/query")
async def aquery(query: RagQuery):
    response = await rag_service.aquery(query)
    if not query.stream:
        return response
    else:
        return StreamingResponse(
            event_generator_async(response[0]),
            headers={"x-session-id": response[1], "reference_docs": response[2]},
            media_type="text/plain",
        )


@router.post("/query/llm")
async def aquery_llm(query: LlmQuery):
    response = await rag_service.aquery_llm(query)
    if not query.stream:
        return response
    else:
        return StreamingResponse(
            event_generator_async(response[0]),
            headers={"x-session-id": response[1]},
            media_type="text/plain",
        )


@router.post("/query/retrieval")
async def aquery_retrieval(query: RetrievalQuery):
    return await rag_service.aquery_retrieval(query)


@router.post("/query/agent")
async def aquery_agent(query: LlmQuery) -> LlmResponse:
    return await rag_service.aquery_agent(query)


@router.patch("/config")
async def aupdate(new_config: Any = Body(None)):
    rag_service.reload(new_config)
    return {"msg": "Update RAG configuration successfully."}


@router.get("/config")
async def aconfig():
    return rag_service.get_config()


@router.get("/get_upload_state")
def task_status(task_id: str):
    status, detail = rag_service.get_task_status(task_id)
    return {"task_id": task_id, "status": status, "detail": detail}


@router.post("/evaluate")
async def batch_evaluate(overwrite: bool = False):
    df, eval_results = await rag_service.aevaluate_retrieval_and_response(
        type="all", overwrite=overwrite
    )
    return {"status": 200, "result": eval_results}


@router.post("/evaluate/retrieval")
async def batch_retrieval_evaluate(overwrite: bool = False):
    df, eval_results = await rag_service.aevaluate_retrieval_and_response(
        type="retrieval", overwrite=overwrite
    )
    return {"status": 200, "result": eval_results}


@router.post("/evaluate/response")
async def batch_response_evaluate(overwrite: bool = False):
    df, eval_results = await rag_service.aevaluate_retrieval_and_response(
        type="response", overwrite=overwrite
    )
    return {"status": 200, "result": eval_results}


@router.post("/evaluate/generate")
async def generate_qa_dataset(overwrite: bool = False):
    qa_datase = await rag_service.aload_evaluation_qa_dataset(overwrite)
    return {"status": 200, "result": qa_datase}


@router.post("/upload_data")
async def upload_data(
    files: List[UploadFile],
    faiss_path: str = Form(None),
    background_tasks: BackgroundTasks = BackgroundTasks(),
):
    task_id = uuid.uuid4().hex
    if not files:
        return {"message": "No upload file sent"}

    tmpdir = tempfile.mkdtemp()
    input_files = []
    for file in files:
        fn = file.filename
        data = await file.read()
        file_hash = hashlib.md5(data).hexdigest()
        save_file = os.path.join(tmpdir, f"{file_hash}_{fn}")

        with open(save_file, "wb") as f:
            f.write(data)
            f.close()
        input_files.append(save_file)

    background_tasks.add_task(
        rag_service.add_knowledge_async,
        task_id=task_id,
        input_files=input_files,
        filter_pattern=None,
        faiss_path=faiss_path,
        enable_qa_extraction=False,
    )

    return {"task_id": task_id}

"""知识库文档与向量索引 API。"""

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile

from app.schemas import (
    KnowledgeDeleteResponse,
    KnowledgeUploadResponse,
    KnowledgeWorkspaceResponse,
    VectorRagRebuildResponse,
)
from app.services.errors import AgentConfigError, AgentRuntimeError
from app.services.knowledge import KnowledgeDocumentError, knowledge_service
from app.services.auth import require_admin


router = APIRouter(prefix="/api/v1/knowledge", tags=["knowledge"])


@router.get("", response_model=KnowledgeWorkspaceResponse)
async def get_knowledge_workspace():
    """查看源文档和向量索引是否可用。"""

    return knowledge_service.status()


@router.post(
    "/documents",
    response_model=KnowledgeUploadResponse,
    status_code=201,
)
async def upload_knowledge_document(
    file: UploadFile = File(...),
    _=Depends(require_admin),
):
    """上传 Markdown、文本或 PDF；上传后由用户显式触发索引重建。"""

    try:
        content = await file.read(knowledge_service.max_upload_bytes + 1)
        document = knowledge_service.save(
            filename=file.filename or "",
            content=content,
        )
    except KnowledgeDocumentError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        await file.close()

    return KnowledgeUploadResponse(
        ok=True,
        document=document,
        message="文档已上传；重建索引后即可被 Agent 检索。",
    )


@router.delete(
    "/documents/{document_name}",
    response_model=KnowledgeDeleteResponse,
)
async def delete_knowledge_document(
    document_name: str,
    _=Depends(require_admin),
):
    """删除一个源文档，随后应重建索引以移除旧向量。"""

    try:
        knowledge_service.delete(filename=document_name)
    except KnowledgeDocumentError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return KnowledgeDeleteResponse(
        ok=True,
        document_name=document_name,
        message="文档已删除；请重建索引以同步向量库。",
    )


@router.post("/rebuild", response_model=VectorRagRebuildResponse)
async def rebuild_knowledge_index(_=Depends(require_admin)):
    """调用真实 embedding 模型并原子重建 Chroma collection。"""

    try:
        return knowledge_service.rebuild()
    except AgentConfigError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except AgentRuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

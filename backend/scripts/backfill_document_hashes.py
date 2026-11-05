"""回填 documents.content_sha256（默认 dry-run，需显式 --apply）。

规则：
* 只读取 ``stored_path`` 位于可信上传根（settings.UPLOAD_DIR）内、且是普通文件的文档；
  其他行报告为不可信/缺失，绝不读取或修改。
* 同一知识库内相同摘要只给一个 canonical 填 hash，其余保持 NULL：
  已有 hash 的行优先作为 canonical；否则取最早创建（created_at, id）的那行。
* 不删除文档、不合并引用、不删除向量；重复组与缺文件都会如实报告。
* 默认 dry-run，不写库；``--apply`` 才写。并发唯一冲突时回滚该组并重新读取 canonical。

用法::

    backend/.venv/Scripts/python.exe scripts/backfill_document_hashes.py
    backend/.venv/Scripts/python.exe scripts/backfill_document_hashes.py --apply
"""
import argparse
import sys
from pathlib import Path

# 允许从 backend 目录或仓库根直接运行
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.exc import IntegrityError  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.models import Document  # noqa: E402
from app.services.document_identity import file_sha256, is_trusted_upload_file  # noqa: E402


def _report(apply: bool, message: str) -> None:
    print(("[apply] " if apply else "[dry-run] ") + message)


def backfill(apply: bool) -> int:
    """返回非零表示存在需要人工关注的问题（冲突/缺文件/不可信路径）。

    已有 hash 也必须与磁盘真实摘要核对：服务端摘要的权威来源是文件字节，
    不能因为数据库里写了值就宣布成功。
    """
    root = Path(settings.UPLOAD_DIR)
    problems = 0
    scanned = updated = duplicate_groups = duplicate_rows = conflict_groups = 0

    with SessionLocal() as db:
        docs = db.scalars(select(Document).order_by(Document.kb_id, Document.created_at, Document.id)).all()
        scanned = len(docs)

        groups: dict[tuple, list[tuple[Document, str]]] = {}
        untrusted: list[Document] = []
        missing: list[Document] = []
        for doc in docs:
            if not is_trusted_upload_file(doc.stored_path, root):
                untrusted.append(doc)
                continue
            try:
                actual = file_sha256(Path(doc.stored_path))
            except OSError:
                missing.append(doc)
                continue
            if doc.content_sha256 and doc.content_sha256.lower() != actual:
                # 已有 hash 与真实文件不符：不覆盖、不参与分组，交由人工确认。
                conflict_groups += 1
                problems += 1
                _report(
                    apply,
                    f"摘要冲突：文档 {doc.id}（知识库 {doc.kb_id}）库存 {doc.content_sha256[:12]}… "
                    f"与文件实际 {actual[:12]}… 不一致，保持原值并跳过",
                )
                continue
            groups.setdefault((doc.kb_id, actual), []).append((doc, actual))

        for (kb_id, digest), members in groups.items():
            existing = [doc for doc, _ in members if doc.content_sha256]
            if len(existing) > 1:
                # 唯一索引存在时不可能出现；索引缺失的历史数据不要在此自动收敛。
                conflict_groups += 1
                problems += 1
                _report(
                    apply,
                    f"重复摘要：知识库 {kb_id} 摘要 {digest[:12]}… 已有 {len(existing)} 行非 NULL，"
                    f"整组保持原样，请人工确认 canonical",
                )
                continue
            canonical = existing[0] if existing else members[0][0]
            if len(members) > 1:
                duplicate_groups += 1
                duplicate_rows += len(members) - 1
                others = [doc for doc, _ in members if doc is not canonical]
                _report(
                    apply,
                    f"重复组：知识库 {kb_id} 摘要 {digest[:12]}… 有 {len(members)} 行；"
                    f"canonical={canonical.id}，其余 {len(others)} 行保持 NULL（未删除、未合并）",
                )
            if canonical.content_sha256 is None:
                if apply:
                    canonical.content_sha256 = digest
                    try:
                        db.commit()
                        updated += 1
                    except IntegrityError:
                        db.rollback()
                        # 并发下另一行先写入同一摘要：本组保持 NULL，绝不覆盖胜者。
                        conflict_groups += 1
                        problems += 1
                        _report(
                            apply,
                            f"并发冲突：知识库 {kb_id} 摘要 {digest[:12]}… 已由另一行占用，本组保持 NULL",
                        )
                        continue
                else:
                    updated += 1

    for doc in untrusted:
        _report(apply, f"不可信路径：文档 {doc.id} stored_path 不在上传根内或是非普通文件，已跳过")
        problems += 1
    for doc in missing:
        _report(apply, f"缺文件：文档 {doc.id} 的 stored_path 无法读取，已跳过")
        problems += 1

    _report(
        apply,
        f"扫描 {scanned} 个文档；{'写入' if apply else '将写入'} {updated} 个 canonical；"
        f"重复组 {duplicate_groups}（未填 hash 行 {duplicate_rows}）；"
        f"冲突组 {conflict_groups}；问题 {problems}",
    )
    return 1 if problems else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="回填文档 content_sha256（默认 dry-run）")
    parser.add_argument("--apply", action="store_true", help="实际写库；省略则只报告")
    args = parser.parse_args()
    return backfill(args.apply)


if __name__ == "__main__":
    raise SystemExit(main())

"""业务结果人工核验 CLI：仅本地文件，不创建 HTTP 客户端，不访问模型或远端来源。

两种模式：
  --create-template PATH  从报告生成空核验模板（判断全部为 null，不自动通过）
  --reviews PATH          读取人工核验，与报告逐 case 绑定并输出无正文摘要

退出码：0 reviewed_pass；1 失败或机器断言失败；2 输入错误；4 报告/核验绑定不可比；
5 缺核验、null 判定或未决项。人工核验不是自动语义证明，也不代表真实业务验收通过。
"""
import argparse
import json
import os
import tempfile
from pathlib import Path

from app.services.evaluation import EvaluationError
from app.services.evaluation_reviews import (ReviewError, create_template, evaluate_reviews,
                                             load_json_file, output_collision)


def _preflight(outputs, inputs, *, overwrite):
    """写入前一次性检查输出冲突与覆盖，避免只写出一半文件。"""
    for index, path in enumerate(outputs):
        other_outputs = outputs[:index] + outputs[index + 1:]
        output_collision(path, [*inputs, *other_outputs])
        if Path(path).exists() and not overwrite:
            raise ReviewError("output_exists", 2)


def _write_json(path, payload, *, overwrite, inputs):
    """安全写 JSON：不覆盖输入；无覆盖时独占创建，有覆盖时原子替换。

    临时文件与目标同目录，写完后再发布。这样即使检查后有新文件出现，
    也不会被静默截断；硬链接/符号链接指向输入的输出在发布前仍会被拒绝。
    """
    output_collision(path, inputs)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        data = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError):
        raise ReviewError("json_invalid", 2) from None
    temp_name = None
    fd = None
    try:
        fd, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp",
                                         dir=str(target.parent))
        with os.fdopen(fd, "wb") as stream:
            fd = None
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        temp_path = Path(temp_name)
        if overwrite:
            os.replace(temp_path, target)
        else:
            try:
                # 硬链接发布在目标不存在时原子成功；目标已出现则失败，不会覆盖。
                os.link(temp_path, target)
            except FileExistsError:
                raise ReviewError("output_exists", 2) from None
            os.unlink(temp_path)
        temp_name = None
    except ReviewError:
        raise
    except OSError as exc:
        raise ReviewError("io_error", 2) from exc
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if temp_name is not None:
            try:
                os.unlink(temp_name)
            except OSError:
                pass


def _create(args):
    report = load_json_file(args.report)
    template = create_template(report, include_content=args.template_include_content)
    outputs = [args.create_template] + ([args.output] if args.output else [])
    _preflight(outputs, [args.report], overwrite=args.overwrite)
    _write_json(args.create_template, template, overwrite=args.overwrite,
                inputs=[args.report, args.output])
    if args.output:
        _write_json(args.output, {"schema": template["schema"], "run_id": template["run_id"],
                                  "identity_sha256": template["identity_sha256"],
                                  "mode": template["mode"], "cases": len(template["reviews"]),
                                  "review_status": "not_reviewed"},
                    overwrite=args.overwrite, inputs=[args.report, args.create_template])
    print(f"核验模板已生成: cases={len(template['reviews'])}; mode={template['mode']}; "
          f"run_id={template['run_id']}; 所有判断均为 null，尚未人工核验，不构成通过。")
    return 0


def _review(args):
    report = load_json_file(args.report)
    reviews = load_json_file(args.reviews)
    summary = evaluate_reviews(report, reviews)
    inputs = [args.report, args.reviews]
    outputs = [args.output] if args.output else []
    _preflight(outputs, inputs, overwrite=args.overwrite)
    if args.output:
        _write_json(args.output, summary, overwrite=args.overwrite, inputs=inputs)
    human = summary["human_review"]
    print(f"人工核验结论: {summary['review_status']}; exit={summary['exit_code']}; mode={summary['mode']}; "
          f"verified={human['cases_verified']}; failed={human['cases_failed']}; "
          f"unverified={human['cases_unverified']}; missing={human['cases_without_review']}; "
          f"machine_failed={summary['machine_assertions']['failed']}")
    print("人工宣告不是自动语义证明；本轮未运行真实业务或生产验收。")
    return summary["exit_code"]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", required=True, help="evaluate_rag 生成的本地报告 JSON")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--create-template", help="生成本地空核验模板 JSON 的目标路径")
    mode.add_argument("--reviews", help="人工核验 JSON 文件")
    parser.add_argument("--output", help="可选：核验摘要输出路径")
    parser.add_argument("--overwrite", action="store_true", help="允许覆盖已存在的输出文件")
    parser.add_argument("--template-include-content", action="store_true",
                        help="仅与 --create-template 使用：把报告中已有的 --include-content 正文附入模板")
    args = parser.parse_args(argv)
    if args.template_include_content and not args.create_template:
        parser.error("--template-include-content 只能与 --create-template 使用")
    try:
        if args.create_template:
            return _create(args)
        return _review(args)
    except ReviewError as exc:
        print("核验未完成:", exc.reason)
        return exc.exit_code
    except EvaluationError:
        # 不回显报告正文、核验人或文件内敏感字段。
        print("核验未完成: evaluation_error")
        return 2
    except OSError:
        print("核验未完成: io_error")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

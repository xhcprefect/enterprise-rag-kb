"""统一响应结构的辅助函数。

接口一律声明 ``response_model=ApiResponse[X]``（见 ``app/schemas/common.py``），
成功体由 FastAPI 按该模型序列化，OpenAPI 文档里展示的就是真实返回结构。
这里负责生成错误体与整理参数校验提示，由 ``app/main.py`` 的全局异常处理器调用。
"""
from typing import Any

# pydantic 的错误类型 -> 中文模板，避免把英文原文弹给用户
_VALIDATION_TEMPLATES: dict[str, str] = {
    "missing": "缺少必填项",
    "string_too_short": "长度不能少于 {min_length} 个字符",
    "string_too_long": "长度不能超过 {max_length} 个字符",
    "string_pattern_mismatch": "格式不正确",
    "string_type": "应为字符串",
    "int_parsing": "应为整数",
    "int_type": "应为整数",
    "float_parsing": "应为数字",
    "bool_parsing": "应为布尔值（true/false）",
    "bool_type": "应为布尔值（true/false）",
    "uuid_parsing": "应为合法的 UUID",
    "datetime_parsing": "应为合法的时间格式",
    "json_invalid": "请求体不是合法的 JSON",
    "greater_than": "必须大于 {gt}",
    "greater_than_equal": "不能小于 {ge}",
    "less_than": "必须小于 {lt}",
    "less_than_equal": "不能大于 {le}",
    "literal_error": "取值不合法",
    "list_type": "应为数组",
    "dict_type": "应为对象",
}


def error_body(status_code: int, message: str, data: Any = None) -> dict[str, Any]:
    """``code`` 与 HTTP 状态码保持一致，前端只认 ``success`` 即可判断成败。"""
    return {
        "code": status_code,
        "success": False,
        "message": message,
        "data": data,
    }


def _field_name(loc: tuple | list) -> str:
    """``("body", "user", "name")`` -> ``user.name``；去掉 body/query 等来源前缀。"""
    parts = [
        str(x) for x in loc
        if x not in ("body", "query", "path", "header", "cookie")
    ]
    return ".".join(parts) or "请求参数"


def _describe(err: dict) -> str:
    err_type = err.get("type", "")
    ctx = err.get("ctx") or {}
    template = _VALIDATION_TEMPLATES.get(err_type)
    if template:
        try:
            return template.format(**ctx)
        except (KeyError, IndexError):
            return template
    # value_error 等的 msg 以 "Value error, " 开头，去掉这段英文前缀
    msg = str(err.get("msg", "格式不正确"))
    return msg.removeprefix("Value error, ")


def sanitized_validation_errors(errors: list[dict]) -> list[dict]:
    """只保留定位错误所需的字段；input/msg/任意 ctx 均可能夹带原始凭据。"""
    safe = []
    context_keys = {"min_length", "max_length", "gt", "ge", "lt", "le"}
    for error in errors:
        item = {"type": error.get("type", "value_error"), "loc": error.get("loc", [])}
        ctx = error.get("ctx") or {}
        item["ctx"] = {
            key: value for key, value in ctx.items()
            if key in context_keys and isinstance(value, (int, float))
        }
        item["msg"] = _describe(item)
        safe.append(item)
    return safe


def validation_message(errors: list[dict]) -> str:
    """把 pydantic 的字段错误压成一句中文，前端可直接展示。"""
    parts = [f"{_field_name(err.get('loc', []))}: {_describe(err)}" for err in errors]
    return ("参数校验失败 —— " + "；".join(parts)) if parts else "参数校验失败"

"""Identifiers used to make review runs stable and replayable."""

import re


_SHA_PATTERN = re.compile(r"^[0-9a-fA-F]{40,64}$")


def normalize_sha(value: str) -> str:
    """把提交 SHA 规范化成可比较、可存储的统一形式。

    调用方可以传入前后带空格、大小写混合的字符串；这里会先清理空格并转成
    小写，然后验证它确实是 40 到 64 位的十六进制字符串。验证失败时抛出
    ``ValueError``，这样非法标识符不会继续进入数据库或任务队列。

    参数：
        value: Git 提交或对象的完整 SHA 文本，可以带首尾空白并混用大小写。

    返回：
        去除首尾空白、统一为小写后的 SHA。返回值长度仍为 40 到 64 位。

    异常：
        ValueError: 清理后的值长度不合法，或包含十六进制字符以外的内容。

    该函数不访问 Git 仓库，也不判断这个 SHA 是否真实存在；它只负责格式规范化。
    """

    normalized = value.strip().lower()
    if not _SHA_PATTERN.fullmatch(normalized):
        raise ValueError("SHA must contain 40 to 64 hexadecimal characters")
    return normalized


def build_review_version_key(
    repository_id: int,
    pull_request_number: int,
    head_sha: str,
) -> str:
    """生成一个 Pull Request 提交版本的稳定身份键。

    身份键由稳定的仓库数字 ID、PR 编号和规范化后的 head SHA 组成。它用于
    区分“同一个 PR 的不同提交”，因此不能使用展示用的仓库名称替代数字 ID。
    两个数值参数必须为正数；SHA 的详细格式校验委托给 :func:`normalize_sha`。

    参数：
        repository_id: GitHub 分配的稳定仓库数字 ID，必须大于零。
        pull_request_number: PR 在该仓库内的编号，必须大于零。
        head_sha: PR 当前目标提交的完整 SHA；函数会负责清理和转为小写。

    返回：
        ``<repository_id>:<pull_request_number>:<head_sha>`` 形式的字符串。

    异常：
        ValueError: 数字 ID 非正数，或者 ``head_sha`` 不符合完整 SHA 格式。

    同一仓库、PR 和提交总会得到同一个键；同一 PR 推送新提交后会得到新键。
    """

    if repository_id <= 0:
        raise ValueError("repository_id must be positive")
    if pull_request_number <= 0:
        raise ValueError("pull_request_number must be positive")
    return f"{repository_id}:{pull_request_number}:{normalize_sha(head_sha)}"


def build_thread_id(
    repository_id: int,
    pull_request_number: int,
    head_sha: str,
    review_run_id: str,
) -> str:
    """生成一次具体审查运行的线程身份。

    一个提交版本可以被手工重新审查多次，所以仅使用
    ``review_version_key`` 不足以区分执行线程；这里在版本键后追加运行 ID。
    运行 ID 去掉首尾空格后不得为空，版本键本身仍由统一的 SHA 校验逻辑生成。

    参数：
        repository_id: GitHub 仓库数字 ID。
        pull_request_number: PR 编号。
        head_sha: 本次审查绑定的 head SHA。
        review_run_id: 一次审查运行的唯一 ID；允许带首尾空白，但不能为空。

    返回：
        ``<review_version_key>:<review_run_id>`` 形式的线程 ID。它可以区分同一提交
        上由不同幂等键显式触发的多次审查。

    异常：
        ValueError: 运行 ID 为空，或者版本键中的数字/SHA 参数无效。
    """

    run_id = review_run_id.strip()
    if not run_id:
        raise ValueError("review_run_id must not be empty")
    return f"{build_review_version_key(repository_id, pull_request_number, head_sha)}:{run_id}"

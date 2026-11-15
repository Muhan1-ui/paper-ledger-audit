"""防止调度脚本与 CLI 默认值漂移的测试。

现实背景：``cli.py`` 的 ``--max-hours-ahead`` 默认值从 6.0 改成 24.0 后，
``work/run-tai-cai-task.ps1`` 仍然硬编码 ``--max-hours-ahead 6``。命令行显式值
会覆盖 argparse 默认值，所以单元测试全绿、实际运行却没有变化。

本模块把 ps1 中每一处 ``& $Python -m outputs.tai_cai_tracker.cli <子命令> ...``
调用解析出来，再与 ``cli._build_parser()`` 中该子命令的动态默认值逐一比对。
参数清单不写死，全部从 parser 反射取得。
"""

import argparse
import re
import shlex
import unittest
from dataclasses import dataclass
from pathlib import Path

from outputs.tai_cai_tracker import cli

CLI_MODULE_TOKEN = "outputs.tai_cai_tracker.cli"
SCRIPT_PATH = Path(__file__).resolve().parents[1] / "run-tai-cai-task.ps1"

# 只匹配模块调用（-m outputs.tai_cai_tracker.cli），不会误伤
# outputs.tai_cai_tracker.paper_bet 这样的同前缀模块。
_CALL_RE = re.compile(r"-m\s+" + re.escape(CLI_MODULE_TOKEN) + r"(?![\w.])(.*)$")

# PowerShell 简单赋值：$Var = "字面量" 或 $Var = 数字。
# 形如 Join-Path / (Get-Command ...) 的表达式刻意不解释，交给调用方报“未赋值”。
_ASSIGN_RE = re.compile(r"^\s*\$([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.+?)\s*$")
_LITERAL_RE = re.compile(r"""^(?:"([^"]*)"|'([^']*)'|(-?\d+(?:\.\d+)?))$""")
_VARIABLE_RE = re.compile(r"^\$([A-Za-z_][A-Za-z0-9_]*)$")

# PowerShell 重定向（2>&1、>、>>、*> 等）不是参数值。
_REDIRECTION_RE = re.compile(r"^(?:\d+|\*)?(?:>>?|>&)\S*$")


class ScriptParseError(ValueError):
    """脚本无法被可靠解析时抛出，绝不静默跳过。"""


@dataclass(frozen=True)
class OptionAssignment:
    name: str
    value: object
    line: int


@dataclass(frozen=True)
class CliInvocation:
    subcommand: str
    options: tuple
    line: int


def _logical_lines(script_text):
    """按物理行读取，并把行尾反引号（`）续行拼成一条逻辑行。

    当前仓库的 run-tai-cai-task.ps1 里 cli 调用都是单行的；这里保留续行处理，
    以免以后拆分长命令行时漏掉后续行里的参数。
    """
    pending = ""
    start = None
    for number, raw in enumerate(script_text.splitlines(), start=1):
        if start is None:
            start = number
        line = raw.rstrip()
        if line.endswith("`"):
            pending += line[:-1] + " "
            continue
        yield start, pending + line
        pending = ""
        start = None
    if pending:
        yield start, pending


def parse_variable_assignments(script_text):
    """提取脚本中的简单字面量赋值，返回 {变量名: 值字符串}。

    后出现的赋值覆盖先出现的赋值，与 PowerShell 的执行顺序一致。
    """
    assignments = {}
    for _, line in _logical_lines(script_text):
        if line.lstrip().startswith("#"):
            continue
        match = _ASSIGN_RE.match(line)
        if match is None:
            continue
        name, raw_value = match.group(1), match.group(2)
        literal = _LITERAL_RE.match(raw_value)
        if literal is None:
            continue
        if literal.group(1) is not None:
            value = literal.group(1)
        elif literal.group(2) is not None:
            value = literal.group(2)
        else:
            value = literal.group(3)
        assignments[name] = value
    return assignments


def resolve_variable_value(raw_value, assignments, *, line_number):
    """把 ``$Var`` 形式的参数值解析成脚本里赋的字面量。

    找不到赋值时抛 ScriptParseError，而不是跳过比对。
    """
    match = _VARIABLE_RE.match(raw_value.strip())
    if match is None:
        return raw_value
    name = match.group(1)
    if name not in assignments:
        raise ScriptParseError(
            f"第 {line_number} 行: 变量 {name} 在脚本里没有赋值，"
            f"无法核对 {raw_value} 的实际取值"
        )
    return assignments[name]


def parse_invocations(script_text):
    """解析脚本中所有 cli 调用，返回 CliInvocation 列表。

    子命令 = 模块名后面第一个 token；位置参数（如 $Ledger）跳过；
    ``--参数 值`` 按出现顺序记录，``$变量`` 值通过脚本赋值解析。
    """
    assignments = parse_variable_assignments(script_text)
    invocations = []
    for line_number, line in _logical_lines(script_text):
        match = _CALL_RE.search(line)
        if match is None:
            continue
        tokens = shlex.split(match.group(1), posix=False)
        if not tokens:
            continue
        subcommand = tokens[0]
        options = []
        index = 1
        while index < len(tokens):
            token = tokens[index]
            if not token.startswith("--") or token == "--":
                index += 1
                continue
            if "=" in token:
                name, raw_value = token.split("=", 1)
                index += 1
            else:
                name = token
                raw_value = None
                if index + 1 < len(tokens):
                    candidate = tokens[index + 1]
                    if not candidate.startswith("--") and not _REDIRECTION_RE.match(candidate):
                        raw_value = candidate
                        index += 2
                    else:
                        index += 1
                else:
                    index += 1
            if raw_value is None:
                value = True
            else:
                value = resolve_variable_value(
                    raw_value, assignments, line_number=line_number
                )
            options.append(OptionAssignment(name=name, value=value, line=line_number))
        invocations.append(
            CliInvocation(subcommand=subcommand, options=tuple(options), line=line_number)
        )
    return invocations


def _normalize_value(value):
    if not isinstance(value, str):
        return value
    text = value.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1]
    return text.replace("/", "\\")


def values_match(actual, default):
    """宽松但可靠的比较：数字按 float，路径统一分隔符，其余按原样。"""
    if default is None:
        # argparse 默认值为 None（必填/可选未给默认值）时，脚本显式传值合法。
        return True
    if isinstance(default, bool):
        return bool(actual) is default
    if isinstance(default, (list, tuple)):
        # append 型参数的默认值是空列表，显式传一个值属于正常用法。
        return True
    left = _normalize_value(actual)
    right = _normalize_value(default)
    try:
        return abs(float(left) - float(right)) <= 1e-9
    except (TypeError, ValueError):
        return left == right


def _subcommand_parsers(parser=None):
    parser = parser or cli._build_parser()
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action.choices
    raise AssertionError("cli._build_parser() 中找不到 subparsers")


def collect_drift(script_text, parser=None, source="<脚本>"):
    """返回所有“显式传值与 argparse 默认值不一致”的可读描述。"""
    subcommands = _subcommand_parsers(parser)
    problems = []
    for invocation in parse_invocations(script_text):
        subparser = subcommands.get(invocation.subcommand)
        if subparser is None:
            problems.append(
                f"第 {invocation.line} 行: 未知子命令 {invocation.subcommand}（{source}）"
            )
            continue
        actions = {}
        for action in subparser._actions:
            for option in action.option_strings:
                actions[option] = action
        for option in invocation.options:
            action = actions.get(option.name)
            if action is None:
                problems.append(
                    f"第 {option.line} 行: 子命令 {invocation.subcommand} "
                    f"不存在参数 {option.name}（{source}）"
                )
                continue
            if action.nargs == 0:
                if option.value is not True:
                    problems.append(
                        f"第 {option.line} 行: 子命令 {invocation.subcommand} "
                        f"的开关参数 {option.name} 不接受值 {option.value!r}（{source}）"
                    )
                continue
            if not values_match(option.value, action.default):
                problems.append(
                    f"第 {option.line} 行: 子命令 {invocation.subcommand} 的参数 "
                    f"{option.name} 显式传了 {option.value!r}，但 argparse 默认值是 "
                    f"{action.default!r}；脚本硬编码值会覆盖默认值，请同步修改（{source}）"
                )
    return problems


class ScriptArgumentDriftTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.script_text = SCRIPT_PATH.read_text(encoding="utf-8")

    def test_real_script_arguments_match_parser_defaults(self):
        """主用例：真实 ps1 中所有显式参数都必须等于 argparse 默认值。"""
        problems = collect_drift(self.script_text, source=str(SCRIPT_PATH))
        self.assertEqual(problems, [], "\n".join(problems))

    def test_sales_cutoff_policy_variable_resolves_to_its_assignment(self):
        assignments = parse_variable_assignments(self.script_text)
        self.assertEqual(
            resolve_variable_value("$SalesCutoffPolicy", assignments, line_number=0),
            "work\\sales-cutoff-policy.json",
        )

    def test_undefined_variable_raises_instead_of_being_skipped(self):
        script = (
            "& $Python -m outputs.tai_cai_tracker.cli identity-preflight $Queue "
            "--sales-cutoff-policy $MissingPolicy 2>&1\n"
        )
        with self.assertRaisesRegex(
            ScriptParseError, r"变量 MissingPolicy 在脚本里没有赋值"
        ):
            parse_invocations(script)

    def test_numeric_comparison_is_tolerant_but_detects_real_changes(self):
        self.assertTrue(values_match("24", 24.0))
        self.assertTrue(values_match(24.0, "24"))
        self.assertFalse(values_match("24", 6))

    def test_reverse_case_catches_the_original_max_hours_drift(self):
        """反向用例：若 ps1 再次写成 --max-hours-ahead 6，本测试必须变红。"""
        script = (
            "& $Python -m outputs.tai_cai_tracker.cli odds-refresh $Ledger "
            "--max-hours-ahead 6 2>&1\n"
        )
        problems = collect_drift(script, source="<fake-script>")
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("odds-refresh", problems[0])
        self.assertIn("--max-hours-ahead", problems[0])
        self.assertIn("'6'", problems[0])
        self.assertIn("24.0", problems[0])

    def test_none_defaults_accept_explicit_script_values(self):
        script = (
            "& $Python -m outputs.tai_cai_tracker.cli daily-freeze-status "
            "$Ledger $Log --date 2026-11-15 2>&1\n"
        )
        self.assertEqual(collect_drift(script, source="<fake-script>"), [])

    def test_backtick_continuation_is_joined_before_parsing(self):
        # 当前真实脚本里的 cli 调用都是单行的；这里锁住续行拼接能力，
        # 防止以后把长命令拆行后参数被漏读。
        script = (
            "& $Python -m outputs.tai_cai_tracker.cli odds-refresh `\n"
            "    $Ledger --max-hours-ahead 6 2>&1\n"
        )
        invocations = parse_invocations(script)
        self.assertEqual(len(invocations), 1)
        self.assertEqual(invocations[0].subcommand, "odds-refresh")
        self.assertEqual(
            [(option.name, option.value) for option in invocations[0].options],
            [("--max-hours-ahead", "6")],
        )


if __name__ == "__main__":
    unittest.main()

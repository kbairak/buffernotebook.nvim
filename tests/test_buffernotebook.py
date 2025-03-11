import ast
import sys
import threading
import typing
from pathlib import Path
from unittest import mock

import pytest

sys.path.append(str((Path() / "rplugin" / "python3").resolve()))

from buffernotebook import BufferNotebook


@pytest.fixture
def bn():
    nvim = mock.MagicMock(name="nvim")
    nvim.api.create_namespace.return_value = mock.MagicMock(name="namespace")
    buffer = mock.MagicMock(name="buffer")
    bn = BufferNotebook(nvim, buffer)
    yield bn
    if bn._timer is not None:
        bn._timer.cancel()


def test_init(bn):
    assert not bn._enabled
    bn._nvim.api.create_namespace.assert_called_once_with("BufferNotebookNamepsace")
    assert bn._globals == {"__name__": "__main__"}
    assert bn._cache == []
    assert bn._timer is None
    assert bn._popup_window is None


def test_enable(bn):
    bn._nvim.api.buf_get_lines.return_value = [""]
    bn._nvim.api.win_get_cursor.return_value = (1, 0)

    bn.enable()

    assert bn._enabled
    assert isinstance(bn._timer, threading.Timer)
    bn._nvim.api.buf_get_lines.assert_called_once_with(bn._buffer, 0, -1, False)
    bn._nvim.api.win_get_cursor.assert_called_once_with(0)
    bn._nvim.out_write.assert_called_once_with("BufferNotebook enabled\n")


def test_disable(bn):
    bn._nvim.api.buf_get_lines.return_value = [""]
    bn._nvim.api.win_get_cursor.return_value = (1, 0)

    bn.enable()

    bn.disable()

    assert not bn._enabled
    bn._nvim.api.buf_clear_namespace.assert_called_once_with(
        bn._buffer, bn._namespace, 0, -1
    )
    assert bn._nvim.out_write.mock_calls[-1] == mock.call("BufferNotebook disabled\n")


@pytest.mark.parametrize(
    "argument,expected",
    (
        (("a = 1",), ("a = 1",)),
        (("a = 1", "b = 2"), ("a = 1", "b = 2")),
        (("a = 1", "This line cannot be parsed", "b = 2"), ("a = 1", "", "b = 2")),
        (
            ("This line cannot be parsed", "a = 1", "This line cannot be parsed"),
            ("", "a = 1", ""),
        ),
    ),
)
def test_remove_unparseable_lines(argument, expected):
    assert BufferNotebook._remove_unparseable_lines(argument) == expected


class Rest(typing.TypedDict):
    lineno: int
    col_offset: int


rest: Rest = {"lineno": 1, "col_offset": 1}


@pytest.mark.parametrize(
    "stmt,expected",
    (
        (
            ast.Assign(
                [ast.Name(id="a", ctx=ast.Store(), **rest)],
                ast.Constant(1, **rest),
                **rest
            ),
            1,
        ),
        (
            ast.Assign(
                [
                    ast.Name(id="a", ctx=ast.Store(), **rest),
                    ast.Name(id="b", ctx=ast.Store(), **rest),
                ],
                ast.Tuple([ast.Constant(1, **rest), ast.Constant(2, **rest)], **rest),
                **rest
            ),
            (1, 2),
        ),
        (ast.Expr(ast.Constant(1, **rest)), 1),
        (ast.Expr(ast.Constant("hello world", **rest)), "hello world"),
        (
            ast.Expr(
                ast.BinOp(
                    ast.Constant(1, **rest), ast.Add(), ast.Constant(2, **rest), **rest
                ),
                **rest
            ),
            3,
        ),
        (ast.Import([ast.alias("sys", **rest)], **rest), sys),
        (ast.ImportFrom("pathlib", [ast.alias(name="Path", **rest)], 0, **rest), Path),
        (
            ast.Expr(
                ast.BinOp(
                    ast.Constant(1, **rest), ast.Div(), ast.Constant(0, **rest), **rest
                ),
                **rest
            ),
            ZeroDivisionError("division by zero"),
        ),
    ),
)
def test_evaluate_statement(bn, stmt, expected):
    actual = bn._evaluate_statement(0, stmt)
    if isinstance(actual, Exception):
        assert isinstance(expected, type(actual))
        assert expected.args == actual.args
        assert len(bn._cache) == 1
        assert bn._cache[0][0] == ast.dump(stmt)
        assert bn._cache[0][1] == actual
    else:
        assert bn._evaluate_statement(0, stmt) == expected
        assert bn._cache == [(ast.dump(stmt), expected)]


def test_evaluate_aug_assign(bn):
    stmt1 = ast.Assign(
        [ast.Name(id="a", ctx=ast.Store(), **rest)], ast.Constant(1, **rest), **rest
    )
    stmt2 = ast.AugAssign(
        ast.Name(id="a", ctx=ast.Store(), **rest),
        ast.Add(),
        ast.Constant(2, **rest),
        **rest
    )
    bn._evaluate_statement(0, stmt1)

    actual = bn._evaluate_statement(1, stmt2)

    assert actual == 3
    assert bn._cache == [(ast.dump(stmt1), 1), (ast.dump(stmt2), 3)]

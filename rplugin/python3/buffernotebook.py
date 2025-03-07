import ast
import functools
import pprint
import re
import threading
from typing import Any, Optional

import pynvim
import pynvim.api

# TODOs:
# - [✅] One command with one argument for everything
# - [✅] Render complex assigns  (eg `a, b = 1, 2`)
# - [✅] Command/mapping to clear cache and re-evaluate
# - [✅] Command/mapping to save results in buffer
# - [✅] Figure something out for multiline
# - [✅] Copy to clipboard
# - [✅] Show multiline annotations in popup on hover
# - [✅] Annotate import statements


nothing_to_show = object()


class BufferNotebook:
    def __init__(self, nvim: pynvim.Nvim, buffer: pynvim.api.Buffer):
        self.nvim = nvim
        self.buffer = buffer

        self.enabled = False
        self.namespace = self.nvim.api.create_namespace("BufferNotebookNamepsace")

        self.globals = {"__name__": "__main__"}
        self.cache: list[tuple[str, Any]] = []
        self.timer = None
        self.popup_window = None

    def enable(self):
        self.enabled = True
        self.on_change()
        self.nvim.out_write("BufferNotebook enabled\n")

    def disable(self):
        self.enabled = False
        self.clear()
        if self.popup_window is not None:
            self.nvim.api.win_close(self.popup_window, True)
            self.popup_window = None
        self.nvim.out_write("BufferNotebook disabled\n")

    def toggle(self):
        if self.enabled:
            self.disable()
        else:
            self.enable()

    def clear(self):
        self.nvim.api.buf_clear_namespace(self.buffer, self.namespace, 0, -1)

    def on_change(self):
        if not self.enabled:
            return
        if self.timer is not None:
            self.timer.cancel()
        self.timer = threading.Timer(0.3, lambda: self.nvim.async_call(self._on_change))
        self.timer.start()

    def _on_change(self):
        self.timer = None
        self.clear()

        lines = tuple(self.nvim.api.buf_get_lines(self.buffer, 0, -1, False))

        top_level_statements: list[tuple[int, ast.stmt]] = [
            (statement.lineno - 1, statement) for statement in self.parse(lines).body
        ]

        marks = {index for index, line in enumerate(lines) if self._has_mark(line)}

        for index, (start_line_number, statement) in enumerate(top_level_statements):
            result = self.evaluate_statement(index, statement)

            try:
                end, _ = top_level_statements[index + 1]
            except IndexError:
                end = len(lines)
            for line_number in set(range(start_line_number, end)) & marks:
                self.annotate(line_number, result)

    @staticmethod
    def _has_mark(line):
        return re.search(r"#\s*=\s*$", line) or re.search(r"^#\s<<<\s*$", line)

    @functools.lru_cache
    def parse(self, lines: tuple[str, ...]) -> ast.Module:
        return ast.parse("\n".join(self._parse(lines)))

    def _parse(self, lines: tuple[str, ...]) -> tuple[str, ...]:
        """Assuming most lines are ok, try to replacing as few lines as possible with empty ones so
        that the end result is parse-able.
        """

        if not lines:
            return ()  # Recursion exit

        end = len(lines)
        while end > 0:
            try:
                # Bad:  [ G   G   G   B   G   G ]
                # Bad:  [ G   G   G   B   G ] G
                # Bad:  [ G   G   G   B ] G   G
                # Good: [ G   G   G ] B   G   G
                ast.parse("\n".join(lines[:end]))
            except Exception:
                end -= 1
            else:
                # Good:   [G G G]           B G G
                # Return: [G G G] + _parse([B G G])
                return lines[:end] + self._parse(lines[end:])

        # Ended the for-loop without encountering a good chunk; (at least) first line must be bad
        #          B             B             G G ...
        # Return: [""] + _parse([B             G G ...])
        # Return: [""] +        [""] + _parse([G G ...])
        return ("",) + self._parse(lines[1:])

    def evaluate_statement(self, index: int, statement: ast.stmt) -> Any:
        key = ast.dump(statement)
        try:
            cache_key, cache_result = self.cache[index]
        except IndexError:
            pass
        else:
            if key == cache_key:
                return cache_result
            else:
                self.cache = self.cache[:index]

        if isinstance(statement, ast.Assign):
            try:
                self._do_exec(statement)
            except Exception as exc:
                result = exc

            else:
                if len(statement.targets) != 1:
                    result = nothing_to_show

                if isinstance(statement.targets[0], ast.Tuple) and all(
                    isinstance(elt, ast.Name) for elt in statement.targets[0].elts
                ):
                    result = []
                    try:
                        for elt in statement.targets[0].elts:
                            assert isinstance(elt, ast.Name)
                            result.append(self.globals[elt.id])
                    except KeyError:
                        result = nothing_to_show
                    else:
                        result = tuple(result)

                elif isinstance(statement.targets[0], ast.Name):
                    try:
                        result = self.globals[statement.targets[0].id]
                    except KeyError:
                        result = nothing_to_show

                else:
                    result = nothing_to_show

        elif isinstance(statement, ast.AugAssign) and isinstance(
            statement.target, ast.Name
        ):
            try:
                self._do_exec(statement)
            except Exception as exc:
                result = exc
            else:
                try:
                    result = self.globals[statement.target.id]
                except KeyError:
                    result = nothing_to_show

        elif isinstance(statement, ast.Expr):
            try:
                result = eval(
                    compile(ast.Expression(statement.value), "<string>", "eval"),
                    self.globals,
                )

            except Exception as exc:
                result = exc

        elif isinstance(statement, (ast.Import, ast.ImportFrom)):
            try:
                self._do_exec(statement)
            except Exception as exc:
                result = exc
            else:
                try:
                    result = [
                        self.globals[name.asname or name.name]
                        for name in statement.names
                    ]
                except KeyError:
                    result = nothing_to_show
                else:
                    if len(result) == 1:
                        result = result[0]
                    else:
                        result = tuple(result)

        else:
            try:
                self._do_exec(statement)
            except Exception as exc:
                result = exc
            else:
                result = nothing_to_show

        self.cache.append((key, result))
        return result

    def annotate(self, line_number: int, value: Any):
        if value is nothing_to_show:
            return
        elif isinstance(value, Exception):
            text = f"! {value!r}"
        else:
            text = repr(value)

        self.nvim.api.buf_set_virtual_text(
            self.buffer, self.namespace, line_number, [(text, "Info")], {}
        )

    def reset(self):
        self.globals = {"__name__": "__main__"}
        self.cache = []
        self._on_change()

    def inject(self):
        result, statement = self._evaluate_statement_under_cursor()
        if result is nothing_to_show:
            return

        assert statement is not None
        inject_at = statement.end_lineno or statement.lineno

        chunks = self._format_multiline_result(result).splitlines()

        self.nvim.api.buf_set_lines(
            self.buffer,
            inject_at,
            inject_at,
            False,
            [f"# >>> {chunks[0]}"] + [f"# ... {chunk}" for chunk in chunks[1:]],
        )

    def copy(self):
        result, _ = self._evaluate_statement_under_cursor()

        if result is nothing_to_show:
            return
        if isinstance(result, str):
            self.nvim.funcs.setreg("+", result)
        else:
            self.nvim.funcs.setreg("+", repr(result))

    def on_cursor_moved(self):
        if self.popup_window is not None:
            self.nvim.api.win_close(self.popup_window, True)
            self.popup_window = None

        if not self.enabled:
            return

        lines = tuple(self.nvim.api.buf_get_lines(self.buffer, 0, -1, False))
        current_line_position = self.nvim.api.win_get_cursor(0)[0] - 1

        if not self._has_mark(lines[current_line_position]):
            return

        result, _ = self._evaluate_statement_under_cursor()
        if result is nothing_to_show:
            return
        result = self._format_multiline_result(result)
        if "\n" not in result:
            return

        popup_buffer = self.nvim.api.create_buf(False, True)
        self.nvim.api.buf_set_lines(popup_buffer, 0, -1, False, result.splitlines())
        width = max(len(line) for line in result.splitlines())
        height = len(result.splitlines())
        self.popup_window = self.nvim.api.open_win(
            popup_buffer,
            False,
            {
                "relative": "cursor",
                "width": width,
                "height": height,
                "col": 0,
                "row": 1,
                "style": "minimal",
                "border": "single",
            },
        )

    def _evaluate_statement_under_cursor(self) -> tuple[Any, Optional[ast.stmt]]:
        if not self.enabled:
            self.enable()
        lines = tuple(self.nvim.api.buf_get_lines(self.buffer, 0, -1, False))
        current_line_position = self.nvim.api.win_get_cursor(0)[0] - 1

        for index, statement in enumerate(self.parse(lines).body):
            result = self.evaluate_statement(index, statement)
            if (
                statement.lineno - 1
                <= current_line_position
                <= (statement.end_lineno or statement.lineno)
            ):
                return result, statement

        return nothing_to_show, None

    def _format_multiline_result(self, result: Any):
        if isinstance(result, Exception):
            return f"! {result!r}"
        elif isinstance(result, str):
            return result
        else:
            return pprint.pformat(result, sort_dicts=False)

    def _do_exec(self, statement):
        exec(
            compile(
                ast.Module(body=[statement], type_ignores=[]),
                "<string>",
                "exec",
            ),
            self.globals,
        )


@pynvim.plugin
class BufferNotebookPlugin:
    def __init__(self, nvim: pynvim.Nvim):
        self.nvim = nvim
        self.notebooks = {}

    def get_notebook(self) -> BufferNotebook:
        buffer = self.nvim.current.buffer
        if buffer.number not in self.notebooks:
            self.notebooks[buffer.number] = BufferNotebook(self.nvim, buffer)
        return self.notebooks[buffer.number]

    @pynvim.autocmd("TextChanged,TextChangedI", pattern="*")
    def on_change(self, *_):
        self.get_notebook().on_change()

    @pynvim.autocmd("BufDelete", pattern="*")
    def on_buffer_delete(self, *_):
        buffer = self.nvim.current.buffer
        try:
            del self.notebooks[buffer.number]
        except KeyError:
            pass

    @pynvim.autocmd("CursorMoved", pattern="*")
    def on_cursor_moved(self, *_):
        self.get_notebook().on_cursor_moved()

    @pynvim.command(
        "BufferNotebook", nargs=1, complete="customlist,BufferNotebookCompletions"
    )
    def command(self, args: str):
        (subcommand,) = args
        if subcommand == "enable":
            self.get_notebook().enable()
        elif subcommand == "disable":
            self.get_notebook().disable()
        elif subcommand == "toggle":
            self.get_notebook().toggle()
        elif subcommand == "reset":
            self.get_notebook().reset()
        elif subcommand == "inject":
            self.get_notebook().inject()
        elif subcommand == "copy":
            self.get_notebook().copy()
        else:  # pragma: no cover
            raise Exception("Unreachable code")

    @pynvim.function("BufferNotebookCompletions", sync=True)
    def get_completions(self, *_):
        return ["enable", "disable", "toggle", "reset", "inject", "copy"]

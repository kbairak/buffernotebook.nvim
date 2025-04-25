import ast
import functools
import pprint
import re
import threading
from dataclasses import dataclass
from typing import Any, Callable, Optional

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
# - [✅] Don't run python while python is running
# - [✅] Disable folds for popup window
# - [ ] Show running state (using `buf_set_extmark`)


@dataclass
class Timer:
    """Timer utility, is initialized with a callback.

    - Responds to events by invoking a delay
    - Every time an event arrives, a timer starts
    - If an event arrives before the previous timer has finished, the timer is cancelled and
      replaced by a new one
    - When the timer finishes, the callback is invoked
    - If an event arrives before the callback is finished, a timer is set again (in parallel)
    - However, if the timer finishes while the callback is still running, the callback is not
      invoked immediately but waits until the previous callback finishes

    Simple scenario:

        event -> timer_start -> timer_finish -> callback_start -> callback_finish

    Event while timer is running:

        event -> timer_start -> event -> timer_cancel -> new_timer_start -> new_timer_finish
            -> callback_start -> callback_finish

    Event while callback is running, callback finishes before new timer:

        event -> timer_start -> timer_finish -> callback_start -> event -> new_timer_start
            -> callback_finish -> new_timer_finish -> new_callback_start -> new_callback_finish

    Event while callback is running, new_timer finishes before callback:

        event -> timer_start -> timer_finish -> callback_start -> event -> new_timer_start
            -> new_timer_finish -> callback_finish -> new_callback_start -> new_callback_finish
    """

    callback: Callable[[], None]
    delay: float = 0.3
    _timer: threading.Timer | None = None
    _is_executing: bool = False
    _execute_on_finish: bool = False

    def event(self):
        if self._timer is not None:
            self._timer.cancel()
        self._timer = threading.Timer(self.delay, self._on_timeout)
        self._timer.start()

    def _on_timeout(self):
        self._timer = None
        if self._is_executing:
            self._execute_on_finish = True
        else:
            self._is_executing = True
            self.callback()
            self._is_executing = False
            if self._execute_on_finish:
                self._execute_on_finish = False
                self.callback()


nothing_to_show = object()


class BufferNotebook:
    def __init__(self, nvim: pynvim.Nvim, buffer: pynvim.api.Buffer):
        self._nvim = nvim
        self._buffer = buffer

        self._enabled = False
        self._namespace = self._nvim.api.create_namespace("BufferNotebookNamepsace")

        self._globals: dict = {"__name__": "__main__"}
        self._cache: list[tuple[str, Any]] = []
        self._popup_window = None

        self._timer = Timer(lambda: self._nvim.async_call(self._evaluate_and_annotate))

    def enable(self):
        self._enabled = True
        self.on_change()
        self.on_cursor_moved()
        self._nvim.out_write("BufferNotebook enabled\n")

    def disable(self):
        self._enabled = False
        self._clear()
        self._remove_popup()
        self._nvim.out_write("BufferNotebook disabled\n")

    def toggle(self):
        if self._enabled:
            self.disable()
        else:
            self.enable()

    def inject(self):
        result, statement = self._evaluate_statement_under_cursor()
        if result is nothing_to_show:
            return

        assert statement is not None
        inject_at = statement.end_lineno or statement.lineno

        chunks = self._format_multiline_result(result).splitlines()

        self._nvim.api.buf_set_lines(
            self._buffer,
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
            self._nvim.funcs.setreg("+", result)
        else:
            self._nvim.funcs.setreg("+", repr(result))

    def reset(self):
        self._globals = {"__name__": "__main__"}
        self._cache = []
        self._evaluate_and_annotate()

    def on_change(self):
        if not self._enabled:
            return
        self._remove_popup()
        self._timer.event()

    def on_cursor_moved(self):
        self._remove_popup()

        if not self._enabled:
            return

        lines = tuple(self._nvim.api.buf_get_lines(self._buffer, 0, -1, False))
        current_line_position, currenct_cursor_position = self._nvim.api.win_get_cursor(
            0
        )
        current_line_position -= 1

        if not self._has_mark(lines[current_line_position]):
            return

        result, _ = self._evaluate_statement_under_cursor()
        if result is nothing_to_show:
            return
        result = self._format_multiline_result(result)
        if "\n" not in result:
            return

        popup_buffer = self._nvim.api.create_buf(False, True)
        self._nvim.api.buf_set_lines(popup_buffer, 0, -1, False, result.splitlines())
        width = max(len(line) for line in result.splitlines())
        height = len(result.splitlines())
        self._popup_window = self._nvim.api.open_win(
            popup_buffer,
            False,
            {
                "relative": "cursor",
                "width": width,
                "height": height,
                "col": -currenct_cursor_position,
                "row": 1,
                "style": "minimal",
                "border": "single",
            },
        )
        self._nvim.api.win_set_option(self._popup_window, "foldenable", False)

    def _clear(self):
        self._nvim.api.buf_clear_namespace(self._buffer, self._namespace, 0, -1)

    @functools.lru_cache
    def _parse(self, lines: tuple[str, ...]) -> ast.Module:
        return ast.parse("\n".join(self._remove_unparseable_lines(lines)))

    @staticmethod
    def _remove_unparseable_lines(lines: tuple[str, ...]) -> tuple[str, ...]:
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
                # Good:   [G G G]                              B G G
                # Return: [G G G] + _remove_unparseable_lines([B G G])
                return lines[:end] + BufferNotebook._remove_unparseable_lines(
                    lines[end:]
                )

        # Ended the for-loop without encountering a good chunk; (at least) first line must be bad
        #          B                                B                                G G ...
        # Return: [""] + _remove_unparseable_lines([B                                G G ...])
        # Return: [""] +                           [""] + _remove_unparseable_lines([G G ...])
        return ("",) + BufferNotebook._remove_unparseable_lines(lines[1:])

    def _evaluate_and_annotate(self):
        self._clear()

        lines = tuple(self._nvim.api.buf_get_lines(self._buffer, 0, -1, False))

        top_level_statements: list[tuple[int, ast.stmt]] = [
            (statement.lineno - 1, statement) for statement in self._parse(lines).body
        ]

        marks = {index for index, line in enumerate(lines) if self._has_mark(line)}

        for index, (start_line_number, statement) in enumerate(top_level_statements):
            result = self._evaluate_statement(index, statement)

            try:
                end, _ = top_level_statements[index + 1]
            except IndexError:
                end = len(lines)
            for line_number in set(range(start_line_number, end)) & marks:
                self._annotate(line_number, result)

    def _evaluate_statement(self, index: int, statement: ast.stmt) -> Any:
        key = ast.dump(statement)
        try:
            cache_key, cache_result = self._cache[index]
        except IndexError:
            pass
        else:
            if key == cache_key:
                return cache_result
            else:
                self._cache = self._cache[:index]

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
                            result.append(self._globals[elt.id])
                    except KeyError:
                        result = nothing_to_show
                    else:
                        result = tuple(result)

                elif isinstance(statement.targets[0], ast.Name):
                    try:
                        result = self._globals[statement.targets[0].id]
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
                    result = self._globals[statement.target.id]
                except KeyError:
                    result = nothing_to_show

        elif isinstance(statement, ast.Expr):
            try:
                result = eval(
                    compile(ast.Expression(statement.value), "<string>", "eval"),
                    self._globals,
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
                        self._globals[name.asname or name.name]
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

        self._cache.append((key, result))
        return result

    @staticmethod
    def _has_mark(line):
        return re.search(r"#\s*=\s*$", line) or re.search(r"#\s*<<<\s*$", line)

    def _annotate(self, line_number: int, value: Any):
        if value is nothing_to_show:
            return
        elif isinstance(value, Exception):
            text = f"! {value!r}"
        else:
            text = repr(value)

        self._nvim.api.buf_set_virtual_text(
            self._buffer, self._namespace, line_number, [(text, "Info")], {}
        )

    def _evaluate_statement_under_cursor(self) -> tuple[Any, Optional[ast.stmt]]:
        if not self._enabled:
            self.enable()
        lines = tuple(self._nvim.api.buf_get_lines(self._buffer, 0, -1, False))
        current_line_position = self._nvim.api.win_get_cursor(0)[0] - 1

        for index, statement in enumerate(self._parse(lines).body):
            result = self._evaluate_statement(index, statement)
            if (
                statement.lineno - 1
                <= current_line_position
                < (statement.end_lineno or statement.lineno)
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
            self._globals,
        )

    def _remove_popup(self):
        try:
            if self._popup_window is not None:
                self._nvim.api.win_close(self._popup_window, True)
                self._popup_window = None
        except Exception:
            self._popup_window = None


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
